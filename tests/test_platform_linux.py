import os
import subprocess
from pathlib import Path

import pytest

from devproxy.platform import linux as linux_mod
from devproxy.platform.base import Action, Status
from devproxy.platform.fake import FakePlatform
from devproxy.platform.linux import LinuxPlatform


def test_status_defaults():
    s = Status(ok=True)
    assert s.detail == ""
    assert s.hint == ""


def test_action_defaults():
    a = Action(description="装 caddy")
    assert a.kind == "run"
    assert a.command == []
    assert a.path is None
    assert a.privileged is False


def test_linux_platform_name_and_dns_port():
    p = LinuxPlatform()
    assert p.name == "linux"
    assert p.dns_port == 5353


def test_linux_service_unit_path_is_user_scope(tmp_path):
    p = LinuxPlatform(user_home=tmp_path)
    assert p.service_unit_path("caddy") == tmp_path / ".config" / "systemd" / "user" / "caddy.service"


def test_linux_binary_version_parses_output(tmp_path):
    fake = tmp_path / "caddy"
    fake.write_text('#!/bin/sh\necho "v2.11.7 h1:abc="\n')
    fake.chmod(0o755)
    assert LinuxPlatform().binary_version(fake) == "v2.11.7"


def test_linux_binary_version_empty_when_missing(tmp_path):
    assert LinuxPlatform().binary_version(tmp_path / "absent") == ""


def test_fake_platform_records_writes(tmp_path):
    p = FakePlatform()
    sess = p.privilege()
    sess.write(tmp_path / "f.txt", "hello")
    assert (tmp_path / "f.txt").read_text() == "hello"
    assert p.written[tmp_path / "f.txt"] == "hello"


def test_fake_platform_service_lifecycle():
    p = FakePlatform()
    p.install_service("caddy", "[Service]\n")
    assert p.service_active("caddy") is False
    p.active.add("caddy")
    assert p.service_active("caddy") is True
    assert "caddy.service" in p.service_unit_path("caddy").name


def test_fake_platform_privilege_session_is_cached():
    p = FakePlatform()
    assert p.privilege() is p.privilege(), "提权会话必须复用，否则会重复提示密码"


# ---------- Fix round 1: 提权写入 / 日志流式 / 服务操作失败即抛 ----------


def test_linux_elevated_write_uses_system_temp_then_sudo_cp(monkeypatch, tmp_path):
    """内容须在系统临时目录备好，再 sudo cp 到目标；临时文件不得落在目标目录里。"""
    calls = []

    def fake_run(argv):
        calls.append(list(argv))
        if argv[:2] == ["sudo", "cp"]:
            assert Path(argv[2]).read_text(encoding="utf-8") == "hello", "cp 时临时文件内容必须已写好"
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    target = tmp_path / "protected" / "devproxy.conf"
    sess = linux_mod.LinuxElevated()
    sess.write(target, "hello")

    cp_calls = [c for c in calls if c[:2] == ["sudo", "cp"]]
    assert len(cp_calls) == 1
    cp = cp_calls[0]
    assert cp[3] == str(target), "cp 的目标必须是真实目标路径"
    tmp_used = Path(cp[2])
    assert tmp_used.parent != target.parent, "临时文件不能放在目标目录内（特权目录不可写）"
    assert tmp_used.name.startswith("devproxy-elev-")
    assert not tmp_used.exists(), "成功后临时文件应清理"


def test_linux_elevated_write_failure_raises_and_cleans_temp(monkeypatch, tmp_path):
    """sudo cp 失败时抛 PermissionError，且临时文件也必须清理（try/finally）。"""
    seen_tmp = []

    def fake_run(argv):
        if argv[:2] == ["sudo", "cp"]:
            seen_tmp.append(Path(argv[2]))
            return 1, "boom"
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    sess = linux_mod.LinuxElevated()
    with pytest.raises(PermissionError):
        sess.write(tmp_path / "protected" / "devproxy.conf", "hello")
    assert seen_tmp and not seen_tmp[0].exists(), "失败路径也要清理临时文件"


def test_linux_tail_service_log_streams_to_terminal(monkeypatch):
    """follow 日志必须继承终端 stdio（不捕获），否则既看不到输出也永不返回。"""
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = list(argv)
        recorded["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(linux_mod.subprocess, "run", fake_run)
    LinuxPlatform().tail_service_log("caddy")
    assert recorded["argv"] == ["journalctl", "--user", "-u", "caddy", "-f"]
    assert "capture_output" not in recorded["kwargs"], "不得捕获输出，须流式写到终端"


def test_linux_service_mutations_raise_on_failure(monkeypatch, tmp_path):
    """enable/restart/daemon-reload 失败必须立即抛 RuntimeError，带上服务名与 stderr。"""
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (1, "unit not found"))
    p = LinuxPlatform(user_home=tmp_path)

    with pytest.raises(RuntimeError) as ei:
        p.enable_service("caddy")
    assert "caddy" in str(ei.value) and "unit not found" in str(ei.value)

    with pytest.raises(RuntimeError) as ei:
        p.restart_service("caddy")
    assert "caddy" in str(ei.value) and "unit not found" in str(ei.value)

    with pytest.raises(RuntimeError) as ei:
        p.install_service("caddy", "[Service]\n")
    assert "caddy" in str(ei.value) and "daemon-reload" in str(ei.value)
    assert "unit not found" in str(ei.value)


def test_linux_elevated_read_distinguishes_missing_from_permission(monkeypatch):
    """文件缺失抛 FileNotFoundError，其余失败（如权限）抛 PermissionError。"""

    def fake_run(argv):
        if "cat" in argv:
            if "missing" in argv[-1]:
                return 1, "cat: /etc/missing: No such file or directory"
            return 1, "cat: /etc/secret: Permission denied"
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    sess = linux_mod.LinuxElevated()
    with pytest.raises(FileNotFoundError):
        sess.read(Path("/etc/missing"))
    with pytest.raises(PermissionError):
        sess.read(Path("/etc/secret"))


def test_fake_elevated_copy_records_command_and_written(tmp_path):
    """FakeElevated.copy 须与 write 一致地记录 commands 与 written。"""
    src = tmp_path / "src.txt"
    src.write_text("data", encoding="utf-8")
    dest = tmp_path / "sub" / "dest.txt"
    p = FakePlatform()
    p.privilege().copy(src, dest)
    assert dest.read_text(encoding="utf-8") == "data"
    assert p.written[dest] == "data"
    assert ["cp", str(src), str(dest)] in p.commands


# -- CA 信任判定：真信任库，而不是"暂存目录里有这个文件" -------------------


def test_linux_ca_trusted_true_when_openssl_verifies(tmp_path, monkeypatch):
    dest = tmp_path / "devproxy-local.crt"
    dest.write_text("CA", encoding="utf-8")
    ca = tmp_path / "root.crt"
    ca.write_text("CA", encoding="utf-8")
    monkeypatch.setattr(linux_mod, "SYSTEM_CA_DEST", dest)
    monkeypatch.setattr(linux_mod, "SYSTEM_TRUST_DIR", tmp_path / "certs")
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return 0, "root.crt: OK"

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().ca_trusted(ca) is True
    assert calls[0][:2] == ["openssl", "verify"]
    assert str(tmp_path / "certs") in calls[0]


def test_linux_ca_trusted_false_when_openssl_rejects(tmp_path, monkeypatch):
    """CA 只是躺在暂存目录里、没进信任库时，必须报未信任。"""
    dest = tmp_path / "devproxy-local.crt"
    dest.write_text("CA", encoding="utf-8")
    ca = tmp_path / "root.crt"
    ca.write_text("CA", encoding="utf-8")
    monkeypatch.setattr(linux_mod, "SYSTEM_CA_DEST", dest)
    monkeypatch.setattr(linux_mod, "SYSTEM_TRUST_DIR", tmp_path / "certs")
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv: (1, "error 20 at 0 depth lookup: unable to get local issuer"),
    )
    assert LinuxPlatform().ca_trusted(ca) is False


def test_linux_ca_trusted_falls_back_when_openssl_missing(tmp_path, monkeypatch):
    """没有 openssl 时退回比对暂存文件内容（老行为）。"""
    dest = tmp_path / "devproxy-local.crt"
    dest.write_text("CA", encoding="utf-8")
    ca = tmp_path / "root.crt"
    ca.write_text("CA", encoding="utf-8")
    monkeypatch.setattr(linux_mod, "SYSTEM_CA_DEST", dest)
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (127, "命令不存在: openssl"))
    assert LinuxPlatform().ca_trusted(ca) is True


def test_linux_ca_trusted_false_when_staged_file_missing(tmp_path, monkeypatch):
    ca = tmp_path / "root.crt"
    ca.write_text("CA", encoding="utf-8")
    monkeypatch.setattr(linux_mod, "SYSTEM_CA_DEST", tmp_path / "absent.crt")
    monkeypatch.setattr(linux_mod, "_run", lambda argv: pytest.fail("不该执行 openssl"))
    assert LinuxPlatform().ca_trusted(ca) is False


def test_linux_ca_trusted_never_raises_on_binary_file(tmp_path, monkeypatch):
    """非 UTF-8 文件不能让 check() 抛异常 —— doctor 依赖它不炸。"""
    dest = tmp_path / "devproxy-local.crt"
    dest.write_bytes(b"\xff\xfe\x00binary")
    ca = tmp_path / "root.crt"
    ca.write_text("CA", encoding="utf-8")
    monkeypatch.setattr(linux_mod, "SYSTEM_CA_DEST", dest)
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (127, "命令不存在: openssl"))
    assert LinuxPlatform().ca_trusted(ca) is False


# -- NSS 库判定 -------------------------------------------------------------


def test_linux_nss_trusted_when_certutil_finds_nickname(tmp_path, monkeypatch):
    p = LinuxPlatform(user_home=tmp_path)
    nss = tmp_path / ".pki" / "nssdb"
    nss.mkdir(parents=True)
    (nss / "cert9.db").write_text("", encoding="utf-8")
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return 0, "devproxy Local CA"

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert p.nss_trusted(tmp_path / "root.crt") is True
    assert calls[0][:2] == ["certutil", "-L"]


def test_linux_nss_trusted_false_without_db(tmp_path, monkeypatch):
    p = LinuxPlatform(user_home=tmp_path)
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv: pytest.fail("没有 NSS 库就不该跑 certutil")
    )
    assert p.nss_trusted(tmp_path / "root.crt") is False


def test_linux_nss_trusted_false_when_certutil_missing(tmp_path, monkeypatch):
    p = LinuxPlatform(user_home=tmp_path)
    nss = tmp_path / ".pki" / "nssdb"
    nss.mkdir(parents=True)
    (nss / "cert9.db").write_text("", encoding="utf-8")
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (127, "命令不存在: certutil"))
    assert p.nss_trusted(tmp_path / "root.crt") is False


def test_fake_platform_nss_trusted_tracks_its_own_set(tmp_path):
    p = FakePlatform()
    ca = tmp_path / "root.crt"
    assert p.nss_trusted(ca) is False
    p.nss.add(ca)
    assert p.nss_trusted(ca) is True
