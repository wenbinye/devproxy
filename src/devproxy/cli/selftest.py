from __future__ import annotations

import typer

from devproxy.cli import app
from devproxy.cli._shared import EXIT_FAIL

from devproxy.cli import _shared as s

@app.command()
def selftest() -> None:
    """隔离 Caddy 实例端到端自检（不碰生产 443/2019）。"""
    from devproxy.gateway.selftest import run_selftest

    _, config, _, _ = s.build_context()
    result = run_selftest(config.caddy_bin_path)
    for desc, ok in result.checks:
        s.console.print(f"  {'✅' if ok else '❌'} {desc}")
    if result.failed:
        s.err_console.print(f"devproxy: selftest 失败 {len(result.failed)} 项")
        raise typer.Exit(EXIT_FAIL)
    s.console.print(f"selftest 全部通过 ✅（{len(result.checks)} 项）")
