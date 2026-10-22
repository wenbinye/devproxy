import base64
import os
import subprocess
from pathlib import Path

import pytest

from devproxy.platform import linux as linux_mod
from devproxy.platform.base import Action, Status
from devproxy.platform.fake import FakePlatform
from devproxy.platform.linux import LinuxPlatform, render_systemd_unit


@pytest.fixture
def allow_platform_mutation(monkeypatch):
    """本文件专门验证平台的写操作：_run 已被替换、路径指向 tmp_path，
    可以安全地关掉"测试期禁写"护栏。"""
    monkeypatch.setattr(linux_mod, "_ALLOW_MUTATION", True)


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
    p.install_service("caddy", exec_start=["/bin/caddy", "run"], env={})
    assert p.service_active("caddy") is False
    p.active.add("caddy")
    assert p.service_active("caddy") is True
    assert "caddy.service" in p.service_unit_path("caddy").name


def test_fake_platform_install_service_renders_unit_and_env():
    """单元由平台渲染：exec_start / env 都要落到单元文本里。"""
    p = FakePlatform()
    p.install_service(
        "caddy",
        exec_start=["/bin/caddy", "run", "--config", "/tmp/Caddyfile"],
        exec_reload=["/bin/caddy", "reload"],
        env={"MINIKUBE_IP": "1.2.3.4"},
    )
    unit = p.services["caddy"]
    assert "ExecStart=/bin/caddy run --config /tmp/Caddyfile" in unit
    assert "Environment=MINIKUBE_IP=1.2.3.4" in unit
    assert "ExecReload=/bin/caddy reload" in unit
    # 单元也落盘 —— 组件的“单元是否过期”判据要读它
    assert p.service_unit_path("caddy").read_text(encoding="utf-8") == unit


def test_fake_platform_privilege_session_is_cached():
    p = FakePlatform()
    assert p.privilege() is p.privilege(), "提权会话必须复用，否则会重复提示密码"


# ---------- Fix round 1: 提权写入 / 日志流式 / 服务操作失败即抛 ----------


def test_linux_elevated_write_uses_system_temp_then_sudo_install(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """内容须在系统临时目录备好，再 sudo install 到目标；临时文件不得落在目标目录里。"""
    calls = []

    def fake_run(argv):
        calls.append(list(argv))
        if argv[:3] == ["sudo", "install", "-m"]:
            assert Path(argv[8]).read_text(encoding="utf-8") == "hello", "安装时临时文件内容必须已写好"
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    target = tmp_path / "protected" / "devproxy.conf"
    sess = linux_mod.LinuxElevated()
    sess.write(target, "hello")

    install_calls = [c for c in calls if c[:3] == ["sudo", "install", "-m"]]
    assert len(install_calls) == 1
    inst = install_calls[0]
    assert inst[-1] == str(target), "install 的目标必须是真实目标路径"
    tmp_used = Path(inst[-2])
    assert tmp_used.parent != target.parent, "临时文件不能放在目标目录内（特权目录不可写）"
    assert tmp_used.name.startswith("devproxy-elev-")
    assert not tmp_used.exists(), "成功后临时文件应清理"


def test_linux_elevated_write_produces_world_readable_root_file(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """特权写入必须落成 root:root 0644 —— 0600 的文件普通用户自己都读不了。

    mkstemp 建出来的临时文件是 0600，`cp` 会把权限一并带过去，
    于是 /usr/local/share/ca-certificates/ 与 /etc/systemd/resolved.conf.d/
    下的文件变成 root:root 0600，doctor 永远报"读不到"。
    """
    calls = []

    def fake_run(argv):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    linux_mod.LinuxElevated().write(tmp_path / "devproxy.conf", "x")

    installs = [c for c in calls if c[:3] == ["sudo", "install", "-m"]]
    assert installs, "特权写入必须走 install（可显式设定权限与归属）"
    argv = installs[0]
    assert "-m" in argv and argv[argv.index("-m") + 1] == "644", "必须写成 0644"
    assert "-o" in argv and argv[argv.index("-o") + 1] == "root"
    assert "-g" in argv and argv[argv.index("-g") + 1] == "root"
    assert not [c for c in calls if c[:2] == ["sudo", "cp"]], "不能再用 cp（会带上 0600）"


def test_linux_elevated_copy_produces_world_readable_root_file(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """CA 装进系统信任库走 copy —— 同样必须落成 root:root 0644。"""
    calls = []

    def fake_run(argv):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    src = tmp_path / "root.crt"
    src.write_text("CA", encoding="utf-8")
    linux_mod.LinuxElevated().copy(src, tmp_path / "devproxy-local.crt")

    installs = [c for c in calls if c[:3] == ["sudo", "install", "-m"]]
    assert installs, "复制到特权目录必须走 install"
    argv = installs[0]
    assert argv[argv.index("-m") + 1] == "644"
    assert argv[argv.index("-o") + 1] == "root"
    assert not [c for c in calls if c[:2] == ["sudo", "cp"]], "不能再用 cp（会带上 0600）"


def test_linux_elevated_write_failure_raises_and_cleans_temp(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """特权写入失败时抛 PermissionError，且临时文件也必须清理（try/finally）。"""
    seen_tmp = []

    def fake_run(argv):
        if argv[:3] == ["sudo", "install", "-m"]:
            seen_tmp.append(Path(argv[-2]))
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


def test_linux_service_mutations_raise_on_failure(
    monkeypatch, tmp_path, allow_platform_mutation
):
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
        p.install_service("caddy", exec_start=["/bin/caddy", "run"], env={})
    assert "caddy" in str(ei.value) and "daemon-reload" in str(ei.value)
    assert "unit not found" in str(ei.value)


# -- 单元渲染：平台自己决定单元长什么样 -------------------------------------


def test_render_systemd_unit_contains_exec_and_env():
    unit = render_systemd_unit(
        "caddy",
        ["/home/u/.local/bin/caddy", "run", "--config", "/home/u/.config/devproxy/Caddyfile"],
        ["/home/u/.local/bin/caddy", "reload"],
        ["/home/u/.local/bin/caddy", "stop"],
        {"MINIKUBE_IP": "192.168.49.2"},
        "Caddy local HTTP/HTTPS server (managed by devproxy)",
    )
    assert "ExecStart=/home/u/.local/bin/caddy run --config /home/u/.config/devproxy/Caddyfile" in unit
    assert "ExecReload=/home/u/.local/bin/caddy reload" in unit
    assert "ExecStop=/home/u/.local/bin/caddy stop" in unit
    assert "Environment=MINIKUBE_IP=192.168.49.2" in unit
    assert "WantedBy=default.target" in unit


def test_render_systemd_unit_rejects_whitespace_in_args():
    """systemd 单元不做 shell 引号解析 —— 参数里的空白会截断命令行。"""
    with pytest.raises(RuntimeError, match="空白"):
        render_systemd_unit(
            "caddy",
            ["/opt/my caddy/caddy", "run"],
            None,
            None,
            {},
        )
    with pytest.raises(RuntimeError, match="空白"):
        render_systemd_unit(
            "caddy",
            ["/opt/caddy", "run", "--config", "/home/u/my config/Caddyfile"],
            None,
            None,
            {},
        )


def test_render_systemd_unit_service_type_follows_program():
    """只有 caddy 会发 sd_notify；dnsmasq 硬套 Type=notify 会永远起不来。"""
    caddy = render_systemd_unit("caddy", ["/usr/bin/caddy", "run"], None, None, {})
    dnsmasq = render_systemd_unit(
        "dnsmasq", ["/usr/sbin/dnsmasq", "--keep-in-foreground"], None, None, {}
    )
    assert "Type=notify" in caddy
    assert "Type=simple" in dnsmasq


def test_linux_install_service_writes_unit_file(
    tmp_path, monkeypatch, allow_platform_mutation
):
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (0, ""))
    p = LinuxPlatform(user_home=tmp_path)
    p.install_service(
        "caddy",
        exec_start=["/bin/caddy", "run", "--config", "/tmp/Caddyfile"],
        env={"MINIKUBE_IP": "1.2.3.4"},
    )
    unit = (tmp_path / ".config" / "systemd" / "user" / "caddy.service").read_text(
        encoding="utf-8"
    )
    assert "ExecStart=/bin/caddy run --config /tmp/Caddyfile" in unit
    assert "Environment=MINIKUBE_IP=1.2.3.4" in unit


def test_linux_elevated_read_distinguishes_missing_from_permission(
    monkeypatch, allow_platform_mutation
):
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


# -- 非交互的特权读（doctor 不得弹密码框） ----------------------------------


def test_linux_privilege_read_is_non_interactive(monkeypatch, tmp_path):
    """特权读必须带 sudo -n：没有缓存凭据时立刻失败，绝不等待输入。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="unit\n", stderr="")

    monkeypatch.setattr(linux_mod.subprocess, "run", fake_run)
    out = LinuxPlatform().privilege_read(tmp_path / "caddy.service")
    assert out == "unit\n"
    assert calls[0][:3] == ["sudo", "-n", "cat"]
    assert "-v" not in calls[0], "不得跑 sudo -v（那会弹密码框）"


def test_linux_privilege_read_returns_none_on_failure(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="sudo: a password is required")

    monkeypatch.setattr(linux_mod.subprocess, "run", fake_run)
    assert LinuxPlatform().privilege_read(tmp_path / "caddy.service") is None


def test_linux_privilege_read_returns_none_when_sudo_missing(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("sudo")

    monkeypatch.setattr(linux_mod.subprocess, "run", fake_run)
    assert LinuxPlatform().privilege_read(tmp_path / "caddy.service") is None


# -- CA 信任判定：按指纹命中活库，而不是"openssl 能不能验通这条链" ---------


def _fake_pem(seed: str) -> str:
    """造一份 PEM 证书文本（内容是 base64 出来的伪 DER）。

    判据是"同一份字节"（sha256 指纹），不解析 X.509 结构 ——
    所以测试里不需要真的证书。
    """
    body = base64.b64encode(f"fake-der:{seed}".encode("utf-8")).decode("ascii")
    return (
        "-----BEGIN CERTIFICATE-----\n"
        f"{body}\n"
        "-----END CERTIFICATE-----\n"
    )


@pytest.fixture
def trust_store(tmp_path, monkeypatch):
    """把系统信任库指到 tmp_path：测试绝不读真实的 /etc/ssl/certs。

    返回 (bundle 文件, 散列索引目录, 暂存目录)。
    """
    bundle = tmp_path / "ca-certificates.crt"
    hashed = tmp_path / "certs"
    hashed.mkdir()
    staged_dir = tmp_path / "usr" / "local" / "share" / "ca-certificates"
    staged_dir.mkdir(parents=True)
    monkeypatch.setattr(linux_mod, "TRUST_BUNDLE_FILES", (bundle,))
    monkeypatch.setattr(linux_mod, "TRUST_HASH_DIR", hashed)
    monkeypatch.setattr(linux_mod, "SYSTEM_TRUST_DIR", hashed)
    return bundle, hashed, staged_dir


def test_linux_ca_trusted_true_when_fingerprint_in_live_store(
    trust_store, tmp_path, monkeypatch
):
    """CA 的指纹出现在活库里就算信任 —— 即使 openssl 因同主题歧义报错。

    这是那条永久假阴性的核心场景：信任库里有多份同 SUBJECT 的根，
    openssl 挑到另一份报 error 18，而 curl 其实完全正常。
    """
    bundle, hashed, staged = trust_store
    ca = tmp_path / "root.crt"
    ca.write_text(_fake_pem("mine"), encoding="utf-8")
    bundle.write_text(_fake_pem("other") + _fake_pem("mine"), encoding="utf-8")

    def fake_run(argv):
        # 复现歧义：openssl 按 SUBJECT 选到了另一份根
        return 1, "error 18 at 0 depth lookup: self-signed certificate"

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().ca_trusted(ca) is True


def test_linux_ca_trusted_true_when_fingerprint_in_hashed_dir(
    trust_store, tmp_path, monkeypatch
):
    """散列索引目录（/etc/ssl/certs/*.0）里的证书同样算活库。"""
    bundle, hashed, staged = trust_store
    ca = tmp_path / "root.crt"
    ca.write_text(_fake_pem("mine"), encoding="utf-8")
    (hashed / "abc123.0").write_text(_fake_pem("mine"), encoding="utf-8")
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv: (1, "error 18 at 0 depth lookup: self-signed certificate"),
    )
    assert LinuxPlatform().ca_trusted(ca) is True


def test_linux_ca_trusted_false_when_only_staged_file_present(
    trust_store, tmp_path, monkeypatch
):
    """暂存目录里有这个文件 ≠ 系统信任它 —— update-ca-certificates 可能没跑成功。"""
    bundle, hashed, staged = trust_store
    ca = tmp_path / "root.crt"
    ca.write_text(_fake_pem("mine"), encoding="utf-8")
    (staged / "devproxy-local.crt").write_text(_fake_pem("mine"), encoding="utf-8")

    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv: (1, "error 20 at 0 depth lookup: unable to get local issuer"),
    )
    assert LinuxPlatform().ca_trusted(ca) is False


def test_linux_ca_trusted_false_when_same_subject_other_root(
    trust_store, tmp_path, monkeypatch
):
    """活库里只有同主题的另一份根 —— 不是这一份，不能算信任。"""
    bundle, hashed, staged = trust_store
    ca = tmp_path / "root.crt"
    ca.write_text(_fake_pem("mine"), encoding="utf-8")
    bundle.write_text(_fake_pem("other"), encoding="utf-8")

    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv: (1, "error 18 at 0 depth lookup: self-signed certificate"),
    )
    assert LinuxPlatform().ca_trusted(ca) is False


def test_linux_ca_trusted_true_when_openssl_verifies(trust_store, tmp_path, monkeypatch):
    """openssl 验通是补充的肯定信号（指纹扫描没覆盖到时的兜底）。"""
    bundle, hashed, staged = trust_store
    ca = tmp_path / "root.crt"
    ca.write_text(_fake_pem("mine"), encoding="utf-8")
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return 0, "root.crt: OK"

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().ca_trusted(ca) is True
    assert calls[0][:2] == ["openssl", "verify"]
    assert str(hashed) in calls[0]


def test_linux_ca_trusted_never_raises_on_binary_file(
    trust_store, tmp_path, monkeypatch
):
    """非 UTF-8 文件不能让 check() 抛异常 —— doctor 依赖它不炸。"""
    bundle, hashed, staged = trust_store
    bundle.write_bytes(b"\xff\xfe\x00binary")
    ca = tmp_path / "root.crt"
    ca.write_text(_fake_pem("mine"), encoding="utf-8")
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv: (127, "命令不存在: openssl")
    )
    assert LinuxPlatform().ca_trusted(ca) is False


def test_linux_ca_trusted_false_when_ca_not_pem(trust_store, tmp_path, monkeypatch):
    """CA 文件不是可解析的 PEM —— 判为未信任，不抛异常。"""
    bundle, hashed, staged = trust_store
    ca = tmp_path / "root.crt"
    ca.write_text("这不是证书", encoding="utf-8")
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv: (127, "命令不存在: openssl")
    )
    assert LinuxPlatform().ca_trusted(ca) is False


# -- NSS 库判定 -------------------------------------------------------------


PEM_OLD = (
    "-----BEGIN CERTIFICATE-----\n"
    "T0xEQ0VSVElGSUNBVEUtT0xE\n"
    "-----END CERTIFICATE-----\n"
)
PEM_NEW = (
    "-----BEGIN CERTIFICATE-----\n"
    "TkVXQ0VSVElGSUNBVEUtTkVX\n"
    "-----END CERTIFICATE-----\n"
)


def _nss_platform(tmp_path):
    p = LinuxPlatform(user_home=tmp_path)
    nss = tmp_path / ".pki" / "nssdb"
    nss.mkdir(parents=True)
    (nss / "cert9.db").write_text("", encoding="utf-8")
    return p


def test_linux_nss_trusted_true_when_stored_cert_matches(tmp_path, monkeypatch):
    """判定依据是库里的**证书内容**，不只是同名条目存在。"""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return 0, PEM_OLD

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert p.nss_trusted(ca) is True
    assert calls[0][:2] == ["certutil", "-L"]
    assert "-a" in calls[0], "必须让 certutil 打出 PEM 才能比对内容"


def test_linux_nss_trusted_false_when_stored_cert_differs(tmp_path, monkeypatch):
    """Caddy 重新生成 CA 后旧条目还在 —— 同名但内容不同，必须报未信任。"""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_NEW, encoding="utf-8")
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (0, PEM_OLD))
    assert p.nss_trusted(ca) is False


def test_linux_nss_trusted_ignores_blank_lines_and_crlf(tmp_path, monkeypatch):
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    noisy = "\r\n" + PEM_OLD.replace("\n", "\r\n") + "\r\n\r\n"
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (0, noisy))
    assert p.nss_trusted(ca) is True


def test_linux_nss_trusted_false_when_entry_missing(tmp_path, monkeypatch):
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv: (255, "certutil: could not find cert: devproxy Local CA"),
    )
    assert p.nss_trusted(ca) is False


def test_linux_nss_trusted_false_when_ca_unreadable(tmp_path, monkeypatch):
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_bytes(b"\xff\xfe\x00binary")
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (0, PEM_OLD))
    assert p.nss_trusted(ca) is False


def test_linux_nss_trusted_false_without_db(tmp_path, monkeypatch):
    p = LinuxPlatform(user_home=tmp_path)
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv: pytest.fail("没有 NSS 库就不该跑 certutil")
    )
    assert p.nss_trusted(tmp_path / "root.crt") is False


def test_linux_nss_trusted_false_when_certutil_missing(tmp_path, monkeypatch):
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (127, "命令不存在: certutil"))
    assert p.nss_trusted(ca) is False


def test_fake_platform_nss_trusted_tracks_its_own_set(tmp_path):
    p = FakePlatform()
    ca = tmp_path / "root.crt"
    assert p.nss_trusted(ca) is False
    p.nss.add(ca)
    assert p.nss_trusted(ca) is True


# -- 特权写入前建父目录（干净机器上 /etc/... 目录可能不存在） ----------------


def test_linux_elevated_write_creates_parent_dir_before_install(
    monkeypatch, tmp_path, allow_platform_mutation
):
    calls = []

    def fake_run(argv):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    target = tmp_path / "etc" / "dnsmasq.d" / "devproxy.conf"
    linux_mod.LinuxElevated().write(target, "hello")

    mkdir = [c for c in calls if c[:3] == ["sudo", "install", "-d"]]
    write = [c for c in calls if c[:3] == ["sudo", "install", "-m"]]
    assert mkdir and mkdir[0][3:] == [str(target.parent)]
    assert write, "创建目录后仍须写入目标"
    assert calls.index(mkdir[0]) < calls.index(write[0]), "install -d 必须先于写入"


def test_linux_elevated_write_raises_when_mkdir_fails(
    monkeypatch, tmp_path, allow_platform_mutation
):
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv: (0, "") if argv == ["sudo", "-v"] else (1, "permission denied"),
    )
    with pytest.raises(PermissionError, match="创建目录"):
        linux_mod.LinuxElevated().write(tmp_path / "etc" / "devproxy.conf", "x")


# -- resolved 活跃探测 / dns_query / http_ok --------------------------------


def test_linux_resolved_active_true_from_systemctl(monkeypatch):
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv: (0, "active") if "is-active" in argv else (1, "")
    )
    assert LinuxPlatform().resolved_active() is True


def test_linux_resolved_active_falls_back_to_resolvectl(monkeypatch):
    def fake_run(argv):
        if "is-active" in argv:
            return 1, "inactive"
        if argv[:1] == ["resolvectl"]:
            return 0, "Global"
        return 1, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().resolved_active() is True


def test_linux_resolved_active_false_when_both_fail(monkeypatch):
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (1, "inactive"))
    assert LinuxPlatform().resolved_active() is False


def test_linux_dns_query_prefers_resolvectl(monkeypatch):
    calls = []

    def fake_run(argv):
        calls.append(argv)
        if argv[:2] == ["resolvectl", "query"]:
            return 0, "foo.test: 127.0.0.1\n          127.0.0.1"
        return 1, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().dns_query("foo.test") == "127.0.0.1"
    assert calls[0][:2] == ["resolvectl", "query"]


def test_linux_dns_query_falls_back_to_dig(monkeypatch):
    calls = []

    def fake_run(argv):
        calls.append(argv)
        if argv[:2] == ["resolvectl", "query"]:
            return 1, "not found"
        if argv[:1] == ["dig"]:
            return 0, "127.0.0.1\n::1"
        return 1, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().dns_query("foo.test") == "127.0.0.1"
    assert any(c[:1] == ["dig"] for c in calls)


def test_linux_dns_query_returns_none_when_unresolvable(monkeypatch):
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (1, "not found"))
    assert LinuxPlatform().dns_query("foo.test") is None


def test_linux_http_ok_uses_bounded_curl(monkeypatch):
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().http_ok("https://localhost") is True
    assert calls[0][:2] == ["curl", "-fsS"]
    assert "--max-time" in calls[0]


def test_linux_http_ok_false_on_curl_failure(monkeypatch):
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (7, "connection refused"))
    assert LinuxPlatform().http_ok("https://localhost") is False


def test_linux_dns_route_installed_accepts_suffix():
    """接口带域名后缀 —— macOS 要按后缀查 /etc/resolver/<suffix>。"""
    assert LinuxPlatform().dns_route_installed("resolved", "test") in (True, False)
    assert LinuxPlatform().dns_route_installed("unknown-backend", "test") is False


def test_fake_platform_dns_route_installed_uses_backend():
    p = FakePlatform()
    assert p.dns_route_installed("resolved", "test") is False
    p.dns_backends.add("resolved")
    assert p.dns_route_installed("resolved", "test") is True


# -- linger -----------------------------------------------------------------


def test_linux_enable_linger_uses_sudo_loginctl(monkeypatch, allow_platform_mutation):
    calls = []
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv: calls.append(argv) or (0, ""),
    )
    code, _ = LinuxPlatform().enable_linger()
    assert code == 0
    assert calls[0][:3] == ["sudo", "loginctl", "enable-linger"]


def test_linux_linger_enabled_reads_value(monkeypatch):
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv: (0, "yes") if "--value" in argv else (1, "")
    )
    assert LinuxPlatform().linger_enabled() is True


def test_linux_linger_enabled_false(monkeypatch):
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (0, "no"))
    assert LinuxPlatform().linger_enabled() is False


def test_linux_linger_enabled_true_when_loginctl_unavailable(monkeypatch):
    """无 loginctl 的宿主（容器 / WSL / 无 systemd）→ linger 不适用。

    判成"未启用"会让每次 setup 都计划一个注定失败、还要 sudo 的动作，
    失败时 setup 直接退 1 —— 整个工具在那种机器上不可用。
    """
    monkeypatch.setattr(linux_mod, "_run", lambda argv: (127, "命令不可用: loginctl"))
    assert LinuxPlatform().linger_enabled() is True


# -- 测试期护栏：真实平台不许改系统 -------------------------------------------


def test_linux_platform_refuses_mutation_under_test(tmp_path, monkeypatch):
    """护栏：测试进程里，真实平台的写操作一律拒绝。

    平台对象一旦拿到真实 HOME，就能改写 ~/.config/systemd/user/ 下的单元、
    启停真实服务、甚至 sudo 写 /etc —— 隔离漏掉一处就会改掉用户机器。
    """
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x")
    p = LinuxPlatform(user_home=tmp_path)
    with pytest.raises(RuntimeError, match="禁止"):
        p.install_service("caddy", exec_start=["/bin/caddy", "run"], env={})
    with pytest.raises(RuntimeError, match="禁止"):
        p.enable_service("caddy")
    with pytest.raises(RuntimeError, match="禁止"):
        p.restart_service("caddy")
    with pytest.raises(RuntimeError, match="禁止"):
        p.enable_linger()
    with pytest.raises(RuntimeError, match="禁止"):
        p.privilege()


def test_linux_platform_refuses_mutation_when_flag_set(tmp_path, monkeypatch):
    """显式开关同样能禁写（不依赖 pytest 环境变量）。"""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("DEVPROXY_NO_MUTATION", "1")
    p = LinuxPlatform(user_home=tmp_path)
    with pytest.raises(RuntimeError, match="禁止"):
        p.install_service("caddy", exec_start=["/bin/caddy", "run"], env={})


def test_linux_platform_read_only_calls_still_work_under_test(tmp_path, monkeypatch):
    """只读探测不受护栏影响 —— doctor 的体检还得能跑。"""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x")
    p = LinuxPlatform(user_home=tmp_path)
    assert p.service_unit_path("caddy") == (
        tmp_path / ".config" / "systemd" / "user" / "caddy.service"
    )
    assert p.binary_version(tmp_path / "absent") == ""
    assert p.linger_enabled() in (True, False)
