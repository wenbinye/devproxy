"""init 命令：在项目里生成一份 devproxy.toml。

只做交互与退出码 —— 生成逻辑在 `gateway.project`。
"""

from __future__ import annotations

from pathlib import Path

import typer

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_USAGE
from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import validate_name
from devproxy.gateway.project import MODE_UP
from devproxy.gateway.project import PROJECT_FILE_NAME
from devproxy.gateway.project import ProjectRoute
from devproxy.gateway.project import detect_project_root
from devproxy.gateway.project import render_project_file
from devproxy.gateway.project import route_name_from_dir
from devproxy.gateway.project import suggest_command
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.routes import load_table


@app.command()
def init(
    name: str = typer.Option(None, "--name", help="路由名（默认取项目目录名）"),
    command: str = typer.Option(
        None, "--command", help='启动命令，如 "npm run dev"（不写则按项目类型猜）'
    ),
    from_route: str = typer.Option(
        None,
        "--from",
        help="从已注册的同名路由导入它的启动命令（路由表是全局的，所以要指名）",
    ),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 devproxy.toml"),
) -> None:
    """在项目里生成 devproxy.toml。"""
    root = detect_project_root()
    target = root / PROJECT_FILE_NAME
    if target.exists() and not force:
        s.fail(
            f"{target} 已存在。要覆盖请加 --force（覆盖前建议先看一眼现有内容）",
            EXIT_USAGE,
        )

    route_name = name or route_name_from_dir(root)
    try:
        validate_name(route_name)
    except InvalidNameError as e:
        s.fail(f"非法路由名 {route_name!r}：{e}", EXIT_USAGE)

    argv, notes = _resolve_command(root, command, from_route)

    content = render_project_file(
        [ProjectRoute(name=route_name, mode=MODE_UP, command=argv)],
        notes=notes,
    )
    try:
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        s.fail(f"写入 {target} 失败: {e}", EXIT_FAIL)

    s.console.print(f"✔ 已生成 {target}")
    s.console.print(f"  路由名: {route_name}  →  https://{route_name}.<后缀>")
    if argv:
        s.console.print(f"  启动命令: {' '.join(argv)}")
    else:
        s.console.print("  启动命令: （空）—— 请填上实际命令")
    for note in notes:
        s.console.print(f"  {note}")
    s.console.print("")
    s.console.print("下一步：")
    s.console.print("  1. 检查上面的启动命令对不对（多半要改）")
    s.console.print("  2. devproxy setup    # 只需一次：装 Caddy、写 DNS 路由、装 CA 信任")
    s.console.print("  3. devproxy up        # 起服务")
    raise typer.Exit(EXIT_OK)


def _resolve_command(
    root: Path, command: str | None, from_route: str | None
) -> tuple[list[str], list[str]]:
    """决定 command 怎么来，并给出要写进文件注释里的提示。

    优先级：显式 --command > --from（导入已有路由）> 按项目类型猜。
    """
    if command:
        # --command 是给人敲的一行 —— 按 shell 的词法拆（引号、空格都认）。
        import shlex

        try:
            argv = shlex.split(command)
        except ValueError as e:
            s.fail(f"--command 解析失败: {e}", EXIT_USAGE)
        if not argv:
            s.fail("--command 不能为空", EXIT_USAGE)
        return argv, []

    if from_route:
        imported, error = _import_command(from_route)
        if error:
            s.fail(error, EXIT_USAGE)
        return imported or [], [f"启动命令是从已注册的路由 {from_route} 导入的"]

    suggested, reason = suggest_command(root)
    if suggested is None:
        return [], [f"{reason}"]
    return suggested, [f"{reason} —— 请确认它就是你要的启动命令"]


def _import_command(route_name: str) -> tuple[list[str] | None, str]:
    """从全局路由表里取一条路由的启动命令。

    路由表是**全局**的（所有项目的路由都在一张表里），所以只能按名字精确取 ——
    不做「把当前所有路由都导进来」这种事，那会把别的项目的路由也写进文件。
    """
    paths, _, _, _ = s.build_context()
    try:
        table = load_table(paths.routes_json)
    except RouteTableError as e:
        return None, str(e)
    route = table.get(route_name)
    if route is None:
        known = "、".join(sorted(r.name for r in table.routes)) or "（路由表是空的）"
        return None, f"路由表里没有 {route_name!r}。已注册的路由：{known}"
    if not route.command:
        return None, (f"路由 {route_name!r} 没有启动命令（expose 模式的路由只记上游端口）")
    return list(route.command), ""
