"""The uninstall command: only output and exit codes; planning and execution live in
lifecycle.uninstall."""

from __future__ import annotations

import typer

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_DECLINED
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_OK


@app.command()
def uninstall(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Only print the steps; change nothing and need no sudo"
    ),
    yes: bool = typer.Option(False, "--yes", help="Non-interactive mode; do not ask"),
    purge: bool = typer.Option(
        False,
        "--purge",
        help="Also delete the Caddy storage, backups, and the downloaded caddy binary "
        "(asked interactively when omitted)",
    ),
    remove_ca: bool = typer.Option(
        False,
        "--remove-ca",
        help="Also remove the CA (asked interactively when omitted; migrated CAs are kept by default)",
    ),
) -> None:
    """Uninstall devproxy: stop services, remove units, the DNS route, and (optionally) the CA.

    Your config, personal sites, Caddy storage, and backups are kept by default.
    """
    paths, config, _, _ = s.build_context()
    platform = s._platform()

    def _plan(purge_: bool, remove_ca_: bool):
        return s.build_uninstall_plan(
            platform,
            paths,
            config,
            purge=purge_,
            remove_ca=remove_ca_,
            dry_run=dry_run,
        )

    try:
        plan = _plan(purge, remove_ca)
    except Exception as e:  # noqa: BLE001 -- a failed plan must become a readable message + exit 1
        s.fail(str(e))

    s.render_uninstall_plan(plan, dry_run=dry_run)

    if plan.is_noop:
        s.console.print("Nothing to uninstall.")
        s.print_uninstall_kept(plan, dry_run=dry_run)
        raise typer.Exit(EXIT_OK)

    if dry_run:
        s.print_uninstall_kept(plan, dry_run=True)
        s.console.print("Preview finished: no changes were applied.")
        raise typer.Exit(EXIT_OK)

    interactive = s._interactive() and not yes
    if not interactive and not yes:
        s.err_console.print("Non-interactive environment without --yes; refusing to uninstall.")
        raise typer.Exit(EXIT_DECLINED)

    if interactive:
        # Ask about the two optional destructive parts one by one: skip when given on the
        # command line, ask when not -- so users need not memorize two rarely used flags
        # just to uninstall.
        chosen_purge = purge
        chosen_remove_ca = remove_ca
        if not purge:
            chosen_purge = s.typer.confirm(
                "Also delete the Caddy storage (CA and certificates), the migration backups, "
                "and the downloaded caddy binary? (irreversible)",
                default=False,
            )
        if not remove_ca and plan.ca_kept:
            chosen_remove_ca = s.typer.confirm(
                "Remove the CA from the system trust store? "
                "(it existed before this tool took over, so your sites may still rely on it)",
                default=False,
            )
        if chosen_purge != purge or chosen_remove_ca != remove_ca:
            plan = _plan(chosen_purge, chosen_remove_ca)
            s.console.print("")
            s.render_uninstall_plan(plan, dry_run=False)
        if not s.typer.confirm("About to run the uninstall steps above. Continue?", default=False):
            s.console.print("Cancelled: nothing was changed (no changes were applied).")
            raise typer.Exit(EXIT_DECLINED)

    results = s.apply_uninstall_plan(plan)
    ok = s.print_uninstall_results(results)
    s.print_uninstall_kept(plan)
    if not ok:
        raise typer.Exit(EXIT_FAIL)
    s.console.print("Uninstall complete.")
    raise typer.Exit(EXIT_OK)
