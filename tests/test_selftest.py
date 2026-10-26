"""Unit tests for selftest's fail-closed paths -- no real caddy needed.

The version floor and the log evidence that "host trust store installation is
disabled" are the only barrier keeping the isolated sandbox from polluting the host
trust store, so they must be pinned down in tests (I-5).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devproxy.gateway import selftest
from devproxy.gateway.selftest import MIN_CADDY_VERSION
from devproxy.gateway.selftest import TRUST_DISABLED_LOG
from devproxy.gateway.selftest import _caddy_version
from devproxy.gateway.selftest import _wait_trust_disabled_log
from devproxy.gateway.selftest import run_selftest


class _FakeRun:
    """Return the scripted result by subcommand name; fail immediately on an unscripted call."""

    def __init__(self, answers: dict[str, subprocess.CompletedProcess]):
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        for key, proc in self.answers.items():
            if key in cmd:
                return proc
        raise AssertionError(f"test did not script this call: {cmd}")


def _completed(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


@pytest.fixture
def caddy_bin(tmp_path) -> Path:
    bin_path = tmp_path / "caddy"
    bin_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bin_path.chmod(0o755)
    return bin_path


def test_min_version_floor_is_2_7_and_log_string_is_pinned():
    """install_trust only exists since Caddy 2.7.0; the log evidence string must match in real-machine logs."""
    assert MIN_CADDY_VERSION == (2, 7, 0)
    # Verified verbatim against the local caddy v2.11.7 binary (strings hit):
    # "...root certificate trust store installation disabled; unconfigured
    #  clients may show warnings". Use a prefix substring match to tolerate wording
    # changes in the second half.
    assert TRUST_DISABLED_LOG == "root certificate trust store installation disabled"
    assert (
        "root certificate trust store installation disabled; unconfigured clients may show warnings"
    ).startswith(TRUST_DISABLED_LOG)


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ("v2.11.7 h1:yj0Y4fYZGPkSvibBJ1sTWE33xC0fxztVyXEW5IIdUT4=\n", (2, 11, 7)),
        ("v2.7.0\n", (2, 7, 0)),
        ("2.6.4 h1:xxx\n", (2, 6, 4)),
        ("v2.7.0-beta.1\n", (2, 7, 0)),
    ],
)
def test_caddy_version_parses_version_strings(monkeypatch, stdout, expected):
    fake = _FakeRun({"version": _completed(stdout=stdout)})
    monkeypatch.setattr(selftest.subprocess, "run", fake)
    assert _caddy_version(Path("/unused")) == expected


@pytest.mark.parametrize(
    "stdout", ["not a version at all\n", "", "unknown\n"], ids=["garbage", "empty", "unknown"]
)
def test_caddy_version_returns_none_on_unparseable_output(monkeypatch, stdout):
    fake = _FakeRun({"version": _completed(stdout=stdout)})
    monkeypatch.setattr(selftest.subprocess, "run", fake)
    assert _caddy_version(Path("/unused")) is None


def test_caddy_version_returns_none_when_binary_fails(monkeypatch):
    def broken_run(cmd, *args, **kwargs):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(selftest.subprocess, "run", broken_run)
    assert _caddy_version(Path("/unused")) is None


def test_selftest_fails_closed_on_caddy_older_than_floor(monkeypatch, caddy_bin):
    """caddy older than 2.7 silently ignores install_trust -- must fail and must not start the sandbox."""
    started = []

    def no_popen(*args, **kwargs):
        started.append(args)
        raise AssertionError("must not start the sandbox caddy when the version is too old")

    monkeypatch.setattr(
        selftest.subprocess,
        "run",
        _FakeRun({"version": _completed(stdout="v2.6.4 h1:xxx\n")}),
    )
    monkeypatch.setattr(selftest.subprocess, "Popen", no_popen)

    result = run_selftest(caddy_bin)

    assert result.failed, "must fail below the version floor"
    assert any("is too old" in desc for desc, _ in result.checks)
    assert not started, "must not start the sandbox caddy when the version is too old"


def test_selftest_accepts_caddy_at_floor(monkeypatch, caddy_bin):
    """Reaching 2.7.0 -> no version floor failure appears (later step failures are irrelevant)."""
    monkeypatch.setattr(
        selftest.subprocess,
        "run",
        _FakeRun(
            {
                "version": _completed(stdout="v2.7.0 h1:xxx\n"),
                "adapt": _completed(rc=1, stderr="adapt failed (scripted by the test)"),
            }
        ),
    )
    monkeypatch.setattr(
        selftest.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not start the sandbox")),
    )

    result = run_selftest(caddy_bin)

    version_checks = [ok for desc, ok in result.checks if "caddy version" in desc]
    assert version_checks and all(version_checks), ">= 2.7 must not report a version floor failure"
    assert not any("is too old" in desc for desc, _ in result.checks)
    # Actually got past the version gate (adapt is the first step after the version check passes)
    assert any("adapt" in desc for desc, _ in result.checks)


# ------------------------------------------- Log evidence: install_trust is really off


def test_trust_disabled_log_line_is_detected(tmp_path):
    log = tmp_path / "caddy.log"
    log.write_text(
        '{"level":"info","ts":1,"msg":"root certificate trust store '
        'installation disabled; unconfigured clients may show warnings"}\n',
        encoding="utf-8",
    )
    assert _wait_trust_disabled_log(log, timeout=0.2) is True


def test_trust_disabled_log_missing_fails_closed(tmp_path):
    """No such evidence line in the log -> fail (a false positive is better than assuming the host was not polluted)."""
    log = tmp_path / "caddy.log"
    log.write_text("some unrelated caddy output\n", encoding="utf-8")
    assert _wait_trust_disabled_log(log, timeout=0.2) is False


def test_trust_disabled_log_absent_file_fails_closed(tmp_path):
    assert _wait_trust_disabled_log(tmp_path / "absent.log", timeout=0.2) is False


# --------------------------------- I10: dynamic ports + Admin instance identity checks


def _serve_config(payload: dict):
    import http.server
    import json
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_wait_admin_rejects_instance_with_other_storage():
    """Someone else's Caddy is alive on the port -> must not claim it (otherwise reload would modify theirs)."""
    srv, base = _serve_config({"storage": {"module": "file_system", "root": "/someone/else"}})
    try:
        assert selftest._wait_admin(base, "/our/sandbox", timeout=0.5) is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_wait_admin_accepts_matching_sandbox_storage():
    srv, base = _serve_config({"storage": {"module": "file_system", "root": "/our/sandbox"}})
    try:
        assert selftest._wait_admin(base, "/our/sandbox", timeout=2) is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_free_ports_are_distinct_and_bindable():
    import socket

    ports = selftest._free_ports(3)
    assert len(set(ports)) == 3, "ports held at the same time must not repeat"
    for port in ports:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
        finally:
            s.close()


def test_selftest_uses_dynamic_ports_and_production_caddyfile(monkeypatch, caddy_bin):
    """The sandbox Caddyfile must be generated by build_main_caddyfile, with all ports dynamically allocated."""
    captured: dict[str, str] = {}
    monkeypatch.setattr(selftest, "_caddy_version", lambda _: (2, 7, 0))
    monkeypatch.setattr(selftest, "_free_ports", lambda n: [21001, 21002, 21003])
    monkeypatch.setattr(selftest, "build_main_caddyfile", lambda spec: "MARKER-CADDYFILE\n")

    def fake_adapt(caddy_bin_, caddyfile, config_json):
        captured["text"] = caddyfile.read_text(encoding="utf-8")
        return False, "adapt failed (scripted by the test)"

    monkeypatch.setattr(selftest, "_adapt", fake_adapt)

    result = run_selftest(caddy_bin)

    assert captured["text"] == "MARKER-CADDYFILE\n", "must reuse the production generator"
    assert any("adapt" in desc for desc, _ in result.checks)


# --------------------------------- I11: adapt / reload must have timeouts


def test_adapt_timeout_returns_false(monkeypatch, tmp_path):
    def timeout_run(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))

    monkeypatch.setattr(selftest.subprocess, "run", timeout_run)
    ok, msg = selftest._adapt(
        Path("/unused"), tmp_path / "Caddyfile", tmp_path / "config.json", timeout=1
    )
    assert ok is False
    assert "timed out" in msg


def test_adapt_oserror_returns_false(monkeypatch, tmp_path):
    def broken_run(cmd, *args, **kwargs):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(selftest.subprocess, "run", broken_run)
    ok, msg = selftest._adapt(Path("/unused"), tmp_path / "Caddyfile", tmp_path / "config.json")
    assert ok is False
    assert "could not run" in msg


def test_reload_timeout_returns_false(monkeypatch, tmp_path):
    def timeout_run(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))

    monkeypatch.setattr(selftest.subprocess, "run", timeout_run)
    ok, msg = selftest._reload(Path("/unused"), tmp_path / "config.json", 12345, timeout=1)
    assert ok is False
    assert "timed out" in msg


def test_reload_oserror_returns_false(monkeypatch, tmp_path):
    def broken_run(cmd, *args, **kwargs):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(selftest.subprocess, "run", broken_run)
    ok, msg = selftest._reload(Path("/unused"), tmp_path / "config.json", 12345)
    assert ok is False
    assert "could not run" in msg


# ---------------------------- I12: after mkdtemp, every exception must go through unified cleanup


def test_selftest_cleans_tmp_when_setup_raises_after_mkdtemp(monkeypatch, caddy_bin, tmp_path):
    import tempfile

    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def fake_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(prefix=kwargs.get("prefix", ""), dir=tmp_path)
        created.append(Path(path))
        return path

    monkeypatch.setattr(selftest.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(selftest, "_caddy_version", lambda _: (2, 7, 0))

    def boom(*args, **kwargs):
        raise RuntimeError("setup boom")

    monkeypatch.setattr(selftest, "_adapt", boom)

    with pytest.raises(RuntimeError):
        run_selftest(caddy_bin)

    assert created, "no temp directory created?"
    assert not created[0].exists(), "the exception path must reclaim the /tmp sandbox directory"
