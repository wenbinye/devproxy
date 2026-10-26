"""Subprocess lifecycle for up: port allocation, start, readiness wait,
signal forwarding, group cleanup.

Following devp's proven approach: the child starts with
start_new_session=True (its own process group), signals are forwarded to the
group, and on exit the group gets SIGTERM -> SIGKILL so no grandchildren are
left behind.

pid-reuse protection: the leader is kept as a zombie (waitid + WNOWAIT)
until the group cleanup finishes -- while the zombie exists the kernel will
not reuse its pid, so killpg necessarily hits the original process group.
Only when the leader has already been reaped elsewhere and the recorded
identity no longer matches do we give up group cleanup.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

from devproxy.core.config import Config
from devproxy.core.logs import RotatingLogWriter
from devproxy.gateway.caddy import CaddyAdmin
from devproxy.gateway.caddy import CaddyError
from devproxy.gateway.names import domain_for
from devproxy.gateway.names import validate_name
from devproxy.gateway.routes import Route
from devproxy.gateway.routes import RouteTable
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.store import RouteStore
from devproxy.gateway.store import RouteStoreError

HEARTBEAT_INTERVAL = 30.0

# os.waitid's WNOWAIT is only available on some Unixes. When absent we fall
# back to Popen.wait(), which reaps the leader early -- stop_process_group
# then gives up on group cleanup (better to leave a stray process than to
# signal a pgid that may have been reused).
_HAS_WAITID = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")


class _RouteGone(Exception):
    """The route is gone from the route table (the user ran down, or a new up
    took it over)."""


class UpError(RuntimeError):
    """up failed."""


class PreconditionError(UpError):
    """A precondition is not met (e.g. Caddy is not running) -- the CLI should
    map this to exit code 3."""


@dataclass
class UpRequest:
    """Input for one up. Parameters are consolidated here so the orchestration
    function does not keep growing."""

    name: str
    cmd: list[str]
    port: int | None = None
    ready_timeout: int | None = None
    on_ready: Callable[[str, int], None] | None = None
    # Working directory for the child. None = inherit devproxy's current
    # directory (old behavior). The project-level config (devproxy.toml)
    # lives in the project root and commands usually must run **there** --
    # when the user runs sync from a subdirectory, the app's cwd must still
    # be the project root.
    cwd: Path | None = None
    # Declaration source (absolute path of devproxy.toml). Stored in the
    # route table; `devproxy up --prune` uses it to decide "was this route
    # declared by this file?".
    source: str | None = None
    # Log file for app output. When set, the child's output is captured in a
    # pipe and written to this file by devproxy itself -- only holding the
    # handle ourselves allows rotation at runtime (see core.logs). None =
    # output inherits the current terminal (old behavior of foreground
    # `devproxy up`).
    log_path: Path | None = None


def _start_log_pump(stream, path: Path) -> None:
    """Move the child's output into the log file (background thread).

    This thread **must keep draining the pipe** -- once the pipe buffer is
    full, the child's next write blocks and the app mysteriously hangs. So:
    on write failure (disk full and the like) discard the content but keep
    reading; only stop on a real EOF.
    """

    def pump() -> None:
        writer = RotatingLogWriter(path)
        try:
            while True:
                try:
                    chunk = stream.readline()
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                writer.write(chunk)
        finally:
            writer.close()
            try:
                stream.close()
            except OSError:
                pass

    thread = threading.Thread(target=pump, name="devproxy-log-pump", daemon=True)
    thread.start()


class _ChildHandle:
    """A started child process and its process group identity, shared by
    signal handling and cleanup."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.pgid: int | None = None
        self.start: str | None = None


def alloc_port() -> int:
    """Bind :0 to get a random free port, then release it immediately. There
    is a tiny race window."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def reachable(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _config_hosts_contains(config: object, domain: str) -> bool:
    """Find a host match in the structured JSON from /config/.

    Do not substring-match the whole JSON: the user's sites.caddy may contain
    `header X-Dev "foo.test"` or a TLS SAN with that domain, and a substring
    match would treat it as a live route, so a genuinely lost route would
    never be rewritten.
    """
    apps = config.get("apps") if isinstance(config, dict) else None
    http = apps.get("http") if isinstance(apps, dict) else None
    servers = http.get("servers") if isinstance(http, dict) else None
    if not isinstance(servers, dict):
        return False
    for server in servers.values():
        if not isinstance(server, dict):
            continue
        routes = server.get("routes")
        if not isinstance(routes, list):
            continue
        for route in routes:
            matches = route.get("match") if isinstance(route, dict) else None
            if not isinstance(matches, list):
                continue
            for match in matches:
                hosts = match.get("host") if isinstance(match, dict) else None
                if isinstance(hosts, list) and domain in hosts:
                    return True
    return False


def _route_live(admin: CaddyAdmin, domain: str) -> bool:
    """Whether the route is really in Caddy's current config (judged
    structurally from host lists)."""
    code, body = admin.api("GET", "/config/")
    if code != 200 or body is None:
        return False
    return _config_hosts_contains(body, domain)


def _pid_is_running(pid: int) -> bool:
    """Whether the process exists and is not a zombie (a zombie counts as
    exited).

    On platforms without /proc (macOS) fall back to `ps -o stat=`; when
    neither works, treat it as "exited" -- the cleanup flow waits for "no
    longer running", not "the pid still exists".
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        state = proc.stdout.strip()
        return bool(state) and not state.startswith("Z")
    fields = stat.rsplit(")", 1)[1].split()
    return bool(fields) and fields[0] != "Z"


def _leader_status(proc: subprocess.Popen) -> int | None:
    """Non-blocking read of the leader's exit status without reaping
    (WNOWAIT); None = still running."""
    if proc.returncode is not None:
        return proc.returncode
    if _HAS_WAITID:
        try:
            info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except (ChildProcessError, OSError):
            return proc.poll()
        if info is None:
            return None
        if info.si_code == getattr(os, "CLD_EXITED", None):
            return info.si_status
        return -info.si_status
    return proc.poll()


def _wait_leader(proc: subprocess.Popen) -> int:
    """Wait for the leader to exit, leaving reaping to the cleanup phase (the
    pid is not reused while it is a zombie)."""
    while True:
        status = _leader_status(proc)
        if status is not None:
            return status
        time.sleep(0.1)


def _wait_exit_unreaped(proc: subprocess.Popen, timeout: float) -> bool:
    """Wait for the leader to exit but keep it as a zombie; returns whether
    the pgid is still "pinned" (not reaped).

    A False return means the platform lacks WNOWAIT or the leader was already
    reaped elsewhere -- in that case the caller must give up the subsequent
    group cleanup.
    """
    if not _HAS_WAITID:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except (ChildProcessError, OSError):
            return False
        if info is not None:
            return True
        time.sleep(0.05)
    # Timed out without exiting: the leader is still alive, so the pid will
    # not be reused either
    return True


def stop_process_group(
    proc: subprocess.Popen | None,
    pgid: int | None = None,
    sig: int = signal.SIGTERM,
    expected_start: str | None = None,
) -> None:
    """Signal the child process group -> bounded wait -> SIGKILL the group,
    so no grandchildren are left either.

    pgid is captured at startup: when the leader exits first (the launcher
    case) grandchildren remain in the process group.

    Safety precondition: when signaling, the leader must still be unreaped (a
    zombie counts), or the recorded start time must match the pgid's current
    identity. Once the leader is reaped, the pid/pgid may be reused by the
    kernel, and a blind killpg would kill an unrelated process group.
    """
    if proc is None:
        return
    if pgid is None:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = proc.pid

    if proc.returncode is not None:
        # Leader already reaped elsewhere: only signal the group when the
        # identity still matches.
        if expected_start is None or proc_start_time(pgid) != expected_start:
            print(
                f"⚠️  devproxy: leader of process group {pgid} was reaped and "
                "its identity cannot be verified; skipping group cleanup "
                "(grandchildren may remain)",
                file=sys.stderr,
            )
            return
        try:
            os.killpg(pgid, sig)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return

    # Leader not reaped (alive or zombie) -> the pid cannot be reused, so
    # signaling the group is safe.
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass

    if _wait_exit_unreaped(proc, 5):
        # Leader has exited but is still a zombie and the pgid is still
        # pinned: send one more signal to clear grandchildren in the group.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _wait_group_gone(pid: int, expected_start: str, timeout: float) -> bool:
    """Poll until the pid is no longer a "running process" (zombies included)
    or its identity changed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        if proc_start_time(pid) != expected_start:
            return True
        time.sleep(0.05)
    return False


def kill_pid_group(pid: int, proc_start: str | None = None, timeout: float = 5.0) -> bool:
    """Kill a leftover child from the previous up by process group
    (pid == pgid).

    routes.json survives restarts and pids are recycled by the OS -- signal
    only when the current process start time matches what was recorded at
    registration; if the identity does not match or cannot be verified,
    prefer leaving a stray process over killing an innocent process group
    (up's own finally still cleans up its children through the direct
    handle).

    SIGTERM first, then SIGKILL if it still has not exited after a bounded
    wait -- consistent with stop_process_group's policy, and it guarantees
    the listening port is released on return. Returns whether a signal was
    actually sent to a verified process group.
    """
    current = proc_start_time(pid)
    if current is None:
        return False  # process no longer exists; nothing to do
    if proc_start is None:
        print(
            f"⚠️  cannot verify the identity of process {pid} "
            "(route table is missing proc_start); skipping cleanup",
            file=sys.stderr,
        )
        return False
    if proc_start != current:
        print(
            f"⚠️  pid {pid} was reused "
            "(recorded start time does not match the current one); skipping cleanup",
            file=sys.stderr,
        )
        return False
    if not _pid_is_running(pid):
        return False  # only a zombie remains; the port is already released
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    if _wait_group_gone(pid, proc_start, timeout):
        return True
    # SIGTERM timed out and the identity still matches -> escalate to SIGKILL
    if proc_start_time(pid) != proc_start or not _pid_is_running(pid):
        return True
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return False
    _wait_group_gone(pid, proc_start, 2.0)
    return True


def proc_start_time(pid: int) -> str | None:
    """Read the process "identity": /proc/<pid>/stat field 22 (start time,
    clock ticks).

    Pids are recycled by the OS, and the pid alone cannot tell whether it is
    still the original process -- the start time is the standard way to
    detect pid reuse. On platforms without /proc (macOS) fall back to
    `ps -o lstart=`. Return None when neither works (= cannot verify).
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        try:
            proc = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout.strip() or None
    # Field 2, comm, is parenthesized and may contain spaces -- split only
    # after the last ')'. After the split the first field is field 3 state,
    # and field 22 starttime is at index 19.
    fields = stat.rsplit(")", 1)[1].split()
    if len(fields) <= 19:
        return None
    return fields[19]


def _same_owner(current: Route, expected: Route) -> bool:
    """Ownership check for same-named routes: matching name is not enough;
    pid/port must match too."""
    return (
        current.name == expected.name
        and current.pid == expected.pid
        and current.port == expected.port
    )


class UpRunner:
    heartbeat_interval = HEARTBEAT_INTERVAL

    def __init__(self, store: RouteStore, config: Config, admin: CaddyAdmin):
        self.store = store
        self.config = config
        self.admin = admin

    def run(
        self,
        name: str,
        cmd: list[str],
        port: int | None = None,
        ready_timeout: int | None = None,
        on_ready: Callable[[str, int], None] | None = None,
    ) -> int:
        """CLI-compatible entry point; the real orchestration lives in
        `_execute`.

        The parameter signature is kept for now (cli/up.py calls it
        positionally/by keyword); internally everything is consolidated into
        UpRequest so new options need not spread into the orchestration
        logic.
        """
        return self.run_request(
            UpRequest(
                name=name,
                cmd=list(cmd),
                port=port,
                ready_timeout=ready_timeout,
                on_ready=on_ready,
            )
        )

    def run_request(self, req: UpRequest) -> int:
        """Execute one up from an UpRequest -- the single orchestration entry
        point.

        `run()` is only a thin wrapper that consolidates positional
        parameters into an UpRequest; callers that need extra information
        such as cwd / source (the background worker of `devproxy up`)
        construct an UpRequest directly and call this.
        """
        return self._execute(req)

    def _execute(self, req: UpRequest) -> int:
        self._validate_request(req)
        if not self.admin.is_alive():
            raise PreconditionError(
                "Caddy is not running (Admin API unreachable); run devproxy setup first or start Caddy"
            )

        # Clean up any same-named route first (taking down the old child too)
        self._reclaim_existing(req.name)
        port = self._choose_port(req.port)

        stop = threading.Event()
        child = _ChildHandle()
        old_handlers = self._install_handlers(stop, child)
        route: Route | None = None
        registered = False
        watcher: threading.Thread | None = None
        try:
            proc = self._spawn(req, port, child)
            self._wait_ready(proc, port, self._timeout(req))

            route = self._make_route(req, port, child)
            # Set the flag before registering: a signal may arrive inside
            # mutate (validate+reload, a window of seconds) and raise
            # SystemExit, in which case finally must still try to deregister
            # so no route points at a dead port; conditional removal ensures
            # a new same-named route is not deleted by mistake.
            registered = True
            self.store.mutate(lambda t: t.upsert(route))
            watcher = threading.Thread(target=self._watch, args=(route, stop), daemon=True)
            watcher.start()
            if req.on_ready is not None:
                req.on_ready(route.domain, port)
            rc = _wait_leader(proc)
        finally:
            stop.set()
            # Kill the process group before join/deregister: this shortens
            # the cleanup window and prevents the watcher from writing the
            # route back after the child dies.
            stop_process_group(child.proc, child.pgid, expected_start=child.start)
            if watcher is not None:
                watcher.join(timeout=10)
            if registered and route is not None:
                self._deregister(route)
            for s, handler in old_handlers.items():
                signal.signal(s, handler)
        # When killed by a signal wait() returns -n; map it to the shell
        # convention 128+n
        return 128 - rc if rc < 0 else rc

    def _validate_request(self, req: UpRequest) -> None:
        try:
            validate_name(req.name)
        except ValueError as e:
            raise UpError(str(e)) from e
        if req.port is not None and not (1 <= req.port <= 65535):
            raise UpError(f"invalid port {req.port}: must be between 1 and 65535")

    def _timeout(self, req: UpRequest) -> int:
        return req.ready_timeout if req.ready_timeout is not None else self.config.ready_timeout

    def _reclaim_existing(self, name: str) -> None:
        """Unregister the old same-named route and kill the old child.

        "Read old route + remove" must happen in the same mutate callback
        (holding flock), otherwise a concurrent up/down can slip in between
        the check and the write and delete the other's live route. The old
        route is carried out of the lock through a closure container, and
        killing the process happens outside the lock (it may take several
        seconds).
        """
        if self.store.get(name) is None:
            return
        removed: list[Route] = []

        def apply(t: RouteTable) -> None:
            old = t.get(name)
            if old is not None:
                removed.append(old)
                t.remove(name)

        self.store.mutate(apply)
        if not removed:
            return
        old = removed[0]
        if old.pid:
            kill_pid_group(old.pid, old.proc_start)

    def _choose_port(self, explicit: int | None) -> int:
        if explicit is None:
            return alloc_port()
        # The old process has already been waited out in _reclaim_existing
        # (kill_pid_group returns only after the port is released); probing
        # here is what reliably answers "is another service really holding
        # it".
        if reachable(explicit, timeout=0.2):
            raise UpError(f"port {explicit} is already in use")
        return explicit

    def _spawn(self, req: UpRequest, port: int, child: _ChildHandle) -> subprocess.Popen:
        env = dict(os.environ, PORT=str(port))
        try:
            # cwd=None behaves exactly as before (inherit the current working
            # directory); when cwd is set, place the child in that directory
            # (the project root).
            extra: dict[str, Any] = {}
            if req.log_path is not None:
                # Capture into a pipe: devproxy writes the log file itself so
                # it can rotate at runtime. The cost is an extra pump thread
                # -- it **must keep draining the pipe** or the child will
                # hang once the pipe buffer fills.
                extra = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
            proc = subprocess.Popen(req.cmd, env=env, cwd=req.cwd, start_new_session=True, **extra)
        except OSError as e:
            # Covers FileNotFoundError / PermissionError / Exec format error:
            # all map to UpError so the CLI prints a formatted error instead
            # of a raw traceback.
            raise UpError(f"cannot start command {req.cmd[0]!r}: {e}") from e
        child.proc = proc
        child.pgid = proc.pid  # start_new_session=True -> child is process group leader
        child.start = proc_start_time(proc.pid)
        if req.log_path is not None and proc.stdout is not None:
            _start_log_pump(proc.stdout, req.log_path)
        return proc

    def _wait_ready(self, proc: subprocess.Popen, port: int, timeout: int) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = _leader_status(proc)
            if status is not None:
                raise UpError(f"child exited first (code={status}); not registered")
            if reachable(port, timeout=1):
                return
            time.sleep(0.3)
        raise UpError(
            f"ready timeout ({timeout}s) -- the app may not be using $PORT; "
            "specify one explicitly with --port"
        )

    def _make_route(self, req: UpRequest, port: int, child: _ChildHandle) -> Route:
        proc = child.proc
        if proc is None:  # pragma: no cover - only called after _spawn succeeds
            raise UpError("internal error: child process has not started")
        return Route(
            name=req.name,
            domain=domain_for(req.name, self.config.domain_suffix),
            port=port,
            mode="up",
            upstream=f"127.0.0.1:{port}",
            command=list(req.cmd),
            pid=proc.pid,
            proc_start=child.start,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            source=req.source,
        )

    def _deregister(self, route: Route) -> None:
        """Deregister the route this process registered -- a new same-named
        route must not be deleted by mistake."""

        def apply(t: RouteTable) -> None:
            current = t.get(route.name)
            if current is not None and _same_owner(current, route):
                t.remove(route.name)

        try:
            self.store.mutate(apply)
        except Exception:
            # Swallowing it silently would leave a stale route pointing at a
            # killed child -- the user must be told
            print(
                f"⚠️  devproxy: deregistration failed; to remove the stale route "
                f"run devproxy down {route.name}",
                file=sys.stderr,
            )

    def _install_handlers(
        self, stop: threading.Event, child: _ChildHandle
    ) -> dict[int, signal.Handlers | Callable[[int, FrameType | None], Any] | int | None]:
        def on_signal(signum, frame):
            # Ignore a second signal during cleanup so finally is not
            # interrupted and leaves stragglers
            if stop.is_set():
                return
            stop.set()
            if child.pgid is not None:
                child_sig = signal.SIGINT if signum == signal.SIGINT else signal.SIGTERM
                try:
                    os.killpg(child.pgid, child_sig)
                except (ProcessLookupError, PermissionError):
                    pass
            raise SystemExit(128 + signum)

        return {
            s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }

    def _watch(self, route: Route, stop: threading.Event) -> None:
        """Every heartbeat_interval seconds, confirm the route is still in
        Caddy.

        Routes are file-based, and normally a Caddy restart loads them
        automatically; this self-check covers "Caddy was started with a
        different config" and "someone edited the rendered output by hand".
        The check for "is it still in the route table" must live inside the
        mutate callback, under the same flock as the write-back -- if the
        check were outside the lock, down could slip in exactly between the
        check and the write-back and resurrect a just-unregistered route as
        an entry pointing at a dead port.

        Failures must not be swallowed silently: when Caddy reload keeps
        failing the user has to know, otherwise routes return 404/502 while
        `devproxy list` still shows everything is fine. The same error is
        reported only once (deduplicated by error text); an unknown exception
        is logged and then ends the thread.
        """
        last_error: str | None = None
        while not stop.wait(self.heartbeat_interval):
            try:
                if _route_live(self.admin, route.domain):
                    continue

                def apply(t: RouteTable) -> None:
                    current = t.get(route.name)
                    if current is None:
                        raise _RouteGone  # user already unregistered -- do not resurrect
                    if not _same_owner(current, route):
                        raise _RouteGone  # new same-named up took over -- old heartbeat must not overwrite

                    t.upsert(route)

                # An exception raised from the callback escapes mutate before
                # render/write/reload, so the transaction ends without writing
                # anything -- it neither resurrects the route nor wastes a
                # reload
                try:
                    self.store.mutate(apply)
                except _RouteGone:
                    return  # route no longer exists; end the thread
            except (RouteStoreError, RouteTableError, CaddyError, OSError) as e:
                message = f"{type(e).__name__}: {e}"
                if message != last_error:
                    print(
                        f"⚠️  devproxy: heartbeat failed for route {route.name} "
                        f"(will keep retrying): {message}",
                        file=sys.stderr,
                    )
                    last_error = message
            except Exception as e:  # noqa: BLE001 - an unknown exception must not kill the daemon thread silently
                print(
                    f"⚠️  devproxy: heartbeat for route {route.name} hit an "
                    f"unexpected exception; stopping: {e}",
                    file=sys.stderr,
                )
                return
