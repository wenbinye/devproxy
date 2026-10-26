"""uninstall 命令：只做输出与退出码编排，计划与执行在 lifecycle.uninstall。"""

from __future__ import annotations

import typer

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_DECLINED
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_OK


@app.command()
def uninstall(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="只打印将要执行的步骤，不做任何变更、不需要 sudo"
    ),
    yes: bool = typer.Option(False, "--yes", help="非交互模式，不询问"),
    purge: bool = typer.Option(
        False,
        "--purge",
        help="直接删除 Caddy 存储、备份与下载的 caddy 二进制（不给就在交互时问）",
    ),
    remove_ca: bool = typer.Option(
        False,
        "--remove-ca",
        help="直接卸载 CA（不给就在交互时问；迁移来的 CA 默认保留）",
    ),
) -> None:
    """卸载 devproxy：停止服务、移除单元、DNS 路由与（可选）CA。

    默认保留你的配置、个人站点、Caddy 存储与备份。
    """
    paths, config, _, _ = s.build_context()
    platform = s._platform()

    def _plan(purge_: bool, remove_ca_: bool):
        return s.build_uninstall_plan(
            platform,
            paths,
            config,
            purge=purge_,
            remove_ca=remove_ca_,
            dry_run=dry_run,
        )

    try:
        plan = _plan(purge, remove_ca)
    except Exception as e:  # noqa: BLE001 —— 计划失败必须变成可读信息 + 退出码 1
        s.fail(str(e))

    s.render_uninstall_plan(plan, dry_run=dry_run)

    if plan.is_noop:
        s.console.print("没有可卸载的东西。")
        s.print_uninstall_kept(plan, dry_run=dry_run)
        raise typer.Exit(EXIT_OK)

    if dry_run:
        s.print_uninstall_kept(plan, dry_run=True)
        s.console.print("预览结束：未执行任何变更。")
        raise typer.Exit(EXIT_OK)

    interactive = s._interactive() and not yes
    if not interactive and not yes:
        s.err_console.print("非交互环境且未指定 --yes，拒绝执行卸载。")
        raise typer.Exit(EXIT_DECLINED)

    if interactive:
        # 两个"可选破坏面"逐项问：命令行给了就跳过，没给就问 ——
        # 免得用户为了卸载先记住两个不常用的 flag。
        chosen_purge = purge
        chosen_remove_ca = remove_ca
        if not purge:
            chosen_purge = s.typer.confirm(
                "还要删除 Caddy 存储（CA 与证书）、迁移备份与下载的 caddy 二进制吗？（不可恢复）",
                default=False,
            )
        if not remove_ca and plan.ca_kept:
            chosen_remove_ca = s.typer.confirm(
                "要把 CA 从系统信任库卸载吗？（它在本工具接管前就存在，你的站点可能仍在使用）",
                default=False,
            )
        if chosen_purge != purge or chosen_remove_ca != remove_ca:
            plan = _plan(chosen_purge, chosen_remove_ca)
            s.console.print("")
            s.render_uninstall_plan(plan, dry_run=False)
        if not s.typer.confirm("即将执行以上卸载步骤，继续？", default=False):
            s.console.print("已取消：什么都没改（未执行任何变更）。")
            raise typer.Exit(EXIT_DECLINED)

    results = s.apply_uninstall_plan(plan)
    ok = s.print_uninstall_results(results)
    s.print_uninstall_kept(plan)
    if not ok:
        raise typer.Exit(EXIT_FAIL)
    s.console.print("卸载完成。")
    raise typer.Exit(EXIT_OK)
