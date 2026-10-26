from __future__ import annotations

import os
from pathlib import Path
from shutil import which

from rich.markup import escape
from rich.text import Text
import typer

from devproxy.cli import app
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_SUDO
from devproxy.cli._shared import EXIT_USAGE
from devproxy.core.logs import follow_log
from devproxy.core.logs import follow_many
from devproxy.gateway.project import ProjectFileError
from devproxy.gateway.project import find_project_file
from devproxy.gateway.project import load_project_file
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


def _declared_route_names() -> tuple[set[str] | None, Path | None]:
    """当前项目（devproxy.toml）声明了哪些路由。

    返回 (名字集合, 项目文件路径)。不在项目里返回 (None, None)；
    项目文件坏了返回 (None, 路径) —— 调用方提示后按"无项目"处理。
    """
    found = find_project_file()
    if found is None:
        return None, None
    try:
        project = load_project_file(found)
    except ProjectFileError as e:
        s.err_console.print(f"⚠️  {escape(str(e))} —— 按全部路由列出")
        return None, found
    return {route.name for route in project.routes}, found


def _log_line(path: Path) -> str:
    try:
        size = _format_size(path.stat().st_size)
    except OSError:
        return f"{path.stem:<20} （还没有日志）"
    return f"{path.stem:<20} {size:>8}"


_PALETTE = ("cyan", "magenta", "yellow", "green", "blue", "bright_red",
            "bright_cyan", "bright_magenta", "bright_yellow", "bright_green")


def _follow_project(project_path: Path, names: list[str]) -> None:
    """跟随本项目所有应用的日志。一个就是 tail -f；多个带彩色标签。"""
    paths, _, _, _ = s.build_context()
    targets: list[tuple[str, Path]] = []
    missing: list[str] = []
    for name in names:
        path = route_log_path(paths, name)
        if path.exists():
            targets.append((name, path))  # 标签就是应用名
        else:
            missing.append(name)

    if not targets:
        s.err_console.print(
            f"devproxy: 本项目声明了 {'、'.join(names)}，但还没有日志 —— 跑一次 devproxy up"
        )
        raise typer.Exit(EXIT_USAGE)
    if missing:
        s.err_console.print(
            f"   已声明但还没有日志（跳过）：{'、'.join(missing)}"
        )

    try:
        if len(targets) == 1:
            follow_log(targets[0][1])
        else:
            colors = {
                tag: _PALETTE[i % len(_PALETTE)] for i, (tag, _) in enumerate(targets)
            }
            width = max(len(tag) for tag, _ in targets)
            s.console.print("跟随中（Ctrl-C 退出）：")
            for tag, _ in targets:
                # 名字必须转义：rich 会把方括号内容当标记解析。
                # 不带路径 —— 长路径会被折行，且名字本身就是身份
                s.console.print(f"  {escape(f'[{tag}]')}", style=colors[tag])
            for tag, line in follow_many(targets):
                text = Text()
                # 名字补齐到同一宽度，几行日志能纵向对齐
                text.append(f"[{tag}]".ljust(width + 2) + " ", style=colors[tag])
                text.append(line.rstrip("\n"))
                s.console.print(text)
    except KeyboardInterrupt:
        # Ctrl-C：正常退出方式，不是错误
        raise typer.Exit(0)


def _print_one(service: str) -> None:
    """`logs <名字> --list`：只看这一个目标的信息。"""
    if service in _SERVICE_LOGS:
        s.console.print(f"服务日志：{service}（用 devproxy logs {service} 跟随）")
        return
    known = _known_route_logs()
    paths, _, _, _ = s.build_context()
    path = route_log_path(paths, service)
    if service in known:
        s.console.print(f"路由日志：{_log_line(path)}")
        s.console.print(f"  完整路径：{path}")
        s.console.print(f"  跟随：devproxy logs {service}")
        return
    s.err_console.print(
        f"devproxy: 没有叫 {service} 的日志"
        f"（可选：{' / '.join(_SERVICE_LOGS)}，或路由名）"
    )
    if known:
        s.err_console.print(f"   可用的路由日志：{'、'.join(known)}")
    raise typer.Exit(EXIT_USAGE)


# 找不到 EDITOR 时的候选：按顺序探测 PATH，用第一个存在的
_EDITOR_CANDIDATES = ("vim", "vi", "nano")


def _resolve_editor() -> str:
    """用哪个编辑器：$EDITOR 优先，其次 $VISUAL，都没有就找 vim / vi / nano。

    候选是**探测**出来的（which），不是硬塞 —— 硬塞一个用户机器上根本没装的
    编辑器，只会换来一句 "command not found"。三个都没有才报错退出 2。
    """
    editor = (os.environ.get("EDITOR") or os.environ.get("VISUAL") or "").strip()
    if editor:
        return editor
    for candidate in _EDITOR_CANDIDATES:
        if which(candidate):
            return candidate
    s.err_console.print(
        "devproxy: 不知道用哪个编辑器打开日志 —— 没有设置 EDITOR（或 VISUAL），"
        "PATH 里也没有 vim / vi / nano。\n"
        "   例：export EDITOR=vim   然后重跑 devproxy logs <名字> --edit"
    )
    raise typer.Exit(EXIT_USAGE)


def _edit_log(service: str) -> None:
    """`logs <名字> --edit`：用 $EDITOR 打开日志文件。"""
    if service in _SERVICE_LOGS:
        s.err_console.print(
            f"devproxy: {service} 是服务日志，不是文件，编辑器打不开"
            f"（用 devproxy logs {service} 跟随）"
        )
        raise typer.Exit(EXIT_USAGE)

    known = _known_route_logs()
    if service not in known:
        s.err_console.print(
            f"devproxy: 没有叫 {service} 的日志"
            f"（可选：{' / '.join(_SERVICE_LOGS)}，或路由名）"
        )
        if known:
            s.err_console.print(f"   可用的路由日志：{'、'.join(known)}")
        raise typer.Exit(EXIT_USAGE)

    editor = _resolve_editor()
    paths, _, _, _ = s.build_context()
    path = route_log_path(paths, service)
    try:
        code = s._platform().open_in_editor(path, editor)
    except FileNotFoundError as e:
        s.err_console.print(f"devproxy: 打不开编辑器 {editor}：{escape(str(e))}")
        raise typer.Exit(EXIT_FAIL)
    except OSError as e:
        s.err_console.print(f"devproxy: 打开日志失败：{escape(str(e))}")
        raise typer.Exit(EXIT_FAIL)
    if code != 0:
        s.err_console.print(f"devproxy: 编辑器 {editor} 退出码 {code}")
        raise typer.Exit(EXIT_FAIL)


def _print_list(project_names: set[str] | None, project_path: Path | None) -> None:
    """列出能跟的日志目标。

    在项目里（devproxy.toml 存在）只列**本项目声明**的路由 —— 机器上的
    路由表是全局的，别的项目的日志混进来只会碍事。
    """
    in_project = project_names is not None
    s.console.print("服务日志：")
    for name in _SERVICE_LOGS:
        s.console.print(f"  {name}")

    files = _route_log_files()
    if project_names is not None:
        files = [f for f in files if f.stem in project_names]

    if not files:
        if in_project:
            s.console.print(
                "路由日志：（本项目声明了 "
                f"{'、'.join(sorted(project_names))}，但还没有日志 —— 跑一次 devproxy up）"
            )
        else:
            s.console.print("路由日志：（没有 —— 跑一次 devproxy up 就会有）")
        s.console.print("")
        s.console.print("用 devproxy logs <名字> 跟随。")
        return

    # 标题行不带长路径（Rich 80 列会折行，名字与大小就对不齐）；
    # 每个日志的完整路径单独成行 —— 折了也不影响上面对齐，
    # 用户要复制路径（喂给编辑器、tail、grep）时也不用自己拼
    if in_project:
        s.console.print(f"本项目（devproxy.toml：{project_path}）：", soft_wrap=True)
    else:
        s.console.print("路由日志：")
    for path in files:
        try:
            size = _format_size(path.stat().st_size)
        except OSError:
            size = "?"
        s.console.print(f"  {path.stem:<20} {size:>8}")
        # soft_wrap：不折行。路径被 Rich 折成两行就没法复制了（终端自己会软换行，
        # 但流里仍是一整行）
        s.console.print(f"    {path}", soft_wrap=True)

    # 项目里声明了、但还没起过（没有日志文件）的路由也提示一下
    if in_project:
        have = {f.stem for f in files}
        missing = sorted(project_names - have)
        if missing:
            s.console.print(f"  已声明但还没有日志：{'、'.join(missing)}（跑一次 devproxy up）")

    s.console.print("")
    s.console.print("用 devproxy logs <名字> 跟随。")


@app.command()
def logs(
    service: str = typer.Argument(None, help="caddy|dnsmasq|<路由名>（默认 caddy）"),
    list_all: bool = typer.Option(
        False, "--list", help="列出能跟的日志目标（项目里只列本项目的），不跟随"
    ),
    edit: bool = typer.Option(
        False,
        "--edit",
        "-e",
        help="用 $EDITOR 打开日志文件（只对路由日志有效；没设 EDITOR 就依次找 vim / vi / nano）",
    ),
) -> None:
    """跟踪日志：服务（caddy / dnsmasq）或路由（sync 起的应用）。"""
    if list_all and edit:
        s.err_console.print("devproxy: --list 与 --edit 不能同时用")
        raise typer.Exit(EXIT_USAGE)

    if list_all:
        if service:
            _print_one(service)
            raise typer.Exit(0)
        project_names, project_path = _declared_route_names()
        _print_list(project_names, project_path)
        raise typer.Exit(0)

    if edit:
        if not service:
            s.err_console.print(
                "devproxy: --edit 要指定打开哪个日志（devproxy logs <名字> --edit）"
            )
            raise typer.Exit(EXIT_USAGE)
        _edit_log(service)
        raise typer.Exit(0)

    if service is None:
        # 裸 `devproxy logs`：项目里跟本项目的**所有应用**；项目外跟 caddy（老行为）
        found = find_project_file()
        if found is not None:
            try:
                project = load_project_file(found)
            except ProjectFileError as e:
                s.err_console.print(f"⚠️  {escape(str(e))} —— 改为跟随 caddy")
            else:
                names = [route.name for route in project.routes]
                _follow_project(found, names)
                raise typer.Exit(0)
        service = "caddy"

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
            s.err_console.print("   （当前没有路由日志 —— 跑一次 devproxy up 就会有）")
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
