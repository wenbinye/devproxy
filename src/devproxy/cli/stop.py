"""The stop command: stop every app declared in this project (devproxy.toml).

The inverse of `devproxy up`: whatever up started, one command stops.
Only routes declared by this file are touched -- manually started routes and routes
belonging to other projects are left alone.
"""

from __future__ import annotations

import typer

from devproxy.cli import _shared as s
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_USAGE
from devproxy.gateway.project import ProjectFileError
from devproxy.gateway.project import load_project_file
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.store import RouteStoreError


def stop_project_apps() -> None:
    """Stop every app declared in this project (devproxy.toml). Restart with: devproxy up"""
    found = s.find_project_file()
    if found is None:
        s.fail(
            "Not inside a project (no devproxy.toml found). "
            "To stop a single route use devproxy down <name>",
            EXIT_USAGE,
        )
    try:
        project = load_project_file(found)
    except ProjectFileError as e:
        s.fail(str(e), EXIT_USAGE)

    declared = {route.name for route in project.routes}
    _, _, store, _ = s.build_context()

    stopped: list[tuple[str, int | None, str | None]] = []
    try:

        def _remove_declared(table) -> None:
            for route in list(table.routes):
                if route.name in declared:
                    stopped.append((route.name, route.pid, route.proc_start))
                    table.remove(route.name)

        store.mutate(_remove_declared)
    except (RouteStoreError, RouteTableError) as e:
        s.fail(str(e))

    if not stopped:
        s.console.print("No running apps.")
        raise typer.Exit(EXIT_OK)

    for name, pid, proc_start in stopped:
        if pid:
            # Pass the process identity recorded at registration time -- the pid may have
            # been reused by now, in which case the identity will not match and nothing is killed
            s.kill_pid_group(pid, proc_start)
        s.console.print(f"✔ Stopped {name}")

    s.console.print("")
    s.console.print(f"All stopped ({len(stopped)} apps). Restart with: devproxy up")
    raise typer.Exit(EXIT_OK)
