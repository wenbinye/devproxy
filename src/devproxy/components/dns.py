"""DNS 路由组件：把 *.test 指向本机。

两个后端：
- resolved（Linux 默认）：写 /etc/systemd/resolved.conf.d/devproxy.conf
- system：写 /etc/dnsmasq.d/devproxy.conf（需自行处理 53 端口冲突）
"""

from __future__ import annotations

from pathlib import Path

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Action, Platform, Status

RESOLVED_CONF_DIR = Path("/etc/systemd/resolved.conf.d")
RESOLVED_CONF = RESOLVED_CONF_DIR / "devproxy.conf"
SYSTEM_DNSMASQ_CONF = Path("/etc/dnsmasq.d/devproxy.conf")


def render_resolved_conf(*, port: int, domains: list[str]) -> str:
    suffix = ",".join(f"~.{d.lstrip('.')}" for d in domains)
    return (
        "[Resolve]\n"
        f"DNS=127.0.0.1:{port}\n"
        "FallbackDNS=1.1.1.1 8.8.8.8\n"
        f"Domains={suffix}\n"
    )


def render_system_dnsmasq_conf(*, suffix: str) -> str:
    s = suffix.lstrip(".")
    return f"address=/.{s}/127.0.0.1\naddress=/.{s}/::1\n"


class DnsComponent:
    name = "dns"

    def __init__(
        self, platform: Platform, paths: Paths, config: Config, backend: str = "auto"
    ):
        self.platform = platform
        self.paths = paths
        self.config = config
        self.backend = backend

    @property
    def resolved_backend(self) -> bool:
        """auto 时优先 resolved（不占 53，用户态守护）。"""
        if self.backend == "system":
            return False
        if self.backend == "resolved":
            return True
        return RESOLVED_CONF_DIR.is_dir()

    @property
    def target_path(self) -> Path:
        return RESOLVED_CONF if self.resolved_backend else SYSTEM_DNSMASQ_CONF

    # -- check ---------------------------------------------------------
    def check(self) -> Status:
        key = "resolved" if self.resolved_backend else "system"
        if self.platform.dns_route_installed(key):
            return Status(ok=True, detail=f"DNS 路由已配置（{key} 后端）")
        return Status(
            ok=False,
            detail=f"DNS 路由缺失（{key} 后端）：{self.target_path}",
            hint="devproxy setup 会写入（需要 sudo）",
        )

    # -- plan ----------------------------------------------------------
    def plan(self) -> list[Action]:
        if self.resolved_backend:
            content = render_resolved_conf(
                port=self.platform.dns_port, domains=[self.config.domain_suffix]
            )
            desc = "写入 systemd-resolved 路由（*.%s → 127.0.0.1）" % self.config.domain_suffix
            reload_action = Action(
                description="重载 systemd-resolved 使路由生效",
                kind="run",
                command=["systemctl", "restart", "systemd-resolved"],
                privileged=True,
            )
        else:
            content = render_system_dnsmasq_conf(suffix=self.config.domain_suffix)
            desc = "写入系统 dnsmasq 路由（*.%s → 127.0.0.1）" % self.config.domain_suffix
            reload_action = Action(
                description="重载系统 dnsmasq 使路由生效",
                kind="run",
                command=["systemctl", "restart", "dnsmasq"],
                privileged=True,
            )
        return [
            Action(description=desc, kind="write", path=self.target_path, content=content, privileged=True),
            reload_action,
        ]

    # -- apply ---------------------------------------------------------
    def apply(self, actions: list[Action]) -> None:
        elevated = self.platform.privilege()
        for action in actions:
            if action.kind == "write" and action.path is not None:
                elevated.write(action.path, action.content)
                continue
            if action.privileged:
                elevated.run(action.command)
                continue
            raise RuntimeError(f"未知动作: {action.command}")

    # -- verify --------------------------------------------------------
    def verify(self) -> Status:
        key = "resolved" if self.resolved_backend else "system"
        if not self.platform.dns_route_installed(key):
            return Status(
                ok=False,
                detail="DNS 路由未生效",
                hint="systemctl restart systemd-resolved",
            )
        return Status(ok=True, detail="DNS 路由已配置")
