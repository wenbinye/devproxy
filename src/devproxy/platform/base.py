"""平台能力协议。组件层只依赖这些能力，不直接调用系统命令。"""

from __future__ import annotations

import plistlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

# 证书信任的全局约定：组件与平台实现共用同一份，避免两边对不上。
CA_NICKNAME = "devproxy Local CA"
SYSTEM_CA_DEST = Path("/usr/local/share/ca-certificates/devproxy-local.crt")
SYSTEM_TRUST_DIR = Path("/etc/ssl/certs")
NSS_DIR = Path("~/.pki/nssdb")

# macOS 的对应物：CA 进系统钥匙串（Safari/Chrome 都认它），
# DNS 路由走每域解析器文件（/etc/resolver/<后缀>），不动 /etc/resolv.conf。
SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"
RESOLVER_DIR = Path("/etc/resolver")


@dataclass
class Status:
    """组件检查结果。ok=False 时 detail 说明现状，hint 给出可执行的修复命令。"""

    ok: bool
    detail: str = ""
    hint: str = ""


def render_resolver_file(port: int) -> str:
    """/etc/resolver/<后缀> 的内容（macOS 的每域解析器格式）。

    macOS 原生支持每域解析器，不必改 /etc/resolv.conf。
    放在这里是因为组件层（DNS 组件）也要用它 —— 组件不直接依赖某个平台实现。
    """
    return f"nameserver 127.0.0.1\nport {port}\n"


def unit_exec_argv(text: str) -> list[str] | None:
    """从服务单元文本里解析出启动命令行。

    两种格式都要认：
    - systemd：``ExecStart=/path/caddy run --config /path/Caddyfile``
    - launchd plist：``ProgramArguments`` 数组

    组件判断"单元是不是当前的"靠它 —— 只做子串匹配会把顺序与参数配对
    都放过（"run --config" 和 "--config … run" 都能蒙混过关），单元
    指向别的 Caddyfile 时就判不出来了。
    """
    try:
        data = plistlib.loads(text.encode("utf-8"))
    except Exception:  # noqa: BLE001 —— 不是 plist 就当 systemd 单元看
        data = None
    if isinstance(data, dict):
        argv = data.get("ProgramArguments")
        if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):
            return list(argv)
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("ExecStart="):
            continue
        args = line[len("ExecStart="):].split()
        if args:
            return args
    return None


@dataclass
class Action:
    """一条待执行动作。"""

    description: str
    kind: str = "run"  # "run" | "write"
    command: list[str] = field(default_factory=list)
    path: Path | None = None
    content: str = ""
    privileged: bool = False


@runtime_checkable
class ElevatedSession(Protocol):
    """一次提权会话：一次密码验证，后续操作复用同一会话。"""

    def run(self, argv: list[str]) -> tuple[int, str]: ...

    def write(self, path: Path, content: str) -> None: ...

    def read(self, path: Path) -> str: ...

    def copy(self, src: Path, dest: Path) -> None: ...


@runtime_checkable
class Platform(Protocol):
    name: str
    dns_port: int

    def privilege(self) -> ElevatedSession: ...

    def run(self, argv: list[str]) -> tuple[int, str]: ...

    def binary_version(self, binary: Path) -> str: ...

    def service_active(self, name: str) -> bool: ...

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
        """安装并注册一个服务。单元格式由平台自己决定（systemd / launchd）。

        组件层只给"跑什么、带什么环境变量"，不碰单元格式 ——
        否则 systemd 与 launchd 的差异会渗进组件里。
        """

    def enable_service(self, name: str) -> None: ...

    def restart_service(self, name: str) -> None: ...

    def service_unit_path(self, name: str) -> Path: ...

    def tail_service_log(self, name: str) -> None: ...

    def enable_linger(self) -> tuple[int, str]:
        """`loginctl enable-linger <user>`：让 user unit 开机自启、无需登录。"""

    def linger_enabled(self) -> bool:
        """当前用户是否已启用 linger（只读探测）。"""

    def resolved_active(self) -> bool:
        """systemd-resolved 是否真的在运行（auto DNS 后端的判据）。"""

    def dns_query(self, name: str) -> str | None:
        """按工具预期的路径解析域名，返回首个 A/AAAA 答案；失败返回 None。"""

    def http_ok(self, url: str) -> bool:
        """有超时上限的本地 HTTP(S) 探测；成功（2xx/3xx）返回 True。"""

    def ca_trusted(self, ca: Path) -> bool: ...

    def nss_trusted(self, ca: Path) -> bool:
        """NSS 库（Chrome/Chromium 的信任库）里存的证书**内容**是否就是该 CA。

        注意：这是内容比对，不是“同名条目是否存在”—— Caddy 重新生成 CA 后
        旧条目仍在，只按 nickname 判定会误报“已信任”。
        """

    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        """DNS 路由是否已配置。backend 取 "resolved" / "system" / "resolver"。

        suffix 是域名后缀：macOS 的解析器文件按后缀命名（/etc/resolver/<suffix>），
        Linux 的两个后端不关心它，但接口保持一致。
        """
