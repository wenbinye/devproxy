"""Linux 平台实现：systemd(user) / setcap / update-ca-certificates / NSS / resolved。"""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import ipaddress
import os
import subprocess
import tempfile
from pathlib import Path

from devproxy.platform.base import (
    CA_NICKNAME,
    SYSTEM_TRUST_DIR,
    ElevatedSession,
)

# 系统信任库（活库）里可能存放 CA 的位置。
#
# - 汇总包：Debian/Ubuntu 的 /etc/ssl/certs/ca-certificates.crt、
#   RHEL 的 /etc/pki/tls/certs/ca-bundle.crt
# - 散列索引目录：/etc/ssl/certs 下的 *.0（主题散列软链）与 *.pem
#
# 刻意**不含**暂存目录 /usr/local/share/ca-certificates —— 那个文件存在
# 只说明有人放了它进去，不代表 update-ca-certificates 跑成功过。
TRUST_BUNDLE_FILES = (
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
)
TRUST_HASH_DIR = Path("/etc/ssl/certs")


def _pem_body(text: str) -> str:
    """PEM 的规范化正文：去掉头尾标记、空行与所有空白。"""
    lines = (line.strip() for line in text.splitlines())
    return "".join(line for line in lines if line and not line.startswith("-----"))


def _pem_der_blocks(text: str) -> list[bytes]:
    """从 PEM 文本里取出全部证书的 DER 字节。

    只做 base64 解码，不做 X.509 解析 —— 判据是"同一份字节"，
    解析证书结构反而会引入额外的失败点。
    """
    blocks: list[bytes] = []
    buf: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("-----BEGIN CERTIFICATE"):
            inside = True
            buf = []
            continue
        if line.startswith("-----END CERTIFICATE"):
            inside = False
            try:
                blocks.append(base64.b64decode("".join(buf), validate=False))
            except (binascii.Error, ValueError):
                pass
            buf = []
            continue
        if inside:
            buf.append(line)
    return blocks


def _cert_fingerprint(der: bytes) -> str:
    """证书指纹：DER 的 sha256（与 `openssl x509 -fingerprint -sha256` 同义）。"""
    return hashlib.sha256(der).hexdigest().upper()


def _pem_fingerprints(text: str) -> set[str]:
    return {_cert_fingerprint(der) for der in _pem_der_blocks(text)}


def _store_candidates() -> list[Path]:
    """系统信任库里可能放着 CA 的文件（汇总包 + 散列索引目录）。

    只读、只列名字；任何列目录的错误都吞掉 —— 判据失败必须退化成
    "未信任"，绝不能让 doctor 抛异常。
    """
    files: list[Path] = list(TRUST_BUNDLE_FILES)
    try:
        if TRUST_HASH_DIR.is_dir():
            for pattern in ("*.0", "*.pem", "*.crt"):
                files.extend(sorted(TRUST_HASH_DIR.glob(pattern)))
    except OSError:
        pass
    return files


def ca_in_trust_store(ca: Path) -> bool:
    """CA 是否**按指纹**存在于系统信任库。永不抛异常。

    判据是"活库里有没有这份字节完全相同的证书"，而不是"openssl 能不能
    验通这条链"：openssl 按 **SUBJECT** 选锚点，信任库里存在多个同主题
    根时它会挑到另一个，报 error 18（self-signed certificate）——
    而 curl 其实完全正常。拿它当判据会让 setup 永远认为"没信任"，
    而重装同一个 CA 只会增加更多同主题根，永远修不好。
    """
    try:
        ca_text = ca.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    target = _pem_fingerprints(ca_text)
    if not target:
        # 不是可解析的 PEM 证书 —— 无法按指纹比对，判为未信任
        return False
    for path in _store_candidates():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            if target & _pem_fingerprints(text):
                return True
        except Exception:  # noqa: BLE001 —— 单个文件读坏了不影响其余
            continue
    return False


def _pem_digest(text: str) -> str:
    return hashlib.sha256(_pem_body(text).encode("utf-8")).hexdigest()


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except OSError as e:
        # 命令不存在（FileNotFoundError）、可执行位缺失（PermissionError）等
        # 都归一为“命令不可用”，让调用方按返回码处理而不是 traceback。
        return 127, f"命令不可用: {argv[0]}（{e}）"
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def _first_ip(text: str) -> str | None:
    """从 resolvectl / dig 输出里取第一个 A/AAAA 答案。"""
    for line in text.splitlines():
        for token in line.replace(",", " ").split():
            candidate = token.strip("[];()")
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                continue
    return None


# 平台自身单元测试的显式豁免：那些用例要把 _run 换成假的、路径指向
# tmp_path，覆盖的正是写操作的失败处理 —— 不需要护栏拦着。
_ALLOW_MUTATION = False


def _assert_may_mutate() -> None:
    """测试里禁止改动真实系统。

    平台对象一旦拿到真实 HOME，就能改写 ~/.config/systemd/user/ 下的单元、
    启停真实服务、甚至 sudo 写 /etc。隔离一旦漏掉一处，就会静默改掉用户机器
    （曾经真的覆盖过用户自己的 caddy.service）。

    护栏放在平台层而不是测试夹具里：只有平台知道哪些调用会动系统。
    只读探测（service_active / dns_query / http_ok …）不受影响。
    """
    if _ALLOW_MUTATION:
        return
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("DEVPROXY_NO_MUTATION"):
        raise RuntimeError("测试中禁止调用真实平台的写操作")


def render_systemd_unit(
    name: str,
    exec_start: list[str],
    exec_reload: list[str] | None,
    exec_stop: list[str] | None,
    env: dict[str, str],
    description: str = "",
    service_type: str | None = None,
) -> str:
    """生成 systemd user 单元。形态与现有的 caddy.service 保持一致。

    systemd 单元不做 shell 引号解析：参数里的空白会截断命令行。
    路径来自配置（用户可改），必须在这里拦住。

    service_type 默认按被拉起的程序判断：只有 caddy 会发 sd_notify，
    dnsmasq 这类程序硬套 Type=notify 会永远卡在 "start timed out"。
    """
    for label, args in (
        ("ExecStart", exec_start),
        ("ExecReload", exec_reload),
        ("ExecStop", exec_stop),
    ):
        for value in args or []:
            if any(c.isspace() for c in value):
                raise RuntimeError(f"{label} 参数不能包含空白字符: {value}")
    if service_type is None:
        program = Path(exec_start[0]).name if exec_start else ""
        service_type = "notify" if program.startswith("caddy") else "simple"
    lines = [
        "[Unit]",
        f"Description={description or f'{name} (managed by devproxy)'}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        f"Type={service_type}",
    ]
    for k, v in env.items():
        lines.append(f"Environment={k}={v}")
    lines.append(f"ExecStart={' '.join(exec_start)}")
    if exec_reload:
        lines.append(f"ExecReload={' '.join(exec_reload)}")
    if exec_stop:
        lines.append(f"ExecStop={' '.join(exec_stop)}")
    lines += [
        "TimeoutStopSec=5s",
        "LimitNOFILE=1048576",
        "Restart=on-failure",
        "RestartSec=2s",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


class LinuxElevated:
    """提权会话。构造时执行一次 `sudo -v` 完成预认证。"""

    def __init__(self) -> None:
        # 护栏也必须拦构造函数：直接 new 一个提权会话会真的跑 sudo -v。
        _assert_may_mutate()
        self._authenticated = False
        self._authenticate()

    def _authenticate(self) -> None:
        code, msg = _run(["sudo", "-v"])
        if code != 0:
            raise PermissionError(f"提权失败：{msg}")
        self._authenticated = True

    def run(self, argv: list[str]) -> tuple[int, str]:
        return _run(["sudo", *argv])

    def write(self, path: Path, content: str) -> None:
        """通过系统临时目录中的文件 + 特权安装写入，避免 shell 引号问题。

        父目录不一定存在：`/etc/systemd/resolved.conf.d` 与 `/etc/dnsmasq.d`
        在干净机器上可能尚未创建（不归任何 dpkg 包所有）。先 `install -d`
        建目录，否则写入必然失败，setup 永远无法收敛。
        临时文件必须放在用户可写的系统临时目录（目标目录本身可能不可写），
        且无论成功失败都要清理。

        用 `install -m 644 -o root -g root` 而不是 `cp`：mkstemp 建出来的
        临时文件是 0600，`cp` 会把权限一并带过去，于是写进
        /usr/local/share/ca-certificates/ 与 /etc/systemd/resolved.conf.d/
        的文件变成 root:root 0600 —— 调用的普通用户自己都读不了，
        doctor 会永远报"文件读不到"。macOS 那条路径已经改成 0644，
        Linux 必须对齐。
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
                    "root",
                    str(tmp),
                    str(path),
                ]
            )
            if code != 0:
                raise PermissionError(f"写入 {path} 失败: {msg}")
        finally:
            tmp.unlink(missing_ok=True)

    def read(self, path: Path) -> str:
        """读取特权文件。仅当输出表明文件不存在时抛 FileNotFoundError，
        其余失败（如权限被拒）抛 PermissionError。"""
        code, out = self.run(["cat", str(path)])
        if code != 0:
            if "No such file" in out:
                raise FileNotFoundError(path)
            raise PermissionError(f"读取 {path} 失败: {out}")
        return out

    def copy(self, src: Path, dest: Path) -> None:
        """复制文件（CA 装进系统信任库走这条路）。

        同样用 `install -m 644 -o root -g root`：`cp` 会把源文件的权限
        带过去，源文件若是 0600，目标就成了 root:root 0600。
        """
        code, msg = self.run(
            [
                "install",
                "-m",
                "644",
                "-o",
                "root",
                "-g",
                "root",
                str(src),
                str(dest),
            ]
        )
        if code != 0:
            raise PermissionError(f"复制 {src} → {dest} 失败：{msg}")


class LinuxPlatform:
    name = "linux"
    dns_port = 5353

    def __init__(self, user_home: Path | None = None) -> None:
        self.user_home = user_home or Path.home()
        self._elevated: LinuxElevated | None = None

    def privilege(self) -> ElevatedSession:
        _assert_may_mutate()
        if self._elevated is None:
            self._elevated = LinuxElevated()
        return self._elevated

    def run(self, argv: list[str]) -> tuple[int, str]:
        return _run(argv)

    def privilege_read(self, path: Path) -> str | None:
        """非交互地读特权文件（`sudo -n`）。读不到返回 None。

        doctor 是只读命令，绝不能因为权限问题弹出密码框 —— 那既违反
        "只读体检"的承诺，也会让脚本里跑的 doctor 卡在密码提示上。
        `sudo -n` 在没有缓存凭据时立刻失败，绝不等待输入。
        """
        try:
            proc = subprocess.run(
                ["sudo", "-n", "cat", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def binary_version(self, binary: Path) -> str:
        if not binary.exists():
            return ""
        code, out = _run([str(binary), "version"])
        if code != 0:
            return ""
        return out.split()[0] if out else ""

    def service_unit_path(self, name: str) -> Path:
        return self.user_home / ".config" / "systemd" / "user" / f"{name}.service"

    def service_active(self, name: str) -> bool:
        code, out = _run(["systemctl", "--user", "is-active", name])
        return code == 0 and out.strip() == "active"

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
        _assert_may_mutate()
        path = self.service_unit_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_systemd_unit(
                name, exec_start, exec_reload, exec_stop, env, description
            ),
            encoding="utf-8",
        )
        code, msg = _run(["systemctl", "--user", "daemon-reload"])
        if code != 0:
            raise RuntimeError(f"安装 {name} 后 systemctl daemon-reload 失败: {msg}")

    def enable_service(self, name: str) -> None:
        _assert_may_mutate()
        code, msg = _run(["systemctl", "--user", "enable", "--now", name])
        if code != 0:
            raise RuntimeError(f"systemctl enable --now {name} 失败: {msg}")

    def restart_service(self, name: str) -> None:
        _assert_may_mutate()
        code, msg = _run(["systemctl", "--user", "restart", name])
        if code != 0:
            raise RuntimeError(f"systemctl restart {name} 失败: {msg}")

    def tail_service_log(self, name: str) -> None:
        """流式跟随服务日志：继承终端 stdio（不捕获），Ctrl-C 自然传播。"""
        subprocess.run(["journalctl", "--user", "-u", name, "-f"])

    def enable_linger(self) -> tuple[int, str]:
        _assert_may_mutate()
        return _run(["sudo", "loginctl", "enable-linger", getpass.getuser()])

    def linger_enabled(self) -> bool:
        """当前用户是否已启用 linger（只读探测）。

        loginctl 不可用（容器 / WSL / 没有 systemd）时，linger 根本不适用 ——
        判成"未启用"会让每次 setup 都计划一个注定失败、还要 sudo 的动作，
        失败时 setup 直接退 1，整个工具在那种机器上不可用。
        所以探测失败一律按"无需启用"处理。
        """
        code, out = _run(
            ["loginctl", "show-user", getpass.getuser(), "-p", "Linger", "--value"]
        )
        if code != 0:
            return True
        return out.strip() == "yes"

    def resolved_active(self) -> bool:
        code, out = _run(["systemctl", "is-active", "systemd-resolved"])
        if code == 0 and out.strip() == "active":
            return True
        # 部分环境没有 systemd-resolved 的 systemd 单元名，但 resolvectl
        # 能工作（例如容器）；以能否查询状态作为兜底判据。
        code, _ = _run(["resolvectl", "status"])
        return code == 0

    def dns_query(self, name: str) -> str | None:
        """优先走系统解析器（resolved），失败再直接问本机 dnsmasq。"""
        code, out = _run(["resolvectl", "query", name])
        if code == 0:
            answer = _first_ip(out)
            if answer is not None:
                return answer
        code, out = _run(
            ["dig", "@127.0.0.1", "-p", str(self.dns_port), name, "+short"]
        )
        if code == 0:
            return _first_ip(out)
        return None

    def http_ok(self, url: str) -> bool:
        code, _ = _run(["curl", "-fsS", "--max-time", "3", url])
        return code == 0

    def ca_trusted(self, ca: Path) -> bool:
        """CA 是否**真的在系统信任库里**。

        权威判据是**指纹命中**（见 ca_in_trust_store）：活库里存在与该 CA
        字节完全相同的证书才算信任。

        只比对暂存目录（/usr/local/share/ca-certificates）里的文件是不够的：
        那个文件在 update-ca-certificates 没跑成功时也在那儿，系统却不认它。

        openssl 只作为**补充的肯定信号**：验通了说明信任链确实成立；
        它验不通可能只是上面的同主题歧义，绝不用来否定指纹命中。
        """
        try:
            if ca_in_trust_store(ca):
                return True
        except Exception:  # noqa: BLE001 —— 判定异常一律按"未信任"
            pass
        code, _ = _run(
            ["openssl", "verify", "-CApath", str(SYSTEM_TRUST_DIR), str(ca)]
        )
        return code == 0

    def nss_trusted(self, ca: Path) -> bool:
        """NSS 库里存的证书是否**就是这一份** CA。只读、非特权。

        只按 nickname 判"条目存在"是不够的：Caddy 重新生成内部 CA（删
        pki/、换 storage、迁移）后旧条目仍在，Chrome 会拒绝新 CA，而工具
        却以为已就绪、连修复路径都不给。所以让 certutil 打出 PEM 比对内容。
        """
        nss = self.user_home / ".pki" / "nssdb"
        if not (nss / "cert9.db").exists():
            return False
        try:
            expected = ca.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        code, stored = _run(
            ["certutil", "-L", "-d", f"sql:{nss}", "-n", CA_NICKNAME, "-a"]
        )
        if code != 0:
            return False
        return _pem_digest(stored) == _pem_digest(expected)

    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        """Linux 的两个后端都是固定路径，不看域名后缀（macOS 才按后缀分文件）。"""
        if backend == "resolved":
            return Path("/etc/systemd/resolved.conf.d/devproxy.conf").exists()
        if backend == "system":
            return Path("/etc/dnsmasq.d/devproxy.conf").exists()
        return False
