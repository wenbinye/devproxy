import hashlib
import json
from pathlib import Path

import pytest

from devproxy import backup as backup_mod
from devproxy.backup import BackupManager, BackupRestoreError, load_backup


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


# --- 非普通文件目标必须被拒绝，不得静默记成"原本不存在" ---


def test_directory_target_is_rejected(tmp_path):
    target = tmp_path / "adir"
    target.mkdir()
    with pytest.raises(ValueError) as exc:
        BackupManager(tmp_path / "backups").create([target])
    assert str(target) in str(exc.value)


def test_rejected_target_leaves_no_backup_directory(tmp_path):
    target = tmp_path / "adir"
    target.mkdir()
    with pytest.raises(ValueError):
        BackupManager(tmp_path / "backups").create([target])
    base = tmp_path / "backups"
    assert not base.exists() or not any(base.iterdir()), "被拒绝的备份不得留下孤儿目录"


def test_symlink_target_is_rejected(tmp_path):
    real = tmp_path / "real"
    real.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError) as exc:
        BackupManager(tmp_path / "backups").create([link])
    assert str(link) in str(exc.value)


def test_dangling_symlink_is_rejected_not_treated_as_missing(tmp_path):
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "nowhere")
    with pytest.raises(ValueError):
        BackupManager(tmp_path / "backups").create([link])


def test_missing_path_is_still_recorded_as_not_existed(tmp_path):
    missing = tmp_path / "absent.conf"
    b = BackupManager(tmp_path / "backups").create([missing])
    assert b.entries[0].existed is False


# --- sha256 必须落在备份副本上，而不是复制后的源文件上 ---


def test_sha256_is_computed_on_the_backup_copy(tmp_path):
    src = tmp_path / "f"
    src.write_text("hello")
    b = BackupManager(tmp_path / "backups").create([src])
    copy = b.directory / b.entries[0].backup
    assert b.entries[0].sha256 == hashlib.sha256(copy.read_bytes()).hexdigest()


def test_sha256_matches_source_when_source_is_unchanged(tmp_path):
    src = tmp_path / "f"
    src.write_text("hello")
    b = BackupManager(tmp_path / "backups").create([src])
    assert b.entries[0].sha256 == hashlib.sha256(b"hello").hexdigest()


# --- 还原中途失败不得丢失已还原清单，也不得跳过其余条目 ---


def test_restore_failure_keeps_restored_list_and_continues(tmp_path):
    s1 = tmp_path / "s1"
    s1.write_text("one")
    s2 = tmp_path / "s2"
    s2.write_text("two")
    s3 = tmp_path / "s3"
    s3.write_text("three")
    b = BackupManager(tmp_path / "backups").create([s1, s2, s3])
    # 破坏第二个条目的备份文件，模拟备份丢失
    (b.directory / b.entries[1].backup).unlink()
    s1.write_text("modified1")
    s2.write_text("modified2")
    s3.write_text("modified3")
    with pytest.raises(BackupRestoreError) as exc:
        b.restore()
    assert exc.value.restored == [s1, s3], "已还原清单不得丢失其余成功条目"
    assert [p for p, _ in exc.value.failures] == [s2]
    assert s1.read_text() == "one", "失败前的条目必须已被还原"
    assert s3.read_text() == "three", "失败不得中断其余条目的还原"


def test_restore_returns_all_paths_when_nothing_fails(tmp_path):
    s1 = tmp_path / "s1"
    s1.write_text("one")
    s2 = tmp_path / "s2"
    s2.write_text("two")
    b = BackupManager(tmp_path / "backups").create([s1, s2])
    assert b.restore() == [s1, s2]


# --- 备份过程失败不得留下没有 manifest 的孤儿目录 ---


def test_failed_copy_leaves_no_orphan_backup_directory(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.write_text("1")
    b = tmp_path / "b"
    b.write_text("2")
    real_copy = backup_mod.shutil.copy2
    calls = []

    def flaky(src, dst, *args, **kwargs):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("boom")
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(backup_mod.shutil, "copy2", flaky)
    with pytest.raises(OSError):
        BackupManager(tmp_path / "backups").create([a, b])
    base = tmp_path / "backups"
    assert not base.exists() or not any(base.iterdir()), "失败的备份不得留下孤儿目录"
