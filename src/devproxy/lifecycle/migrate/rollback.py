"""备份与回滚：迁移前的快照，以及失败/手动回滚。"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from devproxy.core.backup import Backup, BackupManager, BackupRestoreError
from devproxy.core.paths import Paths
from devproxy.platform.base import Platform

from devproxy.lifecycle.migrate.legacy import MigrationError

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
            except PermissionError:
                code, msg = platform.privilege().run(["rm", "-f", str(dest)])
                if code != 0:
                    raise OSError(f"删除 {dest} 失败: {msg}")
                return
        try:
            shutil.copy2(src, dest)
        except PermissionError:
            platform.privilege().write(dest, src.read_text(encoding="utf-8"))

    return restore

def _create_backup(
    platform: Platform, paths: Paths, targets: list[Path]
) -> Backup:
    """备份全部待改动的文件。失败即中止迁移。"""
    try:
        return BackupManager(paths.backup_dir, copier=_make_copier(platform)).create(
            targets
        )
    except ValueError as e:
        raise MigrationError(
            f"备份目标不是普通文件（可能是符号链接或目录），迁移已中止：{e}"
        ) from e
    except OSError as e:
        raise MigrationError(
            f"备份失败，迁移已中止（未改动任何文件）：{e}"
        ) from e

def _restore_after_failure(
    platform: Platform, backup: Backup, exc: Exception
) -> None:
    """改写失败 → 按备份还原，绝不把机器留在半迁移状态。"""
    hint = (
        f"备份目录：{backup.directory}"
        f"（可手动还原：devproxy setup --rollback {backup.directory}）"
    )
    try:
        backup.restorer = _make_restorer(platform)
        backup.restore()
    except BackupRestoreError as restore_exc:
        raise MigrationError(
            f"迁移失败且还原不完整：{exc}；还原失败：{restore_exc}；{hint}"
        ) from exc
    except OSError as restore_exc:
        raise MigrationError(
            f"迁移失败且还原失败：{exc}；还原失败：{restore_exc}；{hint}"
        ) from exc
    raise MigrationError(f"迁移失败，已按备份还原：{exc}；{hint}") from exc

def rollback(backup: Backup, *, platform: Platform | None = None) -> list[Path]:
    """按备份还原。返回被还原（或被删除）的原始路径。

    部分条目失败时抛 BackupRestoreError —— 调用方必须捕获它，
    否则"已还原了哪些"会随异常一起丢掉。
    """
    if platform is not None:
        backup.restorer = _make_restorer(platform)
    return backup.restore()
