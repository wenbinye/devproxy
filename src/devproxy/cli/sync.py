"""sync 命令：按项目根目录的 devproxy.toml 同步路由声明。

只做展示与退出码 —— 计划与执行在 `gateway.sync`，文件解析在
`gateway.project`。
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.markup import escape

from devproxy.cli import app
from devproxy.cli._shared import EXIT_DECLINED
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_PRECONDITION
from devproxy.cli._shared import EXIT_USAGE
from devproxy.gateway.project import ProjectFileError
from devproxy.gateway.project import load_project_file
from devproxy.gateway.project import resolve_project_file
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.store import RouteStoreError

from devproxy.cli import _shared as s

_STATUS_MARKS = {
    "created": "✅ 创建",
    "skipped": "➖ 跳过",
    "conflict": "⚠️  冲突",
    "failed": "✗ 失败",
    "pruned": "🧹 移除",
}


def run_project_sync(
    *, file: Path | None = None, prune: bool = False, yes: bool = False
) -> None:
    """按项目根目录的 devproxy.toml 应用路由声明（`devproxy up` 的实现）。

    由 cli/up.py 在「没给路由名」时调用 —— compose 的 `up -d` 语义。
    """
    # 1) 定位并解析项目文件 —— 找不到 / 写错了都是「用法错误」（退出码 2）
    try:
        path = resolve_project_file(file)
        project = load_project_file(path)
    except ProjectFileError as e:
        s.fail(str(e), EXIT_USAGE)

    paths, config, store, admin = s.build_context()
    s._require_caddyfile(paths)
    # sync 要往 Caddy 里注册活路由 —— Caddy 没跑属于前置条件不满足（3）
    if not admin.is_alive():
        s.fail(
            "Caddy 未运行（Admin API 不可达），请先执行 devproxy setup 或启动 Caddy",
            EXIT_PRECONDITION,
        )
    try:
        existing = store.list()
    except RouteTableError as e:
        s.fail(str(e))

    plan = s.build_sync_plan(project, existing, prune)

    # 2) --prune 会删路由 —— 先确认，再动手（确认前什么都没改）
    if plan.prune:
        names = "、".join(r.name for r in plan.prune)
        interactive = s._interactive() and not yes
        if not interactive and not yes:
            s.err_console.print(
                "非交互环境且未指定 --yes，拒绝执行 --prune"
                f"（将移除：{names}）。"
            )
            raise typer.Exit(EXIT_DECLINED)
        if interactive and not s.typer.confirm(
            f"即将移除本文件里已删掉的路由：{names}（子进程也会一并收掉），继续？",
            default=False,
        ):
            s.console.print("已取消：什么都没改。")
            raise typer.Exit(EXIT_DECLINED)

    s.console.print(f"配置文件：{escape(str(project.path))}")
    s.console.print("")

    # 3) 执行
    try:
        outcomes = s.execute_sync_plan(
            project, plan, store=store, config=config, paths=paths
        )
    except (RouteStoreError, RouteTableError) as e:
        s.fail(str(e))

    failed = 0
    conflicts = 0
    created = 0
    skipped = 0
    pruned = 0
    for outcome in outcomes:
        mark = _STATUS_MARKS.get(outcome.status, outcome.status)
        line = f"{mark} {escape(outcome.name)}"
        if outcome.domain:
            line += f" → https://{escape(outcome.domain)}"
        if outcome.detail:
            line += f" —— {escape(outcome.detail)}"
        s.console.print(line)
        if outcome.log_path is not None and outcome.status == "created":
            s.console.print(f"   日志：{escape(str(outcome.log_path))}")
        if outcome.hint:
            s.console.print(f"   修复：{escape(outcome.hint)}")
        if outcome.status == "created":
            created += 1
        elif outcome.status == "skipped":
            skipped += 1
        elif outcome.status == "conflict":
            conflicts += 1
        elif outcome.status == "failed":
            failed += 1
        elif outcome.status == "pruned":
            pruned += 1

    s.console.print("")
    if created:
        # CA 是在第一条路由注册时才生成的 —— 和 up/expose 一样提示一次
        s._print_trust_hint_if_needed(paths, config)
    if not outcomes:
        s.console.print("文件里没有声明任何路由。")
        raise typer.Exit(EXIT_OK)
    if created == 0 and skipped and not conflicts and not failed and not pruned:
        s.console.print(f"全部已是最新（{skipped} 条声明）。")
        raise typer.Exit(EXIT_OK)
    s.console.print(
        f"同步完成：创建 {created}，跳过 {skipped}，冲突 {conflicts}，"
        f"失败 {failed}，移除 {pruned}。"
    )
    if conflicts:
        s.err_console.print(
            f"{conflicts} 条路由与文件声明不一致 —— 已报告冲突，未做任何改动"
            "（工具绝不自动杀进程重启）。"
        )
    if failed:
        s.err_console.print(f"{failed} 条路由启动失败 —— 详见上面的错误信息。")
        raise typer.Exit(EXIT_FAIL)
    if conflicts:
        raise typer.Exit(EXIT_FAIL)
    raise typer.Exit(EXIT_OK)
