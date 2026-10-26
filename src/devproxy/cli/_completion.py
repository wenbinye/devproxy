"""Completion candidates for the shell.

Typer/Click ship `--install-completion` / `--show-completion` (bash / zsh / fish /
powershell) on their own, so command and option completion needs no code here. What
this module adds is the **value** domain: the candidates for "what should this
argument be" (registered route names, service log names).

Completion runs on every <TAB>, so it must be **fast and unfailing**: an unreadable
route table (not set up yet, corrupt routes.json) simply yields no candidates.
Never print an error to stdout -- bash turns every output line into a completion item.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from devproxy.core.paths import default_paths
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.routes import load_table

# `devproxy logs` accepts service log names in addition to route names
SERVICE_LOGS = ("caddy", "dnsmasq")


def _route_names() -> list[str]:
    """Registered route names (no candidates if the route table cannot be read)."""
    try:
        table = load_table(default_paths().routes_json)
    except (RouteTableError, OSError):
        return []
    return sorted(route.name for route in table.routes)


def _complete(candidates: Iterable[str], incomplete: str) -> list[str]:
    return [c for c in candidates if c.startswith(incomplete)]


# The ctx type is whatever typer passes in (click.Context on typer 0.24, the bundled
# typer._click.Context on 0.27) -- do **not** import click: an installed typer may not
# ship it (0.27 absorbed click as typer._click and it is no longer a separate
# dependency). This code never uses it, so Any is enough.


def route_names(ctx: Any, args: list[str], incomplete: str) -> list[str]:
    """Route-name candidates (`down <name>`, `logs <name>`)."""
    return _complete(_route_names(), incomplete)


def log_targets(ctx: Any, args: list[str], incomplete: str) -> list[str]:
    """Candidates for `logs <name>`: service log names plus route names."""
    return _complete((*SERVICE_LOGS, *_route_names()), incomplete)
