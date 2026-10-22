"""用户级 dnsmasq 组件：配置增量合并 + systemd user 单元。

配置只做增量修改 —— 用户自己的 `~/.config/dnsmasq/dnsmasq.conf` 里可能有
`.develop` 之类的域名条目、`no-resolv`、上游 server、`filter-AAAA`，工具不得
重写整个文件，只保证 `port=`、本工具域名的 `address=` 条目与（必要时）
上游 server 存在。
"""

from __future__ import annotations

from pathlib import Path

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Action, Platform, Status

DNSMASQ_CONF = Path("~/.config/dnsmasq/dnsmasq.conf")

# 无用户上游时补的公共 DNS。绝不能补 127.0.0.53（systemd-resolved 桩）——
# resolved 会被指回本机 dnsmasq，形成解析环路。
DEFAULT_UPSTREAMS = ("server=1.1.1.1", "server=8.8.8.8")


def merge_dnsmasq_config(existing: str, *, port: int, suffix: str) -> str:
    """增量修改 dnsmasq 配置：确保 port、address 与上游存在，其余原样保留。

    绝不重写整个文件 —— 用户可能有 .develop 等自己的域名条目。

    上游规则：配置里已有 `server=` 就原样保留（用户自己选的）；没有才补
    `no-resolv` + 公共 DNS。否则 dnsmasq 会拿 /etc/resolv.conf 当上游 ——
    systemd-resolved 机器上那是 127.0.0.53 桩，而 resolved 又被指向本机
    dnsmasq，解析请求会绕成死循环。
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
    has_server = any(line.strip().startswith("server=") for line in out)
    if not has_server:
        if "no-resolv" not in present:
            out.append("no-resolv")
            present.add("no-resolv")
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
                self.platform.install_service("dnsmasq", self._unit())
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

    # -- unit ----------------------------------------------------------
    def _conf_dir_spec(self) -> str:
        """单元里用 %h 表达家目录；配置目录不在家目录下时用绝对路径。"""
        conf_dir = self.conf_path.parent
        try:
            rel = conf_dir.relative_to(Path.home())
        except ValueError:
            return str(conf_dir)
        return "%h" if str(rel) == "." else f"%h/{rel}"

    def _unit(self) -> str:
        """systemd user 单元。

        dnsmasq 默认 fork 后父进程退出 —— Type=simple 会认为主进程立刻结束，
        单元随即变 inactive。必须 `--keep-in-foreground` 让 systemd 盯住真实
        进程。配合 `--keep-in-foreground` 不再需要 pid 文件（也顺带去掉对
        `~/.cache` 目录存在的依赖）。
        """
        return (
            "[Unit]\n"
            "Description=dnsmasq for devproxy (user scope)\n"
            "After=network.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            "ExecStart=/usr/sbin/dnsmasq --keep-in-foreground "
            f"--conf-dir={self._conf_dir_spec()}\n"
            "ExecReload=/bin/kill -HUP $MAINPID\n"
            "Restart=on-failure\n"
            "RestartSec=2s\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
