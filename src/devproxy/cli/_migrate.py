"""遗留环境迁移与回滚的命令层编排。"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from rich.markup import escape
import typer

from devproxy.cli._shared import EXIT_DECLINED
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_SUDO
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.lifecycle.migrate import LegacyState
from devproxy.lifecycle.migrate import MigrationError
from devproxy.lifecycle.migrate import migrate
from devproxy.lifecycle.migrate import rollback as do_rollback
from devproxy.platform.base import Platform

from devproxy.cli import _shared as s


@dataclass
class MigrateRequest:
    """一次遗留环境迁移的输入。

    参数收敛在请求对象里，调用点不必记住一长串位置/关键字参数；
    `backend` 已折进 `config.dns_backend`，迁移与组件看到的是同一个值。
    """

    platform: Platform
    paths: Paths
    config: Config
    state: LegacyState
    yes: bool = False
    notes: list[str] = field(default_factory=list)


def _migrate_legacy(req: MigrateRequest) -> None:
    """把遗留的 caddy 服务迁到本工具管理之下。

    迁移会改写用户的单元与 Caddyfile —— 非交互环境必须显式 --yes。
    """
    s.console.print("[yellow]检测到遗留的 caddy 服务（非本工具管理）[/yellow]")
    s.console.print(f"  单元: {req.state.unit_path}")
    s.console.print(f"  配置: {req.state.config_path}")
    s.console.print(f"  环境变量: {req.state.env or '无'}")
    s.console.print("将执行：备份 → 拆出个人站点到 sites.caddy → 改写单元 → 重启")
    interactive = s._interactive()
    if not req.yes and not interactive:
        s.err_console.print("非交互环境且未指定 --yes，拒绝迁移。")
        raise typer.Exit(EXIT_SUDO)
    if not req.yes and interactive:
        if not typer.confirm("即将迁移上述遗留环境（会改写单元与 Caddyfile），继续？", default=True):
            s.console.print("已取消：什么都没改（迁移未开始）。")
            raise typer.Exit(EXIT_DECLINED)
    collected = list(req.notes)
    try:
        backup = migrate(
            req.platform,
            req.paths,
            req.config,
            req.state,
            dnsmasq_conf=s.DNSMASQ_CONF.expanduser(),
            dns_route=s.default_dns_route(req.platform, req.paths, req.config),
            notes=collected,
        )
    except MigrationError as e:
        # 失败路径也要把提示打出来：用户得知道丢了哪些块、sites.caddy 有没有被覆盖
        s._print_notes(collected)
        s.fail(str(e), EXIT_FAIL)
    s._print_notes(collected)
    s.console.print(f"已备份到: {backup.directory}")
    s.console.print(f"回滚命令: devproxy setup --rollback {backup.directory}")


def _caddy_unit_exists(platform) -> bool:
    """回滚后是否还有 caddy 服务单元。

    没有单元 = 迁移前本机就没有这个服务，谈不上“重启”。判断不了
    （平台探测异常）时按存在处理 —— 宁可尝试重启并如实报错，也不要
    静默跳过。
    """
    try:
        return platform.service_unit_path("caddy").exists()
    except (OSError, RuntimeError):
        return True


def _restart_caddy_after_rollback(platform, restored_count: int) -> None:
    """重启 Caddy，让进程真正用上刚还原的单元。

    区分两种失败：
    - 服务本来就不存在（没有单元）：可忽略；
    - 重启失败：文件已还原，但进程可能仍跑迁移后的配置 —— 必须报出来，
      并明确“回滚本身已完成”，不能把它说成回滚失败。
    """
    if not _caddy_unit_exists(platform):
        s.console.print("（迁移前没有 caddy 服务单元，跳过重启）")
        return
    try:
        platform.restart_service("caddy")
    except PermissionError as e:
        s.err_console.print(
            f"devproxy: 已还原 {restored_count} 个文件，但重启 caddy 需要提权而失败："
            f"{escape(str(e))}"
        )
        s.err_console.print(
            "   Caddy 可能仍在运行迁移后的配置。修复: 以普通用户重新运行 "
            "devproxy setup --rollback <备份目录>，或手动重启 caddy。"
        )
        raise typer.Exit(EXIT_SUDO)
    except Exception as e:  # noqa: BLE001 —— 重启失败必须报出来，不能吞
        s.err_console.print(
            f"devproxy: 已还原 {restored_count} 个文件，但重启 caddy 失败："
            f"{escape(str(e))}"
        )
        s.err_console.print(
            "   Caddy 可能仍在运行迁移后的配置（或已被停掉）。修复: 手动执行 "
            "systemctl --user restart caddy（macOS: launchctl kickstart -k），"
            "或检查还原后的单元文件是否有效。"
        )
        raise typer.Exit(EXIT_FAIL)


def _do_rollback(directory: Path) -> None:
    """按备份目录回滚。"""
    from devproxy.core.backup import BackupRestoreError, load_backup

    try:
        backup = load_backup(directory)
    except (OSError, ValueError) as e:
        s.fail(f"无法读取备份目录 {directory}: {e}", EXIT_FAIL)
    platform = s._platform()
    try:
        touched = do_rollback(backup, platform=platform)
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
    _restart_caddy_after_rollback(platform, len(touched))
