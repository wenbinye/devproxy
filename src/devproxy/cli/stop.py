"""stop 命令：停掉本项目（devproxy.toml）声明的所有应用。

是 `devproxy up` 的逆操作：up 起来的，一条命令全停。
只动本项目声明的路由 —— 手工 up 的、别的项目的都不碰。
"""

from __future__ import annotations

import typer

from devproxy.cli import _shared as s
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_USAGE
from devproxy.gateway.project import ProjectFileError
from devproxy.gateway.project import load_project_file
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.store import RouteStoreError


def stop_project_apps() -> None:
    """停掉本项目（devproxy.toml）声明的所有应用。重新启动：devproxy up"""
    found = s.find_project_file()
    if found is None:
        s.fail(
            "当前目录不在项目里（找不到 devproxy.toml）。停单条路由用 devproxy down <名字>",
            EXIT_USAGE,
        )
    try:
        project = load_project_file(found)
    except ProjectFileError as e:
        s.fail(str(e), EXIT_USAGE)

    declared = {route.name for route in project.routes}
    _, _, store, _ = s.build_context()

    stopped: list[tuple[str, int | None, str | None]] = []
    try:

        def _remove_declared(table) -> None:
            for route in list(table.routes):
                if route.name in declared:
                    stopped.append((route.name, route.pid, route.proc_start))
                    table.remove(route.name)

        store.mutate(_remove_declared)
    except (RouteStoreError, RouteTableError) as e:
        s.fail(str(e))

    if not stopped:
        s.console.print("没有在跑的应用。")
        raise typer.Exit(EXIT_OK)

    for name, pid, proc_start in stopped:
        if pid:
            # 带上注册时的进程身份 —— pid 可能已被复用，身份不符不杀
            s.kill_pid_group(pid, proc_start)
        s.console.print(f"✔ 已停止 {name}")

    s.console.print("")
    s.console.print(f"全部停止（{len(stopped)} 个应用）。重新启动：devproxy up")
    raise typer.Exit(EXIT_OK)
