from __future__ import annotations

from pathlib import Path

from rich.markup import escape
import typer

from devproxy.cli import app
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_SUDO
from devproxy.cli._shared import EXIT_USAGE
from devproxy.core.logs import follow_log
from devproxy.gateway.sync import route_log_path

from devproxy.cli import _shared as s

_SERVICE_LOGS = ("caddy", "dnsmasq")


def _route_log_files() -> list[Path]:
    """有哪些路由日志文件（按名字排序）。"""
    try:
        paths, _, _, _ = s.build_context()
    except Exception:  # noqa: BLE001 —— 读不到就当没有，不该因此报错
        return []
    log_dir = paths.log_dir / "routes"
    if not log_dir.is_dir():
        return []
    return sorted(log_dir.glob("*.log"))


def _known_route_logs() -> list[str]:
    """有哪些路由日志可看（只给名字）。"""
    return [path.stem for path in _route_log_files()]


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size // 1024} KB"
    return f"{size // (1024 * 1024)} MB"


def _print_list() -> None:
    """列出所有能跟的日志目标。"""
    s.console.print("服务日志：")
    for name in _SERVICE_LOGS:
        s.console.print(f"  {name}")

    files = _route_log_files()
    if not files:
        s.console.print("路由日志：（没有 —— 跑一次 devproxy sync 就会有）")
        return
    # 目录只打一次：每行都带全路径会被 Rich 的 80 列折行，名字与大小就对不齐了
    s.console.print(f"路由日志（{files[0].parent}/）：")
    for path in files:
        try:
            size = _format_size(path.stat().st_size)
        except OSError:
            size = "?"
        s.console.print(f"  {path.stem:<20} {size:>8}")
    s.console.print("")
    s.console.print("用 devproxy logs <名字> 跟随。")


@app.command()
def logs(
    service: str = typer.Argument("caddy", help="caddy|dnsmasq|<路由名>"),
    list_all: bool = typer.Option(
        False, "--list", help="列出所有能跟的日志目标，不跟随"
    ),
) -> None:
    """跟踪日志：服务（caddy / dnsmasq）或路由（sync 起的应用）。"""
    if list_all:
        _print_list()
        raise typer.Exit(0)

    if service in _SERVICE_LOGS:
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
        return

    # 路由日志：~/.local/state/devproxy/logs/routes/<名字>.log
    known = _known_route_logs()
    if service not in known:
        # 参数错误按全局约定退出 2（§9.1），而不是运行失败 1
        s.err_console.print(
            f"devproxy: 未知服务 {service}"
            f"（可选：{' / '.join(_SERVICE_LOGS)}，或路由名）"
        )
        if known:
            s.err_console.print(f"   可用的路由日志：{'、'.join(known)}")
        else:
            s.err_console.print("   （当前没有路由日志 —— 跑一次 devproxy sync 就会有）")
        raise typer.Exit(EXIT_USAGE)

    paths, _, _, _ = s.build_context()
    path = route_log_path(paths, service)
    try:
        follow_log(path)
    except KeyboardInterrupt:
        raise typer.Exit(0)
    except FileNotFoundError:
        s.err_console.print(f"devproxy: 日志文件不存在: {path}")
        raise typer.Exit(EXIT_FAIL)
