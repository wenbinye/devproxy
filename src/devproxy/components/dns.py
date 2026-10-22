"""DNS 路由组件：把 *.test 指向本机。

两个后端：
- resolved（Linux 默认）：写 /etc/systemd/resolved.conf.d/devproxy.conf
- system：写 /etc/dnsmasq.d/devproxy.conf（需自行处理 53 端口冲突）
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Action, Platform, Status

RESOLVED_CONF_DIR = Path("/etc/systemd/resolved.conf.d")
RESOLVED_CONF = RESOLVED_CONF_DIR / "devproxy.conf"
SYSTEM_DNSMASQ_CONF = Path("/etc/dnsmasq.d/devproxy.conf")

# 记录上次写入的路由内容哈希 —— /etc 下的文件普通用户未必读得到，
# 靠自家 state 目录里的哈希判断“是否已就绪”，让 setup 第二次跑成 no-op。
STATE_FILE = "dns-route.json"


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


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        """auto 时优先 resolved（不占 53，用户态守护）。

        判据必须是 resolved **真的在运行**，而不是目录存在 ——
        /etc/systemd/resolved.conf.d 可能只是被手工创建过；反过来干净机器
        上目录不存在也不代表 resolved 没装。目录存在与否都不是可靠信号。
        """
        if self.backend == "system":
            return False
        if self.backend == "resolved":
            return True
        return self.platform.resolved_active()

    @property
    def target_path(self) -> Path:
        return RESOLVED_CONF if self.resolved_backend else SYSTEM_DNSMASQ_CONF

    def _backend_key(self) -> str:
        """本次要写/要查的 DNS 路由后端。

        macOS 没有 systemd-resolved，用 launchd 之外的 resolver 机制
        （/etc/resolver/<suffix>），后端名是 "resolver"。
        """
        if self.platform.name == "mac":
            return "resolver"
        return "resolved" if self.resolved_backend else "system"

    # -- check ---------------------------------------------------------
    def check(self) -> Status:
        key = self._backend_key()
        if self.platform.dns_route_installed(key, self.config.domain_suffix):
            return Status(ok=True, detail=f"DNS 路由已配置（{key} 后端）")
        return Status(
            ok=False,
            detail=f"DNS 路由缺失（{key} 后端）：{self.target_path}",
            hint="devproxy setup 会写入（需要 sudo）",
        )

    # -- plan ----------------------------------------------------------
    def plan(self) -> list[Action]:
        """路由已是预期内容时返回空列表 —— 重复 setup 不该再要 sudo、
        更不该再重启系统 resolver（那会造成全机 DNS 抖动）。"""
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
        if self._route_in_place(content):
            return []
        return [
            Action(description=desc, kind="write", path=self.target_path, content=content, privileged=True),
            reload_action,
        ]

    # -- apply ---------------------------------------------------------
    def apply(self, actions: list[Action]) -> None:
        """提权会话按需获取 —— 没有特权动作时绝不触发 sudo 密码提示。

        状态只在**全部动作成功后**记录：重载失败时若先记状态，下次 plan 会
        返回空列表，把“未生效”误报成“已就绪”。
        """
        elevated = None
        written: tuple[Path, str] | None = None
        for action in actions:
            if action.kind == "write" and action.path is not None:
                if action.privileged:
                    if elevated is None:
                        elevated = self.platform.privilege()
                    elevated.write(action.path, action.content)
                else:
                    action.path.parent.mkdir(parents=True, exist_ok=True)
                    action.path.write_text(action.content, encoding="utf-8")
                if action.path == self.target_path:
                    written = (action.path, action.content)
                continue
            if action.privileged:
                if elevated is None:
                    elevated = self.platform.privilege()
                code, msg = elevated.run(action.command)
                if code != 0:
                    raise RuntimeError(f"执行 {' '.join(action.command)} 失败: {msg}")
                continue
            raise RuntimeError(f"未知动作: {action.command}")
        if written is not None:
            self._record_state(*written)

    # -- verify --------------------------------------------------------
    def verify(self) -> Status:
        """路由文件在位还不够 —— 必须真的能经系统解析器解析到本机。"""
        key = self._backend_key()
        if not self.platform.dns_route_installed(key, self.config.domain_suffix):
            return Status(
                ok=False,
                detail="DNS 路由未生效",
                hint="systemctl restart systemd-resolved",
            )
        name = f"foo.{self.config.domain_suffix}"
        answer = self.platform.dns_query(name)
        if answer not in ("127.0.0.1", "::1"):
            return Status(
                ok=False,
                detail=(
                    f"系统解析器未把 {name} 指到本机（"
                    f"{answer or '无响应'}）；路由文件已写但未生效"
                ),
                hint="systemctl restart systemd-resolved；resolvectl query " + name,
            )
        return Status(ok=True, detail=f"DNS 路由已生效（{name} → {answer}）")

    # -- 状态记录 ------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.paths.state_dir / STATE_FILE

    def _read_state(self) -> dict:
        path = self.state_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _record_state(self, path: Path, content: str) -> None:
        payload = {
            "backend": self._backend_key(),
            "path": str(path),
            "content_sha256": _sha256(content),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _route_in_place(self, content: str) -> bool:
        """路由是否已是预期内容。

        三重判据，缺一不可：
        1. 目标文件还在 —— 用户删掉后 setup 不能还说“已就绪”；
        2. 文件读得到时，直接比内容（用户在 /etc 下改过就重写）；
        3. 文件读不到时（普通用户未必能读 /etc 下的文件），退回 state 目录里
           上次写入的哈希。
        """
        if not self.target_path.exists():
            return False
        digest = _sha256(content)
        try:
            current = self.target_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = None
        if current is not None:
            return _sha256(current) == digest
        state = self._read_state()
        return (
            state.get("content_sha256") == digest
            and state.get("path") == str(self.target_path)
        )
