"""The sync command: reconcile routes against the devproxy.toml at the project root.

Only presentation and exit codes live here -- planning and execution are in
`gateway.sync`, file parsing is in `gateway.project`.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.markup import escape

from devproxy.cli import _shared as s
from devproxy.cli._shared import EXIT_DECLINED
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_PRECONDITION
from devproxy.cli._shared import EXIT_USAGE
from devproxy.gateway.project import ProjectFileError
from devproxy.gateway.project import load_project_file
from devproxy.gateway.project import resolve_project_file
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.store import RouteStoreError

_STATUS_MARKS = {
    "created": "✅ created",
    "skipped": "➖ skipped",
    "conflict": "⚠️  conflict",
    "failed": "✗ failed",
    "pruned": "🧹 pruned",
}


def run_project_sync(*, file: Path | None = None, prune: bool = False, yes: bool = False) -> None:
    """Apply the route declarations from the project's devproxy.toml (the body of `devproxy up`).

    Called by cli/up.py when no route name is given -- compose's `up -d` semantics.
    """
    # 1) Locate and parse the project file -- missing or malformed is a usage error (exit 2)
    try:
        path = resolve_project_file(file)
        project = load_project_file(path)
    except ProjectFileError as e:
        s.fail(str(e), EXIT_USAGE)

    paths, config, store, admin = s.build_context()
    s._require_caddyfile(paths)
    # sync registers live routes with Caddy -- Caddy not running is an unmet precondition (3)
    if not admin.is_alive():
        s.fail(
            "Caddy is not running (Admin API unreachable); run devproxy setup or start Caddy first",
            EXIT_PRECONDITION,
        )
    try:
        existing = store.list()
    except RouteTableError as e:
        s.fail(str(e))

    plan = s.build_sync_plan(project, existing, prune)

    # 2) --prune deletes routes -- confirm first, act second (nothing has changed yet)
    if plan.prune:
        names = ", ".join(r.name for r in plan.prune)
        interactive = s._interactive() and not yes
        if not interactive and not yes:
            s.err_console.print(
                f"Non-interactive environment without --yes; refusing to run --prune "
                f"(would remove: {names})."
            )
            raise typer.Exit(EXIT_DECLINED)
        if interactive and not s.typer.confirm(
            f"About to remove routes deleted from this file: {names} "
            "(their child processes are stopped too). Continue?",
            default=False,
        ):
            s.console.print("Cancelled: nothing was changed.")
            raise typer.Exit(EXIT_DECLINED)

    s.console.print(f"Config file: {escape(str(project.path))}")
    s.console.print("")

    # 3) Execute
    try:
        outcomes = s.execute_sync_plan(project, plan, store=store, config=config, paths=paths)
    except (RouteStoreError, RouteTableError) as e:
        s.fail(str(e))

    failed = 0
    conflicts = 0
    created = 0
    skipped = 0
    pruned = 0
    for outcome in outcomes:
        mark = _STATUS_MARKS.get(outcome.status, outcome.status)
        line = f"{mark} {escape(outcome.name)}"
        if outcome.domain:
            line += f" → https://{escape(outcome.domain)}"
        if outcome.detail:
            line += f" -- {escape(outcome.detail)}"
        s.console.print(line)
        if outcome.log_path is not None and outcome.status == "created":
            s.console.print(f"   Log: {escape(str(outcome.log_path))}")
        if outcome.hint:
            s.console.print(f"   Fix: {escape(outcome.hint)}")
        if outcome.status == "created":
            created += 1
        elif outcome.status == "skipped":
            skipped += 1
        elif outcome.status == "conflict":
            conflicts += 1
        elif outcome.status == "failed":
            failed += 1
        elif outcome.status == "pruned":
            pruned += 1

    s.console.print("")
    if created:
        # The CA only exists once the first route is registered -- hint once, like up/expose
        s._print_trust_hint_if_needed(paths, config)
    if not outcomes:
        s.console.print("The file does not declare any routes.")
        raise typer.Exit(EXIT_OK)
    if created == 0 and skipped and not conflicts and not failed and not pruned:
        s.console.print(f"Everything is up to date ({skipped} declarations).")
        raise typer.Exit(EXIT_OK)
    s.console.print(
        f"Sync complete: created {created}, skipped {skipped}, conflicts {conflicts}, "
        f"failed {failed}, pruned {pruned}."
    )
    if conflicts:
        s.err_console.print(
            f"{conflicts} route(s) disagree with the file -- conflicts were reported and "
            "nothing was changed (this tool never kills and restarts processes on its own)."
        )
    if failed:
        s.err_console.print(f"{failed} route(s) failed to start -- see the errors above.")
        raise typer.Exit(EXIT_FAIL)
    if conflicts:
        raise typer.Exit(EXIT_FAIL)
    raise typer.Exit(EXIT_OK)
