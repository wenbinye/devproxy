from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

from rich.markup import escape
from rich.table import Table
import typer

from devproxy.cli import app
from devproxy.cli._migrate import _do_rollback
from devproxy.cli._migrate import _migrate_legacy
from devproxy.cli._shared import EXIT_DECLINED
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_PRECONDITION
from devproxy.cli._shared import EXIT_SUDO
from devproxy.cli._shared import EXIT_USAGE
from devproxy.cli._shared import TRUST_HINT
from devproxy.cli._shared import UP_EXAMPLE
from devproxy.core.config import ConfigError
from devproxy.core.config import validate_config
from devproxy.lifecycle.migrate import detect_legacy
from devproxy.lifecycle.migrate import resolve_storage

from devproxy.cli import _shared as s

@app.command()
def setup(
    dns_backend: str = typer.Option(None, "--dns-backend", help="auto|resolved|system"),
    domain_suffix: str = typer.Option(None, "--domain-suffix"),
    no_trust: bool = typer.Option(False, "--no-trust", help="跳过证书信任"),
    yes: bool = typer.Option(False, "--yes", help="非交互模式，不询问"),
    rollback_dir: str = typer.Option(None, "--rollback", help="从指定备份目录回滚"),
) -> None:
    """部署本地开发环境（Caddy + dnsmasq + DNS 路由 + 证书信任 + 自启）。"""
    paths, config, _, _ = s.build_context()

    if rollback_dir:
        _do_rollback(Path(rollback_dir))
        raise typer.Exit(EXIT_OK)

    backend = s._dns_backend(dns_backend, config)
    if domain_suffix:
        config = replace(config, domain_suffix=domain_suffix)
        try:
            # --domain-suffix 会直通 Caddyfile 与 /etc 下的 DNS 配置，
            # 与 config.toml 同等对待：必须过校验，不能绕过
            validate_config(config)
        except ConfigError as e:
            s.fail(str(e), EXIT_USAGE)

    platform = s._platform()
    # 迁移必须先于组件流程：组件会读迁移留下的状态（单元环境变量、storage）
    state = detect_legacy(platform, paths)
    if state is not None:
        config, storage_note = resolve_storage(config, state)
        _migrate_legacy(
            platform,
            paths,
            config,
            state,
            yes=yes,
            backend=backend,
            notes=[storage_note] if storage_note else [],
        )

    components = s._components(paths, config, backend, platform)
    if no_trust:
        components = [c for c in components if c.name != "cert"]

    plan = s.build_plan(components)

    table = Table(show_header=True, header_style="bold")
    table.add_column("组件")
    table.add_column("现状")
    table.add_column("将要执行")
    for r in plan.reports:
        table.add_row(
            r.name,
            s._status_cell(r.status),
            "\n".join(escape(a.description) for a in r.actions) or "—",
        )
    s.console.print(table)

    # 整机作用域的披露：DNS= 与 FallbackDNS= 改的是**整台机器**的解析路径，
    # 表格里放不下也看不清 —— 单独打一遍，用户点确认之前就该看到。
    for note in s._global_scope_notes(plan):
        s.console.print(f"[yellow]{escape(note)}[/yellow]")

    if plan.is_noop:
        unhealthy = [r for r in plan.reports if not r.status.ok]
        if not unhealthy:
            s.console.print("所有组件已就绪，无需变更。")
            if any("尚无" in r.status.detail for r in plan.reports if r.status.ok):
                s.console.print(TRUST_HINT)
            raise typer.Exit(EXIT_OK)
        # 没有可执行的动作、但组件本身不健康 —— 不能谎报"已就绪"
        for r in unhealthy:
            s.err_console.print(f"devproxy: {r.name}: {escape(r.status.detail)}")
        raise typer.Exit(EXIT_PRECONDITION)

    if plan.needs_sudo:
        s.console.print(
            f"[yellow]以下步骤需要 sudo（共 {len(plan.needs_sudo)} 项）：[/yellow]"
        )
        for a in plan.needs_sudo:
            s.console.print(f"  · {escape(a.description)}")
        if not yes and not sys.stdin.isatty():
            s.err_console.print("非交互环境且未指定 --yes，拒绝执行需要提权的步骤。")
            raise typer.Exit(EXIT_SUDO)

    # 交互模式下的最后确认（§6.1）；--yes 或非交互环境直接跳过。
    if not yes and s._interactive():
        prompt = (
            f"以上 {len(plan.needs_sudo)} 项需要 sudo，继续？"
            if plan.needs_sudo
            else "即将执行以上变更，继续？"
        )
        if not typer.confirm(prompt, default=True):
            s.console.print("已取消：什么都没改（未执行任何变更）。")
            raise typer.Exit(EXIT_DECLINED)

    results = s.apply_plan(plan)
    ok = True
    for name, status in results:
        mark = "✔" if status.ok else "✗"
        s.console.print(f"{mark} {name}: {escape(status.detail)}")
        if not status.ok:
            ok = False
            if status.hint:
                s.console.print(f"    修复: {escape(status.hint)}")
    if not ok:
        raise typer.Exit(EXIT_FAIL)
    s.console.print(f"完成。试试: {UP_EXAMPLE}")
    s.console.print(TRUST_HINT)
