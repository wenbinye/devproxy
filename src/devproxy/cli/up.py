from __future__ import annotations

from pathlib import Path

import typer

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_PRECONDITION
from devproxy.cli._shared import EXIT_USAGE
from devproxy.core.config import READY_TIMEOUT_MAX
from devproxy.core.config import READY_TIMEOUT_MIN
from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import port_env_name
from devproxy.gateway.names import validate_name
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.runner import PreconditionError
from devproxy.gateway.runner import UpError
from devproxy.gateway.store import RouteStoreError


def _validate_cli_args(name: str, port: int | None, ready_timeout: int | None) -> None:
    """Usage errors exit with 2, not the runtime-failure 1.

    UpRunner validates name/port too, but that is the gateway layer's last gate; the
    CLI validates first so that "you typed the command wrong" and "the environment
    broke" stay distinguishable (CI and scripts branch on the exit code).
    """
    try:
        validate_name(name)
    except InvalidNameError as e:
        s.fail(str(e), EXIT_USAGE)
    if port is not None and not (1 <= port <= 65535):
        s.fail(f"invalid port {port}: must be between 1 and 65535", EXIT_USAGE)
    if ready_timeout is not None and not (READY_TIMEOUT_MIN <= ready_timeout <= READY_TIMEOUT_MAX):
        s.fail(
            f"invalid ready timeout {ready_timeout}: must be between "
            f"{READY_TIMEOUT_MIN} and {READY_TIMEOUT_MAX} seconds",
            EXIT_USAGE,
        )


@app.command(context_settings={"ignore_unknown_options": True})
def up(
    ctx: typer.Context,
    name: str = typer.Argument(
        None,
        help="Route name (\\[a-z0-9-], single label). "
        "Omit it to apply every declaration in this project's devproxy.toml "
        "(compose's up -d)",
    ),
    port: int = typer.Option(
        None, "--port", help="Explicit port (allocated automatically by default)"
    ),
    ready_timeout: int = typer.Option(None, "--ready-timeout", help="Ready timeout in seconds"),
    file: Path = typer.Option(
        None, "--file", help="[project mode] Explicit devproxy.toml (searched upwards by default)"
    ),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="[project mode] Remove routes deleted from this file (needs --yes when non-interactive)",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="[project mode] Non-interactive mode; do not ask"
    ),
) -> None:
    """Allocate a port, start the command, and register https://<name>.test."""
    data = ctx.ensure_object(dict)
    if name is None:
        # Project mode (compose's up -d): apply every declaration in devproxy.toml
        if not data.get("has_sep"):
            from devproxy.cli.sync import run_project_sync

            run_project_sync(file=file, prune=prune, yes=yes)
            raise typer.Exit(EXIT_OK)
        s.fail(
            "Missing route name: start a single app with devproxy up <name> -- CMD...; "
            "start everything this project declares with plain devproxy up",
            EXIT_USAGE,
        )
    if not data.get("has_sep"):
        s.fail("Missing '--': devproxy up <name> [options] -- CMD...", EXIT_USAGE)
    cmd = list(data.get("trailing") or [])
    if not cmd:
        s.fail("Missing start command: CMD... is required after --", EXIT_USAGE)
    _validate_cli_args(name, port, ready_timeout)

    paths, config, store, admin = s.build_context()
    s._require_caddyfile(paths)
    runner = s.UpRunner(store, config, admin)

    def on_ready(domain: str, allocated: int) -> None:
        s.console.print(f"✅ https://{domain}  (port {allocated})")
        s._print_trust_hint_if_needed(paths, config)

    try:
        rc = runner.run(
            name,
            cmd,
            port=port,
            ready_timeout=ready_timeout,
            on_ready=on_ready,
            # Sibling routes that are already registered: their ports are
            # known, so the app can be pointed at them (a frontend at its
            # API) even in foreground mode.
            extra_env={
                port_env_name(r.name): str(r.port)
                for r in store.list()
                if r.port and r.name != name
            },
        )
    except PreconditionError as e:
        # Caddy not running is an unmet precondition, not a runtime failure -- exit 3
        s.fail(str(e), EXIT_PRECONDITION)
    except (UpError, RouteStoreError, RouteTableError) as e:
        # The arguments were validated above; UpError here means the child process or the
        # readiness wait failed
        s.fail(str(e))
    raise typer.Exit(rc)
