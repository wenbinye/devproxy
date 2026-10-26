from __future__ import annotations

import typer

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_FAIL


@app.command()
def selftest() -> None:
    """End-to-end self-check against an isolated Caddy instance (leaves 443/2019 alone)."""
    from devproxy.gateway.selftest import run_selftest

    _, config, _, _ = s.build_context()
    result = run_selftest(config.caddy_bin_path)
    for desc, ok in result.checks:
        s.console.print(f"  {'✅' if ok else '❌'} {desc}")
    if result.failed:
        s.err_console.print(f"devproxy: selftest failed {len(result.failed)} check(s)")
        raise typer.Exit(EXIT_FAIL)
    s.console.print(f"selftest passed ✅ ({len(result.checks)} checks)")
