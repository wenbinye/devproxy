"""sync orchestration: compare current state -> build a plan -> execute.

Division of labor:

- `gateway.project`: file discovery, parsing, validation (pure functions, no
  side effects)
- `gateway.sync` (this module): plan (pure function) + execution (side
  effects)
- `cli.sync`: presentation and exit codes only

**Never restart automatically**: when a route with the same name already
exists in the route table but its declaration changed (command or port),
only report the conflict -- do not kill the process or restart it. Changing
the port/command means changing the service, and whether to restart is a
human decision (the fix the tool suggests is `devproxy down <name>` then
sync).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from devproxy.core.config import Config
from devproxy.core.logs import rotate_at_start
from devproxy.core.paths import Paths
from devproxy.gateway.names import domain_for
from devproxy.gateway.names import port_env_name
from devproxy.gateway.project import MODE_EXPOSE
from devproxy.gateway.project import MODE_UP
from devproxy.gateway.project import ProjectFile
from devproxy.gateway.project import ProjectRoute
from devproxy.gateway.project import referenced_port_vars
from devproxy.gateway.routes import Route
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.runner import UpError
from devproxy.gateway.runner import alloc_port
from devproxy.gateway.runner import kill_pid_group
from devproxy.gateway.runner import reachable
from devproxy.gateway.store import RouteStore
from devproxy.gateway.store import RouteStoreError

# Background worker process: it handles "start the app + stay with the app +
# unregister the route when the app exits". It lives in the cli package
# because it reuses cli._context.build_context (there is only one
# implementation for parsing config/storage archives). Here it is referenced
# only by module path string -- no import, so the gateway layer does not end
# up depending on the CLI layer.
WORKER_MODULE = "devproxy.cli._worker"
PAYLOAD_ENV = "DEVPROXY_UP_PAYLOAD"

# The worker goes through "start -> ready" before registering the route, and
# sync leaves some extra margin after that; the real timeout is enforced by
# the worker itself via ready_timeout (on timeout it exits and reports).
_WAIT_MARGIN = 30.0


@dataclass
class SyncPlan:
    """Differences between current state and the declarations."""

    create_up: list[ProjectRoute] = field(default_factory=list)
    create_expose: list[ProjectRoute] = field(default_factory=list)
    skip: list[tuple[ProjectRoute, Route]] = field(default_factory=list)
    conflict: list[tuple[ProjectRoute, Route, str]] = field(default_factory=list)
    prune: list[Route] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.create_up or self.create_expose or self.prune)


@dataclass
class SyncOutcome:
    """The final outcome for one route."""

    name: str
    status: str  # created | skipped | conflict | failed | pruned
    detail: str = ""
    domain: str | None = None
    port: int | None = None
    log_path: Path | None = None
    hint: str = ""


def route_log_path(paths: Paths, name: str) -> Path:
    """Log file path for background apps (both stdout/stderr go here)."""
    return paths.log_dir / "routes" / f"{name}.log"


def app_process_alive(route: Route) -> bool:
    """Is the app process in up mode still alive?

    expose routes have no managed process (the upstream is an external
    service), so there is nothing to check and they are treated as "alive";
    likewise old routes without a pid. **A pid that no longer exists**, or a
    proc_start that does not match (the pid was reused by the system for
    another process), both count as dead -- this is exactly what sync's skip
    check most easily misses: the route record is still there, but the app
    exited long ago, so sync says "already up to date" while the domain is
    dead.
    """
    if route.mode != MODE_UP or route.pid is None:
        return True
    from devproxy.gateway.runner import pid_is_running
    from devproxy.gateway.runner import proc_start_time

    # Not "/proc/{pid}": macOS has no /proc at all, and a hardcoded check there
    # calls every **running** app dead -- sync then tears down and rebuilds an
    # app that is working fine. The platform-agnostic probe (Linux: /proc,
    # macOS: ps) is what the rest of the process handling already uses.
    if not pid_is_running(route.pid):
        return False
    if route.proc_start is not None:
        return proc_start_time(route.pid) == route.proc_start
    return True


def build_sync_plan(project: ProjectFile, existing: list[Route], prune: bool = False) -> SyncPlan:
    """Turn declarations and current state into a plan.

    This reads the process table to determine whether up-mode apps are still alive
    (calling it a "pure function" is outdated: a dead app with a leftover
    route record is the most common bad state, and it must be detected here).
    """
    plan = SyncPlan()
    by_name = {r.name: r for r in existing}
    for declared in project.routes:
        current = by_name.get(declared.name)
        if current is None:
            if declared.mode == MODE_UP:
                plan.create_up.append(declared)
            else:
                plan.create_expose.append(declared)
            continue
        reason = conflict_reason(current, declared)
        if reason is None:
            if not app_process_alive(current):
                # App dead but route record still present -> recreate.
                # UpRunner cleans up the old same-named route
                plan.create_up.append(declared)
                continue
            plan.skip.append((declared, current))
        else:
            plan.conflict.append((declared, current, reason))
    if prune:
        # Only clean up routes that "this file declared but no longer
        # declares". Routes with source None (created manually via
        # up/expose) are never touched.
        declared_names = {r.name for r in project.routes}
        plan.prune = [
            r for r in existing if r.source == str(project.path) and r.name not in declared_names
        ]
    return plan


def conflict_reason(existing: Route, declared: ProjectRoute) -> str | None:
    """Why current state and declaration differ; None when they match."""
    if existing.mode != declared.mode:
        return f"mode differs (current {existing.mode}, expected {declared.mode})"
    if declared.port is not None and existing.port != declared.port:
        return f"port differs (current {existing.port}, expected {declared.port})"
    if declared.mode == MODE_UP:
        current_cmd = list(existing.command or [])
        if current_cmd != list(declared.command or []):
            return f"start command differs (current {current_cmd}, expected {declared.command})"
    return None


def execute_sync_plan(
    project: ProjectFile,
    plan: SyncPlan,
    *,
    store: RouteStore,
    config: Config,
    paths: Paths,
) -> list[SyncOutcome]:
    """Execute the plan. Returns one outcome per entry (in declaration order,
    pruned ones last)."""
    outcomes: dict[str, SyncOutcome] = {}

    def record(outcome: SyncOutcome) -> None:
        outcomes[outcome.name] = outcome

    # 0) Port allocation happens here, in one place, before anything starts:
    # dynamic ports only help when a frontend can *find* its API's port, and
    # that is only knowable if every port of this batch is chosen up front.
    # The map is exported to every app as DEVPROXY_<NAME>_PORT.
    known_ports, allocated = _plan_ports(project, plan, store)
    restarts = _apply_env_drift(plan, known_ports, allocated)

    # 1) up mode: start all background workers first (in parallel), then wait
    # for each to become ready.
    workers: list[tuple[ProjectRoute, subprocess.Popen, Path]] = []
    for declared in plan.create_up:
        log_path = route_log_path(paths, declared.name)
        try:
            proc = _spawn_worker(
                project,
                declared,
                log_path,
                port=allocated[declared.name],
                extra_env=_sibling_env(known_ports),
            )
        except OSError as e:
            record(SyncOutcome(declared.name, "failed", f"cannot start background process: {e}"))
            continue
        workers.append((declared, proc, log_path))

    # 2) expose mode: register the route directly (no process).
    for declared in plan.create_expose:
        try:
            route = register_expose(store, config, declared, str(project.path))
        except (RouteStoreError, RouteTableError) as e:
            record(SyncOutcome(declared.name, "failed", str(e)))
            continue
        detail = f"upstream 127.0.0.1:{route.port}"
        if not reachable(route.port):
            detail += " (upstream not responding right now; route registered)"
        record(
            SyncOutcome(
                declared.name,
                "created",
                detail,
                domain=route.domain,
                port=route.port,
            )
        )

    # 3) Wait for up-mode readiness.
    for declared, proc, log_path in workers:
        timeout = (declared.ready_timeout or config.ready_timeout) + _WAIT_MARGIN
        ok, reason = _wait_ready(store, declared.name, proc, timeout)
        if ok:
            ready_route = store.get(declared.name)
            if ready_route is None:
                # Exited immediately after becoming ready -- the worker
                # already unregistered the route
                record(
                    SyncOutcome(
                        declared.name,
                        "failed",
                        "app exited immediately after starting (route was unregistered)",
                        log_path=log_path,
                    )
                )
                continue
            restart_reason = restarts.get(declared.name)
            detail = f"port {ready_route.port}"
            if restart_reason:
                detail = f"{restart_reason}; {detail}"
            record(
                SyncOutcome(
                    declared.name,
                    "restarted" if restart_reason else "created",
                    detail,
                    domain=ready_route.domain,
                    port=ready_route.port,
                    log_path=log_path,
                )
            )
        else:
            tail = _log_tail(log_path)
            detail = f"{reason}: {tail}" if tail else reason
            record(SyncOutcome(declared.name, "failed", detail, log_path=log_path))

    # 4) Skips and conflicts (report only, take no action).
    for declared, current in plan.skip:
        record(
            SyncOutcome(
                declared.name,
                "skipped",
                f"already up to date (port {current.port})",
                domain=current.domain,
                port=current.port,
            )
        )
    for declared, current, reason in plan.conflict:
        record(
            SyncOutcome(
                declared.name,
                "conflict",
                f"{reason}; current: {_describe(current)}",
                domain=current.domain,
                port=current.port,
                hint=f"run devproxy down {declared.name} and then devproxy up again",
            )
        )

    # 5) prune: clean up declarations removed from this file.
    if plan.prune:
        try:
            prune_routes(store, plan.prune)
        except (RouteStoreError, RouteTableError) as e:
            for route in plan.prune:
                record(SyncOutcome(route.name, "failed", str(e)))
        else:
            for route in plan.prune:
                record(
                    SyncOutcome(
                        route.name,
                        "pruned",
                        "this file no longer declares this route",
                        domain=route.domain,
                    )
                )

    # Output order: declaration order (pruned ones last -- they are no longer
    # declared)
    ordered: list[SyncOutcome] = []
    for declared in project.routes:
        outcome = outcomes.get(declared.name)
        if outcome is not None:
            ordered.append(outcome)
    for route in plan.prune:
        outcome = outcomes.get(route.name)
        if outcome is not None:
            ordered.append(outcome)
    return ordered


def _describe(route: Route) -> str:
    """One-line description of the current route state (for conflict
    reports)."""
    parts = [f"mode {route.mode}", f"port {route.port}"]
    if route.command:
        parts.append(f"command {route.command}")
    if route.pid:
        parts.append(f"pid {route.pid}")
    return ", ".join(parts)


def register_expose(
    store: RouteStore, config: Config, declared: ProjectRoute, source: str
) -> Route:
    """Register an expose route (the upstream port comes from the
    declaration)."""
    port = int(declared.port or 0)
    route = Route(
        name=declared.name,
        domain=domain_for(declared.name, config.domain_suffix),
        port=port,
        mode=MODE_EXPOSE,
        upstream=f"127.0.0.1:{port}",
        source=source,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    store.mutate(lambda t: t.upsert(route))
    return route


def prune_routes(store: RouteStore, routes: list[Route]) -> None:
    """Remove the given routes and take down their child processes too (same
    semantics as `devproxy down`)."""
    names = [r.name for r in routes]

    def apply(table) -> None:
        for name in names:
            table.remove(name)

    store.mutate(apply)
    for route in routes:
        if route.pid:
            # Include the process identity recorded at registration -- the
            # pid may have been reused; do not kill when the identity does
            # not match
            kill_pid_group(route.pid, route.proc_start)


def _plan_ports(
    project: ProjectFile, plan: SyncPlan, store: RouteStore
) -> tuple[dict[str, int], dict[str, int]]:
    """Choose the port of every route this project touches, up front.

    Returns (known_ports, allocated):

    - known_ports: name -> port for every route of this project -- already
      running ones (skip/conflict keep their port), new expose routes
      (declared port), and new up routes (allocated here when the declaration
      does not pin one). This map is what DEVPROXY_<NAME>_PORT exports to each
      app. It is deliberately scoped to the project: a route of another
      project has no business in this app's environment. (Foreground
      `devproxy up <name>` is different: with no project file to consult, it
      exports the ports of **all registered** routes -- documented in
      docs/apps.md.)
    - allocated: the port chosen for each new up-mode route (the declaration's
      port when it pins one). The worker is told to use exactly this port, so
      the map stays consistent with what actually starts.
    """
    existing = {r.name: r for r in store.list()}
    known: dict[str, int] = {}
    for declared in project.routes:
        current = existing.get(declared.name)
        if current is not None and current.port:
            known[declared.name] = current.port
        elif declared.mode == MODE_EXPOSE and declared.port:
            known[declared.name] = int(declared.port)
    allocated: dict[str, int] = {}
    # Every port that is already spoken for: the running routes, the pinned
    # ones, and the ones handed out in this batch. alloc_port() releases the
    # socket before returning, so the OS may hand out the same port twice --
    # two routes on one port would make the second app fail to bind.
    used = set(known.values())
    for declared in plan.create_up:
        if declared.port:
            port = int(declared.port)
        else:
            port = _free_port(used)
        used.add(port)
        allocated[declared.name] = port
        known[declared.name] = port
    return known, allocated


def _free_port(used: set[int]) -> int:
    """A free port that is not already handed out in this batch."""
    for _ in range(20):
        port = alloc_port()
        if port not in used:
            return port
    raise UpError("could not find a free port (the ephemeral port range is exhausted?)")


def _sibling_env(known_ports: dict[str, int]) -> dict[str, str]:
    """DEVPROXY_<NAME>_PORT for every route of the project (own name included:
    it duplicates PORT, but keeps the map symmetric and shell-simple)."""
    return {port_env_name(n): str(p) for n, p in known_ports.items()}


def _apply_env_drift(
    plan: SyncPlan, known_ports: dict[str, int], allocated: dict[str, int]
) -> dict[str, str]:
    """Restart the apps whose sibling port moved under their feet.

    An app that was started with DEVPROXY_API_PORT=32847 keeps that value in
    its environment. When the API is restarted and gets another port, the
    frontend is now pointing at a dead port -- and nothing about its own
    declaration changed, so the ordinary conflict check stays silent. The
    route table remembers what each app was started with, so the drift is
    detected here.

    Such an app is **broken**, not "different from the declaration": keeping
    it running serves no purpose, and `up`'s job is to make the machine match
    the declarations. So a drifted route is turned into a restart instead of a
    conflict -- the same thing `devproxy down <name> && devproxy up` would
    do, minus the extra round trip.

    The restart keeps the route's **port**: the old process is stopped first
    (the worker does that), so the port is free again and the domain keeps
    pointing at the same upstream.

    A *declaration* change (a different command, or a pinned port in the
    file) stays a conflict: there the question is "should the running
    process be replaced by this new definition?", and that is the user's
    call, not devproxy's.

    Returns the routes that were restarted, with the reason.
    """
    expected = _sibling_env(known_ports)
    restarts: dict[str, str] = {}
    for declared, current in list(plan.skip):
        if not current.env:
            # Started before devproxy exported sibling ports -- nothing to
            # compare against, and no stale pointer either.
            continue
        # Only the variables the app actually uses matter: a route that never
        # mentions $DEVPROXY_WEB_PORT does not care that the web route got
        # another port.
        used = referenced_port_vars(current.command)
        if not used:
            continue
        changes = [
            f"{key}: {current.env.get(key, '-')} -> {expected.get(key, '-')}"
            for key in used
            if current.env.get(key) != expected.get(key)
        ]
        if not changes:
            continue
        reason = "sibling port changed (" + ", ".join(changes) + ")"
        plan.skip.remove((declared, current))
        plan.create_up.append(declared)
        # Same port as before: the old process is stopped by the worker, so
        # the port is free again and the domain keeps its upstream.
        allocated[declared.name] = current.port
        restarts[declared.name] = reason
    return restarts


def _spawn_worker(
    project: ProjectFile,
    declared: ProjectRoute,
    log_path: Path,
    *,
    port: int,
    extra_env: dict[str, str],
) -> subprocess.Popen:
    """Start the background worker: it runs the app and stays with it.

    Independent session (start_new_session) -- it keeps staying with the app
    after sync itself exits; the working directory is **the directory
    containing the project file** (not the user's current directory).
    """
    payload = {
        "name": declared.name,
        "command": list(declared.command or []),
        "port": port,
        "ready_timeout": declared.ready_timeout,
        "cwd": str(project.project_dir),
        "source": str(project.path),
        "log_path": str(log_path),
        # Sibling ports (DEVPROXY_<NAME>_PORT) -- the app's own port is
        # already in the payload as "port".
        "extra_env": extra_env,
    }
    env = dict(os.environ)
    env[PAYLOAD_ENV] = json.dumps(payload)
    env["PYTHONPATH"] = _pythonpath()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Move the previous run's log to .1: repeated restarts do not pile logs
    # into one
    rotate_at_start(log_path)
    # stdout/stderr are left for the worker to write into the log file itself
    # (see _LogProxy in cli/_worker.py) -- that way the worker's messages and
    # the app's output share one writer, so when the file is rotated half the
    # messages are not left behind in the old inode.
    return subprocess.Popen(
        [sys.executable, "-m", WORKER_MODULE],
        env=env,
        cwd=str(project.project_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _pythonpath() -> str:
    """Put this package's parent directory on PYTHONPATH -- the background
    process must be able to import devproxy.

    With a normal install (pip/uv into a venv) this step is unnecessary; but
    when running straight from the source tree (development, tests),
    sys.executable may not find the package.
    """
    import devproxy

    root = str(Path(devproxy.__file__).resolve().parent.parent)
    existing = os.environ.get("PYTHONPATH", "")
    return f"{root}{os.pathsep}{existing}" if existing else root


def _wait_ready(
    store: RouteStore, name: str, proc: subprocess.Popen, timeout: float
) -> tuple[bool, str]:
    """Wait for the worker to register the route.

    Route appears in the table == app is ready (the worker "registers only
    after readiness"). The worker itself exiting first = startup failure
    (ready timeout, command not found, ...).
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            route = store.get(name)
        except RouteTableError:
            route = None
        if route is not None:
            return True, ""
        rc = proc.poll()
        if rc is not None:
            return False, f"background process exited (exit code {rc})"
        if time.monotonic() >= deadline:
            return False, f"timed out waiting for route registration ({timeout:.0f}s)"
        time.sleep(0.15)


def _log_tail(path: Path, lines: int = 3) -> str:
    """Take the last few log lines -- used to surface the cause when a
    background process fails."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = [line.strip() for line in content.splitlines() if line.strip()]
    return " / ".join(tail[-lines:])
