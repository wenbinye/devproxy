"""Unit tests for the macOS platform implementation.

This file **never touches the real system**: everywhere a subprocess could start
(launchctl / sudo / security / tail / dscacheutil) `macos._run` or
`subprocess.run` is replaced with a fake; all paths point at tmp_path.
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from devproxy.platform import macos as macos_mod
from devproxy.platform.base import ElevatedSession
from devproxy.platform.base import Platform
from devproxy.platform.macos import MacOSElevated
from devproxy.platform.macos import MacOSPlatform
from devproxy.platform.macos import get_platform
from devproxy.platform.macos import render_plist
from devproxy.platform.macos import render_resolver_file


@pytest.fixture
def allow_platform_mutation(monkeypatch):
    """This file is dedicated to the platform's write operations: `_run` is
    replaced and paths point at tmp_path, so the "no writes during tests" guard
    can safely be disabled."""
    monkeypatch.setattr(macos_mod, "_ALLOW_MUTATION", True)


@pytest.fixture
def fake_run(monkeypatch, allow_platform_mutation):
    """Replace macos._run with a fake — never really invoke launchctl / sudo / security.

    It also exempts the "no writes during tests" guard: this fixture is only used
    to exercise the write paths, and all paths point at tmp_path while all
    commands are fake.
    """
    calls: list[list[str]] = []

    def _fake(argv, timeout=None):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", _fake)
    return calls


# ---------- basic shape ----------


def test_dns_port_avoids_mdns():
    """Port 53 is taken by mDNSResponder — a user-scope dnsmasq can only use 5300."""
    assert MacOSPlatform().dns_port == 5300


def test_macos_platform_name_is_mac():
    assert MacOSPlatform().name == "mac"


def test_platform_implements_whole_protocol():
    """The protocol has 19 methods in total — a missing one is a runtime AttributeError, not an import-time error."""
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
        assert callable(getattr(p, method, None)), f"missing protocol method: {method}"


def test_elevated_session_implements_protocol():
    assert isinstance(MacOSElevated.__new__(MacOSElevated), ElevatedSession)


# ---------- service unit paths: caddy is system scope, the rest are user scope ----------


def test_caddy_service_is_system_scope(tmp_path):
    """macOS has no setcap — binding 80/443 can only use a root LaunchDaemon."""
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


# ---------- plist rendering ----------


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
    """launchd runs as root — relative paths and ~ point somewhere else, so they must be rejected."""
    with pytest.raises(RuntimeError, match="absolute"):
        render_plist("caddy", ["caddy", "run"], {}, Path("/tmp/caddy.log"))
    with pytest.raises(RuntimeError, match="absolute"):
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


# ---------- elevated session ----------


def test_macos_privilege_session_is_cached(tmp_path, monkeypatch, allow_platform_mutation):
    """One sudo -v authentication is reused for the whole session — otherwise every operation prompts for the password again."""
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: (0, ""))
    p = MacOSPlatform(user_home=tmp_path)
    assert p.privilege() is p.privilege()


def test_privilege_raises_when_pre_auth_fails(tmp_path, monkeypatch, allow_platform_mutation):
    """A failed pre-auth must raise — silent degradation would make every subsequent write fail mysteriously."""
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: (1, "boom"))
    p = MacOSPlatform(user_home=tmp_path)
    with pytest.raises(PermissionError):
        p.privilege()


# -- elevation on passwordless machines (same semantics as Linux) ---------------


def test_macos_elevated_falls_back_to_passwordless_sudo(
    tmp_path, monkeypatch, allow_platform_mutation
):
    """`sudo -v` fails but `sudo -n true` succeeds → elevation is usable and
    commands carry -n.

    `sudo -v` is a "no command" invocation and does not match the NOPASSWD spec
    in sudoers → it falls through to the default password policy → it always
    fails without a TTY. That machine needs no password at all, so treating it
    as "elevation failed" would disable every privileged step.
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
    """Warm-up succeeds → keep the status quo: sudo <cmd>, without -n."""
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
    """Neither path works → PermissionError, including the output of both commands."""

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


def test_elevated_write_creates_parent_dir_then_installs_644(monkeypatch, tmp_path):
    """/etc/resolver may not exist at all — create the directory first, then install -m 644 it."""
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
    assert install_file, (
        "the target still has to be written with install after creating the directory"
    )
    assert install_file[0][3:8] == ["644", "-o", "root", "-g", "wheel"]
    assert not [c for c in calls if c[:2] == ["sudo", "cp"]], (
        "cp must not be used (it carries mkstemp's 0600 mode to the target)"
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
    assert seen_tmp and not seen_tmp[0].exists(), "the failure path must clean up the temp file too"


def test_elevated_read_distinguishes_missing_from_permission(monkeypatch):
    def fake_run(argv, timeout=None):
        # Existence relies on `test -e`'s exit code; do not parse localized cat messages
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
        lambda argv, timeout=None: calls.append(list(argv)) or (0, ""),
    )
    src = tmp_path / "src.txt"
    src.write_text("data", encoding="utf-8")
    MacOSElevated.__new__(MacOSElevated).copy(src, tmp_path / "dest.txt")
    installs = [c for c in calls if c[:3] == ["sudo", "install", "-m"]]
    assert installs, (
        "copying into a privileged directory must go through install (to set mode and ownership explicitly)"
    )
    argv = installs[0]
    assert argv[3:8] == ["644", "-o", "root", "-g", "wheel"]
    assert not [c for c in calls if c[:2] == ["sudo", "cp"]]


# ---------- read-only probes ----------


def test_run_returns_127_when_command_missing(monkeypatch):
    """A missing command returns (127, …) — callers handle the exit code rather than a traceback."""

    def boom(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(macos_mod.subprocess, "run", boom)
    code, msg = MacOSPlatform().run(["launchctl", "print", "system/x"])
    assert code == 127
    assert "command not found" in msg


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
        lambda argv, timeout=None: calls.append(list(argv)) or (0, "v2.11.7 h1:abc="),
    )
    binary = tmp_path / "caddy"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    assert MacOSPlatform().binary_version(binary) == "v2.11.7"
    assert calls[0] == [str(binary), "version"]


def test_binary_version_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: pytest.fail("must not run"))
    assert MacOSPlatform().binary_version(tmp_path / "absent") == ""


def test_service_active_system_scope_uses_launchctl_print(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: calls.append(list(argv)) or (0, "state = running"),
    )
    assert MacOSPlatform(user_home=tmp_path).service_active("caddy") is True
    assert calls[0] == ["launchctl", "print", "system/com.devproxy.caddy"]


def test_service_active_user_scope_uses_gui_domain(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: calls.append(list(argv)) or (1, ""),
    )
    p = MacOSPlatform(user_home=tmp_path)
    assert p.service_active("dnsmasq") is False
    assert calls[0] == [
        "launchctl",
        "print",
        f"gui/{os.getuid()}/com.devproxy.dnsmasq",
    ]


def test_resolved_active_is_false(monkeypatch):
    """macOS has no systemd-resolved — the auto backend must never select it."""
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: pytest.fail("must not run"))
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
    """A hanging resolver must not stall doctor — there must be a timeout bound."""
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
        lambda argv, timeout=None: calls.append(list(argv)) or (0, ""),
    )
    assert MacOSPlatform().http_ok("https://localhost") is True
    assert calls[0][:3] == ["curl", "--noproxy", "*"], "local probes must bypass the user's proxy"
    assert "-fsS" in calls[0]
    assert "--max-time" in calls[0]


def test_http_ok_false_on_curl_failure(monkeypatch):
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: (7, "refused"))
    assert MacOSPlatform().http_ok("https://localhost") is False


def test_ca_trusted_verifies_against_system_trust(monkeypatch, tmp_path):
    """The trust chain must really be verified — a file in the staging directory does not mean the system trusts it."""
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: calls.append(list(argv)) or (0, "cert verify OK"),
    )
    ca = tmp_path / "root.crt"
    ca.write_text("CA", encoding="utf-8")
    assert MacOSPlatform().ca_trusted(ca) is True
    assert calls[0][:2] == ["security", "verify-cert"]
    assert str(ca) in calls[0]


def test_ca_trusted_false_when_verify_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: (1, "cert not trusted"))
    ca = tmp_path / "root.crt"
    ca.write_text("CA", encoding="utf-8")
    assert MacOSPlatform().ca_trusted(ca) is False


def test_nss_trusted_is_true(monkeypatch):
    """macOS has no NSS store — always true, so the certificate component does not try to install NSS certificates."""
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: pytest.fail("must not run"))
    assert MacOSPlatform().nss_trusted(Path("/tmp/root.crt")) is True


def test_dns_route_installed_checks_resolver_file(monkeypatch, tmp_path):
    monkeypatch.setattr(macos_mod, "RESOLVER_DIR", tmp_path / "etc" / "resolver")
    p = MacOSPlatform(user_home=tmp_path)
    assert p.dns_route_installed("resolver", "test") is False
    (tmp_path / "etc" / "resolver").mkdir(parents=True)
    (tmp_path / "etc" / "resolver" / "test").write_text("x", encoding="utf-8")
    assert p.dns_route_installed("resolver", "test") is True
    assert p.dns_route_installed("resolved", "test") is False


# ---------- linger / resolved: not applicable ----------


def test_enable_linger_is_not_applicable(monkeypatch):
    """macOS has no systemd-linger — not applicable, and must not fail."""
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: pytest.fail("must not run"))
    code, msg = MacOSPlatform().enable_linger()
    assert code == 0
    assert "not applicable" in msg


def test_linger_enabled_is_true(monkeypatch):
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: pytest.fail("must not run"))
    assert MacOSPlatform().linger_enabled() is True


# ---------- service install / enable / restart / logs ----------


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
    """The system scope plist lives in /Library/LaunchDaemons — a regular user cannot write it."""
    calls = []
    target = tmp_path / "LaunchDaemons" / "com.devproxy.caddy.plist"

    def fake_run(argv, timeout=None):
        calls.append(list(argv))
        if argv[:3] == ["sudo", "install", "-m"]:
            # The privileged write's source is a temp file; its content must already be the rendered plist
            src = Path(argv[-2])
            data = plistlib.loads(src.read_bytes())
            assert data["Label"] == "com.devproxy.caddy"
            assert data["ProgramArguments"][0] == "/opt/caddy"
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    p = MacOSPlatform(user_home=tmp_path)
    monkeypatch.setattr(p, "service_unit_path", lambda name: target)
    p.install_service("caddy", exec_start=["/opt/caddy", "run"], env={})

    assert any(c[:3] == ["sudo", "install", "-m"] for c in calls), (
        "system scope must go through a privileged write"
    )
    assert any(c[:2] == ["sudo", "launchctl"] or c[:2] == ["launchctl", "bootstrap"] for c in calls)


def test_install_service_expands_home_in_arguments(tmp_path, fake_run):
    """launchd runs as root — ~ must expand to the user's home directory."""
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
    """enable must never rewrite the plist — rewriting would clear ProgramArguments."""
    p = MacOSPlatform(user_home=tmp_path)
    p.install_service("dnsmasq", exec_start=["/usr/local/sbin/dnsmasq"], env={})
    path = tmp_path / "Library" / "LaunchAgents" / "com.devproxy.dnsmasq.plist"
    before = path.read_bytes()
    calls_before = len(fake_run)
    p.enable_service("dnsmasq")
    assert path.read_bytes() == before
    assert len(fake_run) == calls_before, "enable must not touch the system again"


def test_restart_service_uses_kickstart(tmp_path, fake_run):
    p = MacOSPlatform(user_home=tmp_path)
    p.restart_service("dnsmasq")
    assert [
        "launchctl",
        "kickstart",
        "-k",
        f"gui/{os.getuid()}/com.devproxy.dnsmasq",
    ] in fake_run


def test_restart_service_system_scope_is_elevated(tmp_path, monkeypatch, allow_platform_mutation):
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: calls.append(list(argv)) or (0, ""),
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
    """tail -f must stream and inherit the terminal — capturing output makes it invisible and never returns."""
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
    assert "capture_output" not in recorded["kwargs"], (
        "must not capture output; stream it to the terminal"
    )


# ---------- test-time guard ----------


def test_macos_platform_refuses_mutation_under_test(tmp_path, monkeypatch):
    """Inside a test process, all writes through the real platform are refused."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x")
    p = MacOSPlatform(user_home=tmp_path)
    with pytest.raises(RuntimeError, match="forbidden"):
        p.install_service("caddy", exec_start=["/opt/caddy", "run"], env={})
    with pytest.raises(RuntimeError, match="forbidden"):
        p.enable_service("caddy")
    with pytest.raises(RuntimeError, match="forbidden"):
        p.restart_service("caddy")
    with pytest.raises(RuntimeError, match="forbidden"):
        p.remove_service("caddy")
    with pytest.raises(RuntimeError, match="forbidden"):
        p.privilege()


def test_macos_platform_refuses_mutation_when_flag_set(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("DEVPROXY_NO_MUTATION", "1")
    p = MacOSPlatform(user_home=tmp_path)
    with pytest.raises(RuntimeError, match="forbidden"):
        p.install_service("caddy", exec_start=["/opt/caddy", "run"], env={})


def test_macos_platform_read_only_calls_still_work_under_test(tmp_path, monkeypatch):
    """Read-only probes are unaffected by the guard — doctor's health check must still run."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x")
    p = MacOSPlatform(user_home=tmp_path)
    assert p.service_unit_path("caddy").parent == Path("/Library/LaunchDaemons")
    assert p.binary_version(tmp_path / "absent") == ""
    assert p.linger_enabled() is True
    assert p.resolved_active() is False


# ---------- cross-platform dispatch ----------


def test_get_platform_returns_macos_on_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(get_platform(), MacOSPlatform)


def test_get_platform_returns_linux_elsewhere(monkeypatch):
    from devproxy.platform.linux import LinuxPlatform

    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(get_platform(), LinuxPlatform)


# ---------- service_active: must be "really running", not just "loaded" ----


def test_service_active_requires_running_state(tmp_path, monkeypatch):
    """launchctl print also returns 0 for a service that is "registered but whose
    process has exited".

    Only state is evidence that "the process is really running" — otherwise
    doctor would report a crashed caddy as "running".
    """
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (0, "com.devproxy.caddy = {\n\tstate = running\n}\n"),
    )
    assert MacOSPlatform(user_home=tmp_path).service_active("caddy") is True


def test_service_active_false_when_loaded_but_exited(tmp_path, monkeypatch):
    """Loaded but exited → does not count as running."""
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (0, "com.devproxy.caddy = {\n\tstate = not running\n}\n"),
    )
    assert MacOSPlatform(user_home=tmp_path).service_active("caddy") is False


def test_service_active_false_when_state_line_missing(tmp_path, monkeypatch):
    """No state line in the output → fail closed (treat as not running).

    Better to report "not up" and let the user investigate than to report a
    stopped service as running.
    """
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: (0, "com.devproxy.caddy = {\n\tpid = 42\n}\n"),
    )
    assert MacOSPlatform(user_home=tmp_path).service_active("caddy") is False


# ---------- system scope: unit permissions and log directory ----------


def test_install_service_system_scope_makes_plist_readable(
    tmp_path, monkeypatch, allow_platform_mutation
):
    """After the unit is written to disk it must be user-readable — the tool itself
    reads it to decide "whether the unit is current".

    Privileged writes use `install -m 644 -o root -g wheel`: mkstemp's 0600 is
    not carried to the target, so the unit is naturally root:wheel 0644. Falling
    back to `cp` would make it 0600 root — launchd can read it, but the tool
    cannot, and doctor would forever report "unit stale".
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
    assert installs, "system scope must use install to set permissions explicitly"
    argv = installs[0]
    assert argv[3] == "644", "the unit must land as 0644"
    assert argv[4:8] == ["-o", "root", "-g", "wheel"]
    assert argv[-1] == str(target)
    assert not [c for c in calls if c[:2] == ["sudo", "cp"]], (
        "cp would carry mkstemp's 0600 mode to the target"
    )
    # no separate chmod/chown pass is needed afterwards
    assert not [c for c in calls if c[:2] == ["sudo", "chmod"]]
    assert not [c for c in calls if c[:2] == ["sudo", "chown"]]


def test_install_service_system_scope_creates_log_dir(
    tmp_path, monkeypatch, allow_platform_mutation
):
    """When launchd opens StandardOutPath its parent directory must already exist.

    The component order is caddy → dnsmasq → …, and caddy is installed first —
    at that point the log directory does not exist yet, so launchd cannot start
    it (only a second setup would self-heal, after the first already failed).
    """
    target = tmp_path / "LaunchDaemons" / "com.devproxy.caddy.plist"
    monkeypatch.setattr(macos_mod, "_run", lambda argv, timeout=None: (0, ""))
    p = MacOSPlatform(user_home=tmp_path)
    monkeypatch.setattr(p, "service_unit_path", lambda name: target)
    p.install_service("caddy", exec_start=["/opt/caddy", "run"], env={})
    assert p.log_path("caddy").parent.is_dir(), "the log directory must be created before bootstrap"


def test_install_service_user_scope_creates_log_dir(tmp_path, fake_run):
    p = MacOSPlatform(user_home=tmp_path)
    p.install_service("dnsmasq", exec_start=["/usr/local/sbin/dnsmasq"], env={})
    assert p.log_path("dnsmasq").parent.is_dir()


def test_log_path_follows_the_shared_state_dir(tmp_path, monkeypatch):
    """The state directory has a single source (paths.default_state_dir) — the platform does not assemble paths itself."""
    monkeypatch.setenv("DEVPROXY_STATE_DIR", str(tmp_path / "state"))
    p = MacOSPlatform(user_home=tmp_path)
    assert p.log_path("caddy") == tmp_path / "state" / "logs" / "caddy.log"


# ---------- elevated session guard ----------


def test_elevated_session_cannot_be_constructed_under_test(monkeypatch):
    """Constructing an elevated session directly really runs sudo -v — the guard must also cover the constructor."""
    monkeypatch.setattr(
        macos_mod, "_run", lambda argv, timeout=None: pytest.fail("must not run sudo")
    )
    with pytest.raises(RuntimeError, match="forbidden"):
        MacOSElevated()


# ---------- shared unit parsing ----------


def test_unit_exec_argv_reads_both_unit_formats():
    """Both systemd's ExecStart= line and launchd's ProgramArguments array must be understood."""
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
    assert unit_exec_argv("nothing like a unit") is None


# ---------- p4: timeout defaults, interactive path, and proxy bypass ----------


def test_run_defaults_to_bounded_timeout_and_devnull_stdin(monkeypatch):
    """When `_run` omits timeout it must still wait a bounded time, and must not steal terminal input."""
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
    assert seen.get("timeout"), "MacOSPlatform.run must pass a timeout"


def test_macos_http_ok_bypasses_proxy_env(monkeypatch):
    """Under a corporate proxy, local loopback probes must not be hijacked by http(s)_proxy."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    calls = []
    monkeypatch.setattr(
        macos_mod,
        "_run",
        lambda argv, timeout=None: calls.append(list(argv)) or (0, ""),
    )
    assert MacOSPlatform().http_ok("https://codeup.test") is True
    assert calls[0][1:3] == ["--noproxy", "*"]


def test_macos_elevated_interactive_run_allows_waiting_for_password(
    monkeypatch, allow_platform_mutation
):
    """The interactive path may wait for the user's password; only this path may run without a timeout."""
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
    """The passwordless path does not prompt for a password; it must fall back to the default bounded wait."""
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
    assert "timeout" not in kwargs, "omitting timeout should fall back to DEFAULT_TIMEOUT, not None"


def test_macos_platform_refuses_to_run_as_root(tmp_path, monkeypatch):
    monkeypatch.setattr(macos_mod.os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="regular user"):
        MacOSPlatform(user_home=tmp_path)
