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

    def fake_run(argv, **kwargs):
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

    def fake_run(argv, **kwargs):
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

    def fake_run(argv, **kwargs):
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

    def fake_run(argv, **kwargs):
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


def test_linux_open_in_editor_splits_command_and_streams(monkeypatch, tmp_path):
    """编辑器命令按 shell 词法拆开，文件路径跟在最后；且必须继承 stdio。"""
    recorded = {}

    class _Done:
        returncode = 0

    def fake_run(argv, **kwargs):
        recorded["argv"] = list(argv)
        recorded["kwargs"] = kwargs
        return _Done()

    monkeypatch.setattr(linux_mod.subprocess, "run", fake_run)
    target = tmp_path / "myapp.log"
    assert LinuxPlatform().open_in_editor(target, "code -w") == 0
    assert recorded["argv"] == ["code", "-w", str(target)]
    assert "capture_output" not in recorded["kwargs"], "不得捕获输出，编辑器要接管终端"


def test_linux_service_mutations_raise_on_failure(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """enable/restart/daemon-reload 失败必须立即抛 RuntimeError，带上服务名与 stderr。"""
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (1, "unit not found"))
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
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (0, ""))
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

    def fake_run(argv, **kwargs):
        # 存在性靠 `test -e` 的返回码，不解析本地化的 cat 文案
        if "test" in argv:
            return (1 if "missing" in argv[-1] else 0), ""
        if "cat" in argv:
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
    # 散列索引目录与 openssl -CApath 共用同一个常量（不再有第二份定义）
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

    def fake_run(argv, **kwargs):
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
        lambda argv, **kwargs: (1, "error 18 at 0 depth lookup: self-signed certificate"),
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
        lambda argv, **kwargs: (1, "error 20 at 0 depth lookup: unable to get local issuer"),
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
        lambda argv, **kwargs: (1, "error 18 at 0 depth lookup: self-signed certificate"),
    )
    assert LinuxPlatform().ca_trusted(ca) is False


def test_linux_ca_trusted_true_when_openssl_verifies(trust_store, tmp_path, monkeypatch):
    """openssl 验通是补充的肯定信号（指纹扫描没覆盖到时的兜底）。"""
    bundle, hashed, staged = trust_store
    ca = tmp_path / "root.crt"
    ca.write_text(_fake_pem("mine"), encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
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
        linux_mod, "_run", lambda argv, **kwargs: (127, "命令不存在: openssl")
    )
    assert LinuxPlatform().ca_trusted(ca) is False


def test_linux_ca_trusted_false_when_ca_not_pem(trust_store, tmp_path, monkeypatch):
    """CA 文件不是可解析的 PEM —— 判为未信任，不抛异常。"""
    bundle, hashed, staged = trust_store
    ca = tmp_path / "root.crt"
    ca.write_text("这不是证书", encoding="utf-8")
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: (127, "命令不存在: openssl")
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


def _nss_listing(*rows: tuple[str, str]) -> str:
    """造一份 `certutil -L` 的表格输出（昵称, 信任标志）。

    真实输出长这样（列宽 55，超长的昵称会溢出到标志列前，只隔一个空格）::

        Certificate Nickname                    Trust Attributes
                                                     SSL,S/MIME,JAR/XPI

        devproxy Local CA                             C,,
    """
    lines = [
        "",
        "Certificate Nickname                                         Trust Attributes",
        "                                                             SSL,S/MIME,JAR/XPI",
        "",
    ]
    for nickname, flags in rows:
        lines.append(f"{nickname:<55} {flags}  ")
    return "\n".join(lines) + "\n"


def _fake_certutil(listing: str, pem: str, calls: list | None = None):
    """假 certutil：`-L` 给表格，`-L -n <昵称> -a` 给 PEM。"""

    def fake_run(argv, **kwargs):
        if calls is not None:
            calls.append(list(argv))
        if "-a" in argv:
            return 0, pem
        return 0, listing

    return fake_run


def test_linux_nss_trusted_true_when_stored_cert_matches(tmp_path, monkeypatch):
    """判定依据是库里的**证书内容**，不只是同名条目存在。"""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        linux_mod,
        "_run",
        _fake_certutil(_nss_listing(("devproxy Local CA", "C,,")), PEM_OLD, calls),
    )
    assert p.nss_trusted(ca) is True
    assert calls[0][:2] == ["certutil", "-L"], "要先列出库里的条目"
    assert "-a" in calls[-1], "必须让 certutil 打出 PEM 才能比对内容"
    assert "-n" in calls[-1]


def test_linux_nss_trusted_false_when_stored_cert_differs(tmp_path, monkeypatch):
    """Caddy 重新生成 CA 后旧条目还在 —— 同名但内容不同，必须报未信任。"""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_NEW, encoding="utf-8")
    monkeypatch.setattr(
        linux_mod,
        "_run",
        _fake_certutil(_nss_listing(("devproxy Local CA", "C,,")), PEM_OLD),
    )
    assert p.nss_trusted(ca) is False


def test_linux_nss_trusted_ignores_blank_lines_and_crlf(tmp_path, monkeypatch):
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    listing = _nss_listing(("devproxy Local CA", "C,,")).replace("\n", "\r\n")
    noisy = "\r\n" + PEM_OLD.replace("\n", "\r\n") + "\r\n\r\n"
    monkeypatch.setattr(
        linux_mod,
        "_run",
        _fake_certutil(listing, noisy),
    )
    assert p.nss_trusted(ca) is True


def test_linux_nss_trusted_false_when_entry_missing(tmp_path, monkeypatch):
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")

    def fake_run(argv, **kwargs):
        if "-a" in argv:
            return 255, "certutil: could not find cert: devproxy Local CA"
        return 0, _nss_listing(("devproxy Local CA", "C,,"))

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert p.nss_trusted(ca) is False


def test_linux_nss_trusted_false_when_ca_unreadable(tmp_path, monkeypatch):
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_bytes(b"\xff\xfe\x00binary")
    monkeypatch.setattr(
        linux_mod,
        "_run",
        _fake_certutil(_nss_listing(("devproxy Local CA", "C,,")), PEM_OLD),
    )
    assert p.nss_trusted(ca) is False


def test_linux_nss_trusted_false_without_db(tmp_path, monkeypatch):
    p = LinuxPlatform(user_home=tmp_path)
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: pytest.fail("没有 NSS 库就不该跑 certutil")
    )
    assert p.nss_trusted(tmp_path / "root.crt") is False


def test_linux_nss_trusted_false_when_certutil_missing(tmp_path, monkeypatch):
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (127, "命令不存在: certutil"))
    assert p.nss_trusted(ca) is False


# -- NSS 判定：按内容而不是按昵称（真机首跑暴露的缺陷）---------------------


def test_linux_nss_trusted_true_when_cert_stored_under_other_nickname(
    tmp_path, monkeypatch
):
    """同一张 CA 用别的昵称入库也算已信任。

    真机现象：老 devp 装的 CA 昵称是 "Caddy Local Authority"，
    而 NSS 按 (issuer, serial) 去重 —— 工具用 `certutil -A` 换个昵称
    再装一次会**静默无操作**，条目永远不新增。只按自己的昵称查，
    工具就永远查不到这张 CA。
    """
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(
        linux_mod,
        "_run",
        _fake_certutil(_nss_listing(("Caddy Local Authority", "C,,")), PEM_OLD),
    )
    assert p.nss_trusted(ca) is True


def test_linux_nss_trusted_false_when_entry_has_no_trust_flag(tmp_path, monkeypatch):
    """库里有这张 CA 但标成无信任（",,"）—— 不算已信任。"""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(
        linux_mod,
        "_run",
        _fake_certutil(_nss_listing(("Caddy Local Authority", ",,")), PEM_OLD),
    )
    assert p.nss_trusted(ca) is False


def test_linux_nss_entries_reports_nickname_and_flags(tmp_path, monkeypatch):
    """nss_entries 要给出"库里那条"的昵称与标志 —— 组件靠它补信任标志。"""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(
        linux_mod,
        "_run",
        _fake_certutil(_nss_listing(("Caddy Local Authority", ",,")), PEM_OLD),
    )
    assert p.nss_entries(ca) == [("Caddy Local Authority", ",,")]


def test_linux_nss_entries_ignores_nickname_that_cannot_be_fetched(
    tmp_path, monkeypatch
):
    """表格解析出来的昵称取不到证书时必须跳过 —— 宁可漏判也不能误判。

    必须真的走到**枚举**分支：让自有昵称的内容也对不上（快路径不命中），
    否则这条用例只是把快路径又测了一遍，声称的"跳过坏候选"根本没执行。
    """
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")

    def fake_run(argv, **kwargs):
        if "-a" in argv:
            nickname = argv[argv.index("-n") + 1]
            if "取不到" in nickname:
                return 255, "certutil: could not find cert"
            if nickname == "devproxy Local CA":
                return 0, PEM_NEW  # 内容不同 → 快路径不命中，走枚举
            return 0, PEM_OLD
        return 0, _nss_listing(
            ("取不到的那条", "C,,"),
            ("devproxy Local CA", "C,,"),
            ("用户那条", "C,,"),
        )

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    entries = p.nss_entries(ca)
    assert entries == [("用户那条", "C,,")]
    assert "取不到的那条" not in [n for n, _ in entries]


def test_linux_nss_entries_fast_path_stops_after_own_nickname(tmp_path, monkeypatch):
    """自有昵称命中且可信 → 只查它一条，不为其余条目各跑一次 -L -n -a。"""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "-a" in argv:
            return 0, PEM_OLD
        return 0, _nss_listing(
            ("别人的条目一", "C,,"),
            ("devproxy Local CA", "C,,"),
            ("别人的条目二", "C,,"),
            ("别人的条目三", "C,,"),
        )

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert p.nss_entries(ca) == [("devproxy Local CA", "C,,")]
    # 一次列表 + 一次取自有昵称的 PEM = 2 次调用（全量枚举会是 5 次）
    assert len(calls) == 2, calls


def test_linux_nss_entries_empty_without_db(tmp_path, monkeypatch):
    p = LinuxPlatform(user_home=tmp_path)
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: pytest.fail("没有 NSS 库就不该跑 certutil")
    )
    assert p.nss_entries(tmp_path / "root.crt") == []


def test_linux_nss_rows_parses_nickname_with_spaces_and_long_names():
    """昵称可以含空格，也可以长到溢出列宽 —— 只能从行尾切标志。"""
    listing = _nss_listing(
        ("with space in it", "P,,"),
        ("a-very-long-nickname-that-exceeds-the-column-width-by-quite-a-lot", "C,,"),
    )
    rows = linux_mod._nss_rows(listing)
    assert ("with space in it", "P,,") in rows
    assert (
        "a-very-long-nickname-that-exceeds-the-column-width-by-quite-a-lot",
        "C,,",
    ) in rows


# -- fix round 1：只认 SSL 列的 "C" + 三列标志 -------------------------------


@pytest.mark.parametrize(
    "flags, expected",
    [
        ("C,,", True),
        ("C,C,", True),
        ("C,C,C", True),
        ("T,,", False),
        ("P,,", False),
        (",C,", False),
        (",,", False),
        ("", False),
    ],
)
def test_linux_nss_flags_trusted_only_accepts_ssl_column_c(flags, expected):
    """只有 SSL 列的 "C" 是 HTTPS 的 CA 锚点。

    "T" 是签发**客户端**证书的 CA、"P" 是受信任的**对端**（叶子），
    两者都不是根 CA 锚点；",C," 的 SSL 列是空的。把 T / P 算成已信任
    是假阳性 —— Chrome 根本不会拿它们当锚点。
    """
    assert linux_mod._nss_flags_trusted(flags) is expected


@pytest.mark.parametrize("flags", ["C,,", "C,C,", "C,C,C", ",C,", ",,"])
def test_linux_nss_rows_parses_every_three_column_form(flags):
    """标志是**三列**：`C,,` / `C,C,` / `C,C,C` / `,C,` / `,,` 都要能解析。

    旧正则要求"至少两个连续逗号"，`C,C,C` 直接解析不出来 ——
    而真机上 `Caddy Local Authority` 就是这个形态。
    （`certutil -t ",,,"` 会被归一成 `,,` 入库，列表里不存在四列的形态。）
    """
    rows = linux_mod._nss_rows(_nss_listing(("Caddy Local Authority", flags)))
    assert rows == [("Caddy Local Authority", flags)]


def test_linux_nss_rows_parses_row_without_trailing_padding():
    """`C,C,C` 行尾没有补空格（`C,,` 有两个）—— 必须先 rstrip 再匹配。"""
    listing = "Caddy Local Authority                                        C,C,C"
    assert linux_mod._nss_rows(listing) == [("Caddy Local Authority", "C,C,C")]


def test_linux_nss_rows_ignores_header_lines():
    """两行表头不能被当成条目（它们没有三列逗号形态）。"""
    listing = (
        "\n"
        "Certificate Nickname                                         Trust Attributes\n"
        "                                                             SSL,S/MIME,JAR/XPI\n"
        "\n"
        "devproxy Local CA                                            C,,\n"
    )
    assert linux_mod._nss_rows(listing) == [("devproxy Local CA", "C,,")]


def test_linux_nss_trust_flags_repairs_only_ssl_column():
    """补标志时保留另外两列：`,C,` → `C,C,`，不是 `C,,`。"""
    assert linux_mod._nss_repair_flags("C,,") == "C,,"
    assert linux_mod._nss_repair_flags(",,", ) == "C,,"
    assert linux_mod._nss_repair_flags(",C,") == "C,C,"
    assert linux_mod._nss_repair_flags(",C,C") == "C,C,C"
    # 形态不认识时**原样返回**（绝不重置成 "C,,"）：重置会抹掉未知形态里
    # 可能存在的 S/MIME / JAR 信任，正是本函数要避免的事。
    assert linux_mod._nss_repair_flags("") == ""
    assert linux_mod._nss_repair_flags("C") == "C"


def test_linux_nss_db_path_points_at_user_home(tmp_path):
    """平台要能报出自己认定的库路径（组件靠它跟写入路径对齐）。"""
    p = LinuxPlatform(user_home=tmp_path)
    assert p.nss_db_path() == tmp_path / ".pki" / "nssdb"


def test_linux_nss_entries_accepts_explicit_nss_dir(tmp_path, monkeypatch):
    """显式给库路径时查的就是那个库（复查必须查写进去的那个）。"""
    home = tmp_path / "home"
    other = tmp_path / "other"
    (other / "").mkdir(parents=True, exist_ok=True)
    (other / "cert9.db").write_text("", encoding="utf-8")
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    p = LinuxPlatform(user_home=home)

    seen = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        if "-a" in argv:
            return 0, PEM_OLD
        return 0, _nss_listing(("devproxy Local CA", "C,,"))

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert p.nss_entries(ca, nss_dir=other) == [("devproxy Local CA", "C,,")]
    assert all(f"sql:{other}" in c for c in seen if "-d" in c), (
        "所有 certutil 调用都必须指向显式给的库"
    )


def test_linux_nss_stale_nickname_true_for_other_cert(tmp_path, monkeypatch):
    """自有昵称下是**别的**证书 → 陈旧，该删。"""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_NEW, encoding="utf-8")
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (0, PEM_OLD))
    assert p.nss_stale_nickname(ca, "devproxy Local CA") is True


def test_linux_nss_stale_nickname_false_for_same_cert(tmp_path, monkeypatch):
    """自有昵称下就是当前证书 → 不陈旧，不能删。"""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (0, PEM_OLD))
    assert p.nss_stale_nickname(ca, "devproxy Local CA") is False


def test_linux_nss_stale_nickname_false_when_absent(tmp_path, monkeypatch):
    """昵称下没有条目 → 不是陈旧（没有可删的东西）。"""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv, **kwargs: (255, "certutil: could not find cert: x"),
    )
    assert p.nss_stale_nickname(ca, "devproxy Local CA") is False


def test_linux_nss_has_nickname_true_and_false(tmp_path, monkeypatch):
    """`-L -n <昵称>` 跑通 = 存在；输出明确说找不到 = 不存在。"""
    p = _nss_platform(tmp_path)
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (0, "Certificate: …"))
    assert p.nss_has_nickname("devproxy Local CA") is True
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv, **kwargs: (255, 'certutil: could not find certificate named "x"'),
    )
    assert p.nss_has_nickname("devproxy Local CA") is False


def test_linux_nss_has_nickname_unknown_when_database_unreadable(
    tmp_path, monkeypatch
):
    """库被锁 / 不可读时 `-L` 也非零 —— 这是"判不了"，不是"不存在"。

    实测 `chmod 000 cert9.db` 后 certutil 输出
    `SEC_ERROR_BAD_DATABASE: security library: bad database.`，
    条目其实还在。把它当"不存在"，删除失败就会被当成幂等成功吞掉。
    """
    p = _nss_platform(tmp_path)
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv, **kwargs: (255, "certutil: function failed: SEC_ERROR_BAD_DATABASE"),
    )
    assert p.nss_has_nickname("devproxy Local CA") is None


def test_linux_nss_has_nickname_false_without_db(tmp_path, monkeypatch):
    p = LinuxPlatform(user_home=tmp_path)
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: pytest.fail("没有 NSS 库就不该跑 certutil")
    )
    assert p.nss_has_nickname("devproxy Local CA") is False


def test_linux_nss_stale_nickname_unknown_when_unparsable(tmp_path, monkeypatch):
    """取回来的不是 PEM → 判不了（None），绝不能报"确定不陈旧"。

    删除对象固定是工具自有昵称，不存在误删用户条目的风险；把"判不了"
    压成 False 会让陈旧根永久留在库里。
    """
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (0, "not a pem"))
    assert p.nss_stale_nickname(ca, "devproxy Local CA") is None


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

    def fake_run(argv, **kwargs):
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
        lambda argv, **kwargs: (0, "") if argv == ["sudo", "-v"] else (1, "permission denied"),
    )
    with pytest.raises(PermissionError, match="创建目录"):
        linux_mod.LinuxElevated().write(tmp_path / "etc" / "devproxy.conf", "x")


# -- 免密机器上的提权（真机首跑暴露的缺陷）------------------------------------
#
# 真机 sudoers：
#     (ALL : ALL) ALL
#     (ALL) NOPASSWD: ALL
# `sudo -v` 是"无命令"调用，不匹配 NOPASSWD 的命令规范 → 落到默认策略
# → 需要认证 → 无 TTY 时报 "A terminal is required to authenticate"。
# 这台机器根本不需要密码 —— 判成"提权失败"会让所有特权步骤瘫痪。


def test_linux_elevated_falls_back_to_passwordless_sudo(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """sudo -v 失败但 sudo -n true 成功 → 提权可用，后续命令自带 -n。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv == ["sudo", "-v"]:
            return 1, "sudo: A terminal is required to authenticate"
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    sess = linux_mod.LinuxElevated()
    code, _ = sess.run(["install", "-d", "/etc/devproxy"])
    assert code == 0
    assert ["sudo", "-n", "true"] in calls, "免密能力必须靠 sudo -n true 探测"
    assert ["sudo", "-n", "install", "-d", "/etc/devproxy"] in calls, (
        "免密路径下的特权命令必须自带 -n，绝不能退化成会弹密码框的 sudo"
    )
    assert not [c for c in calls if c[:2] == ["sudo", "install"]], (
        "免密路径下不允许出现会弹密码框的 sudo <cmd>"
    )


def test_linux_elevated_keeps_plain_sudo_when_pre_auth_succeeds(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """sudo -v 成功 → 维持现状：时间戳已预热，命令就是 sudo <cmd>。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    sess = linux_mod.LinuxElevated()
    sess.run(["install", "-d", "/etc/devproxy"])
    assert ["sudo", "install", "-d", "/etc/devproxy"] in calls
    assert not [c for c in calls if c[:2] == ["sudo", "-n", "true"]], (
        "预热成功就不必再探免密"
    )


def test_linux_elevated_raises_when_both_sudo_probes_fail(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """两条路都走不通 → 抛 PermissionError，信息要带上两条命令的输出。"""
    def fake_run(argv, **kwargs):
        if argv == ["sudo", "-v"]:
            return 1, "sudo: a terminal is required to authenticate"
        if argv == ["sudo", "-n", "true"]:
            return 1, "sudo: a password is required"
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    with pytest.raises(PermissionError) as ei:
        linux_mod.LinuxElevated()
    message = str(ei.value)
    assert "sudo -v" in message and "a terminal is required" in message
    assert "sudo -n true" in message and "a password is required" in message


def test_linux_elevated_passwordless_path_survives_a_failing_command(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """免密路径下命令失败照常返回错误码 —— 不该因为 -n 就当成提权失败。"""
    def fake_run(argv, **kwargs):
        if argv == ["sudo", "-v"]:
            return 1, "sudo: A terminal is required to authenticate"
        if argv == ["sudo", "-n", "true"]:
            return 0, ""
        return 1, "install: cannot create directory"

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    sess = linux_mod.LinuxElevated()
    code, msg = sess.run(["install", "-d", "/etc/devproxy"])
    assert code == 1
    assert "cannot create directory" in msg


# -- resolved 活跃探测 / dns_query / http_ok --------------------------------


def test_linux_resolved_active_true_from_systemctl(monkeypatch):
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: (0, "active") if "is-active" in argv else (1, "")
    )
    assert LinuxPlatform().resolved_active() is True


def test_linux_resolved_active_falls_back_to_resolvectl(monkeypatch):
    def fake_run(argv, **kwargs):
        if "is-active" in argv:
            return 1, "inactive"
        if argv[:1] == ["resolvectl"]:
            return 0, "Global"
        return 1, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().resolved_active() is True


def test_linux_resolved_active_false_when_both_fail(monkeypatch):
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (1, "inactive"))
    assert LinuxPlatform().resolved_active() is False


def test_linux_dns_query_prefers_resolvectl(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["resolvectl", "query"]:
            return 0, "foo.test: 127.0.0.1\n          127.0.0.1"
        return 1, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().dns_query("foo.test") == "127.0.0.1"
    assert calls[0][:2] == ["resolvectl", "query"]


def test_linux_dns_query_falls_back_to_dig(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
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
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (1, "not found"))
    assert LinuxPlatform().dns_query("foo.test") is None


def test_linux_http_ok_uses_bounded_curl(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().http_ok("https://localhost") is True
    assert calls[0][:3] == ["curl", "--noproxy", "*"], "本地探测必须绕过用户代理"
    assert "-fsS" in calls[0]
    assert "--max-time" in calls[0]


def test_linux_http_ok_false_on_curl_failure(monkeypatch):
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (7, "connection refused"))
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


def test_linux_enable_linger_prefers_noninteractive_sudo(
    monkeypatch, allow_platform_mutation
):
    """与平台其他特权操作一致：先 `sudo -n`，绝不主动弹密码框。"""
    calls = []
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv, **kwargs: calls.append(argv) or (0, ""),
    )
    code, _ = LinuxPlatform().enable_linger()
    assert code == 0
    assert calls[0][:4] == ["sudo", "-n", "loginctl", "enable-linger"]


def test_linux_linger_enabled_reads_value(monkeypatch):
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: (0, "yes") if "--value" in argv else (1, "")
    )
    assert LinuxPlatform().linger_enabled() is True


def test_linux_linger_enabled_false(tmp_path, monkeypatch):
    # 必须把 systemd 运行目录也指到临时目录：`linger_status()` 在没有
    # /run/systemd/system 的宿主（容器、以及跑着 Linux 测试的 macOS）上
    # 会直接判"linger 不适用"返回 True，那样这条用例测的就不是解析分支了。
    monkeypatch.setattr(linux_mod, "SYSTEMD_RUN_DIR", tmp_path)
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (0, "no"))
    assert LinuxPlatform().linger_enabled() is False


def test_linux_linger_enabled_true_when_loginctl_unavailable(monkeypatch):
    """无 loginctl 的宿主（容器 / WSL / 无 systemd）→ linger 不适用。

    判成"未启用"会让每次 setup 都计划一个注定失败、还要 sudo 的动作，
    失败时 setup 直接退 1 —— 整个工具在那种机器上不可用。
    """
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (127, "命令不可用: loginctl"))
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
        p.remove_service("caddy")
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


# -- 子进程超时：防挂死，不是性能优化 -------------------------------------


def test_run_returns_timeout_code_instead_of_hanging(monkeypatch):
    """卡在交互提示上的命令必须被超时打断。

    真机事故（2026-10-23）：`certutil -N --empty-password` 在某些情形下
    不认这个开关，改为反复打印 "Invalid password. Try again." /
    "Error opening input terminal for read"，两小时写满两个 200GB 的
    重定向文件，把 /home 撑到 100%。没有超时的子进程调用就是**无限写入**。
    """
    from devproxy.platform import linux as linux_mod

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout"))

    monkeypatch.setattr(linux_mod.subprocess, "run", boom)
    code, msg = linux_mod._run(["certutil", "-N"], timeout=1.0)
    assert code == 124, "沿用 coreutils timeout 的约定"
    assert "超时" in msg


def test_platform_run_passes_a_timeout(monkeypatch):
    """普通命令调用必须显式带上超时 —— 漏传就等于回到"无限等待"。"""
    from devproxy.platform import linux as linux_mod

    seen = {}

    def fake(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(linux_mod.subprocess, "run", fake)
    code, _ = linux_mod.LinuxPlatform().run(["true"])
    assert code == 0
    assert seen.get("timeout"), "LinuxPlatform.run 必须传 timeout"


# -- p4：超时默认值、交互路径与代理绕过 ---------------------------------------


def test_run_defaults_to_bounded_timeout_and_devnull_stdin(monkeypatch):
    """`_run` 漏传 timeout 时也必须是有界等待，且不能抢终端输入。"""
    seen = {}

    def fake(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(linux_mod.subprocess, "run", fake)
    code, _ = linux_mod._run(["true"])
    assert code == 0
    assert seen["timeout"] == linux_mod.DEFAULT_TIMEOUT
    assert seen["stdin"] is subprocess.DEVNULL


def test_linux_dns_query_has_bounded_timeout(monkeypatch):
    """resolved 挂起不能把 verify() 永久拖死 —— 两条查询路径都要有上限。"""
    timeouts = []

    def fake_run(argv, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return 1, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().dns_query("foo.test") is None
    assert timeouts and all(t is not None for t in timeouts)
    assert all(t <= 5 for t in timeouts)


def test_linux_http_ok_bypasses_proxy_env(monkeypatch):
    """公司代理环境下，本地回环探测不能被 http(s)_proxy 带走。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    calls = []
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv, **kwargs: (calls.append(list(argv)) or (0, "")),
    )
    assert LinuxPlatform().http_ok("https://codeup.test") is True
    assert calls[0][1:3] == ["--noproxy", "*"]


def test_linux_elevated_interactive_run_allows_waiting_for_password(
    monkeypatch, allow_platform_mutation
):
    """交互路径可能等用户输密码，只有这条路径允许无超时。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    sess = linux_mod.LinuxElevated.__new__(linux_mod.LinuxElevated)
    sess._noninteractive = False
    sess.run(["install", "-d", "/etc/devproxy"])
    argv, kwargs = calls[-1]
    assert argv == ["sudo", "install", "-d", "/etc/devproxy"]
    assert kwargs.get("timeout", "missing") is None


def test_linux_elevated_noninteractive_run_uses_default_timeout(
    monkeypatch, allow_platform_mutation
):
    """免密路径不走密码提示，必须回到默认的有界等待。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    sess = linux_mod.LinuxElevated.__new__(linux_mod.LinuxElevated)
    sess._noninteractive = True
    sess.run(["true"])
    argv, kwargs = calls[-1]
    assert argv == ["sudo", "-n", "true"]
    assert "timeout" not in kwargs, "省略 timeout 应落到 DEFAULT_TIMEOUT 而非 None"


# -- p4：linger 三态与 enable_linger 的交互回退 -------------------------------


def test_linux_linger_status_unknown_is_fail_closed(tmp_path, monkeypatch):
    """systemd 在、探测失败 → 判不了；plan 必须按“未启用”继续尝试。"""
    run_dir = tmp_path / "run-systemd"
    run_dir.mkdir()
    monkeypatch.setattr(linux_mod, "SYSTEMD_RUN_DIR", run_dir)
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: (1, "Failed to connect to bus")
    )
    p = LinuxPlatform()
    assert p.linger_status() is None
    assert p.linger_enabled() is False


def test_linux_linger_status_not_applicable_without_systemd(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_mod, "SYSTEMD_RUN_DIR", tmp_path / "absent")
    monkeypatch.setattr(
        linux_mod, "_run", lambda *a, **k: pytest.fail("没有 systemd 就不该探测")
    )
    assert LinuxPlatform().linger_status() is True


def test_linux_linger_status_not_applicable_without_loginctl(tmp_path, monkeypatch):
    run_dir = tmp_path / "run-systemd"
    run_dir.mkdir()
    monkeypatch.setattr(linux_mod, "SYSTEMD_RUN_DIR", run_dir)
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: (127, "命令不可用: loginctl")
    )
    assert LinuxPlatform().linger_status() is True


def test_linux_enable_linger_falls_back_to_prompting_sudo(
    monkeypatch, allow_platform_mutation
):
    """有 TTY 且 `sudo -n` 失败时才允许回退到会提示密码的 sudo。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if len(calls) == 1:
            return 1, "sudo: a password is required"
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    monkeypatch.setattr(
        linux_mod.sys, "stdin", type("FakeTTY", (), {"isatty": lambda self: True})()
    )
    code, _ = LinuxPlatform().enable_linger()
    assert code == 0
    argv, kwargs = calls[1]
    assert argv[:2] == ["sudo", "loginctl"]
    assert kwargs.get("timeout", "missing") is None


def test_linux_enable_linger_noninteractive_never_prompts(
    monkeypatch, allow_platform_mutation
):
    """无 TTY 时绝不回退到密码提示，而是返回可解释的错误。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return 1, "sudo: a password is required"

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    monkeypatch.setattr(
        linux_mod.sys, "stdin", type("NoTTY", (), {"isatty": lambda self: False})()
    )
    code, msg = LinuxPlatform().enable_linger()
    assert code != 0
    assert "非交互" in msg
    assert len(calls) == 1


# -- p4：单元渲染的换行校验与 root 拒绝 ---------------------------------------


@pytest.mark.parametrize(
    ("env", "description"),
    [
        ({"A": "ok\nExecStartPre=/bin/evil"}, ""),
        ({"A\nB": "x"}, ""),
        ({}, "line1\nline2"),
        ({}, "line1\rline2"),
    ],
)
def test_render_systemd_unit_rejects_newlines(env, description):
    """Environment / Description 也是外部输入，换行会伪造 unit 行。"""
    with pytest.raises(RuntimeError, match="换行"):
        render_systemd_unit(
            "caddy", ["/bin/caddy", "run"], None, None, env, description
        )


def test_linux_platform_refuses_to_run_as_root(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_mod.os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="普通用户"):
        LinuxPlatform(user_home=tmp_path)


# -- p4：NSS 探测的三态与精确缺失文案 -----------------------------------------


def test_linux_nss_probes_treat_stat_failure_as_unknown(tmp_path, monkeypatch):
    """`Path.exists()` 会把 PermissionError 吞成 False —— 必须保留“判不了”。"""
    db = tmp_path / ".pki" / "nssdb"
    db.mkdir(parents=True)
    cert9 = db / "cert9.db"
    cert9.write_text("", encoding="utf-8")
    real_stat = Path.stat

    def guarded_stat(self, *args, **kwargs):
        if self == cert9:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(
        linux_mod, "_run", lambda *a, **k: pytest.fail("库不可读时不该跑 certutil")
    )
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    p = LinuxPlatform(user_home=tmp_path)

    assert p.nss_entries(ca) is None
    assert p.nss_stale_nickname(ca, "devproxy Local CA") is None
    assert p.nss_has_nickname("devproxy Local CA") is None
    assert p.nss_trusted(ca) is False, "判不了时按未信任处理（fail closed）"


def test_linux_nss_entries_unknown_when_listing_fails(tmp_path, monkeypatch):
    """`certutil -L` 非零时无法区分空库与坏库 —— 不能返回 []。"""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: (255, "SEC_ERROR_BAD_DATABASE")
    )
    assert p.nss_entries(ca) is None


def test_linux_nss_missing_cert_only_accepts_exact_markers():
    """泛化的 "not found" 会把模块加载失败误判成条目不存在。"""
    assert linux_mod._nss_missing_cert(
        'certutil: could not find certificate named "x"'
    )
    assert linux_mod._nss_missing_cert("certutil: could not find cert: x")
    assert linux_mod._nss_missing_cert("PR_FILE_NOT_FOUND_ERROR: ...")
    assert not linux_mod._nss_missing_cert("not found")
    assert not linux_mod._nss_missing_cert(
        "could not find the module libsoftokn3.so"
    )


def test_linux_nss_has_nickname_unknown_when_generic_load_error(
    tmp_path, monkeypatch
):
    p = _nss_platform(tmp_path)
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv, **kwargs: (255, "could not find the module libsoftokn3.so"),
    )
    assert p.nss_has_nickname("devproxy Local CA") is None


def test_linux_ca_trusted_does_not_swallow_bugs(monkeypatch, tmp_path):
    """指纹判定的“永不抛异常”由被调函数负责，外层不再宽泛 catch。"""

    def boom(ca):
        raise ValueError("bug")

    monkeypatch.setattr(linux_mod, "ca_in_trust_store", boom)
    with pytest.raises(ValueError, match="bug"):
        LinuxPlatform().ca_trusted(tmp_path / "root.crt")


def test_linux_nss_trusted_does_not_swallow_bugs(tmp_path, monkeypatch):
    p = _nss_platform(tmp_path)

    def boom(ca):
        raise ValueError("bug")

    monkeypatch.setattr(p, "nss_entries", boom)
    with pytest.raises(ValueError, match="bug"):
        p.nss_trusted(tmp_path / "root.crt")


def test_nss_db_dir_expands_home_every_call(tmp_path, monkeypatch):
    """常量不能做成导入期快照 —— 测试重定向 HOME 后必须生效。"""
    from devproxy.platform.base import nss_db_dir

    assert nss_db_dir(tmp_path) == tmp_path / ".pki" / "nssdb"
    monkeypatch.setenv("HOME", str(tmp_path))
    assert nss_db_dir() == tmp_path / ".pki" / "nssdb"


# -- p4：假平台的沙箱与 NSS 语义 ----------------------------------------------


def test_fake_elevated_write_sandboxes_system_paths():
    p = FakePlatform()
    target = Path("/etc/systemd/resolved.conf.d/devproxy.conf")
    p.privilege().write(target, "nameserver 127.0.0.1\n")
    mapped = p.sandbox / "etc/systemd/resolved.conf.d/devproxy.conf"
    assert mapped.read_text(encoding="utf-8") == "nameserver 127.0.0.1\n"
    assert p.written[target] == "nameserver 127.0.0.1\n"


def test_fake_elevated_write_rejects_relative_paths():
    p = FakePlatform()
    with pytest.raises(AssertionError, match="绝对路径"):
        p.privilege().write(Path("relative.conf"), "x")


def test_fake_privilege_read_never_touches_real_system_files():
    p = FakePlatform()
    target = Path("/etc/devproxy/secret.conf")
    p.privilege().write(target, "sandboxed")
    assert p.privilege_read(target) == "sandboxed"
    # 宿主机真实文件不参与：映射后的沙箱里没有它，只能得到 None。
    assert p.privilege_read(Path("/etc/hostname")) is None


def test_fake_elevated_copy_closes_install_then_recheck_loop(tmp_path):
    """copy 成功后立刻记入 trusted —— 安装 → 复查这条链路要真的闭合。"""
    p = FakePlatform()
    ca = tmp_path / "root.crt"
    ca.write_text("FAKE CA\n", encoding="utf-8")
    dest = Path("/usr/local/share/ca-certificates/devproxy-local.crt")
    p.privilege().copy(ca, dest)
    assert p.ca_trusted(ca) is True
    mapped = p.sandbox / "usr/local/share/ca-certificates/devproxy-local.crt"
    assert mapped.read_text(encoding="utf-8") == "FAKE CA\n"
    assert p.written[dest] == "FAKE CA\n"
    assert ["cp", str(ca), str(dest)] in p.commands


def test_fake_certutil_only_ssl_column_counts_as_trusted(tmp_path):
    p = FakePlatform()
    ca = tmp_path / "ca.crt"
    ca.write_text("CA\n", encoding="utf-8")
    p.run(
        ["certutil", "-A", "-d", f"sql:{p.nss_db}", "-n", "n", "-t", ",,", "-i", str(ca)]
    )
    assert p.nss_entries(ca) == [("n", ",,")]
    assert p.nss_trusted(ca) is False, "SSL 列为空不能算已信任"
    p.run(["certutil", "-M", "-d", f"sql:{p.nss_db}", "-n", "n", "-t", "C,,"])
    assert p.nss_trusted(ca) is True


def test_fake_certutil_scopes_writes_and_deletes_by_database(tmp_path):
    p = FakePlatform()
    ca = tmp_path / "ca.crt"
    ca.write_text("CA\n", encoding="utf-8")
    db_a = tmp_path / "db-a"
    db_b = tmp_path / "db-b"
    p.run(
        ["certutil", "-A", "-d", f"sql:{db_a}", "-n", "n", "-t", "C,,", "-i", str(ca)]
    )
    assert p.nss_trusted(ca) is False, "写进 A 库不等于默认库已信任"
    assert p.nss_entries(ca, nss_dir=db_a) == [("n", "C,,")]
    assert p.nss_entries(ca, nss_dir=db_b) == []
    p.run(["certutil", "-D", "-d", f"sql:{db_b}", "-n", "n"])
    assert p.nss_has_nickname("n", nss_dir=db_a) is True, "删错库不能影响 A"
    p.run(["certutil", "-D", "-d", f"sql:{db_a}", "-n", "n"])
    assert p.nss_has_nickname("n", nss_dir=db_a) is False


def test_fake_nss_stale_nickname_matches_linux_tristate(tmp_path):
    p = FakePlatform()
    old = tmp_path / "old.crt"
    old.write_text("OLD\n", encoding="utf-8")
    new = tmp_path / "new.crt"
    new.write_text("NEW\n", encoding="utf-8")
    p.run(
        [
            "certutil",
            "-A",
            "-d",
            f"sql:{p.nss_db}",
            "-n",
            "devproxy Local CA",
            "-t",
            "C,,",
            "-i",
            str(old),
        ]
    )
    assert p.nss_stale_nickname(new, "devproxy Local CA") is True
    assert p.nss_stale_nickname(old, "devproxy Local CA") is False
    assert p.nss_stale_nickname(new, "absent") is False
    old.unlink()
    assert p.nss_stale_nickname(new, "devproxy Local CA") is None, "内容读不到 → 判不了"


def test_fake_nss_helpers_cover_linux_optional_interface(tmp_path):
    """少一个方法，组件测试就会走“降级平台”分支而不是 Linux 分支。"""
    p = FakePlatform()
    for name in (
        "nss_db_path",
        "nss_entries",
        "nss_stale_nickname",
        "nss_has_nickname",
        "nss_trust_flags",
    ):
        assert callable(getattr(p, name)), f"假平台缺少 {name}"
    assert p.nss_db_path() == p.nss_db
    assert p.nss_trust_flags(",C,") == "C,C,"
    assert p.nss_trust_flags("broken") == "broken", "未知形态必须原样返回"


def test_fake_linger_status_tracks_enable_linger():
    p = FakePlatform()
    assert p.linger_status() is False
    p.enable_linger()
    assert p.linger_status() is True
