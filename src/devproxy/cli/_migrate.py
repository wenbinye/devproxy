from __future__ import annotations

from pathlib import Path
import sys

from rich.markup import escape
import typer

from devproxy.cli._shared import EXIT_DECLINED
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_SUDO
from devproxy.lifecycle.migrate import LegacyState
from devproxy.lifecycle.migrate import MigrationError
from devproxy.lifecycle.migrate import migrate
from devproxy.lifecycle.migrate import rollback as do_rollback

from devproxy.cli import _shared as s

def _migrate_legacy(
    platform,
    paths,
    config,
    state: LegacyState,
    *,
    yes: bool,
    backend: str | None = None,
    notes: list[str] | None = None,
) -> None:
    """把遗留的 caddy 服务迁到本工具管理之下。

    迁移会改写用户的单元与 Caddyfile —— 非交互环境必须显式 --yes。
    """
    s.console.print("[yellow]检测到遗留的 caddy 服务（非本工具管理）[/yellow]")
    s.console.print(f"  单元: {state.unit_path}")
    s.console.print(f"  配置: {state.config_path}")
    s.console.print(f"  环境变量: {state.env or '无'}")
    s.console.print("将执行：备份 → 拆出个人站点到 sites.caddy → 改写单元 → 重启")
    if not yes and not sys.stdin.isatty():
        s.err_console.print("非交互环境且未指定 --yes，拒绝迁移。")
        raise typer.Exit(EXIT_SUDO)
    if not yes and s._interactive():
        if not typer.confirm("即将迁移上述遗留环境（会改写单元与 Caddyfile），继续？", default=True):
            s.console.print("已取消：什么都没改（迁移未开始）。")
            raise typer.Exit(EXIT_DECLINED)
    collected = list(notes or [])
    try:
        backup = migrate(
            platform,
            paths,
            config,
            state,
            dnsmasq_conf=s.DNSMASQ_CONF.expanduser(),
            dns_route=s.default_dns_route(platform, paths, config, backend),
            backend=backend,
            notes=collected,
        )
    except MigrationError as e:
        # 失败路径也要把提示打出来：用户得知道丢了哪些块、sites.caddy 有没有被覆盖
        s._print_notes(collected)
        s.fail(str(e), EXIT_FAIL)
    s._print_notes(collected)
    s.console.print(f"已备份到: {backup.directory}")
    s.console.print(f"回滚命令: devproxy setup --rollback {backup.directory}")

def _do_rollback(directory: Path) -> None:
    """按备份目录回滚。"""
    from devproxy.core.backup import BackupRestoreError, load_backup

    try:
        backup = load_backup(directory)
    except (OSError, ValueError) as e:
        s.fail(f"无法读取备份目录 {directory}: {e}", EXIT_FAIL)
    try:
        touched = do_rollback(backup, platform=s._platform())
    except BackupRestoreError as e:
        # 部分条目失败时必须把"已还原了哪些"如实报出来 —— 异常一抛，
        # 用户就完全不知道机器现在是什么状态了。
        s.err_console.print(f"devproxy: 回滚部分失败（{escape(str(e))}）")
        for p in e.restored:
            s.console.print(f"  ✔ 已还原 {escape(str(p))}")
        for p, exc in e.failures:
            s.err_console.print(f"  ✗ {escape(str(p))}: {escape(str(exc))}")
        raise typer.Exit(EXIT_FAIL)
    s.console.print(f"已回滚 {len(touched)} 个文件：")
    for p in touched:
        s.console.print(f"  · {escape(str(p))}")
    # 单元换了但进程没重启的话，跑的还是迁移后的配置
    try:
        s._platform().restart_service("caddy")
    except Exception:  # noqa: BLE001 —— 服务本来就不存在时不必报错
        pass
