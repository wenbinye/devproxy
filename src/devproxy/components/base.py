"""Component protocol. check never raises -- doctor relies on it to list every problem at once."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol
from typing import runtime_checkable

from devproxy.platform.base import Action
from devproxy.platform.base import Status


def _site_host(address: str) -> str:
    """Site address -> hostname (strip the scheme and port).

    A Caddy site address may be written as `localhost`, `localhost:8443`,
    `https://localhost:8443`, or a bare port `:443`. Only the hostname part
    takes part in exact comparison -- substring matching would mistake
    `foo.localhost.test` for localhost.
    """
    raw = address.strip()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    if raw.startswith("["):  # IPv6 literal, e.g. [::1]:443
        end = raw.find("]")
        return (raw[1:end] if end != -1 else raw).lower()
    if ":" in raw:
        raw = raw.rsplit(":", 1)[0]
    return raw.lower()


def has_localhost_site(sites_caddy: Path) -> bool:
    """Whether sites.caddy contains a localhost site block (exact match on the site address token).

    Machines with only `*.test` sites have no localhost site -- probing
    localhost there verifies "the site name matches", not "the service is
    alive".
    """
    try:
        text = sites_caddy.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Site addresses are written before `{`; multiple addresses are separated by commas or whitespace.
        head = stripped.split("{", 1)[0]
        if any(_site_host(address) == "localhost" for address in head.replace(",", " ").split()):
            return True
    return False


def probe_url(routes_json: Path, sites_caddy: Path) -> str | None:
    """Which URL to use for the end-to-end HTTPS probe; None when there is nothing to probe.

    Caddy and the certificates component share this same criterion (writing it
    twice in two places has already drifted once): prefer the first registered
    route, and use localhost only **when a localhost site actually exists**.
    On machines with only sites such as codeup.test, probing localhost fails
    the handshake because the site name does not match, marking a healthy
    machine as broken.
    """
    try:
        state = json.loads(routes_json.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        state = {}
    routes = state.get("routes") if isinstance(state, dict) else None
    for route in routes or []:
        domain = route.get("domain") if isinstance(route, dict) else None
        if isinstance(domain, str) and domain:
            return f"https://{domain}"
    if has_localhost_site(sites_caddy):
        return "https://localhost"
    return None


@runtime_checkable
class Component(Protocol):
    name: str

    def check(self) -> Status:
        """Inspect the current state. Never raises."""

    def plan(self) -> list[Action]:
        """Compute the actions to perform from the current state. An empty list means no changes are needed."""

    def apply(self, actions: list[Action]) -> None:
        """Perform the actions."""
