from __future__ import annotations

import time

import typer

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_PRECONDITION
from devproxy.cli._shared import EXIT_USAGE
from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import domain_for
from devproxy.gateway.names import validate_name
from devproxy.gateway.routes import Route
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.runner import reachable
from devproxy.gateway.store import RouteStoreError


@app.command()
def expose(
    name: str = typer.Argument(..., help="路由名"),
    port: int = typer.Option(..., "--port", help="已在上游监听的端口"),
) -> None:
    """给已在运行的服务开一个域名。注册后立即返回，路由常驻。"""
    try:
        validate_name(name)
    except InvalidNameError as e:
        s.fail(str(e), EXIT_USAGE)
    if not (1 <= port <= 65535):
        s.fail(f"非法端口 {port}：必须在 1..65535 之间", EXIT_USAGE)

    paths, config, store, admin = s.build_context()
    s._require_caddyfile(paths)
    # expose 注册的是常驻活路由，reload 必须真的发生 —— Caddy 未运行属于
    # 前置条件不满足（退出码 3），而不是写表失败的运行错误（退出码 1）
    if not admin.is_alive():
        s.fail(
            "Caddy 未运行（Admin API 不可达），请先执行 devproxy setup 或启动 Caddy",
            EXIT_PRECONDITION,
        )
    domain = domain_for(name, config.domain_suffix)
    route = Route(
        name=name,
        domain=domain,
        port=port,
        mode="expose",
        upstream=f"127.0.0.1:{port}",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    try:
        store.mutate(lambda t: t.upsert(route))
    except (RouteStoreError, RouteTableError) as e:
        s.fail(str(e))
    if not reachable(port):
        s.err_console.print(f"⚠️  上游 127.0.0.1:{port} 当前无响应（路由已注册，稍后可访问）")
    s.console.print(f"✅ https://{domain}  (上游 127.0.0.1:{port})")
    s.console.print(f"   移除: devproxy down {name}")
    s._print_trust_hint_if_needed(paths, config)
