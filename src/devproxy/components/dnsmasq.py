"""用户级 dnsmasq 组件：配置增量合并 + systemd user 单元。

配置只做增量修改 —— 用户自己的 `~/.config/dnsmasq/dnsmasq.conf` 里可能有
`.develop` 之类的域名条目、`no-resolv`、上游 server、`filter-AAAA`，工具不得
重写整个文件，只保证 `port=`、本工具域名的 `address=` 条目与（必要时）
上游 server 存在。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Action, Platform, Status

DNSMASQ_CONF = Path("~/.config/dnsmasq/dnsmasq.conf")

# macOS 上系统不带 dnsmasq —— Homebrew 装在 /opt/homebrew/sbin（Apple Silicon）
# 或 /usr/local/sbin（Intel）。launchd 不认 PATH，必须绝对路径。
MACOS_DNSMASQ_CANDIDATES = (
    "/opt/homebrew/sbin/dnsmasq",
    "/usr/local/sbin/dnsmasq",
    "/usr/sbin/dnsmasq",
)

# 没有全局上游时补的公共 DNS。绝不能补 127.0.0.53（systemd-resolved 桩）——
# resolved 会被指回本机 dnsmasq，形成解析环路。
DEFAULT_UPSTREAMS = ("server=1.1.1.1", "server=8.8.8.8")


def _option_present(lines: list[str], name: str) -> bool:
    """配置里是否已有该选项（带值 `name=` 或裸选项 `name`）。"""
    prefix = name + "="
    for line in lines:
        s = line.strip()
        if s == name or s.startswith(prefix):
            return True
    return False


def _is_global_server(line: str) -> bool:
    """`server=` 是否是**全局**上游。

    dnsmasq 手册：`--server=/<domain>/<ip>` 只服务于该域名，其余域名仍走
    /etc/resolv.conf —— 它不构成全局上游（`server=1.2.3.4,domain` 同理）。
    """
    s = line.strip()
    if not s.startswith("server="):
        return False
    value = s[len("server="):].strip()
    if not value:
        return False
    if "/" in value or "," in value:
        return False
    return True


def merge_dnsmasq_config(existing: str, *, port: int, suffix: str) -> str:
    """增量修改 dnsmasq 配置：确保 port、address、监听范围与上游存在，其余原样保留。

    绝不重写整个文件 —— 用户可能有 .develop 等自己的域名条目。

    上游规则（依据 dnsmasq 手册）：
    - 手册明说 `server=` **不会**抑制读取 /etc/resolv.conf，只有 `--no-resolv`
      才会。而本工具的 resolved drop-in 已把全局 DNS 指向 dnsmasq，于是
      /etc/resolv.conf（resolved 机器上是 127.0.0.53 桩）绝不能再当上游 ——
      否则 resolved ↔ dnsmasq 互指，普通域名解析全机失败。
      故：除非用户显式 `resolv-file=`（自选解析器文件，与 no-resolv 互斥），
      一律确保 `no-resolv` 存在。
    - 域名限定的上游（`server=/corp/10.0.0.1`）不算全局上游；没有全局上游时
      补公共 DNS，且绝不补 127.0.0.53 这类本机桩。
    - 用户显式写了 `resolv-file=` 时一律不动上游：那是自选的解析器文件，
      既不与 no-resolv 并存，也不该被公共 DNS 掺和。
    """
    lines = existing.splitlines()
    out: list[str] = []
    seen_port = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("port="):
            if not seen_port:
                out.append(f"port={port}")
                seen_port = True
            continue
        out.append(line)
    if not seen_port:
        out.append(f"port={port}")
    suffix = suffix.lstrip(".")
    present = {line.strip() for line in out}
    for addr in (f"address=/.{suffix}/127.0.0.1", f"address=/.{suffix}/::1"):
        if addr not in present:
            out.append(addr)
            present.add(addr)
    # 只监听回环：缺这两行时 dnsmasq 会绑 0.0.0.0:5353，成为局域网上的
    # 开放递归解析器（DNS 放大攻击面）。用户已自行设置过就保留不改。
    if not _option_present(out, "listen-address"):
        out.append("listen-address=127.0.0.1")
        present.add("listen-address=127.0.0.1")
    if not _option_present(out, "bind-interfaces"):
        out.append("bind-interfaces")
        present.add("bind-interfaces")
    has_resolv_file = _option_present(out, "resolv-file")
    if not has_resolv_file and not _option_present(out, "no-resolv"):
        out.append("no-resolv")
        present.add("no-resolv")
    if not has_resolv_file and not any(_is_global_server(line) for line in out):
        for upstream in DEFAULT_UPSTREAMS:
            if upstream not in present:
                out.append(upstream)
                present.add(upstream)
    return "\n".join(out) + "\n"


class DnsmasqComponent:
    name = "dnsmasq"

    def __init__(
        self,
        platform: Platform,
        paths: Paths,
        config: Config,
        conf_path: Path | None = None,
    ):
        self.platform = platform
        self.paths = paths
        self.config = config
        # 配置路径可注入：测试指向 tmp_path，绝不写真实 ~/.config/dnsmasq/
        self.conf_path = conf_path or DNSMASQ_CONF.expanduser()

    # -- check ---------------------------------------------------------
    def _service_hint(self) -> str:
        """服务相关的修复提示（按平台给不同的命令）。

        macOS 上不能叫用户去跑 systemctl —— 那里没有 systemd。
        """
        if self.platform.name == "mac":
            return f"launchctl kickstart -k gui/{os.getuid()}/com.devproxy.dnsmasq"
        return "systemctl --user start dnsmasq"

    def check(self) -> Status:
        if not self.platform.service_active("dnsmasq"):
            return Status(
                ok=False,
                detail="dnsmasq 服务未运行",
                hint=self._service_hint(),
            )
        if not self.conf_path.exists():
            return Status(
                ok=False,
                detail=f"dnsmasq 配置不存在: {self.conf_path}",
                hint="devproxy setup 会生成",
            )
        return Status(ok=True, detail="dnsmasq 运行中")

    # -- plan ----------------------------------------------------------
    def plan(self) -> list[Action]:
        """配置与服务都就绪时返回空列表 —— setup 才能判定为 no-op。"""
        existing = ""
        if self.conf_path.exists():
            existing = self.conf_path.read_text(encoding="utf-8")
        merged = merge_dnsmasq_config(
            existing, port=self.platform.dns_port, suffix=self.config.domain_suffix
        )
        if self.platform.service_active("dnsmasq") and existing == merged:
            return []
        return [
            Action(
                description="合并 dnsmasq 配置（保留既有域名条目）",
                kind="write",
                path=self.conf_path,
                content=merged,
            ),
            Action(
                description=(
                    "安装并启动 dnsmasq LaunchAgent（macOS）"
                    if self.platform.name == "mac"
                    else "安装并启动 dnsmasq.service（systemd user）"
                ),
                kind="run",
                command=["install-service", "dnsmasq"],
            ),
        ]

    # -- apply ---------------------------------------------------------
    def apply(self, actions: list[Action]) -> None:
        for action in actions:
            if action.kind == "write" and action.path is not None:
                action.path.parent.mkdir(parents=True, exist_ok=True)
                self._backup_if_changed(action.path, action.content)
                action.path.write_text(action.content, encoding="utf-8")
                continue
            if action.command[:1] == ["install-service"]:
                self.platform.install_service(
                    "dnsmasq",
                    exec_start=self._exec_start(),
                    exec_reload=self._exec_reload(),
                    env={},
                    description=(
                        "dnsmasq for devproxy (LaunchAgent)"
                        if self.platform.name == "mac"
                        else "dnsmasq for devproxy (user scope)"
                    ),
                )
                self.platform.enable_service("dnsmasq")
                continue
            raise RuntimeError(f"未知动作: {action.command}")

    def _backup_if_changed(self, path: Path, content: str) -> None:
        """改写用户既有配置前先备份（Plan 2 Global Constraints）。

        内容没变时绝不落备份文件 —— 否则每次 setup 都会在状态目录里堆垃圾。
        """
        if path != self.conf_path or not path.exists():
            return
        try:
            original = path.read_bytes()
        except OSError:
            return
        if original == content.encode("utf-8"):
            return
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = self.paths.backup_dir / f"dnsmasq.conf.{stamp}.bak"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(original)

    # -- verify --------------------------------------------------------
    def verify(self) -> Status:
        """服务 active 之外，还真要能解析 *.test —— 配置写对不代表 dnsmasq
        加载了它（端口占用、启动失败、单元指向别的 conf-dir 都会骗过 check）。"""
        if not self.platform.service_active("dnsmasq"):
            return Status(
                ok=False,
                detail="dnsmasq 服务未处于 active",
                hint=self._service_hint(),
            )
        name = f"foo.{self.config.domain_suffix}"
        answer = self.platform.dns_query(name)
        if answer not in ("127.0.0.1", "::1"):
            hint = (
                f"launchctl kickstart -k gui/{os.getuid()}/com.devproxy.dnsmasq"
                if self.platform.name == "mac"
                else "systemctl --user restart dnsmasq"
            )
            return Status(
                ok=False,
                detail=(
                    f"dnsmasq 未把 {name} 解析到本机（{answer or '无响应'}）"
                ),
                hint=f"{hint}；dig @127.0.0.1 -p {self.platform.dns_port} {name}",
            )
        return Status(ok=True, detail=f"dnsmasq 运行中（{name} → {answer}）")

    # -- unit ----------------------------------------------------------
    def _conf_dir_spec(self) -> str:
        """单元里用 %h 表达家目录；配置目录不在家目录下时用绝对路径。

        必须先 resolve：systemd 把相对路径按 / 解析，`--conf-dir=rel` 会指到
        根目录下，单元根本起不来。

        macOS 的 launchd 不认 %h —— 一律写绝对路径。
        """
        conf_dir = self.conf_path.resolve().parent
        if self.platform.name == "mac":
            return str(conf_dir)
        try:
            rel = conf_dir.relative_to(Path.home().resolve())
        except ValueError:
            return str(conf_dir)
        return "%h" if str(rel) == "." else f"%h/{rel}"

    def _dnsmasq_binary(self) -> str:
        """dnsmasq 可执行文件的绝对路径。

        Linux 上固定 /usr/sbin/dnsmasq；macOS 上系统不带 dnsmasq，
        Homebrew 装在 /opt/homebrew/sbin 或 /usr/local/sbin —— 按存在的那个挑。

        固定路径优先于 PATH：launchd 不认 PATH，而且 PATH 里那个未必是
        Homebrew 装的那份（换了前缀的旧版本、别的包管理器装的）。
        """
        if self.platform.name != "mac":
            return "/usr/sbin/dnsmasq"
        for candidate in MACOS_DNSMASQ_CANDIDATES:
            if Path(candidate).exists():
                return candidate
        found = shutil.which("dnsmasq")
        if found:
            return found
        return MACOS_DNSMASQ_CANDIDATES[0]

    def _exec_start(self) -> list[str]:
        """dnsmasq 的启动命令。

        dnsmasq 默认 fork 后父进程退出 —— Type=simple 会认为主进程立刻结束，
        单元随即变 inactive。必须 `--keep-in-foreground` 让 systemd 盯住真实
        进程。配合 `--keep-in-foreground` 不再需要 pid 文件（也顺带去掉对
        `~/.cache` 目录存在的依赖）。
        """
        return [
            self._dnsmasq_binary(),
            "--keep-in-foreground",
            f"--conf-dir={self._conf_dir_spec()}",
        ]

    def _exec_reload(self) -> list[str] | None:
        """重载配置：给 dnsmasq 发 HUP，让它重读 conf-dir。

        launchd 的 plist 没有 ExecReload —— macOS 上返回 None，
        重载靠 kickstart 重启服务。
        """
        if self.platform.name == "mac":
            return None
        return ["/bin/kill", "-HUP", "$MAINPID"]
