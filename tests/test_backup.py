import hashlib
import json
from pathlib import Path

import pytest

from devproxy.backup import BackupManager, load_backup


def test_create_backs_up_existing_files(tmp_path):
    src = tmp_path / "unit.service"
    src.write_text("hello")
    b = BackupManager(tmp_path / "backups").create([src])
    assert len(b.entries) == 1
    e = b.entries[0]
    assert e.existed is True
    assert e.original == src
    assert (b.directory / e.backup).read_text() == "hello"
    assert e.sha256 == hashlib.sha256(b"hello").hexdigest()


def test_create_records_missing_file_as_not_existed(tmp_path):
    missing = tmp_path / "absent.conf"
    b = BackupManager(tmp_path / "backups").create([missing])
    assert b.entries[0].existed is False
    assert not (b.directory / b.entries[0].backup).exists()


def test_backup_directory_is_timestamped(tmp_path):
    b = BackupManager(tmp_path / "backups").create([tmp_path / "a"])
    assert b.directory.name.endswith("Z") or "T" in b.directory.name
    assert b.directory.parent == tmp_path / "backups"


def test_manifest_json_is_written(tmp_path):
    src = tmp_path / "f"
    src.write_text("x")
    b = BackupManager(tmp_path / "backups").create([src])
    manifest = json.loads((b.directory / "manifest.json").read_text())
    assert manifest["entries"][0]["original"] == str(src)
    assert manifest["entries"][0]["existed"] is True


def test_manifest_txt_is_human_readable(tmp_path):
    src = tmp_path / "caddy.service"
    src.write_text("x")
    b = BackupManager(tmp_path / "backups").create([src])
    text = (b.directory / "MANIFEST.txt").read_text()
    assert str(src) in text
    assert "caddy.service" in text


def test_two_backups_do_not_overwrite_each_other(tmp_path):
    src = tmp_path / "f"
    src.write_text("v1")
    m = BackupManager(tmp_path / "backups")
    b1 = m.create([src])
    src.write_text("v2")
    b2 = m.create([src])
    assert b1.directory != b2.directory
    assert (b1.directory / b1.entries[0].backup).read_text() == "v1"
    assert (b2.directory / b2.entries[0].backup).read_text() == "v2"


def test_restore_puts_files_back(tmp_path):
    src = tmp_path / "f"
    src.write_text("original")
    b = BackupManager(tmp_path / "backups").create([src])
    src.write_text("modified")
    b.restore()
    assert src.read_text() == "original"


def test_restore_removes_files_that_did_not_exist(tmp_path):
    target = tmp_path / "created-by-tool"
    b = BackupManager(tmp_path / "backups").create([target])
    target.write_text("new")
    b.restore()
    assert not target.exists(), "原本不存在的文件在还原时应被删除"


def test_load_backup_roundtrip(tmp_path):
    src = tmp_path / "f"
    src.write_text("x")
    b = BackupManager(tmp_path / "backups").create([src])
    loaded = load_backup(b.directory)
    assert loaded.entries[0].original == b.entries[0].original
    assert loaded.entries[0].sha256 == b.entries[0].sha256


def test_basename_collision_is_resolved(tmp_path):
    a = tmp_path / "a" / "conf"
    b = tmp_path / "b" / "conf"
    for p in (a, b):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(p.name)
    m = BackupManager(tmp_path / "backups")
    backup = m.create([a, b])
    names = {e.backup for e in backup.entries}
    assert len(names) == 2, "同名文件的备份名不能冲突"
