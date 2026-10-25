from __future__ import annotations

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


def _known_route_logs() -> list[str]:
    """有哪些路由日志可看（按文件名列出）。"""
    try:
        paths, _, _, _ = s.build_context()
    except Exception:  # noqa: BLE001 —— 读不到就当没有，不该因此报错
        return []
    log_dir = paths.log_dir / "routes"
    if not log_dir.is_dir():
        return []
    return sorted(p.stem for p in log_dir.glob("*.log"))


@app.command()
def logs(
    service: str = typer.Argument("caddy", help="caddy|dnsmasq|<路由名>"),
) -> None:
    """跟踪日志：服务（caddy / dnsmasq）或路由（sync 起的应用）。"""
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
