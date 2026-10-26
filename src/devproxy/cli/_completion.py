"""shell 补全的候选来源。

Typer/Click 自带 `--install-completion` / `--show-completion`（bash / zsh / fish /
powershell），命令名与选项的补全不用自己写 —— 这里只补**值域**：
「参数该填什么」的候选（已注册的路由名、服务日志名）。

补全在每次按 <TAB> 时都会跑一遍，所以必须**又快又不能炸**：
路由表读不到（还没 setup、routes.json 损坏）就当没有候选。
绝不把报错打进 stdout —— bash 会把每一行输出都当成补全项。
"""

from __future__ import annotations

from collections.abc import Iterable

from click import Context

from devproxy.core.paths import default_paths
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.routes import load_table

# `devproxy logs` 除了路由名，还能跟服务日志
SERVICE_LOGS = ("caddy", "dnsmasq")


def _route_names() -> list[str]:
    """已注册的路由名（读不到路由表就当没有 —— 补全不该报错）。"""
    try:
        table = load_table(default_paths().routes_json)
    except (RouteTableError, OSError):
        return []
    return sorted(route.name for route in table.routes)


def _complete(candidates: Iterable[str], incomplete: str) -> list[str]:
    return [c for c in candidates if c.startswith(incomplete)]


def route_names(ctx: Context, args: list[str], incomplete: str) -> list[str]:
    """路由名候选（`down <名字>`、`logs <名字>`）。"""
    return _complete(_route_names(), incomplete)


def log_targets(ctx: Context, args: list[str], incomplete: str) -> list[str]:
    """`logs <名字>` 的候选：服务日志名 + 路由名。"""
    return _complete((*SERVICE_LOGS, *_route_names()), incomplete)
