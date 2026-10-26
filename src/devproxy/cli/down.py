from __future__ import annotations

import typer

from devproxy.cli import _completion
from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_USAGE
from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import validate_name
from devproxy.gateway.routes import RouteTable
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.store import RouteStoreError


@app.command()
def down(
    name: str = typer.Argument(
        None,
        help="Route name. Omit it to stop every app declared in this project's devproxy.toml",
        autocompletion=_completion.route_names,
    ),
) -> None:
    """Unregister a route, taking its child process with it. No name = stop the whole project."""
    if name is None:
        from devproxy.cli.stop import stop_project_apps

        stop_project_apps()
        raise typer.Exit(0)
    try:
        validate_name(name)
    except InvalidNameError as e:
        s.fail(str(e), EXIT_USAGE)
    _, _, store, _ = s.build_context()
    try:
        route = store.get(name)
    except RouteTableError as e:
        s.fail(str(e))
    if route is None:
        s.console.print(f"({name} is not registered)")
        raise typer.Exit(EXIT_OK)

    def _remove(table: RouteTable) -> None:
        table.remove(name)

    try:
        store.mutate(_remove)
    except (RouteStoreError, RouteTableError) as e:
        s.fail(str(e))
    if route.pid:
        # Pass the process identity recorded at registration time -- the pid may have
        # been reused by now, in which case the identity will not match and nothing is killed
        s.kill_pid_group(route.pid, route.proc_start)
    s.console.print(f"Unregistered {name}")
