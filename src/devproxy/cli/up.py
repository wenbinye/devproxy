from __future__ import annotations

import typer

from devproxy.cli import app
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_PRECONDITION
from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.runner import PreconditionError
from devproxy.gateway.runner import UpError
from devproxy.gateway.store import RouteStoreError

from devproxy.cli import _shared as s

@app.command(context_settings={"ignore_unknown_options": True})
def up(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="路由名（\\[a-z0-9-]，单标签）"),
    port: int = typer.Option(None, "--port", help="显式端口（默认自动分配）"),
    ready_timeout: int = typer.Option(None, "--ready-timeout", help="就绪超时秒数"),
) -> None:
    """分配端口、启动命令并注册 https://<name>.test。"""
    data = ctx.ensure_object(dict)
    if not data.get("has_sep"):
        s.err_console.print("缺少 '--'：devproxy up <name> [选项] -- CMD...")
        raise typer.Exit(EXIT_FAIL)
    cmd = list(data.get("trailing") or [])
    if not cmd:
        s.err_console.print("缺少启动命令：-- 之后需要 CMD...")
        raise typer.Exit(EXIT_FAIL)

    paths, config, store, admin = s.build_context()
    s._require_caddyfile(paths)
    runner = s.UpRunner(store, config, admin)

    def on_ready(domain: str, allocated: int) -> None:
        s.console.print(f"✅ https://{domain}  (端口 {allocated})")
        s._print_trust_hint_if_needed(paths, config)

    try:
        rc = runner.run(
            name,
            cmd,
            port=port,
            ready_timeout=ready_timeout,
            on_ready=on_ready,
        )
    except PreconditionError as e:
        # Caddy 未运行是前置条件不满足，不是运行失败 —— 退出码 3（§9.1）
        s.fail(str(e), EXIT_PRECONDITION)
    except (UpError, RouteStoreError, RouteTableError, InvalidNameError) as e:
        s.fail(str(e))
    raise typer.Exit(rc)
