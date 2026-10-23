from __future__ import annotations

from rich.markup import escape
import typer

from devproxy.cli import app
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_SUDO
from devproxy.cli._shared import EXIT_USAGE

from devproxy.cli import _shared as s

@app.command()
def logs(
    service: str = typer.Argument("caddy", help="caddy|dnsmasq"),
) -> None:
    """跟踪服务日志。"""
    if service not in ("caddy", "dnsmasq"):
        # 参数错误按全局约定退出 2（§9.1），而不是运行失败 1
        s.err_console.print(f"devproxy: 未知服务 {service}（可选 caddy / dnsmasq）")
        raise typer.Exit(EXIT_USAGE)
    try:
        # 只读命令：不碰 build_context（它会加载配置并解析 storage 留档）
        s._platform().tail_service_log(service)
    except PermissionError as e:
        s.err_console.print(f"devproxy: {escape(str(e))}")
        raise typer.Exit(EXIT_SUDO)
    except OSError as e:
        # journalctl 不存在等：干净报错，不要把 FileNotFoundError 漏成 traceback
        s.err_console.print(f"devproxy: 无法读取 {service} 日志: {escape(str(e))}")
        raise typer.Exit(EXIT_FAIL)
