import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import devproxy
from devproxy.core.config import Config
from devproxy.gateway.caddy import CaddyAdmin
from devproxy.gateway.routes import Route
from devproxy.gateway.runner import PreconditionError
from devproxy.gateway.runner import UpError
from devproxy.gateway.runner import UpRunner
from devproxy.gateway.runner import alloc_port
from devproxy.gateway.runner import kill_pid_group
from devproxy.gateway.runner import proc_start_time
from devproxy.gateway.runner import reachable
from devproxy.gateway.runner import stop_process_group
from devproxy.gateway.store import RouteStore

from .support import wait_until

# Signal tests must import devproxy in a separate python subprocess -- tell it where the source is
SRC_DIR = str(Path(devproxy.__file__).resolve().parent.parent)


class FakeCaddy:
    """Fake Caddy: validate/reload always succeed, and reload count is recorded."""

    def __init__(self):
        self.reloads = 0

    def validate(self, caddyfile=None):
        return True, "ok"

    def reload(self, caddyfile=None):
        self.reloads += 1
        return True, "ok"


class FakeAdmin:
    """Fake Admin API client. config_payload simulates Caddy's current config."""

    def __init__(self, alive=True):
        self.alive = alive
        self.config_payload = {}

    def is_alive(self):
        return self.alive

    def api(self, method, path, body=None):
        return 200, self.config_payload


def _runner(paths, alive=True):
    cfg = Config(storage=str(paths.data_dir))
    store = RouteStore(paths, cfg, FakeCaddy())
    return UpRunner(store, cfg, FakeAdmin(alive=alive))


# Start a child that really listens on $PORT and exits after 0.5s -- this also verifies PORT injection
# Holds the port open, waits for the runner's readiness probe to connect, then exits.
#
# A fixed "sleep 0.5s" instead of this loses the race on a loaded machine: the
# child can be gone before the runner notices the port is up, and the run fails
# with "child exited first" -- a property of the machine, not of the code under
# test.
#
# A plain socket, not an HTTP server: an HTTP server's own accept() would take
# the probe's connection and this accept would then wait for the timeout.
_LISTEN_AND_EXIT = """
import os, socket

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', int(os.environ['PORT'])))
s.listen(8)
s.settimeout(5)
try:
    c, _ = s.accept()
    c.close()
except OSError:
    pass
"""


def _process_running(pid: int) -> bool:
    """A zombie process does not count as alive. When /proc is absent (macOS), fall back to kill(pid, 0).

    Both branches must be combined in one `except OSError`: `FileNotFoundError` is
    a subclass of `OSError`, so listing it separately with `return False` would make
    this **always return False on macOS** -- because macOS has no /proc at all. The
    consequence is not just two failing test cases: assertions like
    `wait_until(lambda: not _process_running(...))` would become true immediately,
    turning into always-true no-op assertions that cannot catch cleanup regressions.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except OSError:
        # No /proc (macOS), or unreadable -- use ps to probe liveness.
        # Do not use os.kill(pid, 0): it only answers "does this pid still exist",
        # and a **zombie process** still exists (just unreaped). Linux identifies
        # zombies via the Z state in /proc; macOS must use the first letter of ps stat.
        try:
            out = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        state = out.stdout.strip()
        return bool(state) and not state.startswith("Z")


def test_alloc_port_returns_bindable_port():
    port = alloc_port()
    assert 1024 < port < 65536
    s = socket.socket()
    s.bind(("127.0.0.1", port))
    s.close()


def test_reachable_true_and_false():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert reachable(port, timeout=1.0) is True
    finally:
        s.close()
    assert reachable(port, timeout=0.2) is False


def test_stop_process_group_kills_grandchildren(tmp_path):
    """After a child forks a grandchild, stop_process_group must clean up the whole group."""
    marker = tmp_path / "grandchild.pid"
    script = tmp_path / "spawn.sh"
    script.write_text(f"#!/bin/sh\n(sleep 60 & echo $! > {marker})\nsleep 60\n")
    script.chmod(0o755)
    proc = subprocess.Popen([str(script)], start_new_session=True, stdout=subprocess.DEVNULL)
    assert wait_until(lambda: marker.exists() and marker.read_text().strip()), (
        "grandchild pid was not written"
    )
    gpid = int(marker.read_text().strip())
    stop_process_group(proc, proc.pid)
    assert wait_until(lambda: not _process_running(gpid)), (
        "grandchild should have been cleaned up by group"
    )


def test_run_requires_caddy_alive(paths):
    paths.ensure_files()
    runner = _runner(paths, alive=False)
    with pytest.raises(UpError) as cm:
        runner.run("codeup", [sys.executable, "-c", "import time; time.sleep(1)"])
    assert "Admin API" in str(cm.value)


def test_run_caddy_not_running_raises_precondition(paths):
    """Caddy not running is an unmet precondition -- must be PreconditionError (CLI maps it to exit code 3)."""
    paths.ensure_files()
    runner = _runner(paths, alive=False)
    with pytest.raises(PreconditionError) as cm:
        runner.run("codeup", [sys.executable, "-c", "import time; time.sleep(1)"])
    assert "Caddy is not running" in str(cm.value)
    assert "devproxy setup" in str(cm.value), "the error must tell the user what to do next"


def test_run_rejects_occupied_explicit_port(paths):
    paths.ensure_files()
    runner = _runner(paths)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        with pytest.raises(UpError) as cm:
            runner.run("codeup", [sys.executable, "-c", "pass"], port=port)
        assert "already in use" in str(cm.value)
    finally:
        s.close()


def test_run_rejects_invalid_name(paths):
    paths.ensure_files()
    runner = _runner(paths)
    with pytest.raises(UpError):
        runner.run("bad.name", [sys.executable, "-c", "pass"])


def test_run_rejects_invalid_port(paths):
    paths.ensure_files()
    runner = _runner(paths)
    with pytest.raises(UpError):
        runner.run("codeup", [sys.executable, "-c", "pass"], port=0)


def test_run_injects_port_and_registers_route(paths):
    """The child should receive the PORT environment variable; the route is registered once ready."""
    paths.ensure_files()
    runner = _runner(paths)
    ready = []
    # The child exits after 0.5s and the route is deregistered before run() returns;
    # so snapshot the route table at on_ready (right after registration is written to disk) to assert it
    snapshots = []

    def on_ready(domain, port):
        ready.append((domain, port))
        snapshots.append(json.loads(paths.routes_json.read_text()))

    rc = runner.run(
        "codeup",
        [sys.executable, "-c", _LISTEN_AND_EXIT],
        on_ready=on_ready,
    )
    assert rc == 0
    assert ready and ready[0][0] == "codeup.test"
    table = snapshots[0]
    assert table["routes"][0]["name"] == "codeup"
    assert table["routes"][0]["mode"] == "up"
    assert table["routes"][0]["port"] == ready[0][1]
    assert table["routes"][0]["command"][0] == sys.executable


def test_run_injects_sibling_port_env(paths, tmp_path):
    """The child must see the sibling route's port (DEVPROXY_<NAME>_PORT) and
    its own under the same name -- that is how a frontend finds its API."""
    paths.ensure_files()
    runner = _runner(paths)
    marker = tmp_path / "env.txt"
    code = (
        "import os, pathlib, socket\n"
        "pathlib.Path(os.environ['MARKER']).write_text(\n"
        "    os.environ.get('DEVPROXY_API_PORT', '') + '|' + os.environ.get('DEVPROXY_CODEUP_PORT', '')\n"
        ")\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', int(os.environ['PORT'])))\n"
        "s.listen(8)\n"
        "s.settimeout(5)\n"
        "try:\n"
        "    c, _ = s.accept()\n"
        "    c.close()\n"
        "except OSError:\n"
        "    pass\n"
    )
    os.environ["MARKER"] = str(marker)
    try:
        runner.run(
            "codeup",
            [sys.executable, "-c", code],
            extra_env={"DEVPROXY_API_PORT": "41001"},
        )
    finally:
        os.environ.pop("MARKER", None)
    sibling, own = marker.read_text(encoding="utf-8").split("|")
    assert sibling == "41001"
    assert own.isdigit() and int(own) > 0, "the route must know its own port too"


def test_run_cleans_up_route_when_child_exits(paths):
    """The route must be deregistered after the child exits."""
    paths.ensure_files()
    runner = _runner(paths)
    runner.run("codeup", [sys.executable, "-c", _LISTEN_AND_EXIT])
    table = json.loads(paths.routes_json.read_text())
    assert table["routes"] == [], "the route must be deregistered after the child exits"


def test_run_kills_process_on_ready_timeout(paths, tmp_path):
    """Ready timeout -> kill the whole child process group (including grandchildren) -> do not register the route."""
    paths.ensure_files()
    runner = _runner(paths)
    marker = tmp_path / "timeout.pids"
    child_code = (
        "import os,subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"open({str(marker)!r},'w').write(f'{{os.getpid()}} {{p.pid}}');"
        "time.sleep(60)"
    )
    with pytest.raises(UpError) as cm:
        runner.run(
            "slow",
            [sys.executable, "-c", child_code],
            ready_timeout=1,
        )
    assert "ready timeout" in str(cm.value)
    assert not paths.routes_json.exists(), "a timeout must not leave a route behind"
    assert wait_until(lambda: marker.exists() and marker.read_text().strip()), (
        "the child did not write the pid marker"
    )
    child_pid, grandchild_pid = (int(x) for x in marker.read_text().split())
    assert wait_until(
        lambda: not _process_running(child_pid) and not _process_running(grandchild_pid)
    ), "after a timeout the child and grandchild must be killed (child cleanup by group)"


def test_watch_re_registers_when_route_lost_from_caddy(paths):
    """The route is lost from Caddy but still in the route table -> the watchdog should re-register it."""
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.1

    route = Route(name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1")
    store.mutate(lambda t: t.upsert(route))
    before = caddy.reloads

    admin.config_payload = {}  # It is not in Caddy's current config
    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    # Event-driven: wait until the watchdog really re-registers, rather than "sleep 0.5s and assume it happened"
    assert wait_until(lambda: caddy.reloads > before), "a lost route should trigger re-registration"
    stop.set()
    t.join(timeout=10)


def test_watch_does_not_revive_removed_route(paths):
    """It is no longer in the route table (the user explicitly ran down) -> the watchdog must not revive it."""
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.1

    route = Route(name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1")
    store.mutate(lambda t: t.upsert(route))
    store.mutate(lambda t: t.remove("x"))
    before = caddy.reloads

    admin.config_payload = {}
    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    # Give the watchdog at least one chance to run (heartbeat_interval=0.1) -- leave enough margin here:
    # under load the first daemon round may be late, and sleeping too briefly would misjudge it as "not revived"
    time.sleep(1.0)
    stop.set()
    t.join(timeout=10)
    assert caddy.reloads == before, "a route the user deregistered must not be revived"


def test_watch_re_registers_when_only_prefixed_domain_remains(paths):
    """When only ax.test remains in Caddy, route x.test must not be misjudged alive by a bare substring match."""
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.1

    route = Route(name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1")
    store.mutate(lambda t: t.upsert(route))
    before = caddy.reloads

    # Contains only another route, ax.test -- the bare substring "x.test" in '"ax.test"' would match
    admin.config_payload = {
        "apps": {"http": {"servers": {"srv0": {"routes": [{"match": [{"host": ["ax.test"]}]}]}}}}
    }
    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    assert wait_until(lambda: caddy.reloads > before), (
        "a prefixed domain (ax.test) must not make x.test look alive"
    )
    stop.set()
    t.join(timeout=10)


def test_run_deregisters_when_signal_lands_during_registration(paths):
    """SIGTERM lands exactly inside the registration transaction (validate+reload window) -> the route must still be deregistered.

    The first reload is the registration transaction: send SIGTERM to this process
    inside it; the signal handler raises SystemExit from inside mutate (no rollback),
    and run()'s finally block deregisters as a fallback. The second reload falls in
    the deregistration transaction in finally -- the second signal at that point must
    be ignored (stop is already set), otherwise the deregistration transaction is
    interrupted and the route is left pointing at a dead child.
    """
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))

    class SignalingCaddy(FakeCaddy):
        """Send SIGTERM to this process on every reload -- hitting the registration and deregistration windows respectively."""

        def reload(self, caddyfile=None):
            ok = super().reload(caddyfile)
            os.kill(os.getpid(), signal.SIGTERM)
            return ok

    caddy = SignalingCaddy()
    store = RouteStore(paths, cfg, caddy)
    runner = UpRunner(store, cfg, FakeAdmin())
    with pytest.raises(SystemExit) as cm:
        runner.run("codeup", [sys.executable, "-c", _LISTEN_AND_EXIT])
    assert cm.value.code == 143
    table = json.loads(paths.routes_json.read_text())
    assert table["routes"] == [], (
        "a signal-interrupted registration must not leave a route pointing at a dead port"
    )
    assert caddy.reloads == 2, (
        "one registration + one deregistration; the second signal must not interrupt deregistration"
    )


def test_run_signal_forwards_to_group_and_deregisters(paths, tmp_path):
    """SIGTERM -> forward to the whole child group, deregister the route, runner exits with 128+15=143.

    Run UpRunner.run in a separate python subprocess (otherwise the signal would hit
    the test process itself); point the route table at tmp_path via DEVPROXY_*_DIR for this test to inspect.
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    grandchild_pid_file = tmp_path / "signal.grandchild.pid"
    child_code = tmp_path / "signal_child.py"
    child_code.write_text(
        "import os, socket, subprocess, sys, time\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', int(os.environ['PORT'])))\n"
        "s.listen(8)\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "open(os.environ['GRANDCHILD_PID_FILE'], 'w').write(str(g.pid))\n"
        "time.sleep(300)\n"
    )

    runner_main = tmp_path / "signal_runner.py"
    runner_main.write_text(
        "import os, sys\n"
        "sys.path.insert(0, os.environ['DEVPROXY_SRC'])\n"
        "from devproxy.core.config import Config\n"
        "from devproxy.core.paths import default_paths\n"
        "from devproxy.gateway.runner import UpRunner\n"
        "from devproxy.gateway.store import RouteStore\n"
        "\n"
        "class FakeCaddy:\n"
        "    def validate(self, caddyfile=None):\n"
        "        return True, 'ok'\n"
        "\n"
        "    def reload(self, caddyfile=None):\n"
        "        return True, 'ok'\n"
        "\n"
        "class FakeAdmin:\n"
        "    def is_alive(self):\n"
        "        return True\n"
        "\n"
        "    def api(self, method, path, body=None):\n"
        "        return 200, {}\n"
        "\n"
        "paths = default_paths()\n"
        "paths.ensure_files()\n"
        "cfg = Config(storage=str(paths.data_dir))\n"
        "store = RouteStore(paths, cfg, FakeCaddy())\n"
        "runner = UpRunner(store, cfg, FakeAdmin())\n"
        "rc = runner.run(\n"
        "    'codeup',\n"
        "    [sys.executable, os.environ['CHILD_CODE']],\n"
        "    port=int(os.environ['CHILD_PORT']),\n"
        ")\n"
        "sys.exit(rc)\n"
    )

    env = dict(
        os.environ,
        DEVPROXY_CONFIG_DIR=str(paths.config_dir),
        DEVPROXY_STATE_DIR=str(paths.state_dir),
        DEVPROXY_DATA_DIR=str(paths.data_dir),
        DEVPROXY_SRC=SRC_DIR,
        CHILD_CODE=str(child_code),
        CHILD_PORT=str(port),
        GRANDCHILD_PID_FILE=str(grandchild_pid_file),
    )
    proc = subprocess.Popen(
        [sys.executable, str(runner_main)],
        env=env,
        start_new_session=True,  # Separate session: SIGTERM only reaches the runner itself
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_pid = None
    try:
        # Wait for registration (child pid acquired) and the grandchild pid file to be written.
        # Timeout is 60s: on a loaded machine even starting a python subprocess takes several seconds
        deadline = time.time() + 60
        table = None
        while time.time() < deadline and (table is None or not grandchild_pid_file.exists()):
            assert proc.poll() is None, f"runner exited early rc={proc.returncode}"
            if paths.routes_json.exists():
                loaded = json.loads(paths.routes_json.read_text())
                if loaded.get("routes"):
                    table = loaded
            time.sleep(0.1)
        assert table is not None, "route was not registered within 60s"
        child_pid = table["routes"][0]["pid"]
        grandchild_pid = int(grandchild_pid_file.read_text())

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=60)
        assert rc == 143, f"killed by SIGTERM should exit with 128+15, got {rc}"

        table = json.loads(paths.routes_json.read_text())
        assert table["routes"] == [], "the route must be deregistered after a signal exit"
        assert wait_until(
            lambda: not _process_running(child_pid) and not _process_running(grandchild_pid)
        ), "the child and grandchild should be cleaned up by group"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if child_pid is not None and _process_running(child_pid):
            # Test's own fallback cleanup: the identity read at this point necessarily matches, so kill directly
            kill_pid_group(child_pid, proc_start_time(child_pid))


# ------------------------------------------- I-3: pid reuse protection (process identity check)


def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )


def test_proc_start_time_is_stable_and_missing_for_dead_pid():
    """Reading the identity twice for the same process is consistent; a nonexistent pid returns None."""
    a = proc_start_time(os.getpid())
    b = proc_start_time(os.getpid())
    assert a is not None and a == b
    assert proc_start_time(999_999_999) is None


def test_kill_pid_group_kills_when_identity_matches():
    proc = _spawn_sleeper()
    try:
        assert kill_pid_group(proc.pid, proc_start_time(proc.pid)) is True
        assert wait_until(lambda: not _process_running(proc.pid)), (
            "the process group should have been killed"
        )
    finally:
        stop_process_group(proc, proc.pid)


def test_kill_pid_group_skips_reused_pid():
    """The recorded start time no longer matches the current process (pid was reused) -> never kill the wrong process."""
    proc = _spawn_sleeper()
    try:
        forged = (proc_start_time(proc.pid) or "0") + "-forged"
        assert kill_pid_group(proc.pid, forged) is False
        time.sleep(0.3)
        assert _process_running(proc.pid), (
            "an innocent process must not be killed when the identity does not match"
        )
    finally:
        stop_process_group(proc, proc.pid)


def test_kill_pid_group_skips_when_identity_unknown():
    """Old route tables have no proc_start -- when identity cannot be verified, prefer leaving the stray process."""
    proc = _spawn_sleeper()
    try:
        assert kill_pid_group(proc.pid, None) is False
        time.sleep(0.3)
        assert _process_running(proc.pid), (
            "must not kill the process when identity cannot be verified"
        )
    finally:
        stop_process_group(proc, proc.pid)


def test_kill_pid_group_on_dead_pid_is_noop():
    proc = _spawn_sleeper()
    pid, start = proc.pid, proc_start_time(proc.pid)
    stop_process_group(proc, proc.pid)
    time.sleep(0.3)
    assert kill_pid_group(pid, start) is False


def test_run_persists_proc_start_with_pid(paths):
    """Persist the process identity together with the pid at registration, so down can guard against pid reuse."""
    paths.ensure_files()
    runner = _runner(paths)
    snapshots = []

    def on_ready(domain, port):
        snapshots.append(json.loads(paths.routes_json.read_text()))

    runner.run("codeup", [sys.executable, "-c", _LISTEN_AND_EXIT], on_ready=on_ready)
    entry = snapshots[0]["routes"][0]
    assert entry["pid"], "pid must be recorded"
    assert entry.get("proc_start"), "proc_start must be recorded"


# --------------------------------------------- I-4: heartbeat must not revive a deregistered route


def test_watch_does_not_resurrect_route_removed_between_check_and_write(paths, monkeypatch):
    """down lands exactly between the heartbeat's "read table" and "write back" -> must not revive (the check must hold the lock)."""
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.05

    route = Route(name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1")
    store.mutate(lambda t: t.upsert(route))
    admin.config_payload = {}  # It is not in Caddy's current config

    real_mutate = store.mutate
    armed = {"hit": False}

    def racing_mutate(fn, *args, **kwargs):
        if armed["hit"]:
            return real_mutate(fn, *args, **kwargs)
        armed["hit"] = True
        # The user completes a down at exactly this moment (between the unlocked check and the write-back)
        real_mutate(lambda t: t.remove("x"))
        return real_mutate(fn, *args, **kwargs)

    monkeypatch.setattr(store, "mutate", racing_mutate)

    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    # The daemon thread must end by itself (the route is gone, so it should exit) -- allow ample timeout;
    # under load the first heartbeat round may be late
    t.join(timeout=30)
    stop.set()
    assert not t.is_alive(), "the watchdog should exit once the route no longer exists"
    table = json.loads(paths.routes_json.read_text())
    assert table["routes"] == [], "a route removed by down must not be revived by the heartbeat"


# --------------------------------------- C1: must not blindly signal a stale pgid after the leader is reaped


def test_stop_process_group_skips_unverifiable_reaped_leader():
    """Leader already reaped + identity unverifiable -> never signal that pgid (it may have been reused)."""
    victim = _spawn_sleeper()
    reaped = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    try:
        reaped.wait()  # The leader has been reaped, so the pid/pgid could in principle be reused
        stop_process_group(reaped, victim.pid, expected_start="forged")
        time.sleep(0.3)
        assert _process_running(victim.pid), (
            "must not kill an unrelated process group when identity does not match"
        )

        stop_process_group(reaped, victim.pid)  # No identity given -> likewise skip
        time.sleep(0.2)
        assert _process_running(victim.pid), "must not signal when identity cannot be verified"
    finally:
        stop_process_group(victim, victim.pid)


def test_run_kills_grandchildren_after_leader_already_exited(paths, tmp_path):
    """Normal exit path: the leader exits first, but grandchildren must still be cleaned up by group.

    The old implementation first proc.wait() reaped the leader and then killpg -- a pid
    reuse window; the new implementation uses waitid(WNOWAIT) to keep the leader as a
    zombie until the finishing blow is done.
    """
    paths.ensure_files()
    runner = _runner(paths)
    marker = tmp_path / "grandchild-left.pid"
    child_code = (
        "import os,socket,subprocess,sys\n"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
        "s.bind(('127.0.0.1',int(os.environ['PORT'])));s.listen(8)\n"
        # Wait for the readiness probe (same reason as _LISTEN_AND_EXIT: a fixed
        # sleep loses the race on a loaded machine), then fork the grandchild.
        "s.settimeout(5)\n"
        "try:\n"
        "    c,_=s.accept();c.close()\n"
        "except OSError:\n"
        "    pass\n"
        "g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        f"open({str(marker)!r},'w').write(str(g.pid))\n"
    )
    rc = runner.run("codeup", [sys.executable, "-c", child_code])
    assert rc == 0
    assert wait_until(lambda: marker.exists() and marker.read_text().strip())
    gpid = int(marker.read_text())
    assert wait_until(lambda: not _process_running(gpid)), (
        "grandchildren must still be cleaned up after the leader exits"
    )


# ------------------------------- I2: deregistration / heartbeat of same-name routes must check ownership


def test_deregister_does_not_remove_route_replaced_by_new_up(paths):
    import threading  # noqa: F401

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    runner = UpRunner(store, cfg, FakeAdmin())

    old = Route(name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1", pid=111)
    new = Route(name="x", domain="x.test", port=2, mode="up", upstream="127.0.0.1:2", pid=222)
    store.mutate(lambda t: t.upsert(new))

    runner._deregister(old)
    assert store.get("x") == new, "cleanup of the old up must not delete the new same-name route"


def test_watch_stops_without_overwriting_route_replaced_by_new_up(paths):
    """The same-name route has been taken over by a new up (different pid/port) -> the old heartbeat neither overwrites nor spins."""
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.05

    old = Route(name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1", pid=111)
    new = Route(name="x", domain="x.test", port=2, mode="up", upstream="127.0.0.1:2", pid=222)
    store.mutate(lambda t: t.upsert(new))
    admin.config_payload = {}  # The old route really is not in Caddy
    before = caddy.reloads

    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(old, stop), daemon=True)
    t.start()
    t.join(timeout=10)
    stop.set()
    assert not t.is_alive(), "the route was taken over; the old heartbeat should end"
    assert caddy.reloads == before, "the old heartbeat must not overwrite the new route"
    assert store.get("x") == new


# --------------------------- I3: kill_pid_group waits for exit and escalates to SIGKILL


def test_kill_pid_group_escalates_to_sigkill(tmp_path):
    marker = tmp_path / "sigterm-ignored.ready"
    code = (
        "import signal,time,pathlib;"
        "signal.signal(signal.SIGTERM, lambda *a: None);"
        f"pathlib.Path({str(marker)!r}).write_text('1');"
        "time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        assert wait_until(marker.exists), "the child did not finish installing the SIGTERM handler"
        start = proc_start_time(proc.pid)
        assert kill_pid_group(proc.pid, start, timeout=0.3) is True
        assert wait_until(lambda: not _process_running(proc.pid)), (
            "a process ignoring SIGTERM must be taken down by SIGKILL"
        )
    finally:
        stop_process_group(proc, proc.pid)


def test_run_reclaims_existing_route_and_reuses_its_port(paths):
    """An old same-name process holds the explicit port: wait for it to die first, then start the new process -- must not falsely report the port as occupied."""
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    port = alloc_port()
    old_code = (
        "import os,socket,time;"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        "s.bind(('127.0.0.1',int(os.environ['PORT'])));s.listen(8);"
        "time.sleep(60)"
    )
    old = subprocess.Popen(
        [sys.executable, "-c", old_code],
        env=dict(os.environ, PORT=str(port)),
        start_new_session=True,
    )
    try:
        assert wait_until(lambda: reachable(port, timeout=0.2))
        store.mutate(
            lambda t: t.upsert(
                Route(
                    name="codeup",
                    domain="codeup.test",
                    port=port,
                    mode="up",
                    upstream=f"127.0.0.1:{port}",
                    pid=old.pid,
                    proc_start=proc_start_time(old.pid),
                )
            )
        )
        runner = UpRunner(store, cfg, FakeAdmin())
        rc = runner.run("codeup", [sys.executable, "-c", _LISTEN_AND_EXIT], port=port)
        assert rc == 0
        assert wait_until(lambda: not _process_running(old.pid)), (
            "the old process must be taken down"
        )
        assert wait_until(lambda: json.loads(paths.routes_json.read_text())["routes"] == [])
    finally:
        stop_process_group(old, old.pid)


# ------------------------------------- I4: non-executable command maps to UpError


def test_run_maps_non_executable_command_to_up_error(paths, tmp_path):
    paths.ensure_files()
    script = tmp_path / "no-exec.sh"
    script.write_text("#!/bin/sh\nexit 0\n")  # deliberately not chmod +x
    with pytest.raises(UpError) as cm:
        _runner(paths).run("codeup", [str(script)])
    assert "cannot start command" in str(cm.value)


def test_run_maps_missing_command_to_up_error(paths, tmp_path):
    paths.ensure_files()
    with pytest.raises(UpError) as cm:
        _runner(paths).run("codeup", [str(tmp_path / "absent-cmd")])
    assert "cannot start command" in str(cm.value)


# ------------------------- I5: heartbeat no longer silently swallows exceptions (retryable reported once, unknown stops)


def test_watch_retries_store_errors_and_reports_once(paths, monkeypatch, capsys):
    import threading

    from devproxy.gateway.store import RouteStoreError

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.05

    route = Route(name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1")
    store.mutate(lambda t: t.upsert(route))
    admin.config_payload = {}

    calls = {"n": 0}

    def failing_mutate(fn, *args, **kwargs):
        calls["n"] += 1
        raise RouteStoreError("reload keeps failing (test)")

    monkeypatch.setattr(store, "mutate", failing_mutate)
    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    assert wait_until(lambda: calls["n"] >= 3), (
        "a retryable error must not kill the watchdog thread"
    )
    stop.set()
    t.join(timeout=10)
    assert not t.is_alive()

    err = capsys.readouterr().err
    assert err.count("heartbeat failed") == 1, "the same error is reported once, not every round"


def test_watch_stops_on_unknown_exception(paths, monkeypatch, capsys):
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    store = RouteStore(paths, cfg, FakeCaddy())
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.05

    route = Route(name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1")
    store.mutate(lambda t: t.upsert(route))
    admin.config_payload = {}

    def broken_mutate(fn, *args, **kwargs):
        raise RuntimeError("unknown error (test)")

    monkeypatch.setattr(store, "mutate", broken_mutate)
    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    t.join(timeout=10)
    stop.set()
    assert not t.is_alive(), "an unknown exception should be logged and end the thread"
    assert "stopping" in capsys.readouterr().err


# --------------------------- M5: distinguish "identity missing" from "pid reused" diagnostics


def test_kill_pid_group_messages_distinguish_unknown_identity_from_reuse(capsys):
    proc = _spawn_sleeper()
    try:
        assert kill_pid_group(proc.pid, None) is False
        err = capsys.readouterr().err
        assert "missing proc_start" in err
        assert "was reused" not in err

        forged = (proc_start_time(proc.pid) or "0") + "-forged"
        assert kill_pid_group(proc.pid, forged) is False
        assert "was reused" in capsys.readouterr().err
    finally:
        stop_process_group(proc, proc.pid)


# ----------------------- M7: route liveness must be structural, not a global substring match


def test_route_live_ignores_domain_string_in_unrelated_fields():
    from devproxy.gateway.runner import _route_live

    admin = FakeAdmin()
    admin.config_payload = {
        "apps": {
            "http": {
                "servers": {
                    "srv0": {
                        "routes": [
                            {"headers": {"X-Dev": ["x.test"]}},
                            {"match": [{"host": ["ax.test"]}]},
                        ]
                    }
                }
            }
        },
        "logging": {"note": "x.test"},
    }
    assert _route_live(admin, "x.test") is False


def test_route_live_sees_structured_host():
    from devproxy.gateway.runner import _route_live

    admin = FakeAdmin()
    admin.config_payload = {
        "apps": {
            "http": {"servers": {"srv0": {"routes": [{"match": [{"host": ["x.test", "y.test"]}]}]}}}
        }
    }
    assert _route_live(admin, "x.test") is True
    assert _route_live(admin, "y.test") is True
    assert _route_live(admin, "z.test") is False


def test_route_live_handles_malformed_payload_without_raising():
    from devproxy.gateway.runner import _route_live

    admin = FakeAdmin()
    admin.config_payload = ["not", "a", "dict"]
    assert _route_live(admin, "x.test") is False
    admin.config_payload = {"apps": {"http": {"servers": "broken"}}}
    assert _route_live(admin, "x.test") is False
