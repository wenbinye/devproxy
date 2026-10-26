"""Backup and rollback: a snapshot before migration, plus failure/manual rollback."""

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
    """Copy function for backups: copy regular files directly; unreadable ones (under /etc) go through an elevated session."""

    def copy(src: Path, dest: Path) -> None:
        try:
            shutil.copy2(src, dest)
        except PermissionError:
            # Drop-ins under /etc are owned by root: read the content via an
            # elevated session, then write it into the backup directory as the
            # user (otherwise the backup copy is also owned by root and the user
            # cannot delete it)
            text = platform.privilege().read(src)
            dest.write_text(text, encoding="utf-8")

    return copy


def _make_restorer(platform: Platform):
    """File operations for restore: src is None means delete dest.

    Files under /etc cannot be written or deleted by an ordinary user -- use an
    elevated session when permissions are insufficient.
    """

    def restore(src: Path | None, dest: Path) -> None:
        if src is None:
            try:
                dest.unlink(missing_ok=True)
                return
            except PermissionError as e:
                code, msg = platform.privilege().run(["rm", "-f", str(dest)])
                if code != 0:
                    raise OSError(f"failed to delete {dest}: {msg}") from e
                return
        try:
            shutil.copy2(src, dest)
        except PermissionError:
            platform.privilege().write(dest, src.read_text(encoding="utf-8"))

    return restore


def _create_backup(platform: Platform, paths: Paths, targets: list[Path]) -> Backup:
    """Back up all files that will be modified. Failure aborts migration."""
    try:
        return BackupManager(paths.backup_dir, copier=_make_copier(platform)).create(targets)
    except ValueError as e:
        raise MigrationError(
            f"backup target is not a regular file (possibly a symlink or directory); "
            f"migration aborted: {e}"
        ) from e
    except OSError as e:
        raise MigrationError(f"backup failed; migration aborted (no files changed): {e}") from e


def _restore_after_failure(
    platform: Platform,
    backup: Backup,
    exc: BaseException,
    *,
    notes: list[str] | None = None,
) -> None:
    """Rewrite failed/interrupted -> restore from backup; never leave the machine half-migrated.

    `BaseException` is deliberate: Ctrl-C (KeyboardInterrupt) is not an
    Exception, and catching only Exception would leave the user exiting in a
    half-migrated state ("sites.caddy written, unit not yet swapped") with no
    idea where the backup is. Interruptions follow the same failure path:
    restore, then convert to a MigrationError carrying the backup path (CLI
    prints it as one readable line).

    `notes` are the notes collected before the failure; they are handed to the
    caller via MigrationError for printing.
    """
    if isinstance(exc, KeyboardInterrupt):
        reason = "migration interrupted (KeyboardInterrupt)"
    else:
        reason = f"migration failed: {exc}"
    hint = (
        f"backup directory: {backup.directory} "
        f"(manual restore: devproxy setup --rollback {backup.directory})"
    )
    try:
        backup.restorer = _make_restorer(platform)
        backup.restore()
    except BackupRestoreError as restore_exc:
        raise MigrationError(
            f"{reason}, and the restore was incomplete: {restore_exc}; {hint}", notes=notes
        ) from exc
    except OSError as restore_exc:
        raise MigrationError(
            f"{reason}, and the restore failed: {restore_exc}; {hint}", notes=notes
        ) from exc
    raise MigrationError(f"{reason}; restored from backup; {hint}", notes=notes) from exc


def rollback(backup: Backup, *, platform: Platform | None = None) -> list[Path]:
    """Restore from backup. Returns the original paths that were restored (or deleted).

    Raises BackupRestoreError when some entries fail -- the caller must catch
    it, otherwise "what has been restored" is lost along with the exception.
    """
    if platform is not None:
        backup.restorer = _make_restorer(platform)
    return backup.restore()
