from __future__ import annotations

import typer

from devproxy.cli import _completion
from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_USAGE
from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import validate_name
from devproxy.gateway.routes import RouteTable
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.store import RouteStoreError


@app.command()
def down(
    name: str = typer.Argument(
        None,
        help="路由名。省略 = 停掉本项目（devproxy.toml）声明的所有应用",
        autocompletion=_completion.route_names,
    ),
) -> None:
    """注销路由；子进程还在则一并收掉。不带名字 = 停掉本项目全部应用。"""
    if name is None:
        from devproxy.cli.stop import stop_project_apps

        stop_project_apps()
        raise typer.Exit(0)
    try:
        validate_name(name)
    except InvalidNameError as e:
        s.fail(str(e), EXIT_USAGE)
    _, _, store, _ = s.build_context()
    try:
        route = store.get(name)
    except RouteTableError as e:
        s.fail(str(e))
    if route is None:
        s.console.print(f"（{name} 未注册）")
        raise typer.Exit(EXIT_OK)

    def _remove(table: RouteTable) -> None:
        table.remove(name)

    try:
        store.mutate(_remove)
    except (RouteStoreError, RouteTableError) as e:
        s.fail(str(e))
    if route.pid:
        # 带上注册时记录的进程身份 —— pid 可能已被复用，身份不符不杀
        s.kill_pid_group(route.pid, route.proc_start)
    s.console.print(f"已注销 {name}")
