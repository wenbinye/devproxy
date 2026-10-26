"""doctor: health check report. Read-only; changes nothing."""

from __future__ import annotations

from pathlib import Path

from devproxy.components.base import Component
from devproxy.lifecycle.migrate import wildcard_addresses
from devproxy.lifecycle.setup import ComponentReport
from devproxy.lifecycle.setup import check_component


def build_report(components: list[Component]) -> list[ComponentReport]:
    """Run check() for each component, reusing setup's exception isolation -- even if one component crashes, the rest are still listed."""
    return [check_component(c)[0] for c in components]


def report_is_healthy(reports: list[ComponentReport]) -> bool:
    return all(r.status.ok for r in reports)


def find_wildcard_sites(
    paths, config, legacy_config: Path | None = None
) -> list[tuple[Path, list[str]]]:
    """Wildcard site blocks in Caddy configs that are still active. Returns [(file, address list)].

    The tool's design premise is "exact site blocks, no wildcards": when a
    wildcard block (e.g. *.test) shares a suffix with exact blocks, Caddy's TLS
    handshake fails entirely (measured). So any wildcard block still present in
    active configs must be reported -- migration is exactly the action that
    removes it.

    Scan scope: the tool's own main Caddyfile and the two files it imports, plus
    the legacy Caddyfile still in effect before migration.
    """
    targets: list[Path] = []
    if legacy_config is not None:
        targets.append(Path(legacy_config))
    targets.extend([paths.caddyfile, paths.routes_caddy, paths.sites_caddy])
    found: list[tuple[Path, list[str]]] = []
    seen: set[Path] = set()
    for path in targets:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        addresses = wildcard_addresses(text, config.domain_suffix)
        if addresses:
            found.append((path, addresses))
    return found
