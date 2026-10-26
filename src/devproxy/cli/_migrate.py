"""Command-layer orchestration for legacy migration and rollback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer
from rich.markup import escape

from devproxy.cli import _shared as s
from devproxy.cli._shared import EXIT_DECLINED
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_SUDO
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.lifecycle.migrate import LegacyState
from devproxy.lifecycle.migrate import MigrationError
from devproxy.lifecycle.migrate import MigrationOutcome
from devproxy.lifecycle.migrate import MigrationRequest
from devproxy.lifecycle.migrate import rollback as do_rollback
from devproxy.lifecycle.migrate import run_migration
from devproxy.platform.base import Platform


@dataclass
class MigrateRequest:
    """The inputs of one legacy-environment migration.

    The arguments are collected in a request object so call sites need not remember a
    long positional/keyword list; `backend` is already folded into `config.dns_backend`,
    so the migration and the components see the same value.
    """

    platform: Platform
    paths: Paths
    config: Config
    state: LegacyState
    yes: bool = False


def _migrate_legacy(req: MigrateRequest) -> MigrationOutcome:
    """Take the legacy caddy service under this tool's management.

    Migration rewrites the user's unit and Caddyfile -- a non-interactive environment
    must pass --yes explicitly. Returns the outcome: the caller must continue the
    component flow with `outcome.config` (it already carries the legacy Caddyfile's
    storage).
    """
    s.console.print("[yellow]Found a legacy caddy service (not managed by this tool)[/yellow]")
    s.console.print(f"  Unit: {req.state.unit_path}")
    s.console.print(f"  Config: {req.state.config_path}")
    s.console.print(f"  Environment: {req.state.env or 'none'}")
    s.console.print(
        "Will do: back up -> split personal sites into sites.caddy -> rewrite the unit -> restart"
    )
    interactive = s._interactive()
    if not req.yes and not interactive:
        s.err_console.print("Non-interactive environment without --yes; refusing to migrate.")
        raise typer.Exit(EXIT_SUDO)
    if not req.yes and interactive:
        if not typer.confirm(
            "About to migrate the legacy environment above (this rewrites the unit and the "
            "Caddyfile). Continue?",
            default=True,
        ):
            s.console.print("Cancelled: nothing was changed (the migration never started).")
            raise typer.Exit(EXIT_DECLINED)
    try:
        outcome = run_migration(
            MigrationRequest(
                platform=req.platform,
                paths=req.paths,
                config=req.config,
                state=req.state,
                dnsmasq_conf=s.DNSMASQ_CONF.expanduser(),
                dns_route=s.default_dns_route(req.platform, req.paths, req.config),
            )
        )
    except MigrationError as e:
        # Print the notes on the failure path too: the user needs to know which blocks
        # were lost and whether sites.caddy was overwritten
        s._print_notes(e.notes)
        s.fail(str(e), EXIT_FAIL)
    s._print_notes(outcome.notes)
    s.console.print(f"Backed up to: {outcome.backup.directory}")
    s.console.print(f"Rollback command: devproxy setup --rollback {outcome.backup.directory}")
    return outcome


def _caddy_unit_exists(platform) -> bool:
    """Whether a caddy service unit still exists after a rollback.

    No unit means the machine never had this service before the migration, so there is
    nothing to "restart". When it cannot be determined (the platform probe raised),
    treat it as existing -- better to attempt a restart and report the failure honestly
    than to skip silently.
    """
    try:
        return platform.service_unit_path("caddy").exists()
    except (OSError, RuntimeError):
        return True


def _restart_caddy_after_rollback(platform, restored_count: int) -> None:
    """Restart Caddy so the process actually runs the unit that was just restored.

    Two failures are distinguished:
    - the service never existed (no unit): ignorable;
    - the restart failed: the files are restored, but the process may still run the
      post-migration config -- this must be reported, and it must be stated plainly that
      the rollback itself completed, not present it as a failed rollback.
    """
    if not _caddy_unit_exists(platform):
        s.console.print(
            "(there was no caddy service unit before the migration; skipping the restart)"
        )
        return
    try:
        platform.restart_service("caddy")
    except PermissionError as e:
        s.err_console.print(
            f"devproxy: restored {restored_count} file(s), but restarting caddy needs "
            f"elevation and failed: {escape(str(e))}"
        )
        s.err_console.print(
            "   Caddy may still be running the post-migration config. Fix: rerun "
            "devproxy setup --rollback <backup dir> as a normal user, or restart caddy manually."
        )
        raise typer.Exit(EXIT_SUDO) from e
    except Exception as e:  # noqa: BLE001 -- a failed restart must be reported, never swallowed
        s.err_console.print(
            f"devproxy: restored {restored_count} file(s), but restarting caddy failed: "
            f"{escape(str(e))}"
        )
        s.err_console.print(
            "   Caddy may still be running the post-migration config (or may be stopped). "
            "Fix: run systemctl --user restart caddy (macOS: launchctl kickstart -k) "
            "manually, or check whether the restored unit file is valid."
        )
        raise typer.Exit(EXIT_FAIL) from e


def _do_rollback(directory: Path) -> None:
    """Roll back from the given backup directory."""
    from devproxy.core.backup import BackupRestoreError
    from devproxy.core.backup import load_backup

    try:
        backup = load_backup(directory)
    except (OSError, ValueError) as e:
        s.fail(f"cannot read the backup directory {directory}: {e}", EXIT_FAIL)
    platform = s._platform()
    try:
        touched = do_rollback(backup, platform=platform)
    except BackupRestoreError as e:
        # When some entries fail, the ones that were restored must be reported honestly --
        # once the exception is raised the user has no idea what state the machine is in.
        s.err_console.print(f"devproxy: the rollback partially failed ({escape(str(e))})")
        for p in e.restored:
            s.console.print(f"  ✔ restored {escape(str(p))}")
        for p, exc in e.failures:
            s.err_console.print(f"  ✗ {escape(str(p))}: {escape(str(exc))}")
        raise typer.Exit(EXIT_FAIL) from e
    s.console.print(f"Rolled back {len(touched)} file(s):")
    for p in touched:
        s.console.print(f"  · {escape(str(p))}")
    # If the unit changed but the process was not restarted, it still runs the
    # post-migration config
    _restart_caddy_after_rollback(platform, len(touched))
