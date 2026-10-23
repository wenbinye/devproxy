"""macOS 平台实现的单元测试。

本文件**绝不碰真实系统**：所有会起子进程的地方（launchctl / sudo / security /
tail / dscacheutil）都把 macos._run 或 subprocess.run 换成假的；路径一律指向
tmp_path。
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from devproxy.platform import macos as macos_mod
from devproxy.platform.base import ElevatedSession, Platform
from devproxy.platform.macos import (
    MacOSElevated,
    MacOSPlatform,
    get_platform,
    render_plist,
    render_resolver_file,
)


@pytest.fixture
def allow_platform_mutation(monkeypatch):
    """本文件专门验证平台的写操作：_run 已被替换、路径指向 tmp_path，
    可以安全地关掉"测试期禁写"护栏。"""
    monkeypatch.setattr(macos_mod, "_ALLOW_MUTATION", True)


@pytest.fixture
def fake_run(monkeypatch, allow_platform_mutation):
    """把 macos._run 换成假的 —— 绝不真的调用 launchctl / sudo / security。

    同时豁免"测试期禁写"护栏：本夹具只用于验证写路径，而所有路径都指向
    tmp_path、命令都是假的。
    """
    calls: list[list[str]] = []

    def _fake(argv, timeout=None):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", _fake)
    return calls


# ---------- 基本形态 ----------


def test_dns_port_avoids_mdns():
    """53 端口被 mDNSResponder 占着 —— 用户级 dnsmasq 只能走 5300。"""
    assert MacOSPlatform().dns_port == 5300


def test_macos_platform_name_is_mac():
    assert MacOSPlatform().name == "mac"


def test_platform_implements_whole_protocol():
    """协议一共 19 个方法 —— 少一个都是运行期 AttributeError，不是导入期。"""
    p = MacOSPlatform()
    assert isinstance(p, Platform)
    assert isinstance(p.privilege.__self__, MacOSPlatform)
    for method in (
        "privilege",
        "run",
        "binary_version",
        "service_active",
        "install_service",
        "enable_service",
        "restart_service",
        "remove_service",
        "service_unit_path",
        "tail_service_log",
        "enable_linger",
        "linger_enabled",
        "resolved_active",
        "dns_query",
        "http_ok",
        "ca_trusted",
        "nss_trusted",
        "dns_route_installed",
        "privilege_read",
    ):
        assert callable(getattr(p, method, None)), f"缺少协议方法: {method}"


def test_elevated_session_implements_protocol():
    assert isinstance(MacOSElevated.__new__(MacOSElevated), ElevatedSession)


# ---------- 服务单元路径：caddy 是 system scope，其余是 user scope ----------


def test_caddy_service_is_system_scope(tmp_path):
    """macOS 没有 setcap —— 绑 80/443 只能靠 root 的 LaunchDaemon。"""
    p = MacOSPlatform(user_home=tmp_path)
    path = p.service_unit_path("caddy")
    assert path.parent == Path("/Library/LaunchDaemons")
    assert path.name == "com.devproxy.caddy.plist"


def test_dnsmasq_service_is_user_scope(tmp_path):
    p = MacOSPlatform(user_home=tmp_path)
    path = p.service_unit_path("dnsmasq")
    assert str(path).startswith(str(tmp_path))
    assert "LaunchAgents" in str(path)
    assert path.name == "com.devproxy.dnsmasq.plist"


def test_log_path_is_under_state_logs(tmp_path):
    p = MacOSPlatform(user_home=tmp_path)
    assert p.log_path("caddy") == (
        tmp_path / ".local" / "state" / "devproxy" / "logs" / "caddy.log"
    )


# ---------- plist 渲染 ----------


def test_render_plist_is_valid_plist():
    text = render_plist(
        "caddy",
        ["/opt/caddy", "run", "--config", "/Users/u/Caddyfile"],
        {"MINIKUBE_IP": "1.2.3.4"},
        Path("/Users/u/.local/state/devproxy/logs/caddy.log"),
    )
    data = plistlib.loads(text.encode())
    assert data["Label"] == "com.devproxy.caddy"
    assert data["ProgramArguments"][0] == "/opt/caddy"
    assert data["EnvironmentVariables"]["MINIKUBE_IP"] == "1.2.3.4"
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    assert data["StandardOutPath"].endswith("caddy.log")
    assert data["StandardErrorPath"].endswith("caddy.log")


def test_render_plist_rejects_relative_program_path():
    """launchd 以 root 运行 —— 相对路径与 ~ 都会指到别处，必须拦住。"""
    with pytest.raises(RuntimeError, match="绝对路径"):
        render_plist("caddy", ["caddy", "run"], {}, Path("/tmp/caddy.log"))
    with pytest.raises(RuntimeError, match="绝对路径"):
        render_plist("caddy", ["~/bin/caddy", "run"], {}, Path("/tmp/caddy.log"))


def test_render_plist_keeps_description():
    text = render_plist(
        "caddy",
        ["/opt/caddy", "run"],
        {},
        Path("/tmp/caddy.log"),
        description="Caddy for devproxy",
    )
    data = plistlib.loads(text.encode())
    assert data["Description"] == "Caddy for devproxy"


def test_render_resolver_file_has_port():
    text = render_resolver_file(5300)
    assert "nameserver 127.0.0.1" in text
    assert "port 5300" in text


# ---------- 提权会话 ----------


def test_macos_privilege_session_is_cached(tmp_path, monkeypatch, allow_platform_mutation):
    """一次 sudo -v 认证，整个会话复用 —— 否则每次操作都要重输密码。"""
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: (0, ""))
    p = MacOSPlatform(user_home=tmp_path)
    assert p.privilege() is p.privilege()


def test_privilege_raises_when_pre_auth_fails(tmp_path, monkeypatch, allow_platform_mutation):
    """预认证失败必须抛 —— 静默降级会让后续每个写操作都莫名失败。"""
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: (1, "boom"))
    p = MacOSPlatform(user_home=tmp_path)
    with pytest.raises(PermissionError):
        p.privilege()


# -- 免密机器上的提权（与 Linux 同一套语义）----------------------------------


def test_macos_elevated_falls_back_to_passwordless_sudo(
    tmp_path, monkeypatch, allow_platform_mutation
):
    """`sudo -v` 失败但 `sudo -n true` 成功 → 提权可用，命令自带 -n。

    `sudo -v` 是"无命令"调用，不匹配 sudoers 里的 NOPASSWD 规范 →
    落到默认的密码策略 → 无 TTY 时必然失败。那台机器根本不需要密码，
    判成"提权失败"会让所有特权步骤瘫痪。
    """
    calls = []

    def fake_run(argv, timeout=None):
        calls.append(list(argv))
        if argv == ["sudo", "-v"]:
            return 1, "sudo: A terminal is required to authenticate"
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    sess = MacOSElevated()
    sess.run(["install", "-d", "/etc/resolver"])
    assert ["sudo", "-n", "true"] in calls
    assert ["sudo", "-n", "install", "-d", "/etc/resolver"] in calls


def test_macos_elevated_keeps_plain_sudo_when_pre_auth_succeeds(
    tmp_path, monkeypatch, allow_platform_mutation
):
    """预热成功 → 维持现状：sudo <cmd>，不带 -n。"""
    calls = []

    def fake_run(argv, timeout=None):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    MacOSElevated().run(["install", "-d", "/etc/resolver"])
    assert ["sudo", "install", "-d", "/etc/resolver"] in calls
    assert not [c for c in calls if c[:2] == ["sudo", "-n", "true"]]


def test_macos_elevated_raises_when_both_sudo_probes_fail(
    tmp_path, monkeypatch, allow_platform_mutation
):
    """两条路都走不通 → PermissionError，信息要带上两条命令的输出。"""
    def fake_run(argv, timeout=None):
        if argv == ["sudo", "-v"]:
            return 1, "sudo: a terminal is required to authenticate"
        if argv == ["sudo", "-n", "true"]:
            return 1, "sudo: a password is required"
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    with pytest.raises(PermissionError) as ei:
        MacOSElevated()
    message = str(ei.value)
    assert "sudo -v" in message and "a terminal is required" in message
    assert "sudo -n true" in message and "a password is required" in message


def test_elevated_write_creates_parent_dir_then_installs_644(
    monkeypatch, tmp_path
):
    """/etc/resolver 可能压根不存在 —— 先建目录，再 install -m 644 写入。"""
    calls = []

    def fake_run(argv, timeout=None):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    target = tmp_path / "etc" / "resolver" / "test"
    MacOSElevated.__new__(MacOSElevated).write(target, "hello")

    install_dir = [c for c in calls if c[:3] == ["sudo", "install", "-d"]]
    install_file = [c for c in calls if c[:3] == ["sudo", "install", "-m"]]
    assert install_dir and install_dir[0][3:] == [str(target.parent)]
    assert install_file, "创建目录后仍须 install 写入"
    assert install_file[0][3:8] == ["644", "-o", "root", "-g", "wheel"]
    assert not [c for c in calls if c[:2] == ["sudo", "cp"]], (
        "不能用 cp（会把 mkstemp 的 0600 带到目标）"
    )
    assert calls.index(install_dir[0]) < calls.index(install_file[0])


def test_elevated_write_failure_raises_and_cleans_temp(monkeypatch, tmp_path):
    seen_tmp = []

    def fake_run(argv, timeout=None):
        if argv[:3] == ["sudo", "install", "-m"]:
            seen_tmp.append(Path(argv[-2]))
            return 1, "boom"
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    with pytest.raises(PermissionError):
        MacOSElevated.__new__(MacOSElevated).write(tmp_path / "x" / "y.conf", "hello")
    assert seen_tmp and not seen_tmp[0].exists(), "失败路径也要清理临时文件"


def test_elevated_read_distinguishes_missing_from_permission(monkeypatch):
    def fake_run(argv, timeout=None):
        # 存在性靠 `test -e` 的返回码，不解析本地化的 cat 文案
        if "test" in argv:
            return (1 if "missing" in argv[-1] else 0), ""
        if "cat" in argv:
            return 1, "cat: /etc/secret: Permission denied"
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    sess = MacOSElevated.__new__(MacOSElevated)
    with pytest.raises(FileNotFoundError):
        sess.read(Path("/etc/missing"))
    with pytest.raises(PermissionError):
        sess.read(Path("/etc/secret"))


def test_elevated_copy_installs_644_root_wheel(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (calls.append(list(argv)) or (0, "")),
    )
    src = tmp_path / "src.txt"
    src.write_text("data", encoding="utf-8")
    MacOSElevated.__new__(MacOSElevated).copy(src, tmp_path / "dest.txt")
    installs = [c for c in calls if c[:3] == ["sudo", "install", "-m"]]
    assert installs, "复制到特权目录必须走 install（可显式设定权限与归属）"
    argv = installs[0]
    assert argv[3:8] == ["644", "-o", "root", "-g", "wheel"]
    assert not [c for c in calls if c[:2] == ["sudo", "cp"]]


# ---------- 只读探测 ----------


def test_run_returns_127_when_command_missing(monkeypatch):
    """命令不存在时返回 (127, …) —— 调用方按返回码处理，而不是 traceback。"""

    def boom(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(macos_mod.subprocess, "run", boom)
    code, msg = MacOSPlatform().run(["launchctl", "print", "system/x"])
    assert code == 127
    assert "命令不存在" in msg


def test_run_returns_stdout(monkeypatch):
    class Proc:
        returncode = 0
        stdout = "hello\n"
        stderr = ""

    monkeypatch.setattr(macos_mod.subprocess, "run", lambda argv, **kwargs: Proc())
    assert MacOSPlatform().run(["echo"]) == (0, "hello")


def test_binary_version_returns_first_token(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (calls.append(list(argv)) or (0, "v2.11.7 h1:abc=")),
    )
    binary = tmp_path / "caddy"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    assert MacOSPlatform().binary_version(binary) == "v2.11.7"
    assert calls[0] == [str(binary), "version"]


def test_binary_version_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        macos_mod, "_run", lambda argv, timeout=None: pytest.fail("不该执行")
    )
    assert MacOSPlatform().binary_version(tmp_path / "absent") == ""


def test_service_active_system_scope_uses_launchctl_print(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (calls.append(list(argv)) or (0, "state = running")),
    )
    assert MacOSPlatform(user_home=tmp_path).service_active("caddy") is True
    assert calls[0] == ["launchctl", "print", "system/com.devproxy.caddy"]


def test_service_active_user_scope_uses_gui_domain(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (calls.append(list(argv)) or (1, "")),
    )
    p = MacOSPlatform(user_home=tmp_path)
    assert p.service_active("dnsmasq") is False
    assert calls[0] == [
        "launchctl",
        "print",
        f"gui/{os.getuid()}/com.devproxy.dnsmasq",
    ]


def test_resolved_active_is_false(monkeypatch):
    """macOS 没有 systemd-resolved —— auto 后端绝不能选它。"""
    monkeypatch.setattr(
        macos_mod, "_run", lambda argv, timeout=None: pytest.fail("不该执行")
    )
    assert MacOSPlatform().resolved_active() is False


def test_dns_query_returns_first_answer(monkeypatch):
    calls = []

    def fake_run(argv, timeout=None):
        calls.append(list(argv))
        if argv[0] == "dscacheutil":
            return 0, "name: foo.test\nip_address: 127.0.0.1"
        return 1, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    assert MacOSPlatform().dns_query("foo.test") == "127.0.0.1"
    assert calls[0][0] == "dscacheutil"
    assert calls[0][-1] == "foo.test"


def test_dns_query_falls_back_to_dig(monkeypatch):
    def fake_run(argv, timeout=None):
        if argv[0] == "dscacheutil":
            return 1, ""
        if argv[0] == "dig":
            return 0, "127.0.0.1\n::1"
        return 1, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    assert MacOSPlatform().dns_query("foo.test") == "127.0.0.1"


def test_dns_query_returns_none_when_unresolvable(monkeypatch):
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: (1, "not found"))
    assert MacOSPlatform().dns_query("foo.test") is None


def test_dns_query_is_bounded_by_timeout(monkeypatch):
    """解析器挂住不能拖死 doctor —— 必须有超时上限。"""
    timeouts = []

    def fake_run(argv, timeout=None):
        timeouts.append(timeout)
        return 1, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    MacOSPlatform().dns_query("foo.test")
    assert timeouts and all(t is not None for t in timeouts)
    assert all(t <= 5 for t in timeouts)


def test_http_ok_uses_bounded_curl(monkeypatch):
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (calls.append(list(argv)) or (0, "")),
    )
    assert MacOSPlatform().http_ok("https://localhost") is True
    assert calls[0][:3] == ["curl", "--noproxy", "*"], "本地探测必须绕过用户代理"
    assert "-fsS" in calls[0]
    assert "--max-time" in calls[0]


def test_http_ok_false_on_curl_failure(monkeypatch):
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: (7, "refused"))
    assert MacOSPlatform().http_ok("https://localhost") is False


def test_ca_trusted_verifies_against_system_trust(monkeypatch, tmp_path):
    """必须真的验信任链 —— 暂存目录里有文件不等于系统认它。"""
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (calls.append(list(argv)) or (0, "cert verify OK")),
    )
    ca = tmp_path / "root.crt"
    ca.write_text("CA", encoding="utf-8")
    assert MacOSPlatform().ca_trusted(ca) is True
    assert calls[0][:2] == ["security", "verify-cert"]
    assert str(ca) in calls[0]


def test_ca_trusted_false_when_verify_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        macos_mod, "_run", lambda argv, timeout=None: (1, "cert not trusted")
    )
    ca = tmp_path / "root.crt"
    ca.write_text("CA", encoding="utf-8")
    assert MacOSPlatform().ca_trusted(ca) is False


def test_nss_trusted_is_true(monkeypatch):
    """macOS 没有 NSS 库 —— 恒为真，证书组件才不会去装 NSS 证书。"""
    monkeypatch.setattr(
        macos_mod, "_run", lambda argv, timeout=None: pytest.fail("不该执行")
    )
    assert MacOSPlatform().nss_trusted(Path("/tmp/root.crt")) is True


def test_dns_route_installed_checks_resolver_file(monkeypatch, tmp_path):
    monkeypatch.setattr(macos_mod, "RESOLVER_DIR", tmp_path / "etc" / "resolver")
    p = MacOSPlatform(user_home=tmp_path)
    assert p.dns_route_installed("resolver", "test") is False
    (tmp_path / "etc" / "resolver").mkdir(parents=True)
    (tmp_path / "etc" / "resolver" / "test").write_text("x", encoding="utf-8")
    assert p.dns_route_installed("resolver", "test") is True
    assert p.dns_route_installed("resolved", "test") is False


# ---------- linger / resolved：不适用 ----------


def test_enable_linger_is_not_applicable(monkeypatch):
    """macOS 没有 systemd-linger —— 不适用，且不得失败。"""
    monkeypatch.setattr(
        macos_mod, "_run", lambda argv, timeout=None: pytest.fail("不该执行")
    )
    code, msg = MacOSPlatform().enable_linger()
    assert code == 0
    assert "不适用" in msg


def test_linger_enabled_is_true(monkeypatch):
    monkeypatch.setattr(
        macos_mod, "_run", lambda argv, timeout=None: pytest.fail("不该执行")
    )
    assert MacOSPlatform().linger_enabled() is True


# ---------- 服务的安装 / 启用 / 重启 / 日志 ----------


def test_install_service_writes_plist_for_user_scope(tmp_path, fake_run):
    p = MacOSPlatform(user_home=tmp_path)
    p.install_service(
        "dnsmasq",
        exec_start=["/usr/local/sbin/dnsmasq", "--keep-in-foreground"],
        env={"MINIKUBE_IP": "1.2.3.4"},
    )
    path = tmp_path / "Library" / "LaunchAgents" / "com.devproxy.dnsmasq.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == "com.devproxy.dnsmasq"
    assert data["ProgramArguments"][0] == "/usr/local/sbin/dnsmasq"
    assert data["EnvironmentVariables"] == {"MINIKUBE_IP": "1.2.3.4"}
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    assert data["StandardOutPath"].endswith("dnsmasq.log")


def test_install_service_bootstraps_the_plist(tmp_path, fake_run):
    p = MacOSPlatform(user_home=tmp_path)
    p.install_service("dnsmasq", exec_start=["/usr/local/sbin/dnsmasq"], env={})
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in fake_run)
    assert any(c[:2] == ["launchctl", "bootout"] for c in fake_run)


def test_install_service_system_scope_uses_elevated_write(
    tmp_path, monkeypatch, allow_platform_mutation
):
    """system scope 的 plist 在 /Library/LaunchDaemons —— 普通用户写不了。"""
    calls = []
    target = tmp_path / "LaunchDaemons" / "com.devproxy.caddy.plist"

    def fake_run(argv, timeout=None):
        calls.append(list(argv))
        if argv[:3] == ["sudo", "install", "-m"]:
            # 特权写入的源是临时文件，内容必须已经是渲染好的 plist
            src = Path(argv[-2])
            data = plistlib.loads(src.read_bytes())
            assert data["Label"] == "com.devproxy.caddy"
            assert data["ProgramArguments"][0] == "/opt/caddy"
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    p = MacOSPlatform(user_home=tmp_path)
    monkeypatch.setattr(p, "service_unit_path", lambda name: target)
    p.install_service("caddy", exec_start=["/opt/caddy", "run"], env={})

    assert any(
        c[:3] == ["sudo", "install", "-m"] for c in calls
    ), "system scope 必须走特权写入"
    assert any(c[:2] == ["sudo", "launchctl"] or c[:2] == ["launchctl", "bootstrap"] for c in calls)


def test_install_service_expands_home_in_arguments(tmp_path, fake_run):
    """launchd 以 root 运行 —— ~ 必须展开成用户的家目录。"""
    p = MacOSPlatform(user_home=tmp_path)
    p.install_service(
        "dnsmasq",
        exec_start=["/usr/local/sbin/dnsmasq", "--conf-dir=~/.config/dnsmasq"],
        env={},
    )
    path = tmp_path / "Library" / "LaunchAgents" / "com.devproxy.dnsmasq.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["ProgramArguments"][1] == f"--conf-dir={tmp_path}/.config/dnsmasq"


def test_enable_service_does_not_rewrite_the_plist(tmp_path, fake_run):
    """enable 绝不能重写 plist —— 重写会把 ProgramArguments 清空。"""
    p = MacOSPlatform(user_home=tmp_path)
    p.install_service("dnsmasq", exec_start=["/usr/local/sbin/dnsmasq"], env={})
    path = tmp_path / "Library" / "LaunchAgents" / "com.devproxy.dnsmasq.plist"
    before = path.read_bytes()
    calls_before = len(fake_run)
    p.enable_service("dnsmasq")
    assert path.read_bytes() == before
    assert len(fake_run) == calls_before, "enable 不该再动系统"


def test_restart_service_uses_kickstart(tmp_path, fake_run):
    p = MacOSPlatform(user_home=tmp_path)
    p.restart_service("dnsmasq")
    assert [
        "launchctl",
        "kickstart",
        "-k",
        f"gui/{os.getuid()}/com.devproxy.dnsmasq",
    ] in fake_run


def test_restart_service_system_scope_is_elevated(
    tmp_path, monkeypatch, allow_platform_mutation
):
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (calls.append(list(argv)) or (0, "")),
    )
    MacOSPlatform(user_home=tmp_path).restart_service("caddy")
    assert [
        "sudo",
        "launchctl",
        "kickstart",
        "-k",
        "system/com.devproxy.caddy",
    ] in calls


def test_tail_service_log_streams_without_capture(monkeypatch, tmp_path):
    """tail -f 必须流式继承终端 —— 捕获输出就永远看不到、也永远不返回。"""
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = list(argv)
        recorded["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(macos_mod.subprocess, "run", fake_run)
    MacOSPlatform(user_home=tmp_path).tail_service_log("caddy")
    assert recorded["argv"][0] == "tail"
    assert "-f" in recorded["argv"]
    assert recorded["argv"][-1].endswith("caddy.log")
    assert "capture_output" not in recorded["kwargs"], "不得捕获输出，须流式写到终端"


# ---------- 测试期护栏 ----------


def test_macos_platform_refuses_mutation_under_test(tmp_path, monkeypatch):
    """测试进程里，真实平台的写操作一律拒绝。"""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x")
    p = MacOSPlatform(user_home=tmp_path)
    with pytest.raises(RuntimeError, match="禁止"):
        p.install_service("caddy", exec_start=["/opt/caddy", "run"], env={})
    with pytest.raises(RuntimeError, match="禁止"):
        p.enable_service("caddy")
    with pytest.raises(RuntimeError, match="禁止"):
        p.restart_service("caddy")
    with pytest.raises(RuntimeError, match="禁止"):
        p.remove_service("caddy")
    with pytest.raises(RuntimeError, match="禁止"):
        p.privilege()


def test_macos_platform_refuses_mutation_when_flag_set(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("DEVPROXY_NO_MUTATION", "1")
    p = MacOSPlatform(user_home=tmp_path)
    with pytest.raises(RuntimeError, match="禁止"):
        p.install_service("caddy", exec_start=["/opt/caddy", "run"], env={})


def test_macos_platform_read_only_calls_still_work_under_test(tmp_path, monkeypatch):
    """只读探测不受护栏影响 —— doctor 的体检还得能跑。"""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x")
    p = MacOSPlatform(user_home=tmp_path)
    assert p.service_unit_path("caddy").parent == Path("/Library/LaunchDaemons")
    assert p.binary_version(tmp_path / "absent") == ""
    assert p.linger_enabled() is True
    assert p.resolved_active() is False


# ---------- 跨平台分派 ----------


def test_get_platform_returns_macos_on_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(get_platform(), MacOSPlatform)


def test_get_platform_returns_linux_elsewhere(monkeypatch):
    from devproxy.platform.linux import LinuxPlatform

    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(get_platform(), LinuxPlatform)


# ---------- service_active：必须是"真的在跑"，不只是"已加载" ----------


def test_service_active_requires_running_state(tmp_path, monkeypatch):
    """launchctl print 对"已注册但进程已退出"的服务也返回 0。

    只有 state 才是"进程真的在跑"的证据 —— 否则 doctor 会把崩溃的
    caddy 报成"在运行"。
    """
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (0, "com.devproxy.caddy = {\n\tstate = running\n}\n"),
    )
    assert MacOSPlatform(user_home=tmp_path).service_active("caddy") is True


def test_service_active_false_when_loaded_but_exited(tmp_path, monkeypatch):
    """已加载但已退出 → 不算在运行。"""
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (0, "com.devproxy.caddy = {\n\tstate = not running\n}\n"),
    )
    assert MacOSPlatform(user_home=tmp_path).service_active("caddy") is False


def test_service_active_false_when_state_line_missing(tmp_path, monkeypatch):
    """输出里没有 state 行 → fail closed（按没跑处理）。

    宁可报"没起来"让用户去查，也不能把没跑的服务报成在运行。
    """
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (0, "com.devproxy.caddy = {\n\tpid = 42\n}\n"),
    )
    assert MacOSPlatform(user_home=tmp_path).service_active("caddy") is False


# ---------- system scope：单元权限与日志目录 ----------


def test_install_service_system_scope_makes_plist_readable(
    tmp_path, monkeypatch, allow_platform_mutation
):
    """单元落盘后必须用户可读 —— 工具自己要读它来判断"单元是不是当前的"。

    特权写入用 `install -m 644 -o root -g wheel`：mkstemp 的 0600 不会
    被带到目标，单元天然是 root:wheel 0644。若退回 `cp`，单元会变成
    0600 root —— launchd 读得了、工具读不了，doctor 永远报"单元过期"。
    """
    calls = []
    target = tmp_path / "LaunchDaemons" / "com.devproxy.caddy.plist"

    def fake_run(argv, timeout=None):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    p = MacOSPlatform(user_home=tmp_path)
    monkeypatch.setattr(p, "service_unit_path", lambda name: target)
    p.install_service("caddy", exec_start=["/opt/caddy", "run"], env={})

    installs = [c for c in calls if c[:3] == ["sudo", "install", "-m"]]
    assert installs, "system scope 必须用 install 显式设定权限"
    argv = installs[0]
    assert argv[3] == "644", "单元必须落成 0644"
    assert argv[4:8] == ["-o", "root", "-g", "wheel"]
    assert argv[-1] == str(target)
    assert not [c for c in calls if c[:2] == ["sudo", "cp"]], (
        "cp 会把 mkstemp 的 0600 带到目标"
    )
    # 不再需要事后再跑一次 chmod/chown
    assert not [c for c in calls if c[:2] == ["sudo", "chmod"]]
    assert not [c for c in calls if c[:2] == ["sudo", "chown"]]


def test_install_service_system_scope_creates_log_dir(
    tmp_path, monkeypatch, allow_platform_mutation
):
    """launchd 打开 StandardOutPath 时父目录必须已经存在。

    组件顺序是 caddy → dnsmasq → …，caddy 最先装 —— 那时日志目录
    还不存在，launchd 起不来（第二次 setup 才会自愈，第一次已经失败）。
    """
    target = tmp_path / "LaunchDaemons" / "com.devproxy.caddy.plist"
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: (0, ""))
    p = MacOSPlatform(user_home=tmp_path)
    monkeypatch.setattr(p, "service_unit_path", lambda name: target)
    p.install_service("caddy", exec_start=["/opt/caddy", "run"], env={})
    assert p.log_path("caddy").parent.is_dir(), "日志目录必须在 bootstrap 之前建好"


def test_install_service_user_scope_creates_log_dir(tmp_path, fake_run):
    p = MacOSPlatform(user_home=tmp_path)
    p.install_service("dnsmasq", exec_start=["/usr/local/sbin/dnsmasq"], env={})
    assert p.log_path("dnsmasq").parent.is_dir()


def test_log_path_follows_the_shared_state_dir(tmp_path, monkeypatch):
    """状态目录只有一个来源（paths.default_state_dir）—— 平台不自己拼路径。"""
    monkeypatch.setenv("DEVPROXY_STATE_DIR", str(tmp_path / "state"))
    p = MacOSPlatform(user_home=tmp_path)
    assert p.log_path("caddy") == tmp_path / "state" / "logs" / "caddy.log"


# ---------- 提权会话的护栏 ----------


def test_elevated_session_cannot_be_constructed_under_test(monkeypatch):
    """直接构造提权会话会真的跑 sudo -v —— 护栏必须连构造函数一起拦。"""
    monkeypatch.setattr(
        macos_mod, "_run", lambda argv, timeout=None: pytest.fail("不该执行 sudo")
    )
    with pytest.raises(RuntimeError, match="禁止"):
        MacOSElevated()


# ---------- 共享的单元解析 ----------


def test_unit_exec_argv_reads_both_unit_formats():
    """systemd 的 ExecStart= 行与 launchd 的 ProgramArguments 数组都要认。"""
    from devproxy.platform.base import unit_exec_argv

    systemd = "[Service]\nExecStart=/opt/caddy run --config /etc/Caddyfile\n"
    assert unit_exec_argv(systemd) == [
        "/opt/caddy",
        "run",
        "--config",
        "/etc/Caddyfile",
    ]
    plist = plistlib.dumps(
        {
            "Label": "com.devproxy.caddy",
            "ProgramArguments": ["/opt/caddy", "run", "--config", "/etc/Caddyfile"],
        }
    ).decode("utf-8")
    assert unit_exec_argv(plist) == [
        "/opt/caddy",
        "run",
        "--config",
        "/etc/Caddyfile",
    ]
    assert unit_exec_argv("什么都不像") is None


# ---------- p4：超时默认值、交互路径与代理绕过 ----------


def test_run_defaults_to_bounded_timeout_and_devnull_stdin(monkeypatch):
    """`_run` 漏传 timeout 时也必须是有界等待，且不能抢终端输入。"""
    seen = {}

    def fake(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(macos_mod.subprocess, "run", fake)
    code, _ = macos_mod._run(["true"])
    assert code == 0
    assert seen["timeout"] == macos_mod.DEFAULT_TIMEOUT
    assert seen["stdin"] is subprocess.DEVNULL


def test_platform_run_is_bounded(monkeypatch):
    seen = {}

    def fake(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(macos_mod.subprocess, "run", fake)
    assert MacOSPlatform().run(["true"])[0] == 0
    assert seen.get("timeout"), "MacOSPlatform.run 必须传 timeout"


def test_macos_http_ok_bypasses_proxy_env(monkeypatch):
    """公司代理环境下，本地回环探测不能被 http(s)_proxy 带走。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (calls.append(list(argv)) or (0, "")),
    )
    assert MacOSPlatform().http_ok("https://codeup.test") is True
    assert calls[0][1:3] == ["--noproxy", "*"]


def test_macos_elevated_interactive_run_allows_waiting_for_password(
    monkeypatch, allow_platform_mutation
):
    """交互路径可能等用户输密码，只有这条路径允许无超时。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    sess = MacOSElevated.__new__(MacOSElevated)
    sess._noninteractive = False
    sess.run(["install", "-d", "/etc/resolver"])
    argv, kwargs = calls[-1]
    assert argv == ["sudo", "install", "-d", "/etc/resolver"]
    assert kwargs.get("timeout", "missing") is None


def test_macos_elevated_noninteractive_run_uses_default_timeout(
    monkeypatch, allow_platform_mutation
):
    """免密路径不走密码提示，必须回到默认的有界等待。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    sess = MacOSElevated.__new__(MacOSElevated)
    sess._noninteractive = True
    sess.run(["true"])
    argv, kwargs = calls[-1]
    assert argv == ["sudo", "-n", "true"]
    assert "timeout" not in kwargs, "省略 timeout 应落到 DEFAULT_TIMEOUT 而非 None"


def test_macos_platform_refuses_to_run_as_root(tmp_path, monkeypatch):
    monkeypatch.setattr(macos_mod.os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="普通用户"):
        MacOSPlatform(user_home=tmp_path)


