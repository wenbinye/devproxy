"""组件协议。check 永不抛异常 —— doctor 依赖它一次列出全部问题。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol
from typing import runtime_checkable

from devproxy.platform.base import Action
from devproxy.platform.base import Status


def _site_host(address: str) -> str:
    """站点地址 → 主机名（去掉 scheme 与端口）。

    Caddy 站点地址可以写成 `localhost`、`localhost:8443`、
    `https://localhost:8443` 或纯端口 `:443`。只有主机名部分才参与
    精确比较 —— 子串匹配会把 `foo.localhost.test` 误当成 localhost。
    """
    raw = address.strip()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    if raw.startswith("["):  # IPv6 字面量，如 [::1]:443
        end = raw.find("]")
        return (raw[1:end] if end != -1 else raw).lower()
    if ":" in raw:
        raw = raw.rsplit(":", 1)[0]
    return raw.lower()


def has_localhost_site(sites_caddy: Path) -> bool:
    """sites.caddy 里是否有 localhost 站点块（精确匹配站点地址 token）。

    只有 `*.test` 站点的机器上没有 localhost 站点 —— 那时探 localhost
    验的是"站点名匹配"而不是"服务活着"。
    """
    try:
        text = sites_caddy.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 站点地址写在 `{` 之前，多个地址用逗号或空白分隔。
        head = stripped.split("{", 1)[0]
        if any(_site_host(address) == "localhost" for address in head.replace(",", " ").split()):
            return True
    return False


def probe_url(routes_json: Path, sites_caddy: Path) -> str | None:
    """该拿哪个 URL 做端到端 HTTPS 探测；没有可探目标时返回 None。

    Caddy 与证书组件共用同一份判据（两处各写一遍已经漂移过一次）：
    优先首条已注册路由，其次**确有 localhost 站点时**才用 localhost。
    只有 codeup.test 这类站点的机器上，探 localhost 会因站点名不匹配而
    握手失败，把健康机器判成故障。
    """
    try:
        state = json.loads(routes_json.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        state = {}
    routes = state.get("routes") if isinstance(state, dict) else None
    for route in routes or []:
        domain = route.get("domain") if isinstance(route, dict) else None
        if isinstance(domain, str) and domain:
            return f"https://{domain}"
    if has_localhost_site(sites_caddy):
        return "https://localhost"
    return None


@runtime_checkable
class Component(Protocol):
    name: str

    def check(self) -> Status:
        """检查现状。永不抛异常。"""

    def plan(self) -> list[Action]:
        """根据当前状态算出要执行的动作。空列表表示无需变更。"""

    def apply(self, actions: list[Action]) -> None:
        """执行动作。"""
