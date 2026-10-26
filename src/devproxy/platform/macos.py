"""macOS 平台实现：launchd、/etc/resolver、security 钥匙串。

与 Linux 的三处关键差异：
- 无 setcap → 80/443 只能靠 root 的 LaunchDaemon（caddy 是 system scope）
- 53 被 mDNSResponder 占用 → 用户级 dnsmasq 走 5300
- DNS 路由写 /etc/resolver/<后缀>，不动 /etc/resolv.conf

所有会动系统的调用都过 `_assert_may_mutate()` 护栏：测试进程里不许改真实
机器（Linux 平台同款护栏，见 platform/linux.py）。
"""

from __future__ import annotations

import ipaddress
import os
import plistlib
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from devproxy.core.paths import default_state_dir
from devproxy.platform.base import RESOLVER_DIR
from devproxy.platform.base import ElevatedSession
from devproxy.platform.base import Platform
from devproxy.platform.base import render_resolver_file as _render_resolver_file

LAUNCH_AGENTS = "Library/LaunchAgents"
LAUNCH_DAEMONS = "/Library/LaunchDaemons"

# DNS 解析的超时上限（秒）：解析器挂住不能把 setup 的自检拖死。
# dns_query 只在 verify() 里被调用（doctor 不做 DNS 解析）。
# 与 Linux 的 DNS_QUERY_TIMEOUT 保持一致。
DNS_QUERY_TIMEOUT = 3.0

# 子进程的默认超时（秒）：跨进程调用必须有界，漏传超时等于允许永久挂起。
DEFAULT_TIMEOUT = 30.0

# launchctl print 里的服务状态行（"state = running" / "state = not running"）。
_LAUNCHD_STATE = re.compile(r"^\s*state\s*=\s*(\S+)", re.MULTILINE)


def _run(argv: list[str], timeout: float | None = DEFAULT_TIMEOUT) -> tuple[int, str]:
    """执行一个 argv 列表（不走 shell）。返回 (返回码, 输出)。

    默认超时是 `DEFAULT_TIMEOUT` 而不是 `None`：漏传超时是有界等待，
    不是无限等待。只有确实要等用户输入密码的调用（sudo 密码路径）才
    显式传 `timeout=None`。`stdin=DEVNULL` 防止交互式探针抢占终端输入
    （`sudo` 从 `/dev/tty` 读密码，不受影响）。
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        # 命令不存在 —— 调用方按返回码处理，而不是拿到 traceback
        return 127, f"命令不存在: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时: {argv[0]}"
    except OSError as e:
        # 可执行位缺失等
        return 127, f"命令不可用: {argv[0]}（{e}）"
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def _first_ip(text: str) -> str | None:
    """从解析器输出里取第一个 A/AAAA 答案。"""
    for line in text.splitlines():
        for token in line.replace(",", " ").split():
            candidate = token.strip("[];()")
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                continue
    return None


def _launchd_running(text: str) -> bool:
    """launchctl print 的输出里，服务是否**真的在跑**。

    输出形如::

        com.devproxy.caddy = {
            active count = 1
            path = /Library/LaunchDaemons/com.devproxy.caddy.plist
            state = running

    launchctl print 对"已注册但进程已退出"的服务也返回 0 —— 只有 state
    才是"进程在跑"的证据。看不到 running 就按没跑处理（fail closed）：
    宁可让用户去查，也不能把崩溃的服务报成在运行。
    """
    match = _LAUNCHD_STATE.search(text)
    return match is not None and match.group(1) == "running"


# 平台自身单元测试的显式豁免：那些用例要把 _run 换成假的、路径指向
# tmp_path，覆盖的正是写操作的失败处理 —— 不需要护栏拦着。
_ALLOW_MUTATION = False


def _assert_may_mutate() -> None:
    """测试里禁止改动真实系统。

    macOS 上的写操作会碰 /Library/LaunchDaemons、/etc/resolver 与系统钥匙串
    —— 隔离一旦漏掉一处，就会静默改掉用户机器。护栏放在平台层：只有平台
    知道哪些调用会动系统。只读探测（service_active / dns_query / http_ok …）
    不受影响。
    """
    if _ALLOW_MUTATION:
        return
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("DEVPROXY_NO_MUTATION"):
        raise RuntimeError("测试中禁止调用真实平台的写操作")


def render_plist(
    name: str,
    exec_start: list[str],
    env: dict[str, str],
    log_path: Path,
    description: str = "",
) -> str:
    """生成 launchd plist。

    路径必须是绝对路径 —— LaunchDaemon 以 root 运行，`~` 是 root 的家目录，
    相对路径则会按 launchd 的工作目录解释，两种都不是用户想要的位置。
    """
    if not exec_start:
        raise RuntimeError(f"launchd 单元 {name} 缺少 ProgramArguments")
    program = exec_start[0]
    if not Path(program).is_absolute():
        raise RuntimeError(f"launchd 单元 {name} 的程序路径必须是绝对路径: {program}")
    data = {
        "Label": f"com.devproxy.{name}",
        "ProgramArguments": list(exec_start),
        "EnvironmentVariables": dict(env),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    if description:
        data["Description"] = description
    return plistlib.dumps(data).decode("utf-8")


def render_resolver_file(port: int) -> str:
    """/etc/resolver/<后缀> 的内容。

    macOS 原生支持每域解析器，不必改 /etc/resolv.conf。
    实现放在 platform/base.py —— DNS 组件也要用同一份格式。
    """
    return _render_resolver_file(port)


class MacOSElevated:
    """提权会话。构造时执行一次 `sudo -v` 完成预认证。

    `sudo -v` 是"无命令"调用，不匹配 sudoers 里的命令规范 —— 免密
    （`NOPASSWD: ALL`）只对具体命令生效，管不到它。于是免密机器上
    `sudo -v` 必然失败，而那台机器根本不需要密码。

    所以 `sudo -v` 失败**不能**直接判定"提权失败"：改用 `sudo -n true`
    探测免密能力 —— 它成功就说明这台机器不需要密码，后续特权命令一律
    自带 `-n`（绝不退化成会弹密码框的 `sudo <cmd>`）。
    与 Linux 平台保持同一套语义。
    """

    # 免密路径的标记。做成类属性：测试里用 __new__ 造出的会话
    # 没有实例属性，也必须能正常跑。
    _noninteractive = False

    def __init__(self) -> None:
        # 护栏也必须拦构造函数：直接 new 一个提权会话会真的跑 sudo -v，
        # 后面还可能往系统钥匙串里写东西。
        _assert_may_mutate()
        self._authenticate()

    def _authenticate(self) -> None:
        # `sudo -v` 可能要等用户输密码 —— 只有这条路径允许无超时。
        code, msg = _run(["sudo", "-v"], timeout=None)
        if code == 0:
            # 时间戳已预热，后续 `sudo <cmd>` 直接复用。
            return
        # 免密探测：`-n` 保证绝不等待输入（非交互环境会直接失败而不是挂住）。
        code_nopasswd, msg_nopasswd = _run(["sudo", "-n", "true"])
        if code_nopasswd == 0:
            # 免密可用。`sudo -v` 的预热在这台机器上不可用，
            # 后续命令必须自带 `-n` 才不会弹密码框。
            self._noninteractive = True
            return
        # 两条路都走不通 —— 错误信息要同时带上两条命令的输出，
        # 用户才能分辨"这台机器没免密"和"sudo 本身坏了"。
        raise PermissionError(f"提权失败：sudo -v: {msg}；sudo -n true: {msg_nopasswd}")

    def run(self, argv: list[str]) -> tuple[int, str]:
        if self._noninteractive:
            return _run(["sudo", "-n", *argv])
        # 交互路径可能要等用户输入密码 —— 不设超时（其余调用一律有界）。
        return _run(["sudo", *argv], timeout=None)

    def write(self, path: Path, content: str) -> None:
        """通过系统临时目录中的文件 + `install -m 644` 写入，避免 shell 引号问题。

        父目录不一定存在：`/etc/resolver` 在干净的 macOS 上可能尚未创建。
        先 `install -d` 建目录，否则写入必然失败，setup 永远无法收敛。
        临时文件必须放在用户可写的系统临时目录（目标目录本身可能不可写），
        且无论成功失败都要清理。

        必须用 `install -m 644 -o root -g wheel` 而不是 `cp`：mkstemp 建出来
        的临时文件是 0600，`cp` 会把权限一并带过去，于是 /etc/resolver/<后缀>
        与 /Library/LaunchDaemons 下的文件变成 root:wheel 0600 —— 普通用户
        自己都读不了，doctor 的内容判据（`read_text`）会永远失效。
        `resolver(5)` 的常规形态也是 root:wheel 0644。与 Linux 对齐。
        """
        code, msg = self.run(["install", "-d", str(path.parent)])
        if code != 0:
            raise PermissionError(f"创建目录 {path.parent} 失败: {msg}")
        fd, tmp_name = tempfile.mkstemp(prefix="devproxy-elev-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            code, msg = self.run(
                [
                    "install",
                    "-m",
                    "644",
                    "-o",
                    "root",
                    "-g",
                    "wheel",
                    str(tmp),
                    str(path),
                ]
            )
            if code != 0:
                raise PermissionError(f"写入 {path} 失败: {msg}")
        finally:
            tmp.unlink(missing_ok=True)

    def read(self, path: Path) -> str:
        """读取特权文件。文件不存在抛 FileNotFoundError，其余失败抛 PermissionError。

        存在性用 `test -e` 的**返回码**判断，不解析 `cat` 的错误文案：
        BSD/GNU coreutils 的报错都会本地化，语言一换就把缺失文件误判成
        权限错误。
        """
        exists, _ = self.run(["test", "-e", str(path)])
        if exists != 0:
            raise FileNotFoundError(path)
        code, out = self.run(["cat", str(path)])
        if code != 0:
            raise PermissionError(f"读取 {path} 失败: {out}")
        return out

    def copy(self, src: Path, dest: Path) -> None:
        """复制文件并落成 root:wheel 0644（理由同 `write`）。"""
        code, msg = self.run(
            [
                "install",
                "-m",
                "644",
                "-o",
                "root",
                "-g",
                "wheel",
                str(src),
                str(dest),
            ]
        )
        if code != 0:
            raise PermissionError(f"复制 {src} → {dest} 失败：{msg}")


class MacOSPlatform:
    """macOS 平台实现。"""

    name = "mac"
    dns_port = 5300

    def __init__(self, user_home: Path | None = None) -> None:
        # 设计前提是"以普通用户运行，内部按需 sudo"。以 root 运行时
        # Path.home() 是 /var/root，`gui/0` 域通常不存在，服务与单元
        # 会装进 root 的家目录 —— 必须在构造时就显式失败。
        if os.geteuid() == 0:
            raise RuntimeError(
                "请以普通用户运行 devproxy：需要特权的步骤由工具内部调用 sudo，"
                "以 root 运行会把服务装进 root 的家目录"
            )
        self.user_home = Path(user_home) if user_home else Path.home()
        self._elevated: MacOSElevated | None = None

    # -- 提权 ----------------------------------------------------------

    def privilege(self) -> ElevatedSession:
        """一次 sudo -v 认证，整个会话复用 —— 否则每次操作都要重输密码。"""
        _assert_may_mutate()
        if self._elevated is None:
            self._elevated = MacOSElevated()
        return self._elevated

    # -- 只读探测 ------------------------------------------------------

    def run(self, argv: list[str]) -> tuple[int, str]:
        # 与 Linux 对齐：普通命令带超时，sudo 那条等用户输密码、不设。
        return _run(argv, timeout=60.0)

    def binary_version(self, binary: Path) -> str:
        if not binary.exists():
            return ""
        code, out = _run([str(binary), "version"])
        if code != 0:
            return ""
        return out.split()[0] if out else ""

    def service_active(self, name: str) -> bool:
        """服务是否**真的在跑**（不只是"已加载"）。

        launchctl print 对已注册但已退出的服务同样返回 0 —— 直接拿退出码
        当"在运行"，doctor 会把崩溃的 caddy 报成好的。
        """
        code, out = _run(["launchctl", "print", f"{self._domain(name)}/com.devproxy.{name}"])
        if code != 0:
            return False
        return _launchd_running(out)

    def resolved_active(self) -> bool:
        """macOS 没有 systemd-resolved —— auto 后端绝不能选它。"""
        return False

    def dns_query(self, name: str) -> str | None:
        """走系统解析器查询。必须有超时上限：解析器挂住会拖死 setup 的自检。"""
        code, out = _run(
            ["dscacheutil", "-q", "host", "-a", "name", name], timeout=DNS_QUERY_TIMEOUT
        )
        if code == 0:
            answer = _first_ip(out)
            if answer is not None:
                return answer
        # dscacheutil 不可用/无答案时，退回 dig（装了 bind 工具就有）
        code, out = _run(["dig", "+short", name], timeout=DNS_QUERY_TIMEOUT)
        if code == 0:
            return _first_ip(out)
        return None

    def http_ok(self, url: str) -> bool:
        # 本地回环探测必须绕过用户代理：curl 默认读 http_proxy/ALL_PROXY，
        # 公司代理配置会让发往 <dnsmasq 解析的 .test 域名> 的请求跑去公网，
        # verify() 于是假阴性。
        code, _ = _run(["curl", "--noproxy", "*", "-fsS", "--max-time", "3", url])
        return code == 0

    def ca_trusted(self, ca: Path) -> bool:
        """CA 是否**真的在系统信任链里**。

        只看暂存文件是不够的：那个文件在 add-trusted-cert 没跑成功时也在，
        系统却不认它。所以让 security 对着系统信任设置验一遍。
        """
        code, _ = _run(["security", "verify-cert", "-c", str(ca), "-p", "ssl"])
        return code == 0

    def privilege_read(self, path: Path) -> str | None:
        """非交互地读特权文件；读不到返回 None（绝不弹密码框）。

        macOS 的 /Library/LaunchDaemons 通常全局可读；真读不到就当
        "读不到"，让调用方按"宁放过也不误报"处理。
        """
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def nss_trusted(self, ca: Path) -> bool:
        """macOS 没有 NSS 库 —— 恒为真，证书组件才不会去装 NSS 证书。"""
        return True

    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        """macOS 只有 resolver 后端：每域解析器文件存在即已配置。"""
        if backend == "resolver":
            return (RESOLVER_DIR / suffix.lstrip(".")).exists()
        return False

    # -- linger（macOS 不适用） -----------------------------------------

    def enable_linger(self) -> tuple[int, str]:
        """macOS 没有 systemd-linger —— 不适用，且不得失败。"""
        return 0, "macOS 无 systemd-linger，不适用"

    def linger_enabled(self) -> bool:
        """macOS 无 linger 可启用 —— 恒为真，setup 不会计划任何 linger 动作。"""
        return True

    # -- 服务 ----------------------------------------------------------

    def service_unit_path(self, name: str) -> Path:
        """caddy 是 system scope，其余是 user scope。

        macOS 没有 setcap：绑 80/443 必须以 root 运行，只能放 LaunchDaemons。
        """
        if self._is_system_scope(name):
            return Path(LAUNCH_DAEMONS) / f"com.devproxy.{name}.plist"
        return self.user_home / LAUNCH_AGENTS / f"com.devproxy.{name}.plist"

    def log_path(self, name: str) -> Path:
        return self._state_dir() / "logs" / f"{name}.log"

    def install_service(
        self,
        name: str,
        *,
        exec_start: list[str],
        exec_reload: list[str] | None = None,
        exec_stop: list[str] | None = None,
        env: dict[str, str],
        description: str = "",
    ) -> None:
        """渲染 launchd plist 并 bootstrap 到对应域。

        单元格式由平台自己决定 —— 组件只给"跑什么、带什么环境变量"。

        `exec_reload` / `exec_stop` 在 launchd 下**不适用**（launchd 用
        kickstart / KeepAlive 管理生命周期，没有对应的 plist 字段），
        这里刻意忽略；协议允许平台忽略这两个可选项。若将来需要
        reload 语义，应作为非协议能力单独加，而不是往 plist 里塞伪字段。
        """
        _assert_may_mutate()
        path = self.service_unit_path(name)
        args = [self._expand(a) for a in exec_start]
        content = render_plist(name, args, env, self.log_path(name), description)
        domain = self._domain(name)
        target = f"{domain}/com.devproxy.{name}"
        # 日志目录必须在 bootstrap 之前就存在：launchd 打开 StandardOutPath
        # 时父目录不存在就直接起不来。组件顺序是 caddy → dnsmasq → …，
        # caddy 最先装 —— 那时日志目录还没人建。目录在用户家目录下，
        # 用用户身份建即可（root 进程照样写得进去）。
        self.log_path(name).parent.mkdir(parents=True, exist_ok=True)
        if self._is_system_scope(name):
            # /Library/LaunchDaemons 归 root —— 必须走特权写入。
            # `MacOSElevated.write` 用 `install -m 644 -o root -g wheel`
            # 落盘，单元天然是 root:wheel 0644（工具自己也读得到），
            # 不再需要额外的 chown/chmod。
            self.privilege().write(path, content)
            self.privilege().run(["launchctl", "bootout", target])
            code, msg = self.privilege().run(["launchctl", "bootstrap", "system", str(path)])
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            # bootout 只是"卸掉可能存在的旧实例"，没装过时失败是正常的
            _run(["launchctl", "bootout", target])
            code, msg = _run(["launchctl", "bootstrap", domain, str(path)])
        if code != 0:
            raise RuntimeError(f"launchctl bootstrap {name} 失败: {msg}")

    def enable_service(self, name: str) -> None:
        """launchd 靠 RunAtLoad=True 自动拉起，无需额外 enable。

        刻意不做任何写操作 —— 重写 plist 会把 ProgramArguments 清空。
        """
        _assert_may_mutate()
        return

    def restart_service(self, name: str) -> None:
        _assert_may_mutate()
        target = f"{self._domain(name)}/com.devproxy.{name}"
        argv = ["launchctl", "kickstart", "-k", target]
        if self._is_system_scope(name):
            code, msg = self.privilege().run(argv)
        else:
            code, msg = _run(argv)
        if code != 0:
            raise RuntimeError(f"launchctl kickstart {name} 失败: {msg}")

    def remove_service(self, name: str) -> None:
        """bootout 并删除 plist；服务没装过时静默成功（幂等）。

        system scope（caddy）的 plist 在 /Library/LaunchDaemons，删除必须
        走提权会话；user scope 在用户自己家目录，直接删。
        """
        _assert_may_mutate()
        target = f"{self._domain(name)}/com.devproxy.{name}"
        path = self.service_unit_path(name)
        if self._is_system_scope(name):
            # bootout 对不存在的服务返回非零 —— 幂等，不看返回码
            self.privilege().run(["launchctl", "bootout", target])
            code, msg = self.privilege().run(["rm", "-f", str(path)])
            if code != 0:
                raise RuntimeError(f"删除 {name} 的 LaunchDaemon 失败: {msg}")
            return
        _run(["launchctl", "bootout", target])
        path.unlink(missing_ok=True)

    def tail_service_log(self, name: str) -> None:
        """流式跟随服务日志：继承终端 stdio（不捕获），Ctrl-C 自然传播。"""
        subprocess.run(["tail", "-f", str(self.log_path(name))])

    def open_in_editor(self, path: Path, editor: str) -> int:
        """用编辑器打开文件：继承 stdio（编辑器要接管终端），返回退出码。"""
        argv = shlex.split(editor) + [str(path)]
        return subprocess.run(argv).returncode

    # -- 内部 ----------------------------------------------------------

    def _is_system_scope(self, name: str) -> bool:
        return name == "caddy"

    def _domain(self, name: str) -> str:
        """launchd 域：system（root 的守护进程）或 gui/<uid>（用户级代理）。"""
        if self._is_system_scope(name):
            return "system"
        return f"gui/{os.getuid()}"

    def _state_dir(self) -> Path:
        """状态目录 —— 唯一来源是 paths.default_state_dir，平台不自己拼路径。"""
        return default_state_dir(self.user_home)

    def _expand(self, arg: str) -> str:
        """展开参数里的 ~ —— launchd 认不出它。

        不只是开头：`--conf-dir=~/.config/dnsmasq` 这种也要展开，
        否则以 root 运行时指到 root 的家目录。
        """
        if arg == "~":
            return str(self.user_home)
        if arg.startswith("~/"):
            return str(self.user_home / arg[2:])
        idx = arg.find("=~/")
        if idx >= 0:
            head = arg[: idx + 1]
            return f"{head}{self.user_home / arg[idx + 3 :]}"
        return arg


def get_platform() -> Platform:
    """跨平台分派工厂（按 brief 放在这里；实现在 platform/__init__.py）。"""
    from devproxy.platform import get_platform as _dispatch

    return _dispatch()
