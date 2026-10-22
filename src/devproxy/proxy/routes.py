"""路由表：数据模型、持久化与 Caddyfile 渲染。

渲染是纯函数且确定性 —— 同一张表永远产出逐字节相同的文本。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

TABLE_VERSION = 1
RENDER_HEADER = "# 由 devproxy 自动生成 —— 请勿手工编辑\n"


class RouteTableError(RuntimeError):
    """路由表文件损坏或不合法。"""


# 站点地址与上游都要拼进 Caddyfile 文本 —— 只放行严格白名单，
# 大括号/换行/引号这类元字符一律拒绝（不是「转义后放行」：
# Caddyfile 没有可靠的通用转义，唯一的正解是不让它们进来）
HOSTNAME_RE = re.compile(
    r"\A[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\Z"
)
HOSTPORT_RE = re.compile(r"\A[a-z0-9.-]+:\d{1,5}\Z")
MAX_HOSTNAME_LEN = 253


@dataclass
class Route:
    """一条路由 = 一个精确站点块。"""

    name: str
    domain: str
    port: int
    mode: str  # "up" | "expose"
    upstream: str
    command: list[str] | None = None
    pid: int | None = None
    # pid 的启动时刻（/proc stat 第 22 字段 / ps lstart）—— pid 会被 OS
    # 复用，down 时用它确认要杀的还是当初那个进程。None = 无法核实
    proc_start: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "domain": self.domain,
            "port": self.port,
            "mode": self.mode,
            "upstream": self.upstream,
        }
        if self.command is not None:
            d["command"] = self.command
        if self.pid is not None:
            d["pid"] = self.pid
        if self.proc_start is not None:
            d["proc_start"] = self.proc_start
        if self.created_at:
            d["created_at"] = self.created_at
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Route":
        return cls(
            name=d["name"],
            domain=d["domain"],
            port=int(d["port"]),
            mode=d["mode"],
            upstream=d["upstream"],
            command=list(d["command"]) if d.get("command") is not None else None,
            pid=int(d["pid"]) if d.get("pid") is not None else None,
            proc_start=d.get("proc_start"),
            created_at=d.get("created_at", ""),
        )


@dataclass
class RouteTable:
    version: int = TABLE_VERSION
    domain_suffix: str = "test"
    routes: list[Route] = field(default_factory=list)

    def get(self, name: str) -> Route | None:
        return next((r for r in self.routes if r.name == name), None)

    def upsert(self, route: Route) -> None:
        """同名就地替换。"""
        for i, r in enumerate(self.routes):
            if r.name == route.name:
                self.routes[i] = route
                return
        self.routes.append(route)

    def remove(self, name: str) -> bool:
        before = len(self.routes)
        self.routes = [r for r in self.routes if r.name != name]
        return len(self.routes) < before

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "domain_suffix": self.domain_suffix,
            "routes": [r.to_dict() for r in self.routes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RouteTable":
        return cls(
            version=int(d.get("version", TABLE_VERSION)),
            domain_suffix=d.get("domain_suffix", "test"),
            routes=[Route.from_dict(r) for r in d.get("routes", [])],
        )


def atomic_write(path: Path, content: str) -> None:
    """先写临时文件再 os.replace —— 避免读到半截内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def load_table(path: Path) -> RouteTable:
    if not path.exists():
        return RouteTable()
    try:
        return RouteTable.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError) as e:
        # routes.json 是工具自己的状态文件，但手工编辑 / 版本跨越 / 写一半
        # 都是现实场景 —— 必须映射成领域错误，不能让 KeyError 裸奔到终端
        raise RouteTableError(f"路由表损坏: {path} — {e}") from e


def save_table(path: Path, table: RouteTable) -> None:
    atomic_write(path, json.dumps(table.to_dict(), ensure_ascii=False, indent=2) + "\n")


def check_renderable(route: Route) -> None:
    """渲染前的最后一道闸：domain 必须是主机名，upstream 必须是 host:port。

    routes.json 是「真相」，但真相也可能是手工编辑过的 —— 渲染器不能把
    任意文本直接灌进 Caddyfile 这种配置语言里。
    """
    domain = route.domain
    if (
        not isinstance(domain, str)
        or len(domain) > MAX_HOSTNAME_LEN
        or not HOSTNAME_RE.match(domain)
    ):
        raise RouteTableError(
            f"路由 {route.name!r} 的 domain {domain!r} 不是合法主机名"
            "（只允许小写字母、数字、连字符与点）"
        )
    upstream = route.upstream
    if not isinstance(upstream, str) or not HOSTPORT_RE.match(upstream):
        raise RouteTableError(
            f"路由 {route.name!r} 的 upstream {upstream!r} 必须形如 host:port"
        )
    if not 1 <= int(upstream.rpartition(":")[2]) <= 65535:
        raise RouteTableError(
            f"路由 {route.name!r} 的 upstream {upstream!r} 端口超出 1..65535"
        )


def render_caddyfile(table: RouteTable) -> str:
    """渲染成 Caddyfile 片段：一个路由 = 一个精确站点块。

    刻意不用通配块与 on-demand —— 精确块让未注册域名拿不到证书。
    """
    for route in table.routes:
        check_renderable(route)
    blocks = [
        f"{r.domain} {{\n\ttls internal\n\treverse_proxy {r.upstream}\n}}"
        for r in table.routes
    ]
    if not blocks:
        return RENDER_HEADER
    return RENDER_HEADER + "\n" + "\n\n".join(blocks) + "\n"
