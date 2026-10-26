"""devproxy command-line entry point.

One module per command. The seams (platform, component assembly, interactivity
checks, ...) live in `_shared`, and command modules look them up at **call time**
through `devproxy.cli._shared`, so patching `devproxy.cli._shared.<name>` in tests
is guaranteed to take effect.
"""

from __future__ import annotations

import sys

import typer
from rich.markup import escape

from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import DevProxyGroup
from devproxy.cli._shared import err_console
from devproxy.core.caddy_env import CaddyEnvArchiveError
from devproxy.core.config import ConfigError
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.store import RouteStoreError

app = typer.Typer(
    cls=DevProxyGroup,
    help="One-command local dev environment and domain gateway",
    no_args_is_help=True,
)

# Importing registers the command: each module attaches itself via @app.command().
from devproxy.cli import config  # noqa: E402,F401
from devproxy.cli import doctor  # noqa: E402,F401
from devproxy.cli import down  # noqa: E402,F401
from devproxy.cli import expose  # noqa: E402,F401
from devproxy.cli import init  # noqa: E402,F401
from devproxy.cli import logs  # noqa: E402,F401
from devproxy.cli import ls  # noqa: E402,F401
from devproxy.cli import run  # noqa: E402,F401
from devproxy.cli import selftest  # noqa: E402,F401
from devproxy.cli import setup  # noqa: E402,F401
from devproxy.cli import trust  # noqa: E402,F401
from devproxy.cli import uninstall  # noqa: E402,F401
from devproxy.cli import up  # noqa: E402,F401


def main() -> None:
    try:
        app()
    except (CaddyEnvArchiveError, ConfigError, RouteStoreError, RouteTableError) as e:
        # Last resort: a domain exception must never reach the terminal as a traceback
        err_console.print(f"devproxy: {escape(str(e))}")
        sys.exit(EXIT_FAIL)
