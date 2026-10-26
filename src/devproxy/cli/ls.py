from __future__ import annotations

import typer
from rich.markup import escape
from rich.table import Table

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_OK
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.runner import reachable


def _list_routes() -> None:
    """List every registered route."""
    _, _, store, _ = s.build_context()
    try:
        routes = store.list()
    except RouteTableError as e:
        s.fail(str(e))
    if not routes:
        s.console.print("(no routes)")
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
            "● reachable" if ok else "⛔ unreachable",
        )
    s.console.print(table)


@app.command(name="ls", help="List every registered route (= ps)")
def ls() -> None:
    _list_routes()


@app.command(name="ps", help="List every registered route (= ls, named after docker compose)")
def ps() -> None:
    _list_routes()
