import base64
import os
import subprocess
from pathlib import Path

import pytest

from devproxy.platform import linux as linux_mod
from devproxy.platform.base import Action
from devproxy.platform.base import Status
from devproxy.platform.fake import FakePlatform
from devproxy.platform.linux import LinuxPlatform
from devproxy.platform.linux import render_systemd_unit


@pytest.fixture
def allow_platform_mutation(monkeypatch):
    """This file is dedicated to the platform's write operations: `_run` is
    replaced and paths point at tmp_path, so the "no writes during tests" guard
    can safely be disabled."""
    monkeypatch.setattr(linux_mod, "_ALLOW_MUTATION", True)


def test_status_defaults():
    s = Status(ok=True)
    assert s.detail == ""
    assert s.hint == ""


def test_action_defaults():
    a = Action(description="install caddy")
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
    assert (
        p.service_unit_path("caddy") == tmp_path / ".config" / "systemd" / "user" / "caddy.service"
    )


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
    """The unit is rendered by the platform: exec_start / env must land in the unit text."""
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
    # The unit is also written to disk — the component's "is the unit stale" check reads it
    assert p.service_unit_path("caddy").read_text(encoding="utf-8") == unit


def test_fake_platform_privilege_session_is_cached():
    p = FakePlatform()
    assert p.privilege() is p.privilege(), (
        "the elevated session must be reused, otherwise the password is prompted repeatedly"
    )


# ---------- Fix round 1: elevated writes / log streaming / service operations fail fast ----------


def test_linux_elevated_write_uses_system_temp_then_sudo_install(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """The content must be staged in the system temp dir, then sudo install to the target; the temp file must not live in the target directory."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[:3] == ["sudo", "install", "-m"]:
            assert Path(argv[8]).read_text(encoding="utf-8") == "hello", (
                "the temp file content must already be written when installing"
            )
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    target = tmp_path / "protected" / "devproxy.conf"
    sess = linux_mod.LinuxElevated()
    sess.write(target, "hello")

    install_calls = [c for c in calls if c[:3] == ["sudo", "install", "-m"]]
    assert len(install_calls) == 1
    inst = install_calls[0]
    assert inst[-1] == str(target), "install's target must be the real target path"
    tmp_used = Path(inst[-2])
    assert tmp_used.parent != target.parent, (
        "the temp file must not be placed inside the target directory (the privileged directory is not writable)"
    )
    assert tmp_used.name.startswith("devproxy-elev-")
    assert not tmp_used.exists(), "the temp file should be cleaned up on success"


def test_linux_elevated_write_produces_world_readable_root_file(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """Privileged writes must land as root:root 0644 — a regular user cannot even
    read a 0600 file.

    mkstemp creates temp files as 0600, and `cp` carries the permissions over,
    so files under /usr/local/share/ca-certificates/ and
    /etc/systemd/resolved.conf.d/ become root:root 0600 and doctor forever
    reports "cannot read".
    """
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    linux_mod.LinuxElevated().write(tmp_path / "devproxy.conf", "x")

    installs = [c for c in calls if c[:3] == ["sudo", "install", "-m"]]
    assert installs, (
        "privileged writes must go through install (to set mode and ownership explicitly)"
    )
    argv = installs[0]
    assert "-m" in argv and argv[argv.index("-m") + 1] == "644", "must be written as 0644"
    assert "-o" in argv and argv[argv.index("-o") + 1] == "root"
    assert "-g" in argv and argv[argv.index("-g") + 1] == "root"
    assert not [c for c in calls if c[:2] == ["sudo", "cp"]], (
        "cp must no longer be used (it carries the 0600 mode over)"
    )


def test_linux_elevated_copy_produces_world_readable_root_file(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """Installing the CA into the system trust store uses copy — it must likewise land as root:root 0644."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    src = tmp_path / "root.crt"
    src.write_text("CA", encoding="utf-8")
    linux_mod.LinuxElevated().copy(src, tmp_path / "devproxy-local.crt")

    installs = [c for c in calls if c[:3] == ["sudo", "install", "-m"]]
    assert installs, "copying into a privileged directory must go through install"
    argv = installs[0]
    assert argv[argv.index("-m") + 1] == "644"
    assert argv[argv.index("-o") + 1] == "root"
    assert not [c for c in calls if c[:2] == ["sudo", "cp"]], (
        "cp must no longer be used (it carries the 0600 mode over)"
    )


def test_linux_elevated_write_failure_raises_and_cleans_temp(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """A failed privileged write raises PermissionError, and the temp file must be cleaned up too (try/finally)."""
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
    assert seen_tmp and not seen_tmp[0].exists(), "the failure path must clean up the temp file too"


def test_linux_tail_service_log_streams_to_terminal(monkeypatch):
    """`follow` logs must inherit the terminal stdio (no capture); otherwise the output is invisible and the call never returns."""
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = list(argv)
        recorded["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(linux_mod.subprocess, "run", fake_run)
    LinuxPlatform().tail_service_log("caddy")
    assert recorded["argv"] == ["journalctl", "--user", "-u", "caddy", "-f"]
    assert "capture_output" not in recorded["kwargs"], (
        "must not capture output; stream it to the terminal"
    )


def test_linux_open_in_editor_splits_command_and_streams(monkeypatch, tmp_path):
    """The editor command is split using shell lexing and the file path is appended last; stdio must also be inherited."""
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
    assert "capture_output" not in recorded["kwargs"], (
        "must not capture output; the editor takes over the terminal"
    )


def test_linux_service_mutations_raise_on_failure(monkeypatch, tmp_path, allow_platform_mutation):
    """enable/restart/daemon-reload failures must raise RuntimeError immediately, including the service name and stderr."""
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


# -- unit rendering: the platform decides what the unit looks like ------------


def test_render_systemd_unit_contains_exec_and_env():
    unit = render_systemd_unit(
        "caddy",
        ["/home/u/.local/bin/caddy", "run", "--config", "/home/u/.config/devproxy/Caddyfile"],
        ["/home/u/.local/bin/caddy", "reload"],
        ["/home/u/.local/bin/caddy", "stop"],
        {"MINIKUBE_IP": "192.168.49.2"},
        "Caddy local HTTP/HTTPS server (managed by devproxy)",
    )
    assert (
        "ExecStart=/home/u/.local/bin/caddy run --config /home/u/.config/devproxy/Caddyfile" in unit
    )
    assert "ExecReload=/home/u/.local/bin/caddy reload" in unit
    assert "ExecStop=/home/u/.local/bin/caddy stop" in unit
    assert "Environment=MINIKUBE_IP=192.168.49.2" in unit
    assert "WantedBy=default.target" in unit


def test_render_systemd_unit_rejects_whitespace_in_args():
    """systemd units do not do shell quote parsing — whitespace inside an argument truncates the command line."""
    with pytest.raises(RuntimeError, match="whitespace"):
        render_systemd_unit(
            "caddy",
            ["/opt/my caddy/caddy", "run"],
            None,
            None,
            {},
        )
    with pytest.raises(RuntimeError, match="whitespace"):
        render_systemd_unit(
            "caddy",
            ["/opt/caddy", "run", "--config", "/home/u/my config/Caddyfile"],
            None,
            None,
            {},
        )


def test_render_systemd_unit_service_type_follows_program():
    """Only caddy sends sd_notify; forcing Type=notify on dnsmasq means it never starts."""
    caddy = render_systemd_unit("caddy", ["/usr/bin/caddy", "run"], None, None, {})
    dnsmasq = render_systemd_unit(
        "dnsmasq", ["/usr/sbin/dnsmasq", "--keep-in-foreground"], None, None, {}
    )
    assert "Type=notify" in caddy
    assert "Type=simple" in dnsmasq


def test_linux_install_service_writes_unit_file(tmp_path, monkeypatch, allow_platform_mutation):
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (0, ""))
    p = LinuxPlatform(user_home=tmp_path)
    p.install_service(
        "caddy",
        exec_start=["/bin/caddy", "run", "--config", "/tmp/Caddyfile"],
        env={"MINIKUBE_IP": "1.2.3.4"},
    )
    unit = (tmp_path / ".config" / "systemd" / "user" / "caddy.service").read_text(encoding="utf-8")
    assert "ExecStart=/bin/caddy run --config /tmp/Caddyfile" in unit
    assert "Environment=MINIKUBE_IP=1.2.3.4" in unit


def test_linux_elevated_read_distinguishes_missing_from_permission(
    monkeypatch, allow_platform_mutation
):
    """A missing file raises FileNotFoundError; other failures (e.g. permission) raise PermissionError."""

    def fake_run(argv, **kwargs):
        # Existence relies on `test -e`'s exit code; do not parse localized cat messages
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
    """FakeElevated.copy must record commands and written consistently with write."""
    src = tmp_path / "src.txt"
    src.write_text("data", encoding="utf-8")
    dest = tmp_path / "sub" / "dest.txt"
    p = FakePlatform()
    p.privilege().copy(src, dest)
    assert dest.read_text(encoding="utf-8") == "data"
    assert p.written[dest] == "data"
    assert ["cp", str(src), str(dest)] in p.commands


# -- non-interactive privileged reads (doctor must not prompt for a password) --


def test_linux_privilege_read_is_non_interactive(monkeypatch, tmp_path):
    """Privileged reads must pass sudo -n: without cached credentials they fail immediately and never wait for input."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="unit\n", stderr="")

    monkeypatch.setattr(linux_mod.subprocess, "run", fake_run)
    out = LinuxPlatform().privilege_read(tmp_path / "caddy.service")
    assert out == "unit\n"
    assert calls[0][:3] == ["sudo", "-n", "cat"]
    assert "-v" not in calls[0], "must not run sudo -v (that prompts for a password)"


def test_linux_privilege_read_returns_none_on_failure(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="sudo: a password is required"
        )

    monkeypatch.setattr(linux_mod.subprocess, "run", fake_run)
    assert LinuxPlatform().privilege_read(tmp_path / "caddy.service") is None


def test_linux_privilege_read_returns_none_when_sudo_missing(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("sudo")

    monkeypatch.setattr(linux_mod.subprocess, "run", fake_run)
    assert LinuxPlatform().privilege_read(tmp_path / "caddy.service") is None


# -- CA trust decision: fingerprint hit in the live store, not "can openssl verify this chain" --


def _fake_pem(seed: str) -> str:
    """Build a PEM certificate text (the body is base64-encoded fake DER).

    The criterion is "the same bytes" (a sha256 fingerprint), not parsing the
    X.509 structure — so the tests do not need a real certificate.
    """
    body = base64.b64encode(f"fake-der:{seed}".encode()).decode("ascii")
    return f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n"


@pytest.fixture
def trust_store(tmp_path, monkeypatch):
    """Point the system trust store at tmp_path: tests never read the real /etc/ssl/certs.

    Returns (bundle file, hashed index directory, staging directory).
    """
    bundle = tmp_path / "ca-certificates.crt"
    hashed = tmp_path / "certs"
    hashed.mkdir()
    staged_dir = tmp_path / "usr" / "local" / "share" / "ca-certificates"
    staged_dir.mkdir(parents=True)
    monkeypatch.setattr(linux_mod, "TRUST_BUNDLE_FILES", (bundle,))
    # The hashed index directory shares a constant with openssl -CApath (no second definition)
    monkeypatch.setattr(linux_mod, "SYSTEM_TRUST_DIR", hashed)
    return bundle, hashed, staged_dir


def test_linux_ca_trusted_true_when_fingerprint_in_live_store(trust_store, tmp_path, monkeypatch):
    """A CA fingerprint present in the live store counts as trusted — even if openssl
    errors out because of same-subject ambiguity.

    This is the core of the permanent false-negative: the trust store has several
    roots with the same SUBJECT, openssl picks another one and reports error 18,
    while curl works perfectly fine.
    """
    bundle, hashed, staged = trust_store
    ca = tmp_path / "root.crt"
    ca.write_text(_fake_pem("mine"), encoding="utf-8")
    bundle.write_text(_fake_pem("other") + _fake_pem("mine"), encoding="utf-8")

    def fake_run(argv, **kwargs):
        # Reproduce the ambiguity: openssl selected another root by SUBJECT
        return 1, "error 18 at 0 depth lookup: self-signed certificate"

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().ca_trusted(ca) is True


def test_linux_ca_trusted_true_when_fingerprint_in_hashed_dir(trust_store, tmp_path, monkeypatch):
    """Certificates in the hashed index directory (/etc/ssl/certs/*.0) also count as the live store."""
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


def test_linux_ca_trusted_false_when_only_staged_file_present(trust_store, tmp_path, monkeypatch):
    """The file being in the staging directory does not mean the system trusts it — update-ca-certificates may not have succeeded."""
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


def test_linux_ca_trusted_false_when_same_subject_other_root(trust_store, tmp_path, monkeypatch):
    """The live store contains only another root with the same subject — not this one, so it does not count as trusted."""
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
    """A successful openssl verification is a supplementary positive signal (fallback when the fingerprint scan misses)."""
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


def test_linux_ca_trusted_never_raises_on_binary_file(trust_store, tmp_path, monkeypatch):
    """A non-UTF-8 file must not make check() raise — doctor depends on it not blowing up."""
    bundle, hashed, staged = trust_store
    bundle.write_bytes(b"\xff\xfe\x00binary")
    ca = tmp_path / "root.crt"
    ca.write_text(_fake_pem("mine"), encoding="utf-8")
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: (127, "command unavailable: openssl")
    )
    assert LinuxPlatform().ca_trusted(ca) is False


def test_linux_ca_trusted_false_when_ca_not_pem(trust_store, tmp_path, monkeypatch):
    """The CA file is not a parseable PEM — treat as untrusted, do not raise."""
    bundle, hashed, staged = trust_store
    ca = tmp_path / "root.crt"
    ca.write_text("not a certificate", encoding="utf-8")
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: (127, "command unavailable: openssl")
    )
    assert LinuxPlatform().ca_trusted(ca) is False


# -- NSS store decision ------------------------------------------------------


PEM_OLD = "-----BEGIN CERTIFICATE-----\nT0xEQ0VSVElGSUNBVEUtT0xE\n-----END CERTIFICATE-----\n"
PEM_NEW = "-----BEGIN CERTIFICATE-----\nTkVXQ0VSVElGSUNBVEUtTkVX\n-----END CERTIFICATE-----\n"


def _nss_platform(tmp_path):
    p = LinuxPlatform(user_home=tmp_path)
    nss = tmp_path / ".pki" / "nssdb"
    nss.mkdir(parents=True)
    (nss / "cert9.db").write_text("", encoding="utf-8")
    return p


def _nss_listing(*rows: tuple[str, str]) -> str:
    """Build a `certutil -L` table output (nickname, trust flags).

    Real output looks like this (column width 55; overly long nicknames spill
    in front of the flags column, separated by a single space)::

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
    """Fake certutil: `-L` returns the table, `-L -n <nickname> -a` returns the PEM."""

    def fake_run(argv, **kwargs):
        if calls is not None:
            calls.append(list(argv))
        if "-a" in argv:
            return 0, pem
        return 0, listing

    return fake_run


def test_linux_nss_trusted_true_when_stored_cert_matches(tmp_path, monkeypatch):
    """The decision is based on the **certificate content** in the store, not merely on a same-named entry existing."""
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
    assert calls[0][:2] == ["certutil", "-L"], "must list the store entries first"
    assert "-a" in calls[-1], "certutil must print the PEM so the content can be compared"
    assert "-n" in calls[-1]


def test_linux_nss_trusted_false_when_stored_cert_differs(tmp_path, monkeypatch):
    """After Caddy regenerates the CA the old entry remains — same name but different content; must report untrusted."""
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
        linux_mod,
        "_run",
        lambda argv, **kwargs: pytest.fail("certutil must not run without an NSS store"),
    )
    assert p.nss_trusted(tmp_path / "root.crt") is False


def test_linux_nss_trusted_false_when_certutil_missing(tmp_path, monkeypatch):
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: (127, "command unavailable: certutil")
    )
    assert p.nss_trusted(ca) is False


# -- NSS decision: by content, not by nickname (a defect exposed on the first real run) --


def test_linux_nss_trusted_true_when_cert_stored_under_other_nickname(tmp_path, monkeypatch):
    """The same CA stored under another nickname also counts as trusted.

    Observed on a real machine: the CA installed by the old devp had the
    nickname "Caddy Local Authority", while NSS deduplicates by
    (issuer, serial) — installing it again under a different nickname with
    `certutil -A` is **silently a no-op**, so the entry is never added. Looking
    only under the tool's own nickname would never find this CA.
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
    """The store has this CA but marks it untrusted (",,") — not trusted."""
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
    """nss_entries must report the nickname and flags of "the entry in the store" — the component relies on it to repair the trust flags."""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(
        linux_mod,
        "_run",
        _fake_certutil(_nss_listing(("Caddy Local Authority", ",,")), PEM_OLD),
    )
    assert p.nss_entries(ca) == [("Caddy Local Authority", ",,")]


def test_linux_nss_entries_ignores_nickname_that_cannot_be_fetched(tmp_path, monkeypatch):
    """A nickname parsed from the table whose certificate cannot be fetched must be
    skipped — better to miss a trust decision than to get it wrong.

    The **enumeration** branch must actually be exercised: make the content under
    the tool's own nickname mismatch too (so the fast path misses); otherwise this
    case merely retests the fast path and never executes the claimed
    "skip bad candidates" behavior.
    """
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")

    def fake_run(argv, **kwargs):
        if "-a" in argv:
            nickname = argv[argv.index("-n") + 1]
            if "unfetchable" in nickname:
                return 255, "certutil: could not find cert"
            if nickname == "devproxy Local CA":
                return 0, PEM_NEW  # Different content → the fast path misses, so enumeration runs
            return 0, PEM_OLD
        return 0, _nss_listing(
            ("unfetchable entry", "C,,"),
            ("devproxy Local CA", "C,,"),
            ("the user's entry", "C,,"),
        )

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    entries = p.nss_entries(ca)
    assert entries == [("the user's entry", "C,,")]
    assert "unfetchable entry" not in [n for n, _ in entries]


def test_linux_nss_entries_fast_path_stops_after_own_nickname(tmp_path, monkeypatch):
    """The tool's own nickname matches and is trusted → query only that one, not a `-L -n -a` run for every other entry."""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "-a" in argv:
            return 0, PEM_OLD
        return 0, _nss_listing(
            ("someone else's entry one", "C,,"),
            ("devproxy Local CA", "C,,"),
            ("someone else's entry two", "C,,"),
            ("someone else's entry three", "C,,"),
        )

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert p.nss_entries(ca) == [("devproxy Local CA", "C,,")]
    # One listing + one PEM fetch for the tool's own nickname = 2 calls (full enumeration would be 5)
    assert len(calls) == 2, calls


def test_linux_nss_entries_empty_without_db(tmp_path, monkeypatch):
    p = LinuxPlatform(user_home=tmp_path)
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv, **kwargs: pytest.fail("certutil must not run without an NSS store"),
    )
    assert p.nss_entries(tmp_path / "root.crt") == []


def test_linux_nss_rows_parses_nickname_with_spaces_and_long_names():
    """Nicknames may contain spaces and may be long enough to overflow the column width — flags can only be split from the end of the line."""
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


# -- fix round 1: only the SSL column's "C" counts + three-column flags ---------


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
    """Only "C" in the SSL column is a CA anchor for HTTPS.

    "T" marks a CA that issues **client** certificates and "P" marks a trusted
    **peer** (leaf); neither is a root CA anchor. The SSL column of ",C," is
    empty. Counting T / P as trusted is a false positive — Chrome would never
    use them as anchors.
    """
    assert linux_mod._nss_flags_trusted(flags) is expected


@pytest.mark.parametrize("flags", ["C,,", "C,C,", "C,C,C", ",C,", ",,"])
def test_linux_nss_rows_parses_every_three_column_form(flags):
    """Flags have **three columns**: `C,,` / `C,C,` / `C,C,C` / `,C,` / `,,` must all parse.

    The old regex required "at least two consecutive commas", so `C,C,C` could
    not be parsed at all — and on a real machine `Caddy Local Authority` has
    exactly that shape.
    (`certutil -t ",,,"` is normalized to `,,` on insert, so the four-column
    form does not exist in the listing.)
    """
    rows = linux_mod._nss_rows(_nss_listing(("Caddy Local Authority", flags)))
    assert rows == [("Caddy Local Authority", flags)]


def test_linux_nss_rows_parses_row_without_trailing_padding():
    """A `C,C,C` row has no trailing padding (a `C,,` row has two spaces) — must rstrip before matching."""
    listing = "Caddy Local Authority                                        C,C,C"
    assert linux_mod._nss_rows(listing) == [("Caddy Local Authority", "C,C,C")]


def test_linux_nss_rows_ignores_header_lines():
    """The two header lines must not be treated as entries (they have no three-column comma form)."""
    listing = (
        "\n"
        "Certificate Nickname                                         Trust Attributes\n"
        "                                                             SSL,S/MIME,JAR/XPI\n"
        "\n"
        "devproxy Local CA                                            C,,\n"
    )
    assert linux_mod._nss_rows(listing) == [("devproxy Local CA", "C,,")]


def test_linux_nss_trust_flags_repairs_only_ssl_column():
    """When repairing flags preserve the other two columns: `,C,` → `C,C,`, not `C,,`."""
    assert linux_mod._nss_repair_flags("C,,") == "C,,"
    assert (
        linux_mod._nss_repair_flags(
            ",,",
        )
        == "C,,"
    )
    assert linux_mod._nss_repair_flags(",C,") == "C,C,"
    assert linux_mod._nss_repair_flags(",C,C") == "C,C,C"
    # Return unrecognized forms **unchanged** (never reset them to "C,,"):
    # resetting would wipe any S/MIME / JAR trust present in the unknown form,
    # which is exactly what this function must avoid.
    assert linux_mod._nss_repair_flags("") == ""
    assert linux_mod._nss_repair_flags("C") == "C"


def test_linux_nss_db_path_points_at_user_home(tmp_path):
    """The platform must report the store path it considers authoritative (the component aligns its write path with it)."""
    p = LinuxPlatform(user_home=tmp_path)
    assert p.nss_db_path() == tmp_path / ".pki" / "nssdb"


def test_linux_nss_entries_accepts_explicit_nss_dir(tmp_path, monkeypatch):
    """With an explicit store path, query exactly that store (the recheck must use the store that was written to)."""
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
        "every certutil call must point at the explicitly given store"
    )


def test_linux_nss_stale_nickname_true_for_other_cert(tmp_path, monkeypatch):
    """The tool's own nickname holds a **different** certificate → stale, should be deleted."""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_NEW, encoding="utf-8")
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (0, PEM_OLD))
    assert p.nss_stale_nickname(ca, "devproxy Local CA") is True


def test_linux_nss_stale_nickname_false_for_same_cert(tmp_path, monkeypatch):
    """The tool's own nickname holds the current certificate → not stale, must not be deleted."""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (0, PEM_OLD))
    assert p.nss_stale_nickname(ca, "devproxy Local CA") is False


def test_linux_nss_stale_nickname_false_when_absent(tmp_path, monkeypatch):
    """No entry under the nickname → not stale (there is nothing to delete)."""
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
    """`-L -n <nickname>` succeeds = exists; output explicitly says not found = does not exist."""
    p = _nss_platform(tmp_path)
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (0, "Certificate: …"))
    assert p.nss_has_nickname("devproxy Local CA") is True
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv, **kwargs: (255, 'certutil: could not find certificate named "x"'),
    )
    assert p.nss_has_nickname("devproxy Local CA") is False


def test_linux_nss_has_nickname_unknown_when_database_unreadable(tmp_path, monkeypatch):
    """When the store is locked / unreadable, `-L` also exits non-zero — that is
    "cannot tell", not "does not exist".

    Measured: after `chmod 000 cert9.db` certutil prints
    `SEC_ERROR_BAD_DATABASE: security library: bad database.`, while the entry
    is in fact still there. Treating that as "does not exist" would let a failed
    delete be swallowed as an idempotent success.
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
        linux_mod,
        "_run",
        lambda argv, **kwargs: pytest.fail("certutil must not run without an NSS store"),
    )
    assert p.nss_has_nickname("devproxy Local CA") is False


def test_linux_nss_stale_nickname_unknown_when_unparsable(tmp_path, monkeypatch):
    """The fetched content is not PEM → cannot tell (None); it must never report
    "definitely not stale".

    The deletion target is always the tool's own nickname, so there is no risk of
    deleting a user entry; collapsing "cannot tell" into False would leave a
    stale root in the store forever.
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


# -- create parent directories before privileged writes (on a clean machine /etc/... may not exist) --


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
    assert write, "still must write the target after creating the directory"
    assert calls.index(mkdir[0]) < calls.index(write[0]), "install -d must come before the write"


def test_linux_elevated_write_raises_when_mkdir_fails(
    monkeypatch, tmp_path, allow_platform_mutation
):
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv, **kwargs: (0, "") if argv == ["sudo", "-v"] else (1, "permission denied"),
    )
    with pytest.raises(PermissionError, match="create directory"):
        linux_mod.LinuxElevated().write(tmp_path / "etc" / "devproxy.conf", "x")


# -- elevation on passwordless machines (a defect exposed on the first real run) --
#
# Real-machine sudoers:
#     (ALL : ALL) ALL
#     (ALL) NOPASSWD: ALL
# `sudo -v` is a "no command" invocation and does not match the NOPASSWD command
# spec → falls through to the default policy → authentication required → without
# a TTY it reports "A terminal is required to authenticate".
# This machine needs no password at all — treating it as "elevation failed"
# would disable every privileged step.


def test_linux_elevated_falls_back_to_passwordless_sudo(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """sudo -v fails but sudo -n true succeeds → elevation is usable and later commands carry -n."""
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
    assert ["sudo", "-n", "true"] in calls, (
        "passwordless capability must be probed with sudo -n true"
    )
    assert ["sudo", "-n", "install", "-d", "/etc/devproxy"] in calls, (
        "privileged commands on the passwordless path must carry -n; they must never degrade to a sudo that prompts for a password"
    )
    assert not [c for c in calls if c[:2] == ["sudo", "install"]], (
        "a prompting sudo <cmd> must not appear on the passwordless path"
    )


def test_linux_elevated_keeps_plain_sudo_when_pre_auth_succeeds(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """sudo -v succeeds → keep the status quo: the timestamp is warmed, so the command is plain sudo <cmd>."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    sess = linux_mod.LinuxElevated()
    sess.run(["install", "-d", "/etc/devproxy"])
    assert ["sudo", "install", "-d", "/etc/devproxy"] in calls
    assert not [c for c in calls if c[:2] == ["sudo", "-n", "true"]], (
        "no need to probe passwordless mode after a successful warm-up"
    )


def test_linux_elevated_raises_when_both_sudo_probes_fail(
    monkeypatch, tmp_path, allow_platform_mutation
):
    """Neither path works → raise PermissionError, including the output of both commands."""

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
    """On the passwordless path a failing command still returns its exit code — it must not be treated as elevation failure just because of -n."""

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


# -- resolved active probe / dns_query / http_ok -----------------------------


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
    assert calls[0][:3] == ["curl", "--noproxy", "*"], "local probes must bypass the user's proxy"
    assert "-fsS" in calls[0]
    assert "--max-time" in calls[0]


def test_linux_http_ok_false_on_curl_failure(monkeypatch):
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (7, "connection refused"))
    assert LinuxPlatform().http_ok("https://localhost") is False


def test_linux_dns_route_installed_accepts_suffix():
    """The interface carries a domain suffix — macOS looks it up by suffix at /etc/resolver/<suffix>."""
    assert LinuxPlatform().dns_route_installed("resolved", "test") in (True, False)
    assert LinuxPlatform().dns_route_installed("unknown-backend", "test") is False


def test_fake_platform_dns_route_installed_uses_backend():
    p = FakePlatform()
    assert p.dns_route_installed("resolved", "test") is False
    p.dns_backends.add("resolved")
    assert p.dns_route_installed("resolved", "test") is True


# -- linger -----------------------------------------------------------------


def test_linux_enable_linger_prefers_noninteractive_sudo(monkeypatch, allow_platform_mutation):
    """Consistent with the platform's other privileged operations: try `sudo -n` first and never prompt for a password proactively."""
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
    # The systemd runtime directory must also point at tmp_path: on hosts
    # without /run/systemd/system (containers, and macOS running the Linux
    # tests) `linger_status()` immediately returns True meaning "linger not
    # applicable", and then this case would not exercise the parsing branch.
    monkeypatch.setattr(linux_mod, "SYSTEMD_RUN_DIR", tmp_path)
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (0, "no"))
    assert LinuxPlatform().linger_enabled() is False


def test_linux_linger_enabled_true_when_loginctl_unavailable(monkeypatch):
    """A host without loginctl (container / WSL / no systemd) → linger is not applicable.

    Treating it as "not enabled" would make every setup schedule an action that
    is bound to fail and requires sudo; on failure setup exits 1 — the whole tool
    would be unusable on such machines.
    """
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: (127, "command unavailable: loginctl")
    )
    assert LinuxPlatform().linger_enabled() is True


# -- test-time guard: the real platform must not modify the system ------------


def test_linux_platform_refuses_mutation_under_test(tmp_path, monkeypatch):
    """Guard: inside a test process, all writes through the real platform are refused.

    Once a platform object holds the real HOME it can rewrite units under
    ~/.config/systemd/user/, start and stop real services, and even sudo-write
    /etc — one missed piece of isolation modifies the user's machine.
    """
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x")
    p = LinuxPlatform(user_home=tmp_path)
    with pytest.raises(RuntimeError, match="forbidden"):
        p.install_service("caddy", exec_start=["/bin/caddy", "run"], env={})
    with pytest.raises(RuntimeError, match="forbidden"):
        p.enable_service("caddy")
    with pytest.raises(RuntimeError, match="forbidden"):
        p.restart_service("caddy")
    with pytest.raises(RuntimeError, match="forbidden"):
        p.remove_service("caddy")
    with pytest.raises(RuntimeError, match="forbidden"):
        p.enable_linger()
    with pytest.raises(RuntimeError, match="forbidden"):
        p.privilege()


def test_linux_platform_refuses_mutation_when_flag_set(tmp_path, monkeypatch):
    """The explicit switch also disables writes (without relying on the pytest env var)."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("DEVPROXY_NO_MUTATION", "1")
    p = LinuxPlatform(user_home=tmp_path)
    with pytest.raises(RuntimeError, match="forbidden"):
        p.install_service("caddy", exec_start=["/bin/caddy", "run"], env={})


def test_linux_platform_read_only_calls_still_work_under_test(tmp_path, monkeypatch):
    """Read-only probes are unaffected by the guard — doctor's health check must still run."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x")
    p = LinuxPlatform(user_home=tmp_path)
    assert p.service_unit_path("caddy") == (
        tmp_path / ".config" / "systemd" / "user" / "caddy.service"
    )
    assert p.binary_version(tmp_path / "absent") == ""
    assert p.linger_enabled() in (True, False)


# -- subprocess timeouts: prevent hangs, not a performance optimization ---------


def test_run_returns_timeout_code_instead_of_hanging(monkeypatch):
    """A command stuck on an interactive prompt must be interrupted by a timeout.

    Real-machine incident (2026-10-23): `certutil -N --empty-password` in some
    situations ignores this flag and instead repeatedly prints
    "Invalid password. Try again." / "Error opening input terminal for read",
    filling two 200GB redirect files in two hours and pushing /home to 100%.
    A subprocess call without a timeout is **unbounded writing**.
    """
    from devproxy.platform import linux as linux_mod

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout"))

    monkeypatch.setattr(linux_mod.subprocess, "run", boom)
    code, msg = linux_mod._run(["certutil", "-N"], timeout=1.0)
    assert code == 124, "follows the coreutils timeout convention"
    assert "timed out" in msg


def test_platform_run_passes_a_timeout(monkeypatch):
    """Ordinary command invocations must explicitly pass a timeout — omitting it means going back to "wait forever"."""
    from devproxy.platform import linux as linux_mod

    seen = {}

    def fake(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(linux_mod.subprocess, "run", fake)
    code, _ = linux_mod.LinuxPlatform().run(["true"])
    assert code == 0
    assert seen.get("timeout"), "LinuxPlatform.run must pass a timeout"


# -- p4: timeout defaults, interactive path, and proxy bypass ------------------


def test_run_defaults_to_bounded_timeout_and_devnull_stdin(monkeypatch):
    """When `_run` omits timeout it must still wait a bounded time, and must not steal terminal input."""
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
    """A hung resolved must not stall verify() forever — both query paths need a bound."""
    timeouts = []

    def fake_run(argv, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return 1, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    assert LinuxPlatform().dns_query("foo.test") is None
    assert timeouts and all(t is not None for t in timeouts)
    assert all(t <= 5 for t in timeouts)


def test_linux_http_ok_bypasses_proxy_env(monkeypatch):
    """Under a corporate proxy, local loopback probes must not be hijacked by http(s)_proxy."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    calls = []
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv, **kwargs: calls.append(list(argv)) or (0, ""),
    )
    assert LinuxPlatform().http_ok("https://codeup.test") is True
    assert calls[0][1:3] == ["--noproxy", "*"]


def test_linux_elevated_interactive_run_allows_waiting_for_password(
    monkeypatch, allow_platform_mutation
):
    """The interactive path may wait for the user's password; only this path may run without a timeout."""
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
    """The passwordless path does not prompt for a password; it must fall back to the default bounded wait."""
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
    assert "timeout" not in kwargs, "omitting timeout should fall back to DEFAULT_TIMEOUT, not None"


# -- p4: linger's tri-state and enable_linger's interactive fallback -----------


def test_linux_linger_status_unknown_is_fail_closed(tmp_path, monkeypatch):
    """systemd is present but the probe fails → cannot tell; plan must continue as if "not enabled"."""
    run_dir = tmp_path / "run-systemd"
    run_dir.mkdir()
    monkeypatch.setattr(linux_mod, "SYSTEMD_RUN_DIR", run_dir)
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (1, "Failed to connect to bus"))
    p = LinuxPlatform()
    assert p.linger_status() is None
    assert p.linger_enabled() is False


def test_linux_linger_status_not_applicable_without_systemd(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_mod, "SYSTEMD_RUN_DIR", tmp_path / "absent")
    monkeypatch.setattr(
        linux_mod, "_run", lambda *a, **k: pytest.fail("must not probe without systemd")
    )
    assert LinuxPlatform().linger_status() is True


def test_linux_linger_status_not_applicable_without_loginctl(tmp_path, monkeypatch):
    run_dir = tmp_path / "run-systemd"
    run_dir.mkdir()
    monkeypatch.setattr(linux_mod, "SYSTEMD_RUN_DIR", run_dir)
    monkeypatch.setattr(
        linux_mod, "_run", lambda argv, **kwargs: (127, "command unavailable: loginctl")
    )
    assert LinuxPlatform().linger_status() is True


def test_linux_enable_linger_falls_back_to_prompting_sudo(monkeypatch, allow_platform_mutation):
    """Only with a TTY and a failed `sudo -n` may it fall back to a sudo that prompts for a password."""
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


def test_linux_enable_linger_noninteractive_never_prompts(monkeypatch, allow_platform_mutation):
    """Without a TTY it must never fall back to a password prompt; return an explainable error instead."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return 1, "sudo: a password is required"

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    monkeypatch.setattr(linux_mod.sys, "stdin", type("NoTTY", (), {"isatty": lambda self: False})())
    code, msg = LinuxPlatform().enable_linger()
    assert code != 0
    assert "non-interactive" in msg
    assert len(calls) == 1


# -- p4: newline validation in unit rendering and root refusal -----------------


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
    """Environment / Description are external input too; a newline would forge unit lines."""
    with pytest.raises(RuntimeError, match="newlines"):
        render_systemd_unit("caddy", ["/bin/caddy", "run"], None, None, env, description)


def test_linux_platform_refuses_to_run_as_root(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_mod.os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="regular user"):
        LinuxPlatform(user_home=tmp_path)


# -- p4: NSS probe tri-state and exact missing-cert messages ------------------


def test_linux_nss_probes_treat_stat_failure_as_unknown(tmp_path, monkeypatch):
    """`Path.exists()` swallows PermissionError into False — "cannot tell" must be preserved."""
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
        linux_mod,
        "_run",
        lambda *a, **k: pytest.fail("certutil must not run when the store is unreadable"),
    )
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    p = LinuxPlatform(user_home=tmp_path)

    assert p.nss_entries(ca) is None
    assert p.nss_stale_nickname(ca, "devproxy Local CA") is None
    assert p.nss_has_nickname("devproxy Local CA") is None
    assert p.nss_trusted(ca) is False, "when we cannot tell, treat it as untrusted (fail closed)"


def test_linux_nss_entries_unknown_when_listing_fails(tmp_path, monkeypatch):
    """When `certutil -L` exits non-zero we cannot distinguish an empty store from a broken one — must not return []."""
    p = _nss_platform(tmp_path)
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_OLD, encoding="utf-8")
    monkeypatch.setattr(linux_mod, "_run", lambda argv, **kwargs: (255, "SEC_ERROR_BAD_DATABASE"))
    assert p.nss_entries(ca) is None


def test_linux_nss_missing_cert_only_accepts_exact_markers():
    """A generic "not found" would misclassify a module load failure as a missing entry."""
    assert linux_mod._nss_missing_cert('certutil: could not find certificate named "x"')
    assert linux_mod._nss_missing_cert("certutil: could not find cert: x")
    assert linux_mod._nss_missing_cert("PR_FILE_NOT_FOUND_ERROR: ...")
    assert not linux_mod._nss_missing_cert("not found")
    assert not linux_mod._nss_missing_cert("could not find the module libsoftokn3.so")


def test_linux_nss_has_nickname_unknown_when_generic_load_error(tmp_path, monkeypatch):
    p = _nss_platform(tmp_path)
    monkeypatch.setattr(
        linux_mod,
        "_run",
        lambda argv, **kwargs: (255, "could not find the module libsoftokn3.so"),
    )
    assert p.nss_has_nickname("devproxy Local CA") is None


def test_linux_ca_trusted_does_not_swallow_bugs(monkeypatch, tmp_path):
    """Fingerprint checks are responsible for never raising; the outer layer must not catch broadly."""

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
    """The constant must not be an import-time snapshot — it must take effect after a test redirects HOME."""
    from devproxy.platform.base import nss_db_dir

    assert nss_db_dir(tmp_path) == tmp_path / ".pki" / "nssdb"
    monkeypatch.setenv("HOME", str(tmp_path))
    assert nss_db_dir() == tmp_path / ".pki" / "nssdb"


# -- p4: fake platform sandbox and NSS semantics ------------------------------


def test_fake_elevated_write_sandboxes_system_paths():
    p = FakePlatform()
    target = Path("/etc/systemd/resolved.conf.d/devproxy.conf")
    p.privilege().write(target, "nameserver 127.0.0.1\n")
    mapped = p.sandbox / "etc/systemd/resolved.conf.d/devproxy.conf"
    assert mapped.read_text(encoding="utf-8") == "nameserver 127.0.0.1\n"
    assert p.written[target] == "nameserver 127.0.0.1\n"


def test_fake_elevated_write_rejects_relative_paths():
    p = FakePlatform()
    with pytest.raises(AssertionError, match="absolute"):
        p.privilege().write(Path("relative.conf"), "x")


def test_fake_privilege_read_never_touches_real_system_files():
    p = FakePlatform()
    target = Path("/etc/devproxy/secret.conf")
    p.privilege().write(target, "sandboxed")
    assert p.privilege_read(target) == "sandboxed"
    # Real host files are not involved: the mapped sandbox does not contain it, so the result is only None.
    assert p.privilege_read(Path("/etc/hostname")) is None


def test_fake_elevated_copy_closes_install_then_recheck_loop(tmp_path):
    """A successful copy immediately records trust — the install → recheck chain must really close."""
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
    p.run(["certutil", "-A", "-d", f"sql:{p.nss_db}", "-n", "n", "-t", ",,", "-i", str(ca)])
    assert p.nss_entries(ca) == [("n", ",,")]
    assert p.nss_trusted(ca) is False, "an empty SSL column must not count as trusted"
    p.run(["certutil", "-M", "-d", f"sql:{p.nss_db}", "-n", "n", "-t", "C,,"])
    assert p.nss_trusted(ca) is True


def test_fake_certutil_scopes_writes_and_deletes_by_database(tmp_path):
    p = FakePlatform()
    ca = tmp_path / "ca.crt"
    ca.write_text("CA\n", encoding="utf-8")
    db_a = tmp_path / "db-a"
    db_b = tmp_path / "db-b"
    p.run(["certutil", "-A", "-d", f"sql:{db_a}", "-n", "n", "-t", "C,,", "-i", str(ca)])
    assert p.nss_trusted(ca) is False, (
        "writing into store A does not mean the default store trusts it"
    )
    assert p.nss_entries(ca, nss_dir=db_a) == [("n", "C,,")]
    assert p.nss_entries(ca, nss_dir=db_b) == []
    p.run(["certutil", "-D", "-d", f"sql:{db_b}", "-n", "n"])
    assert p.nss_has_nickname("n", nss_dir=db_a) is True, (
        "deleting from the wrong store must not affect A"
    )
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
    assert p.nss_stale_nickname(new, "devproxy Local CA") is None, (
        "content unreadable → cannot tell"
    )


def test_fake_nss_helpers_cover_linux_optional_interface(tmp_path):
    """Missing one method makes component tests take the "degraded platform" branch instead of the Linux branch."""
    p = FakePlatform()
    for name in (
        "nss_db_path",
        "nss_entries",
        "nss_stale_nickname",
        "nss_has_nickname",
        "nss_trust_flags",
    ):
        assert callable(getattr(p, name)), f"fake platform is missing {name}"
    assert p.nss_db_path() == p.nss_db
    assert p.nss_trust_flags(",C,") == "C,C,"
    assert p.nss_trust_flags("broken") == "broken", "unrecognized forms must be returned unchanged"


def test_fake_linger_status_tracks_enable_linger():
    p = FakePlatform()
    assert p.linger_status() is False
    p.enable_linger()
    assert p.linger_status() is True
