"""备份与回滚：迁移前的快照，以及失败/手动回滚。"""

from __future__ import annotations

import shutil
from pathlib import Path

from devproxy.core.backup import Backup
from devproxy.core.backup import BackupManager
from devproxy.core.backup import BackupRestoreError
from devproxy.core.paths import Paths
from devproxy.lifecycle.migrate.legacy import MigrationError
from devproxy.platform.base import Platform


def _make_copier(platform: Platform):
    """备份用的复制函数：普通文件直接复制，读不到的（/etc 下的）走提权会话。"""

    def copy(src: Path, dest: Path) -> None:
        try:
            shutil.copy2(src, dest)
        except PermissionError:
            # /etc 下的 drop-in 是 root 拥有的：用提权会话读出内容，
            # 再以用户身份写进备份目录（否则备份副本也归 root，用户删不掉）
            text = platform.privilege().read(src)
            dest.write_text(text, encoding="utf-8")

    return copy


def _make_restorer(platform: Platform):
    """还原用的文件操作：src 为 None 表示删除 dest。

    /etc 下的文件普通用户写不了也删不了 —— 权限不够时走提权会话。
    """

    def restore(src: Path | None, dest: Path) -> None:
        if src is None:
            try:
                dest.unlink(missing_ok=True)
                return
            except PermissionError as e:
                code, msg = platform.privilege().run(["rm", "-f", str(dest)])
                if code != 0:
                    raise OSError(f"删除 {dest} 失败: {msg}") from e
                return
        try:
            shutil.copy2(src, dest)
        except PermissionError:
            platform.privilege().write(dest, src.read_text(encoding="utf-8"))

    return restore


def _create_backup(platform: Platform, paths: Paths, targets: list[Path]) -> Backup:
    """备份全部待改动的文件。失败即中止迁移。"""
    try:
        return BackupManager(paths.backup_dir, copier=_make_copier(platform)).create(targets)
    except ValueError as e:
        raise MigrationError(
            f"备份目标不是普通文件（可能是符号链接或目录），迁移已中止：{e}"
        ) from e
    except OSError as e:
        raise MigrationError(f"备份失败，迁移已中止（未改动任何文件）：{e}") from e


def _restore_after_failure(
    platform: Platform,
    backup: Backup,
    exc: BaseException,
    *,
    notes: list[str] | None = None,
) -> None:
    """改写失败/被中断 → 按备份还原，绝不把机器留在半迁移状态。

    `BaseException` 是刻意的：Ctrl-C（KeyboardInterrupt）不算 Exception，
    只接 Exception 会让用户在“写了 sites.caddy、还没换单元”的半迁移状态
    里退出，且看不到备份位置。中断同样按失败流程还原，并转成带备份
    路径的 MigrationError（CLI 会把它打印成一行可读错误）。

    `notes` 是失败前已收集的提示，随 MigrationError 一起交给调用方打印。
    """
    if isinstance(exc, KeyboardInterrupt):
        reason = "迁移被中断（KeyboardInterrupt）"
    else:
        reason = f"迁移失败：{exc}"
    hint = (
        f"备份目录：{backup.directory}（可手动还原：devproxy setup --rollback {backup.directory}）"
    )
    try:
        backup.restorer = _make_restorer(platform)
        backup.restore()
    except BackupRestoreError as restore_exc:
        raise MigrationError(f"{reason}，且还原不完整：{restore_exc}；{hint}", notes=notes) from exc
    except OSError as restore_exc:
        raise MigrationError(f"{reason}，且还原失败：{restore_exc}；{hint}", notes=notes) from exc
    raise MigrationError(f"{reason}，已按备份还原；{hint}", notes=notes) from exc


def rollback(backup: Backup, *, platform: Platform | None = None) -> list[Path]:
    """按备份还原。返回被还原（或被删除）的原始路径。

    部分条目失败时抛 BackupRestoreError —— 调用方必须捕获它，
    否则"已还原了哪些"会随异常一起丢掉。
    """
    if platform is not None:
        backup.restorer = _make_restorer(platform)
    return backup.restore()
