"""The setup command: only sequences the steps; the individual actions live in the helpers below."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import typer
from rich.markup import escape

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._migrate import MigrateRequest
from devproxy.cli._migrate import _do_rollback
from devproxy.cli._migrate import _migrate_legacy
from devproxy.cli._shared import EXIT_DECLINED
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_PRECONDITION
from devproxy.cli._shared import EXIT_SUDO
from devproxy.cli._shared import EXIT_USAGE
from devproxy.cli._shared import UP_EXAMPLE
from devproxy.core.config import Config
from devproxy.core.config import ConfigError
from devproxy.core.config import validate_config
from devproxy.lifecycle.migrate import detect_legacy


def _handle_rollback(raw: str | None) -> bool:
    """Handle --rollback; return whether the rollback branch was taken.

    Rollback needs only the backup directory and the platform, so it **must not load
    config.toml first** -- a corrupt config is exactly when a rollback is most needed,
    and the recovery path cannot depend on the thing it is recovering.
    An empty string is not "not provided": scripts pass `--rollback "$DIR"` with the
    variable unset, and that would fall through to a full deploy that writes /etc and
    restarts services while the user believes they sent a read-only rollback. Reject it
    as a usage error.
    """
    if raw is None:
        return False
    if not raw.strip():
        s.fail("--rollback needs a backup directory path (got an empty string)", EXIT_USAGE)
    _do_rollback(Path(raw))
    return True


def _apply_cli_overrides(
    config: Config, dns_backend: str | None, domain_suffix: str | None
) -> tuple[Config, str]:
    """Fold the CLI overrides into the config and validate it; return (config, effective backend)."""
    backend = s._dns_backend(dns_backend, config)
    if domain_suffix is not None:
        config = replace(config, domain_suffix=domain_suffix)
        try:
            # --domain-suffix feeds straight into the Caddyfile and the DNS config under
            # /etc, so it is treated like config.toml: it must be validated, not bypassed
            validate_config(config)
        except ConfigError as e:
            s.fail(str(e), EXIT_USAGE)
    return config, backend


def _maybe_migrate(platform, paths, config, backend: str, yes: bool):
    """Detect and migrate a legacy environment; the migration must precede the components.

    Components read state the migration leaves behind (unit environment variables,
    storage). The backend is folded into the config first, so the migration and the
    following components see the same one.

    The legacy Caddyfile is read exactly once, inside the migration: storage continuity
    and the notes are resolved in one pass by `run_migration`, and the effective config
    comes back as `MigrationOutcome.config`. Reading it up front here (with
    `resolve_storage_details`) would parse the same file twice.
    """
    config = replace(config, dns_backend=backend)
    state = detect_legacy(platform, paths)
    if state is None:
        return config
    outcome = _migrate_legacy(
        MigrateRequest(
            platform=platform,
            paths=paths,
            config=config,
            state=state,
            yes=yes,
        )
    )
    return outcome.config


def _confirm_or_exit(plan, yes: bool) -> None:
    """Disclose elevation, refuse when non-interactive, and confirm interactively."""
    if plan.needs_sudo:
        s.console.print(
            f"[yellow]The following steps need sudo ({len(plan.needs_sudo)} total):[/yellow]"
        )
        for a in plan.needs_sudo:
            s.console.print(f"  · {escape(a.description)}")
        if not yes and not s._interactive():
            s.err_console.print(
                "Non-interactive environment without --yes; refusing to run steps that "
                "need elevation."
            )
            raise typer.Exit(EXIT_SUDO)

    # Final confirmation in interactive mode; --yes or a non-interactive environment skips it.
    if not yes and s._interactive():
        prompt = (
            f"The {len(plan.needs_sudo)} step(s) above need sudo. Continue?"
            if plan.needs_sudo
            else "About to apply the changes above. Continue?"
        )
        if not typer.confirm(prompt, default=True):
            s.console.print("Cancelled: nothing was changed (no changes were applied).")
            raise typer.Exit(EXIT_DECLINED)


def _report_noop(plan, paths, config) -> None:
    """Nothing to change: report the trust state if all is ready, otherwise report honestly."""
    unhealthy = [r for r in plan.reports if not r.status.ok]
    if not unhealthy:
        s.console.print("All components are ready; nothing to change.")
        # The trust hint only shows when the CA exists and is not fully trusted; with
        # --no-trust an existing CA is still reported (the user explicitly skipped trust here)
        s._print_trust_hint_if_needed(paths, config)
        raise typer.Exit(EXIT_OK)
    # No actions to run but the components are unhealthy -- never claim "ready"
    s.print_noop_unhealthy(unhealthy)
    raise typer.Exit(EXIT_PRECONDITION)


@app.command()
def setup(
    dns_backend: str = typer.Option(None, "--dns-backend", help="auto|resolved|system"),
    domain_suffix: str = typer.Option(None, "--domain-suffix"),
    no_trust: bool = typer.Option(False, "--no-trust", help="Skip CA trust"),
    yes: bool = typer.Option(False, "--yes", help="Non-interactive mode; do not ask"),
    rollback_dir: str = typer.Option(
        None, "--rollback", help="Roll back from the given backup directory"
    ),
) -> None:
    """Deploy the local dev environment (Caddy + dnsmasq + DNS route + CA trust + autostart)."""
    if _handle_rollback(rollback_dir):
        return

    paths, config, _, _ = s.build_context()

    config, backend = _apply_cli_overrides(config, dns_backend, domain_suffix)
    platform = s._platform()
    s.ensure_backend_supported(backend, platform)
    # Write side effects only start once every argument has been validated: the deploy
    # branch needs the directories and the import target, while read-only commands no
    # longer rely on build_context creating files
    paths.ensure_files()
    config = _maybe_migrate(platform, paths, config, backend, yes)

    components = s._components(paths, config, backend, platform, include_cert=not no_trust)
    # Record the PermissionError that apply_plan swallows during apply, so the exit code
    # can tell "elevation needed" (4) from an ordinary failure (1)
    components, elevation_errors = s.track_elevation_errors(components)

    plan = s.build_plan(components)
    s.render_plan(plan)
    # Machine-wide scope disclosure: DNS= and FallbackDNS= change the resolution path of
    # the **whole machine**, which neither fits nor reads well in the table -- print it
    # separately, before the user is asked to confirm.
    s.print_scope_notes(plan)

    if plan.is_noop:
        _report_noop(plan, paths, config)

    _confirm_or_exit(plan, yes)

    results = s.apply_plan(plan)
    if not s.print_apply_results(results):
        s.fail_for_apply_errors(elevation_errors)
    s.console.print(f"Done. Try: {UP_EXAMPLE}")
    s._print_trust_hint_if_needed(paths, config)
