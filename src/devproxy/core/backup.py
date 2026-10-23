"""迁移前的强制备份与回滚。

备份目录带 UTC 时间戳，多次迁移不互相覆盖；只增不删，由用户自行清理。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# 备份用的复制函数：把源文件复制到备份目录。默认 shutil.copy2；
# /etc 下的文件普通用户读不到，迁移场景要换成走提权会话的实现。
Copier = Callable[[Path, Path], None]

# 还原用的文件操作：src 为 None 表示"删除 dest"。
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
    backup: str  # 相对备份目录的文件名
    existed: bool
    sha256: str = ""


def _verify_backup_copy(entry: BackupEntry, src: Path) -> None:
    """还原前校验备份副本 —— 声明了 sha256 就必须兑现。

    副本被截断/改写（磁盘问题、用户误改、非原子复制遗留）时，直接写回
    等于用损坏内容覆盖现役文件。旧清单可能没有 sha256，跳过校验。
    """
    if not entry.sha256:
        return
    actual = _sha256(src)
    if actual != entry.sha256:
        raise ValueError(
            f"备份副本 sha256 不匹配，拒绝还原：{src}"
            f"（期望 {entry.sha256}，实际 {actual}）"
        )


class BackupRestoreError(RuntimeError):
    """还原过程中有条目失败时抛出。

    还原会尽力完成其余条目，不会因单个条目失败而中断。
    `.restored` 是已成功还原（覆盖或删除）的原始路径列表，
    `.failures` 是失败条目列表，元素为 (原始路径, 异常)。
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
            f"还原失败 {len(self.failures)} 项（已还原 {len(self.restored)} 项）: {detail}"
        )


@dataclass
class Backup:
    directory: Path
    created_at: str
    entries: list[BackupEntry] = field(default_factory=list)
    # 还原时的文件操作。默认 shutil.copy2（不存在则删除）；/etc 下的文件
    # 普通用户写不了也删不了，迁移场景要换成走提权会话的实现。
    restorer: Restorer | None = None

    def manifest_text(self) -> str:
        lines = [
            "# devproxy 备份清单",
            f"# 备份时间: {self.created_at}",
            "# 还原方式: devproxy setup --rollback " + str(self.directory),
            "",
        ]
        for i, e in enumerate(self.entries, 1):
            state = "存在" if e.existed else "原本不存在（还原时将删除）"
            lines.append(f"[{i}] {e.original}")
            lines.append(f"    备份文件: {e.backup}")
            lines.append(f"    原始状态: {state}")
            if e.sha256:
                lines.append(f"    sha256: {e.sha256}")
            lines.append("")
        return "\n".join(lines)

    def restore(self) -> list[Path]:
        """还原所有条目。返回被还原或被删除的原始路径。

        单个条目失败不会中断还原：尽可能还原其余条目，全部尝试完后若仍有失败，
        抛出 `BackupRestoreError`，其 `.restored` 保留已成功还原的原始路径，
        `.failures` 保留失败条目（原始路径, 异常）。调用方必须捕获该异常，
        否则已还原清单会丢失。

        还原前会校验备份副本的 sha256：副本损坏时拒绝写回（记为该条目
        失败），绝不把损坏内容覆盖到现役文件上。
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
        # copier(src, dest)：把源文件复制进备份目录。默认 shutil.copy2；
        # 迁移场景里 /etc 下的文件普通用户读不到，要换成走提权会话的实现。
        self._copier: Copier = copier or (lambda src, dest: shutil.copy2(src, dest))

    def create(self, targets: list[Path]) -> Backup:
        """备份目标文件。目录创建或复制失败一律抛异常 —— 调用方必须中止迁移。

        目标必须是普通文件：目录、符号链接（含悬空链接）、其他特殊文件一律
        抛 `ValueError`，不得被静默记成"原本不存在"，否则还原时可能删除
        原本存在的东西。目标确实不存在时记 `existed=False`。
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
                    raise ValueError(f"备份目标不是普通文件: {target}")
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
                    entries.append(
                        BackupEntry(original=target, backup=backup_name, existed=False)
                    )
        except Exception:
            # 备份未完成：先打标记再清理半成品目录，避免留下没有 manifest 的孤儿备份
            try:
                (directory / "INCOMPLETE").write_text(
                    "本次备份未完成，已被清理；如目录仍存在说明清理失败，请手动删除。\n",
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
