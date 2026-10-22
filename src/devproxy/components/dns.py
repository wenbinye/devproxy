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
from devproxy.platform.base import (
    RESOLVER_DIR,
    Action,
    Platform,
    Status,
    render_resolver_file,
)

RESOLVED_CONF_DIR = Path("/etc/systemd/resolved.conf.d")
RESOLVED_CONF = RESOLVED_CONF_DIR / "devproxy.conf"
SYSTEM_DNSMASQ_CONF = Path("/etc/dnsmasq.d/devproxy.conf")

# resolved 的整机兜底解析器。披露文案与真正写进配置的必须是同一份 ——
# 两处各写一遍就会漂移，用户看到的和实际生效的就对不上了。
FALLBACK_DNS = "1.1.1.1 8.8.8.8"

# 记录上次写入的路由内容哈希 —— /etc 下的文件普通用户未必读得到，
# 靠自家 state 目录里的哈希判断“是否已就绪”，让 setup 第二次跑成 no-op。
STATE_FILE = "dns-route.json"


def global_scope_disclosure(port: int) -> str:
    """resolved 后端的整机作用域披露（多行短句，便于终端阅读）。

    `DNS=` 写在 drop-in 的全局段里 —— 它决定的是**这台机器全部域名**的解析
    都先问用户级的 dnsmasq，不只是 `*.test`。`FallbackDNS=` 则是整机兜底：
    dnsmasq 起不来时，全机解析退化为公共 DNS。

    这两件事必须写进 setup 的变更清单：用户点确认之前就该知道自己在改什么。
    """
    return (
        "⚠ 整机作用域（这台机器全部域名，不只是 *.test）\n"
        f"  · DNS=127.0.0.1:{port}：全机解析先问用户级 dnsmasq\n"
        f"  · FallbackDNS={FALLBACK_DNS}：整机兜底\n"
        "    dnsmasq 起不来时，全机解析退化为公共 DNS"
    )


def render_resolved_conf(*, port: int, domains: list[str]) -> str:
    suffix = ",".join(f"~.{d.lstrip('.')}" for d in domains)
    return (
        "[Resolve]\n"
        f"DNS=127.0.0.1:{port}\n"
        f"FallbackDNS={FALLBACK_DNS}\n"
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

        macOS 根本没有 systemd-resolved —— 恒为 False，后端恒为 resolver。
        """
        if self.platform.name == "mac":
            return False
        if self.backend == "system":
            return False
        if self.backend == "resolved":
            return True
        return self.platform.resolved_active()

    @property
    def target_path(self) -> Path:
        """本次要写/要查的 DNS 路由文件。

        macOS 是每域解析器文件（/etc/resolver/<后缀>）—— 不动 /etc/resolv.conf。
        """
        if self.platform.name == "mac":
            return RESOLVER_DIR / self.config.domain_suffix.lstrip(".")
        return RESOLVED_CONF if self.resolved_backend else SYSTEM_DNSMASQ_CONF

    def _backend_key(self) -> str:
        """本次要写/要查的 DNS 路由后端。

        macOS 没有 systemd-resolved，用 launchd 之外的 resolver 机制
        （/etc/resolver/<suffix>），后端名是 "resolver"。
        """
        if self.platform.name == "mac":
            return "resolver"
        return "resolved" if self.resolved_backend else "system"

    # -- 披露 ------------------------------------------------------------
    def global_scope_note(self) -> str | None:
        """整机作用域的披露文案；不涉及整机时返回 None。

        只有 resolved 后端有整机作用域：`DNS=` 与 `FallbackDNS=` 写在 drop-in 的
        全局段里，改的是**这台机器**的解析路径。macOS 的每域解析器文件与
        `system` 后端的 dnsmasq 片段都只影响本后缀，不涉及整机。

        setup 会把这段话单独打一遍 —— 变更清单的表格里放不下，也放不下重点。
        """
        if not self.resolved_backend:
            return None
        return global_scope_disclosure(self.platform.dns_port)

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
        if self.platform.name == "mac":
            # macOS 的每域解析器文件：mDNSResponder 会自己重载，
            # 不需要（也不能）重启任何解析服务。
            content = render_resolver_file(self.platform.dns_port)
            desc = "写入 %s（macOS 每域解析器）" % self.target_path
            if self._route_in_place(content):
                return []
            return [
                Action(
                    description=desc,
                    kind="write",
                    path=self.target_path,
                    content=content,
                    privileged=True,
                )
            ]
        if self.resolved_backend:
            content = render_resolved_conf(
                port=self.platform.dns_port, domains=[self.config.domain_suffix]
            )
            desc = "写入 systemd-resolved 路由（*.%s → 127.0.0.1，整机 DNS 作用域）" % (
                self.config.domain_suffix,
            )
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
        mac = self.platform.name == "mac"
        if not self.platform.dns_route_installed(key, self.config.domain_suffix):
            return Status(
                ok=False,
                detail="DNS 路由未生效",
                hint=(
                    f"确认 {self.target_path} 存在且内容正确"
                    if mac
                    else "systemctl restart systemd-resolved"
                ),
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
                hint=(
                    f"dscacheutil -q host -a name {name}"
                    if mac
                    else "systemctl restart systemd-resolved；resolvectl query " + name
                ),
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
