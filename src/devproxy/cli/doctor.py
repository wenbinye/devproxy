from __future__ import annotations

import typer
from rich.markup import escape
from rich.table import Table

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_PRECONDITION
from devproxy.lifecycle.doctor import build_report
from devproxy.lifecycle.doctor import find_wildcard_sites
from devproxy.lifecycle.doctor import report_is_healthy
from devproxy.lifecycle.migrate import detect_legacy


@app.command()
def doctor(
    dns_backend: str = typer.Option(None, "--dns-backend", help="auto|resolved|system"),
) -> None:
    """Health check: verify every precondition."""
    paths, config, _, _ = s.build_context()
    backend = s._dns_backend(dns_backend, config)
    platform = s._platform()
    # An explicit resolved backend on macOS is a usage error -- say so early (exit 2)
    # instead of waiting for the component's plan() to raise RuntimeError
    s.ensure_backend_supported(backend, platform)
    reports = build_report(s._components(paths, config, backend))
    # Wildcard site blocks (*.test) in the live config must be reported: they share the
    # suffix with the exact site blocks this tool generates and make the whole TLS
    # handshake fail. Removing them is exactly what the migration does.
    # Probe non-interactively -- doctor is read-only and must never pop a password prompt.
    legacy = detect_legacy(platform, paths, interactive=False)
    wildcards = find_wildcard_sites(paths, config, legacy.config_path if legacy else None)
    table = Table(show_header=True, header_style="bold")
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Details")
    table.add_column("Fix")
    for r in reports:
        table.add_row(
            r.name,
            "✔ ready" if r.status.ok else "✗ missing",
            escape(r.status.detail),
            escape(r.status.hint),
        )
    for path, addresses in wildcards:
        table.add_row(
            "caddy config",
            "✗ wildcard site block",
            escape(f"{path}: {', '.join(addresses)}"),
            "devproxy setup",
        )
    s.console.print(table)
    # Rich truncates the table cells -- print the important bits (impact and fix) again
    for path, addresses in wildcards:
        s.console.print(
            f"[yellow]⚠ {path} still contains wildcard site blocks: {', '.join(addresses)}[/yellow]"
        )
        s.console.print(
            "   When a wildcard block shares the suffix with an exact site block, Caddy's TLS "
            "handshake fails across the board; fix: devproxy setup (finishes the migration "
            "and removes the wildcard block)"
        )
    # Browser-side trust notes (Firefox keeps its own store) get their own lines too:
    # they are **notes**, not defects -- they do not change the verdict, but the user must know
    for note in s.firefox_notes(reports):
        s.console.print(f"[yellow]⚠ {escape(note)}[/yellow]")
    if not report_is_healthy(reports) or wildcards:
        raise typer.Exit(EXIT_PRECONDITION)
