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


@dataclass
class Backup:
    directory: Path
    created_at: str
    entries: list[BackupEntry] = field(default_factory=list)

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
        """还原所有条目。返回被还原或被删除的原始路径。"""
        touched: list[Path] = []
        for e in self.entries:
            src = self.directory / e.backup
            if e.existed:
                e.original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, e.original)
            else:
                e.original.unlink(missing_ok=True)
            touched.append(e.original)
        return touched


class BackupManager:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    def create(self, targets: list[Path]) -> Backup:
        """备份目标文件。目录创建或复制失败一律抛异常 —— 调用方必须中止迁移。"""
        stamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
        directory = self.base_dir / stamp
        n = 0
        while directory.exists():
            n += 1
            directory = self.base_dir / f"{stamp}-{n}"
        directory.mkdir(parents=True)

        entries: list[BackupEntry] = []
        used: set[str] = set()
        for target in targets:
            name = target.name or "root"
            backup_name = f"{name}.bak"
            i = 1
            while backup_name in used:
                i += 1
                backup_name = f"{name}.{i}.bak"
            used.add(backup_name)
            if target.exists() and target.is_file():
                shutil.copy2(target, directory / backup_name)
                entries.append(
                    BackupEntry(
                        original=target,
                        backup=backup_name,
                        existed=True,
                        sha256=_sha256(target),
                    )
                )
            else:
                entries.append(
                    BackupEntry(original=target, backup=backup_name, existed=False)
                )

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
