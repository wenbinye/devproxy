"""devproxy run -- behavior tests for foreground supervision of Caddy.

**Do not really start Caddy**: substitute a fake executable (a python script). It plays three roles:

- `validate` subcommand: succeeds immediately (config validation passes)
- `run` subcommand: waits for SIGTERM after starting, writes a marker file, then exits
- There is also a "stubborn" version: ignores SIGTERM -- used to verify SIGKILL escalation after the graceful-exit timeout

Signal-related cases must run in a **separate process**: signal handlers can only be
installed in the main thread, and pytest's main thread is occupied by the test itself.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.core.config import Config
from devproxy.core.paths import Paths

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

# Fake caddy: `validate` succeeds immediately; `run` behavior is determined by mode.
_FAKE_CADDY = """#!__PYTHON__
import os
import signal
import sys
import time

cmd = sys.argv[1] if len(sys.argv) > 1 else ""
marker = os.environ.get("FAKE_CADDY_MARKER", "")
mode = os.environ.get("FAKE_CADDY_MODE", "wait")

if cmd == "validate":
    sys.exit(0)

if cmd == "run":
    if mode == "quit":
        # Exit by itself immediately (simulate "caddy started and then exited")
        sys.exit(0)

    def on_term(signum, frame):
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("term")
        if mode == "stubborn":
            # Record "SIGTERM received" but do **not exit** -- force SIGKILL
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            return
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    with open(marker + ".started", "w", encoding="utf-8") as fh:
        fh.write("started")
    while True:
        time.sleep(0.05)

sys.exit(0)
"""

# Run the gateway-layer run_foreground in a subprocess (signal handling can only happen in the main thread).
_DRIVER = (
    "import sys;"
    "from pathlib import Path;"
    "from devproxy.gateway.foreground import run_foreground;"
    "sys.exit(run_foreground(Path(sys.argv[1]), Path(sys.argv[2]),"
    " float(sys.argv[3]) if len(sys.argv) > 3 else 10.0))"
)

# Run the full CLI in a subprocess (swap the platform for a fake so it does not depend on whether host services are running).
_CLI_DRIVER = (
    "import devproxy.cli._shared as s;"
    "from devproxy.platform.fake import FakePlatform;"
    "s._platform = lambda: FakePlatform();"
    "from devproxy.cli import main;"
    "main()"
)


def _flat(text: str) -> str:
    """Remove newlines and spaces.

    Rich wraps at 80 columns, so assertion phrases may be split -- whether they are
    split is not the behavior under test, and assertions must be immune to it.
    """
    return "".join(text.split())


def _write_fake_caddy(tmp_path: Path, marker: Path, mode: str = "wait") -> Path:
    """Write a fake caddy executable.

    mode: wait (wait for SIGTERM) / quit (exit immediately after run) /
    stubborn (ignore SIGTERM, force SIGKILL).
    """
    path = tmp_path / "fake-caddy"
    path.write_text(_FAKE_CADDY.replace("__PYTHON__", sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


def _child_env(marker: Path, mode: str = "wait", **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC_DIR)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["FAKE_CADDY_MARKER"] = str(marker)
    env["FAKE_CADDY_MODE"] = mode
    env.update(extra)
    return env


def _wait_for(path: Path, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------ CLI-layer behavior


class _FakeCaddyConfig:
    """Fake CaddyConfig: validation result is configurable, and it records which file was validated."""

    def __init__(self, ok: bool = True, detail: str = ""):
        self.ok = ok
        self.detail = detail
        self.validated: list[Path | None] = []

    def validate(self, caddyfile=None, timeout=30.0):
        self.validated.append(caddyfile)
        return self.ok, self.detail


class _FakePlatform:
    """Provide only the two capabilities the run command uses: platform name and whether the service is running."""

    def __init__(self, name: str, active: bool):
        self.name = name
        self.active = active

    def service_active(self, name: str) -> bool:
        return self.active


@pytest.fixture
def env(tmp_path, monkeypatch):
    """devproxy's three directories + an existing fake caddy binary."""
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    paths.caddyfile.write_text("# main Caddyfile\n", encoding="utf-8")
    binary = tmp_path / "caddy"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    config = Config(caddy_bin=str(binary), storage=str(paths.data_dir))
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (paths, config, None, None),
    )
    return paths, config, binary


@pytest.fixture
def validator(monkeypatch):
    fake = _FakeCaddyConfig()
    monkeypatch.setattr("devproxy.cli._shared.CaddyConfig", lambda **kw: fake)
    return fake


@pytest.fixture
def foreground(monkeypatch):
    """Block actually starting a process: only record calls."""
    calls: list[tuple[Path, Path]] = []

    def fake_run(caddy_bin, caddyfile):
        calls.append((Path(caddy_bin), Path(caddyfile)))
        return 0

    monkeypatch.setattr("devproxy.gateway.foreground.run_foreground", fake_run)
    return calls


def _invoke(args: list[str]):
    return CliRunner().invoke(app, args)


def test_run_without_caddy_binary_exits_3(env, validator, foreground):
    """No caddy installed -> unmet precondition (3), and point at devproxy setup."""
    paths, config, binary = env
    binary.unlink()
    res = _invoke(["run"])
    assert res.exit_code == 3, res.output
    assert "rundevproxysetupfirst" in _flat(res.output)
    assert not foreground, "must not start caddy when a precondition is unmet"


def test_run_without_main_caddyfile_exits_3(env, validator, foreground):
    """Setup has not run yet (main Caddyfile missing) -> exit code 3."""
    paths, config, binary = env
    paths.caddyfile.unlink()
    res = _invoke(["run"])
    assert res.exit_code == 3, res.output
    assert "rundevproxysetupfirst" in _flat(res.output)
    assert not foreground


def test_run_with_missing_explicit_config_exits_2(env, validator, foreground):
    """An explicitly specified nonexistent file is a usage error (2), consistent with sync --file."""
    res = _invoke(["run", "--config", str(env[0].config_dir / "nope")])
    assert res.exit_code == 2, res.output
    assert not foreground


def test_run_with_explicit_config_ignores_missing_main_caddyfile(env, validator, foreground):
    """With --config, the main Caddyfile is no longer required to exist."""
    paths, config, binary = env
    paths.caddyfile.unlink()
    other = paths.config_dir / "other.Caddyfile"
    other.write_text("# other Caddyfile\n", encoding="utf-8")
    res = _invoke(["run", "--config", str(other)])
    assert res.exit_code == 0, res.output
    assert validator.validated == [other]
    assert foreground == [(binary, other)]


@pytest.mark.parametrize(
    "platform_name,stop_command",
    [
        ("linux", "systemctl --user stop caddy"),
        ("mac", "launchctl bootout system/com.devproxy.caddy"),
    ],
)
def test_run_with_active_service_exits_3_with_stop_command(
    env, validator, foreground, monkeypatch, platform_name, stop_command
):
    """An active service is running -> 80/443 already held -> exit code 3, with that platform's stop-service command in the hint."""
    monkeypatch.setattr(
        "devproxy.cli._shared._platform",
        lambda: _FakePlatform(platform_name, True),
    )
    res = _invoke(["run"])
    assert res.exit_code == 3, res.output
    assert _flat(stop_command) in _flat(res.output)
    assert not foreground, "must not start caddy when the port is already held"


def test_run_with_active_service_on_unknown_platform_exits_3(
    env, validator, foreground, monkeypatch
):
    """Even an unrecognized platform reaches a conclusion (there is just no ready-made command to copy)."""
    monkeypatch.setattr(
        "devproxy.cli._shared._platform",
        lambda: _FakePlatform("fake", True),
    )
    res = _invoke(["run"])
    assert res.exit_code == 3, res.output
    assert "thecaddyserviceisstillrunning" in _flat(res.output)


def test_run_with_invalid_config_exits_1_and_shows_detail(env, validator, foreground, monkeypatch):
    """Config validation fails -> print the explanation Caddy gave, exit code 1."""
    validator.ok = False
    validator.detail = "Caddyfile:3 - unrecognized directive 'bogus'"
    res = _invoke(["run"])
    assert res.exit_code == 1, res.output
    assert "unrecognizeddirective" in _flat(res.output)
    assert not foreground, "must not start caddy when validation fails"


def test_run_validates_and_launches_the_main_caddyfile(env, validator, foreground):
    """Validate and load the main Caddyfile by default."""
    paths, config, binary = env
    res = _invoke(["run"])
    assert res.exit_code == 0, res.output
    assert validator.validated == [paths.caddyfile]
    assert foreground == [(binary, paths.caddyfile)]


def test_run_propagates_foreground_exit_code(env, validator, monkeypatch):
    """Caddy exits by itself -> devproxy exit code 1."""
    monkeypatch.setattr("devproxy.gateway.foreground.run_foreground", lambda *a, **k: 1)
    res = _invoke(["run"])
    assert res.exit_code == 1, res.output


def test_run_does_not_write_any_state(env, validator, foreground):
    """Foreground mode leaves no trace: no route table, no service, no config changes."""
    paths, config, binary = env
    before = sorted(p.name for p in paths.config_dir.iterdir())
    res = _invoke(["run"])
    assert res.exit_code == 0, res.output
    assert not (paths.state_dir / "routes.json").exists()
    assert sorted(p.name for p in paths.config_dir.iterdir()) == before


# ------------------------------------------------------- gateway-layer behavior


def test_run_foreground_passes_run_and_config_to_caddy(tmp_path):
    """The command line must be `caddy run --config <path>` (consistent with the service unit)."""
    record = tmp_path / "argv"
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("# empty\n", encoding="utf-8")

    # A fake caddy that records argv: validate succeeds, run exits immediately
    recorder = tmp_path / "recorder"
    recorder.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({str(record)!r}, 'w').write('\\0'.join(sys.argv))\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)

    from devproxy.gateway.foreground import run_foreground

    rc = run_foreground(recorder, caddyfile)
    assert rc == 1, "caddy exits by itself -> exit code 1"
    argv = record.read_text(encoding="utf-8").split("\0")
    assert argv[1:] == ["run", "--config", str(caddyfile)]


def test_run_foreground_reports_caddy_exit_code(tmp_path, capsys, monkeypatch):
    """Caddy exits by itself -> report its exit code; devproxy exit code 1."""
    monkeypatch.setenv("FAKE_CADDY_MODE", "quit")
    monkeypatch.setenv("FAKE_CADDY_MARKER", str(tmp_path / "marker"))
    marker = tmp_path / "marker"
    caddy = _write_fake_caddy(tmp_path, marker)
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("# empty\n", encoding="utf-8")

    from devproxy.gateway.foreground import run_foreground

    # The fake caddy exits 0 immediately after run -- still counts as "exited by itself" -> 1
    rc = run_foreground(caddy, caddyfile)
    assert rc == 1
    assert "caddyexited" in _flat(capsys.readouterr().err)


def test_run_foreground_returns_1_when_binary_missing(tmp_path, capsys):
    """The binary cannot start -> clean error + exit code 1, not a traceback."""
    from devproxy.gateway.foreground import run_foreground

    rc = run_foreground(tmp_path / "no-such-caddy", tmp_path / "Caddyfile")
    assert rc == 1
    assert "cannotstartcaddy" in _flat(capsys.readouterr().err)


def test_run_foreground_stops_caddy_cleanly_on_sigint(tmp_path):
    """Ctrl-C -> send SIGTERM to the Caddy process group -> it exits gracefully -> devproxy exit code 0."""
    marker = tmp_path / "marker"
    caddy = _write_fake_caddy(tmp_path, marker)
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("# empty\n", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, str(caddy), str(caddyfile), "10"],
        env=_child_env(marker),
    )
    try:
        assert _wait_for(tmp_path / "marker.started"), "caddy did not start"
        proc.send_signal(signal.SIGINT)
        rc = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:  # pragma: no cover -- fallback on failure
            proc.kill()
            proc.wait(timeout=10)
    assert rc == 0, f"devproxy exit code {rc}"
    assert marker.read_text(encoding="utf-8") == "term", "caddy did not receive SIGTERM"


def test_run_foreground_escalates_to_sigkill_after_graceful_timeout(tmp_path):
    """Caddy ignores SIGTERM -> escalate to SIGKILL after the graceful-exit timeout (leave no stray process)."""
    marker = tmp_path / "marker"
    caddy = _write_fake_caddy(tmp_path, marker, mode="stubborn")
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("# empty\n", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, str(caddy), str(caddyfile), "1"],
        env=_child_env(marker, mode="stubborn"),
    )
    try:
        assert _wait_for(tmp_path / "marker.started"), "caddy did not start"
        proc.send_signal(signal.SIGINT)
        started = time.monotonic()
        rc = proc.wait(timeout=30)
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:  # pragma: no cover -- fallback on failure
            proc.kill()
            proc.wait(timeout=10)
    assert rc == 0, f"devproxy exit code {rc}"
    assert marker.read_text(encoding="utf-8") == "term", "caddy did not receive SIGTERM"
    assert elapsed >= 1.0, "must not kill caddy before the graceful-exit timeout"


# ------------------------------------------------------------ CLI end-to-end


def test_cli_run_starts_caddy_in_foreground_and_stops_on_sigint(tmp_path):
    """End-to-end: `devproxy run` really starts caddy, and Ctrl-C shuts it down cleanly."""
    marker = tmp_path / "marker"
    caddy = _write_fake_caddy(tmp_path, marker)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "Caddyfile").write_text("# main Caddyfile\n", encoding="utf-8")
    (config_dir / "config.toml").write_text(f'caddy_bin = "{caddy}"\n', encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-c", _CLI_DRIVER, "run", "--config", str(config_dir / "Caddyfile")],
        env=_child_env(
            marker,
            DEVPROXY_CONFIG_DIR=str(config_dir),
            DEVPROXY_STATE_DIR=str(tmp_path / "state"),
            DEVPROXY_DATA_DIR=str(tmp_path / "data"),
        ),
    )
    try:
        assert _wait_for(tmp_path / "marker.started"), "caddy did not start"
        proc.send_signal(signal.SIGINT)
        rc = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:  # pragma: no cover -- fallback on failure
            proc.kill()
            proc.wait(timeout=10)
    assert rc == 0, f"devproxy exit code {rc}"
    assert marker.read_text(encoding="utf-8") == "term", "caddy did not receive SIGTERM"
    # Foreground mode leaves no trace
    assert not (tmp_path / "state" / "routes.json").exists()


def test_run_with_other_config_ignores_active_service(env, validator, foreground, monkeypatch):
    """When `--config` points at a **different** Caddyfile, an active service must not block it.

    The service loads the main Caddyfile; another config usually uses a different
    set of ports (debugging, isolated instances exist for exactly this), so the ports
    do not conflict here. If they really do collide, Caddy itself will report
    "address already in use" -- that is more accurate than speculation here.
    """
    monkeypatch.setattr(
        "devproxy.cli._shared._platform",
        lambda: _FakePlatform("linux", True),
    )
    other = env[0].config_dir / "other.Caddyfile"
    other.write_text("# another config\n", encoding="utf-8")
    res = _invoke(["run", "--config", str(other)])
    assert res.exit_code == 0, res.output
    assert foreground == [(env[2], other)], "should start the foreground instance as usual"
