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
    """体检：检查所有前置条件。"""
    paths, config, _, _ = s.build_context()
    backend = s._dns_backend(dns_backend, config)
    platform = s._platform()
    # macOS 显式 resolved 是参数错误 —— 提前给出明确信息（退出码 2），
    # 不要等到组件 plan() 抛 RuntimeError
    s.ensure_backend_supported(backend, platform)
    reports = build_report(s._components(paths, config, backend))
    # 现役配置里还有通配站点块（*.test）时必须报出来：它与工具生成的
    # 精确站点块同后缀，会让 TLS 握手整体失败。迁移正是移除它的动作。
    # 用非交互的探测 —— doctor 是只读命令，绝不能弹密码框。
    legacy = detect_legacy(platform, paths, interactive=False)
    wildcards = find_wildcard_sites(paths, config, legacy.config_path if legacy else None)
    table = Table(show_header=True, header_style="bold")
    table.add_column("组件")
    table.add_column("状态")
    table.add_column("详情")
    table.add_column("修复")
    for r in reports:
        table.add_row(
            r.name,
            "✔ 就绪" if r.status.ok else "✗ 缺失",
            escape(r.status.detail),
            escape(r.status.hint),
        )
    for path, addresses in wildcards:
        table.add_row(
            "caddy 配置",
            "✗ 通配站点块",
            escape(f"{path}: {', '.join(addresses)}"),
            "devproxy setup",
        )
    s.console.print(table)
    # 表格里的说明会被 Rich 截断 —— 关键信息（影响与修复）单独再打一遍
    for path, addresses in wildcards:
        s.console.print(f"[yellow]⚠ {path} 仍有通配站点块：{', '.join(addresses)}[/yellow]")
        s.console.print(
            "   通配块与精确站点块同后缀时，Caddy 的 TLS 握手会整体失败；"
            "修复: devproxy setup（完成迁移，移除通配块）"
        )
    # 浏览器侧的信任提示（Firefox 有自己的证书库）同样单独打一遍：
    # 它是**提示**而不是缺陷 —— 不影响体检结论，但用户必须知道
    for note in s.firefox_notes(reports):
        s.console.print(f"[yellow]⚠ {escape(note)}[/yellow]")
    if not report_is_healthy(reports) or wildcards:
        raise typer.Exit(EXIT_PRECONDITION)
