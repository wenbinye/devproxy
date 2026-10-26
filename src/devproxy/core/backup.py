"""Forced backup and rollback before migration.

Backup directories carry a UTC timestamp so multiple migrations do not
overwrite each other; they are append-only and left for the user to clean up.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

# Copy function for backups: copies the source file into the backup
# directory. Defaults to shutil.copy2; files under /etc are unreadable to a
# normal user, so migration scenarios must substitute an implementation that
# goes through an elevated session.
Copier = Callable[[Path, Path], None]


def _copy2(src: Path, dest: Path) -> None:
    """Default copy implementation. It explicitly returns None -- shutil.copy2
    returns the destination path itself, so using it directly as a Copier
    makes the types mismatch (Copier's contract is "copy and be done").
    """
    shutil.copy2(src, dest)


# File operation used during restore: src being None means "delete dest".
Restorer = Callable[[Path | None, Path], None]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class BackupEntry:
    original: Path
    backup: str  # file name relative to the backup directory
    existed: bool
    sha256: str = ""


def _verify_backup_copy(entry: BackupEntry, src: Path) -> None:
    """Verify the backup copy before restoring -- a declared sha256 must be honored.

    When the copy is truncated/rewritten (disk problems, accidental user
    edits, leftovers of a non-atomic copy), writing it back means overwriting
    live files with corrupt content. Old manifests may have no sha256; skip
    verification in that case.
    """
    if not entry.sha256:
        return
    actual = _sha256(src)
    if actual != entry.sha256:
        raise ValueError(
            f"backup copy sha256 mismatch, refusing to restore: {src} "
            f"(expected {entry.sha256}, got {actual})"
        )


class BackupRestoreError(RuntimeError):
    """Raised when some entries fail during a restore.

    A restore does its best to complete the remaining entries and does not
    abort because of a single failure. `.restored` is the list of original
    paths that were successfully restored (overwritten or deleted);
    `.failures` is the list of failed entries, each as
    (original path, exception).
    """

    def __init__(
        self,
        restored: list[Path],
        failures: list[tuple[Path, Exception]],
    ):
        self.restored = list(restored)
        self.failures = list(failures)
        detail = "; ".join(f"{p}: {exc}" for p, exc in self.failures)
        super().__init__(
            f"restore failed for {len(self.failures)} item(s) "
            f"(restored {len(self.restored)}): {detail}"
        )


@dataclass
class Backup:
    directory: Path
    created_at: str
    entries: list[BackupEntry] = field(default_factory=list)
    # File operation used during restore. Defaults to shutil.copy2 (delete
    # when the file did not exist); files under /etc cannot be written or
    # deleted by a normal user, so migration scenarios must substitute an
    # implementation that goes through an elevated session.
    restorer: Restorer | None = None

    def manifest_text(self) -> str:
        lines = [
            "# devproxy backup manifest",
            f"# Backup time: {self.created_at}",
            "# Restore: devproxy setup --rollback " + str(self.directory),
            "",
        ]
        for i, e in enumerate(self.entries, 1):
            state = "existed" if e.existed else "did not exist (deleted on restore)"
            lines.append(f"[{i}] {e.original}")
            lines.append(f"    Backup file: {e.backup}")
            lines.append(f"    Original state: {state}")
            if e.sha256:
                lines.append(f"    sha256: {e.sha256}")
            lines.append("")
        return "\n".join(lines)

    def restore(self) -> list[Path]:
        """Restore all entries. Return the original paths restored or deleted.

        A single failing entry does not abort the restore: restore as many
        other entries as possible, and after all have been attempted, raise
        `BackupRestoreError` if any failed. Its `.restored` keeps the
        successfully restored original paths and `.failures` keeps the failed
        entries (original path, exception). The caller must catch this
        exception, otherwise the list of restored paths is lost.

        The sha256 of each backup copy is verified before restoring: refuse to
        write back a corrupt copy (record the entry as failed) and never
        overwrite live files with corrupt content.
        """
        restored: list[Path] = []
        failures: list[tuple[Path, Exception]] = []
        restore = self.restorer
        for e in self.entries:
            try:
                src = self.directory / e.backup
                if e.existed:
                    _verify_backup_copy(e, src)
                    e.original.parent.mkdir(parents=True, exist_ok=True)
                    if restore is None:
                        shutil.copy2(src, e.original)
                    else:
                        restore(src, e.original)
                else:
                    if restore is None:
                        e.original.unlink(missing_ok=True)
                    else:
                        restore(None, e.original)
            except (OSError, ValueError) as exc:
                failures.append((e.original, exc))
                continue
            restored.append(e.original)
        if failures:
            raise BackupRestoreError(restored, failures)
        return restored


class BackupManager:
    def __init__(self, base_dir: Path, *, copier: Copier | None = None):
        self.base_dir = base_dir
        # copier(src, dest): copy the source file into the backup directory.
        # Defaults to shutil.copy2; in migration scenarios files under /etc are
        # unreadable to a normal user, so substitute an implementation that
        # goes through an elevated session.
        self._copier: Copier = copier or _copy2

    def create(self, targets: list[Path]) -> Backup:
        """Back up the target files. Directory creation or copy failures always raise -- the caller must abort the migration.

        Targets must be regular files: directories, symbolic links (including
        dangling ones), and other special files all raise `ValueError` and
        must not be silently recorded as "did not exist", or a restore might
        delete something that did exist. When a target really does not exist,
        record `existed=False`.
        """
        stamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
        directory = self.base_dir / stamp
        n = 0
        while directory.exists():
            n += 1
            directory = self.base_dir / f"{stamp}-{n}"
        directory.mkdir(parents=True)

        entries: list[BackupEntry] = []
        used: set[str] = set()
        try:
            for target in targets:
                if target.is_symlink() or (target.exists() and not target.is_file()):
                    raise ValueError(f"backup target is not a regular file: {target}")
                name = target.name or "root"
                backup_name = f"{name}.bak"
                i = 1
                while backup_name in used:
                    i += 1
                    backup_name = f"{name}.{i}.bak"
                used.add(backup_name)
                if target.exists():
                    copy = directory / backup_name
                    self._copier(target, copy)
                    entries.append(
                        BackupEntry(
                            original=target,
                            backup=backup_name,
                            existed=True,
                            sha256=_sha256(copy),
                        )
                    )
                else:
                    entries.append(BackupEntry(original=target, backup=backup_name, existed=False))
        except Exception:
            # Backup incomplete: mark it first, then clean up the half-finished
            # directory, to avoid leaving orphan backups without a manifest
            try:
                (directory / "INCOMPLETE").write_text(
                    "This backup did not complete and has been cleaned up; "
                    "if the directory still exists, cleanup failed -- delete it manually.\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
            shutil.rmtree(directory, ignore_errors=True)
            raise

        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        backup = Backup(directory=directory, created_at=created_at, entries=entries)
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "created_at": created_at,
                    "entries": [
                        {
                            "original": str(e.original),
                            "backup": e.backup,
                            "existed": e.existed,
                            "sha256": e.sha256,
                        }
                        for e in entries
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (directory / "MANIFEST.txt").write_text(backup.manifest_text(), encoding="utf-8")
        return backup


def load_backup(directory: Path) -> Backup:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    return Backup(
        directory=directory,
        created_at=manifest.get("created_at", ""),
        entries=[
            BackupEntry(
                original=Path(e["original"]),
                backup=e["backup"],
                existed=bool(e["existed"]),
                sha256=e.get("sha256", ""),
            )
            for e in manifest.get("entries", [])
        ],
    )
