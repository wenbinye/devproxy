"""终端渲染：状态表格、提示文案与执行结果汇总。

渲染只消费结构化数据（Status / ComponentReport），不做配置解析，也不
决定退出码。
"""

from __future__ import annotations

from rich.markup import escape
from rich.table import Table
import typer

from devproxy.cli._runtime import EXIT_FAIL
from devproxy.cli._runtime import EXIT_SUDO
from devproxy.cli._runtime import console
from devproxy.cli._runtime import err_console

TRUST_HINT_ROUTE = (
    "提示：CA 根证书已生成，但还没被信任 —— 现在 curl/Chrome 访问会报证书错误。"
    "请运行 devproxy trust 让它们信任它。"
)

UP_EXAMPLE = "devproxy up demo -- sh -c 'python3 -m http.server $PORT'"


def _print_trust_hint_if_needed(paths, config) -> None:
    """CA 已生成但还没被信任时提示一次。

    CA 是**第一条路由注册时**才生成的 —— 第一次 up/expose 之后 curl 与
    Chrome 一定不信任这个证书。不说清楚，用户只会看到"浏览器报不安全"，
    根本不知道还差一步 `devproxy trust`。

    CA 还不存在（没什么可信任）或已被信任时都不提示 —— 后者否则每次
    运行都会冒出来，变成噪音。

    组件与平台经 `_shared` 在调用时查找，测试的替身才能生效。
    """
    from devproxy.cli import _shared as s

    comp = s.CertComponent(s._platform(), paths, config)
    try:
        if comp.fully_trusted():
            return
    except Exception:  # noqa: BLE001 —— 提示失败不该影响主流程
        return
    if not comp.ca_path.exists():
        return
    console.print(TRUST_HINT_ROUTE)


def _has_notice(report) -> bool:
    """ok 但 detail 是“还没到那一步”的提示时返回 True。

    判据来自组件自身的结构化状态（CA 根证书是否存在），不看 detail
    文案 —— 文案随时可能改，改一个字就悄悄改变控制流。
    """
    status = report.status
    if not status.detail:
        return False
    component = getattr(report, "component", None)
    ca_path = getattr(component, "ca_path", None)
    if ca_path is None:
        return False
    try:
        return not ca_path.exists()
    except OSError:
        return False


def _status_cell(report) -> str:
    """表格里的“现状”列。

    ok 但 detail 是一条提示（如“尚无 CA 根证书…”）时必须如实展示 ——
    只写“✔ 就绪”会把“还需要 devproxy trust”这一关键信息藏起来。
    """
    status = report.status
    if not status.ok:
        return "✗ " + escape(status.detail)
    if _has_notice(report):
        return "✔ " + escape(status.detail)
    return "✔ 就绪"


def render_plan(plan) -> None:
    """打印变更清单表格。"""
    table = Table(show_header=True, header_style="bold")
    table.add_column("组件")
    table.add_column("现状")
    table.add_column("将要执行")
    for r in plan.reports:
        table.add_row(
            r.name,
            _status_cell(r),
            "\n".join(escape(a.description) for a in r.actions) or "—",
        )
    console.print(table)


def print_scope_notes(plan) -> None:
    """打印整机作用域披露（表格放不下，单独一遍）。"""
    for note in _global_scope_notes(plan):
        console.print(f"[yellow]{escape(note)}[/yellow]")


def _global_scope_notes(plan) -> list[str]:
    """整机作用域的披露文案。

    变更清单的表格放不下这类话（Rich 会截断），而它恰恰是最该让用户看到的：
    `DNS=` 改的是**整台机器**的解析路径。组件通过可选的 `global_scope_note()`
    提供文案 —— 没有就说明这个组件不涉及整机。

    只要整机作用域真的成立就披露 —— 那台机器此刻就处在"整机解析指向用户级
    dnsmasq"的状态里，用户有权知道。两种情况算数：这次要写它（r.actions 非空），
    或者它早就写好了（组件状态正常）。

    单个组件的探测异常不能带崩 setup（build_plan 已承诺同样的隔离）：
    记一行警告后继续，宁可少一条披露，也不能让用户在确认之前看到 traceback。
    """
    notes = []
    for r in plan.reports:
        fn = getattr(r.component, "global_scope_note", None)
        if not callable(fn):
            continue
        if not r.actions and not r.status.ok:
            # 既没在改、也没装好 —— 谈不上"整机解析已经指向 dnsmasq"。
            continue
        try:
            note = fn()
        except Exception as e:  # noqa: BLE001 —— 披露失败不阻断 setup
            err_console.print(
                f"devproxy: 无法获取 {escape(r.name)} 的整机作用域说明：{escape(str(e))}"
            )
            continue
        if note:
            notes.append(note)
    return notes


def print_noop_unhealthy(reports) -> None:
    """没有可执行动作、但组件不健康时，逐条报出原因与修复提示。"""
    for r in reports:
        err_console.print(f"devproxy: {r.name}: {escape(r.status.detail)}")
        if r.status.hint:
            err_console.print(f"    修复: {escape(r.status.hint)}")


def print_apply_results(results) -> bool:
    """打印逐组件执行结果；返回是否全部成功。"""
    ok = True
    for name, status in results:
        mark = "✔" if status.ok else "✗"
        console.print(f"{mark} {name}: {escape(status.detail)}")
        if not status.ok:
            ok = False
            if status.hint:
                console.print(f"    修复: {escape(status.hint)}")
    return ok


def fail_for_apply_errors(errors: list[tuple[str, PermissionError]]) -> None:
    """执行阶段失败时选择退出码。

    `apply_plan` 把异常类型压成了 Status（单组件失败不阻断），提权失败
    只能由调用方在 apply 外层记录。有 PermissionError 就是“需要提权”
    （退出码 4），否则是普通运行失败（退出码 1）。
    """
    if not errors:
        raise typer.Exit(EXIT_FAIL)
    err_console.print("devproxy: 提权失败，变更未完成：")
    for name, exc in errors:
        err_console.print(f"  · {name}: {escape(str(exc))}")
    err_console.print(
        "修复: 确认当前用户可执行 sudo（且不要用 sudo/root 运行 devproxy），"
        "然后重新运行 devproxy setup。"
    )
    raise typer.Exit(EXIT_SUDO)


def _print_notes(notes: list[str]) -> None:
    """把迁移过程中的提示逐条打出来。"""
    for note in notes:
        console.print(f"  · {note}")


def render_uninstall_plan(plan, *, dry_run: bool) -> None:
    """打印卸载清单。dry-run 时明确标注"只是预览"。"""
    if dry_run:
        console.print("[bold]将要执行以下卸载步骤（预览，不会执行任何变更）：[/bold]")
    else:
        console.print("[bold]将要执行以下卸载步骤：[/bold]")
    if plan.actions:
        for index, action in enumerate(plan.actions, 1):
            sudo = "（需要 sudo）" if action.privileged else ""
            console.print(f"  {index}. {escape(action.description)}{sudo}")
    else:
        console.print("  （没有可执行的步骤）")
    for note in plan.notes:
        console.print(f"  [yellow]⚠ {escape(note)}[/yellow]")


def print_uninstall_results(results) -> bool:
    """逐条打印 ✔/✗；返回是否全部成功。"""
    ok = True
    for result in results:
        mark = "✔" if result.ok else "✗"
        line = f"{mark} {escape(result.description)}"
        if result.detail:
            line += f"：{escape(result.detail)}"
        console.print(line)
        if not result.ok:
            ok = False
    return ok


def print_uninstall_kept(plan, *, dry_run: bool = False) -> None:
    """打印"默认不动"的清单与迁移回滚提示。"""
    if plan.kept:
        heading = (
            "以下内容将保留（不会删除）："
            if dry_run
            else "以下内容已保留（未删除）："
        )
        console.print(heading)
        for item in plan.kept:
            console.print(f"  · {escape(item)}")
    if plan.migration_backup is not None:
        path = escape(str(plan.migration_backup))
        console.print(
            f"原始 caddy.service 与 Caddyfile 在 {path}，"
            f"可用 devproxy setup --rollback {path} 还原"
        )
