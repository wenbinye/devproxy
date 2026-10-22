"""用户级 dnsmasq 组件：配置增量合并 + systemd user 单元。

配置只做增量修改 —— 用户自己的 `~/.config/dnsmasq/dnsmasq.conf` 里可能有
`.develop` 之类的域名条目、`no-resolv`、上游 server、`filter-AAAA`，工具不得
重写整个文件，只保证 `port=` 与本工具域名的 `address=` 条目存在。
"""

from __future__ import annotations

from pathlib import Path

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Action, Platform, Status

DNSMASQ_CONF = Path("~/.config/dnsmasq/dnsmasq.conf")


def merge_dnsmasq_config(existing: str, *, port: int, suffix: str) -> str:
    """增量修改 dnsmasq 配置：确保 port 与 address 条目存在，其余原样保留。

    绝不重写整个文件 —— 用户可能有 .develop 等自己的域名条目。
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
        self.conf_path = conf_path or (
            Path.home() / ".config" / "dnsmasq" / "dnsmasq.conf"
        )

    # -- check ---------------------------------------------------------
    def check(self) -> Status:
        if not self.platform.service_active("dnsmasq"):
            return Status(
                ok=False,
                detail="dnsmasq 服务未运行",
                hint="systemctl --user start dnsmasq",
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
        existing = ""
        if self.conf_path.exists():
            existing = self.conf_path.read_text(encoding="utf-8")
        merged = merge_dnsmasq_config(
            existing, port=self.platform.dns_port, suffix=self.config.domain_suffix
        )
        return [
            Action(
                description="合并 dnsmasq 配置（保留既有域名条目）",
                kind="write",
                path=self.conf_path,
                content=merged,
            ),
            Action(
                description="安装并启动 dnsmasq.service（systemd user）",
                kind="run",
                command=["install-service", "dnsmasq"],
            ),
        ]

    # -- apply ---------------------------------------------------------
    def apply(self, actions: list[Action]) -> None:
        for action in actions:
            if action.kind == "write" and action.path is not None:
                action.path.parent.mkdir(parents=True, exist_ok=True)
                action.path.write_text(action.content, encoding="utf-8")
                continue
            if action.command[:1] == ["install-service"]:
                unit = (
                    "[Unit]\n"
                    "Description=dnsmasq for devproxy (user scope)\n"
                    "After=network.target\n"
                    "\n"
                    "[Service]\n"
                    "Type=simple\n"
                    "ExecStart=/usr/sbin/dnsmasq --conf-dir=%h/.config/dnsmasq "
                    "--pid-file=%h/.cache/dnsmasq.pid\n"
                    "Restart=on-failure\n"
                    "RestartSec=2s\n"
                    "\n"
                    "[Install]\n"
                    "WantedBy=default.target\n"
                )
                self.platform.install_service("dnsmasq", unit)
                self.platform.enable_service("dnsmasq")
                continue
            raise RuntimeError(f"未知动作: {action.command}")

    # -- verify --------------------------------------------------------
    def verify(self) -> Status:
        if not self.platform.service_active("dnsmasq"):
            return Status(
                ok=False,
                detail="dnsmasq 服务未处于 active",
                hint="systemctl --user status dnsmasq",
            )
        return Status(ok=True, detail="dnsmasq 运行中")
