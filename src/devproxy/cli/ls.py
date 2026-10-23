from __future__ import annotations

from rich.markup import escape
from rich.table import Table
import typer

from devproxy.cli import app
from devproxy.cli._shared import EXIT_OK
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.runner import reachable

from devproxy.cli import _shared as s

@app.command()
def ls() -> None:
    """列出所有已注册路由。"""
    _, _, store, _ = s.build_context()
    try:
        routes = store.list()
    except RouteTableError as e:
        s.fail(str(e))
    if not routes:
        s.console.print("（无路由）")
        raise typer.Exit(EXIT_OK)
    table = Table(show_header=True, header_style="bold")
    table.add_column("NAME")
    table.add_column("DOMAIN")
    table.add_column("PORT")
    table.add_column("MODE")
    table.add_column("UPSTREAM")
    table.add_column("STATUS")
    for r in routes:
        ok = reachable(r.port)
        table.add_row(
            escape(r.name),
            escape(r.domain),
            str(r.port),
            escape(r.mode),
            escape(r.upstream),
            "● reachable" if ok else "⛔ 不可达",
        )
    s.console.print(table)
