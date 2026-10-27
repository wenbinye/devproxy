"""The init command: generate a devproxy.toml in the project.

Only interaction and exit codes live here -- generation is in `gateway.project`.
"""

from __future__ import annotations

from pathlib import Path

import typer

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_USAGE
from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import validate_name
from devproxy.gateway.project import MODE_UP
from devproxy.gateway.project import PROJECT_FILE_NAME
from devproxy.gateway.project import ProjectRoute
from devproxy.gateway.project import detect_project_root
from devproxy.gateway.project import render_project_file
from devproxy.gateway.project import route_name_from_dir
from devproxy.gateway.project import suggest_command
from devproxy.gateway.project import suggest_routes
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.routes import load_table


@app.command()
def init(
    name: str = typer.Option(
        None, "--name", help="Route name (defaults to the project directory name)"
    ),
    command: str = typer.Option(
        None,
        "--command",
        help='Start command, e.g. "npm run dev" (guessed from the project type if omitted)',
    ),
    from_route: str = typer.Option(
        None,
        "--from",
        help="Import the start command from an already-registered route of this name "
        "(the route table is global, so the name must be given)",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing devproxy.toml"),
) -> None:
    """Generate a devproxy.toml in this project."""
    root = detect_project_root()
    target = root / PROJECT_FILE_NAME
    if target.exists() and not force:
        s.fail(
            f"{target} already exists. Pass --force to overwrite "
            "(take a look at the current contents first)",
            EXIT_USAGE,
        )

    route_name = name or route_name_from_dir(root)
    try:
        validate_name(route_name)
    except InvalidNameError as e:
        s.fail(f"invalid route name {route_name!r}: {e}", EXIT_USAGE)

    if command or from_route:
        argv, notes = _resolve_command(root, command, from_route)
        routes = [ProjectRoute(name=route_name, mode=MODE_UP, command=argv)]
    else:
        routes, notes = suggest_routes(root)
        # An explicit --name wins over the guessed names only when there is
        # exactly one route; with several, the name is the *API's* prefix and
        # the web route is derived from it.
        if len(routes) == 1:
            routes = [ProjectRoute(name=route_name, mode=MODE_UP, command=routes[0].command)]
        else:
            routes[0] = ProjectRoute(name=route_name, mode=MODE_UP, command=routes[0].command)
            routes[1] = ProjectRoute(
                name=f"{route_name}-web", mode=MODE_UP, command=routes[1].command
            )

    content = render_project_file(routes, notes=notes)
    try:
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        s.fail(f"failed to write {target}: {e}", EXIT_FAIL)

    s.console.print(f"✔ Generated {target}")
    for route in routes:
        domain = f"{route.name}.<suffix>"
        s.console.print(f"  Route {route.name} → https://{domain}")
        if route.command:
            s.console.print(f"    command: {' '.join(route.command)}", soft_wrap=True)
        else:
            s.console.print("    command: (empty) -- fill in the real command")
    for note in notes:
        s.console.print(f"  {note}", soft_wrap=True)
    s.console.print("")
    s.console.print("Next steps:")
    s.console.print("  1. Check the commands above (placeholders must be replaced)")
    s.console.print("  2. devproxy setup    # once: installs Caddy, the DNS route, and CA trust")
    s.console.print("  3. devproxy up       # start everything this file declares")
    raise typer.Exit(EXIT_OK)


def _resolve_command(
    root: Path, command: str | None, from_route: str | None
) -> tuple[list[str], list[str]]:
    """Decide where the command comes from, plus the notes to write into the file.

    Priority: an explicit --command > --from (import an existing route) > guess from the
    project type.
    """
    if command:
        # --command is a line a human types -- split it with shell rules (quotes and
        # spaces are both honoured)
        import shlex

        try:
            argv = shlex.split(command)
        except ValueError as e:
            s.fail(f"failed to parse --command: {e}", EXIT_USAGE)
        if not argv:
            s.fail("--command must not be empty", EXIT_USAGE)
        return argv, []

    if from_route:
        imported, error = _import_command(from_route)
        if error:
            s.fail(error, EXIT_USAGE)
        return imported or [], [f"start command imported from the registered route {from_route}"]

    suggested, reason = suggest_command(root)
    if suggested is None:
        return [], [f"{reason}"]
    return suggested, [f"{reason} -- confirm this is the start command you want"]


def _import_command(route_name: str) -> tuple[list[str] | None, str]:
    """Read a route's start command out of the global route table.

    The route table is **global** (every project's routes live in one table), so this
    can only look a route up by exact name -- importing "all current routes" would drag
    other projects' routes into this file.
    """
    paths, _, _, _ = s.build_context()
    try:
        table = load_table(paths.routes_json)
    except RouteTableError as e:
        return None, str(e)
    route = table.get(route_name)
    if route is None:
        known = ", ".join(sorted(r.name for r in table.routes)) or "(the route table is empty)"
        return None, f"the route table has no {route_name!r}. Registered routes: {known}"
    if not route.command:
        return None, (
            f"route {route_name!r} has no start command (expose-mode routes only carry an upstream port)"
        )
    return list(route.command), ""
