"""devproxy sync: plan, conflicts, prune, exit codes, and background workers."""

import json
import sys

import pytest
from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.gateway.project import load_project_file
from devproxy.gateway.routes import Route
from devproxy.gateway.store import RouteStore
from devproxy.gateway.sync import build_sync_plan
from devproxy.gateway.sync import conflict_reason


class FakeCaddy:
    """Fake Caddy: validate/reload always succeed."""

    def validate(self, caddyfile=None):
        return True, "ok"

    def reload(self, caddyfile=None):
        return True, "ok"


class FakeAdmin:
    """Fake Admin API client."""

    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive

    def api(self, method, path, body=None):
        return 200, {}


class FakeProc:
    """Fake background worker handle."""

    def __init__(self, rc=None):
        self._rc = rc

    def poll(self):
        return self._rc


UP_TOML = """
[[routes]]
name = "web"
command = ["npm", "run", "dev"]
"""

EXPOSE_TOML = """
[[routes]]
name = "legacy"
mode = "expose"
port = 3000
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVPROXY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DEVPROXY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DEVPROXY_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """A project directory containing devproxy.toml (sync's discovery starting point)."""
    directory = tmp_path / "project"
    directory.mkdir()
    monkeypatch.chdir(directory)
    return directory


@pytest.fixture
def ctx(env, monkeypatch):
    """Replace the CLI's dependency wiring with a fake Caddy / fake Admin."""
    paths = Paths(env / "config", env / "state", env / "data")
    paths.ensure_files()
    paths.caddyfile.write_text("# test Caddyfile\n", encoding="utf-8")
    cfg = Config(storage=str(paths.data_dir))
    store = RouteStore(paths, cfg, FakeCaddy())
    admin = FakeAdmin()
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (paths, cfg, store, admin),
    )
    return paths, cfg, store, admin


def _stub_spawn(monkeypatch, store, *, register=True, rc=None, calls=None, recorded=None):
    """Replace the background worker with a fake: register the route on success, exit directly on failure."""

    def spawn(project, declared, log_path, *, port=None, extra_env=None):
        if calls is not None:
            calls.append(declared.name)
        if recorded is not None:
            recorded[declared.name] = {"port": port, "extra_env": extra_env or {}}
        if register:
            used_port = port or declared.port or 45671
            route = Route(
                name=declared.name,
                domain=f"{declared.name}.test",
                port=used_port,
                mode="up",
                upstream=f"127.0.0.1:{used_port}",
                command=list(declared.command or []),
                source=str(project.path),
            )
            store.mutate(lambda t: t.upsert(route))
        return FakeProc(rc)

    monkeypatch.setattr("devproxy.gateway.sync._spawn_worker", spawn)


def _routes_json(env) -> list:
    path = env / "state" / "routes.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["routes"]


# ------------------------------- Plan (pure functions)


def _project(tmp_path, text):
    path = tmp_path / "devproxy.toml"
    path.write_text(text, encoding="utf-8")
    return load_project_file(path)


def test_plan_creates_missing_routes(tmp_path):
    project = _project(tmp_path, UP_TOML + EXPOSE_TOML)
    plan = build_sync_plan(project, [])
    assert [r.name for r in plan.create_up] == ["web"]
    assert [r.name for r in plan.create_expose] == ["legacy"]


def test_plan_skips_identical_route(tmp_path):
    project = _project(tmp_path, UP_TOML)
    existing = [
        Route(
            name="web",
            domain="web.test",
            port=45671,
            mode="up",
            upstream="127.0.0.1:45671",
            command=["npm", "run", "dev"],
        )
    ]
    plan = build_sync_plan(project, existing)
    assert plan.skip and plan.skip[0][0].name == "web"
    assert not plan.create_up
    assert not plan.conflict


def test_plan_reports_command_change_as_conflict(tmp_path):
    project = _project(tmp_path, UP_TOML)
    existing = [
        Route(
            name="web",
            domain="web.test",
            port=45671,
            mode="up",
            upstream="127.0.0.1:45671",
            command=["npm", "run", "old"],
        )
    ]
    plan = build_sync_plan(project, existing)
    assert not plan.create_up
    assert plan.conflict
    assert "start command differs" in plan.conflict[0][2]


def test_plan_reports_port_change_as_conflict(tmp_path):
    project = _project(tmp_path, EXPOSE_TOML)
    existing = [
        Route(
            name="legacy",
            domain="legacy.test",
            port=3001,
            mode="expose",
            upstream="127.0.0.1:3001",
        )
    ]
    plan = build_sync_plan(project, existing)
    assert plan.conflict
    assert "port differs" in plan.conflict[0][2]


def test_plan_reports_mode_change_as_conflict(tmp_path):
    project = _project(tmp_path, EXPOSE_TOML)
    existing = [
        Route(
            name="legacy",
            domain="legacy.test",
            port=3000,
            mode="up",
            upstream="127.0.0.1:3000",
            command=["x"],
        )
    ]
    plan = build_sync_plan(project, existing)
    assert plan.conflict
    assert "mode differs" in plan.conflict[0][2]


def test_plan_prune_only_touches_routes_declared_by_this_file(tmp_path):
    project = _project(tmp_path, UP_TOML)
    mine = Route(
        name="old",
        domain="old.test",
        port=1,
        mode="up",
        upstream="127.0.0.1:1",
        source=str(project.path),
    )
    manual = Route(
        name="handmade",
        domain="handmade.test",
        port=2,
        mode="expose",
        upstream="127.0.0.1:2",
    )
    plan = build_sync_plan(project, [mine, manual], prune=True)
    assert [r.name for r in plan.prune] == ["old"]


def test_plan_without_prune_never_prunes(tmp_path):
    project = _project(tmp_path, UP_TOML)
    mine = Route(
        name="old",
        domain="old.test",
        port=1,
        mode="up",
        upstream="127.0.0.1:1",
        source=str(project.path),
    )
    plan = build_sync_plan(project, [mine])
    assert plan.prune == []


def test_conflict_reason_is_none_for_consistent_declaration(tmp_path):
    project = _project(tmp_path, EXPOSE_TOML)
    declared = project.get("legacy")
    existing = Route(
        name="legacy",
        domain="legacy.test",
        port=3000,
        mode="expose",
        upstream="127.0.0.1:3000",
    )
    assert conflict_reason(existing, declared) is None


# ------------------------------- Command behavior


def test_sync_without_project_file_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(app, ["up"])
    assert res.exit_code == 2, res.output
    assert "devproxy.toml" in res.output


def test_sync_with_broken_toml_exits_2(project_dir):
    (project_dir / "devproxy.toml").write_text("[[routes]\nname='web'\n")
    res = CliRunner().invoke(app, ["up"])
    assert res.exit_code == 2, res.output
    assert "TOML" in res.output


def test_sync_requires_caddy_running(ctx, project_dir):
    paths, cfg, store, admin = ctx
    admin.alive = False
    (project_dir / "devproxy.toml").write_text(UP_TOML)
    res = CliRunner().invoke(app, ["up"])
    assert res.exit_code == 3, res.output


def test_sync_creates_up_route(ctx, env, project_dir, monkeypatch):
    paths, cfg, store, admin = ctx
    (project_dir / "devproxy.toml").write_text(UP_TOML)
    _stub_spawn(monkeypatch, store)
    res = CliRunner().invoke(app, ["up"])
    assert res.exit_code == 0, res.output
    routes = _routes_json(env)
    assert len(routes) == 1
    assert routes[0]["name"] == "web"
    assert routes[0]["mode"] == "up"
    assert routes[0]["command"] == ["npm", "run", "dev"]
    assert routes[0]["source"] == str(project_dir / "devproxy.toml")


TWO_ROUTES_TOML = """
[[routes]]
name = "api"
command = ["npm", "run", "api"]

[[routes]]
name = "web"
command = ["npm", "run", "web"]
"""


def test_sync_allocates_ports_up_front_and_exports_sibling_ports(
    ctx, env, project_dir, monkeypatch
):
    """Every route of a project gets a dynamic port, and each app learns the
    others' ports as DEVPROXY_<NAME>_PORT -- that is what lets a frontend
    point at its API without a hardcoded port."""
    paths, cfg, store, admin = ctx
    (project_dir / "devproxy.toml").write_text(TWO_ROUTES_TOML)
    recorded: dict = {}
    _stub_spawn(monkeypatch, store, recorded=recorded)
    res = CliRunner().invoke(app, ["up"])
    assert res.exit_code == 0, res.output
    api_port = recorded["api"]["port"]
    web_port = recorded["web"]["port"]
    assert api_port and web_port
    assert api_port != web_port, "two routes must not share a port"
    assert recorded["web"]["extra_env"]["DEVPROXY_API_PORT"] == str(api_port)
    assert recorded["api"]["extra_env"]["DEVPROXY_WEB_PORT"] == str(web_port)
    assert recorded["web"]["extra_env"]["DEVPROXY_WEB_PORT"] == str(web_port)


def test_sync_exports_declared_port_to_siblings(ctx, env, project_dir, monkeypatch):
    """A route that pins its port (an expose route, or a fixed-port app)
    still tells its siblings about it."""
    paths, cfg, store, admin = ctx
    (project_dir / "devproxy.toml").write_text(
        '[[routes]]\nname = "db"\nmode = "expose"\nport = 5432\n\n'
        '[[routes]]\nname = "web"\ncommand = ["npm", "run", "web"]\n'
    )
    recorded: dict = {}
    _stub_spawn(monkeypatch, store, recorded=recorded)
    res = CliRunner().invoke(app, ["up"])
    assert res.exit_code == 0, res.output
    assert recorded["web"]["extra_env"]["DEVPROXY_DB_PORT"] == "5432"


def test_sync_creates_expose_route(ctx, env, project_dir):
    paths, cfg, store, admin = ctx
    (project_dir / "devproxy.toml").write_text(EXPOSE_TOML)
    res = CliRunner().invoke(app, ["up"])
    assert res.exit_code == 0, res.output
    routes = _routes_json(env)
    assert routes[0]["name"] == "legacy"
    assert routes[0]["mode"] == "expose"
    assert routes[0]["port"] == 3000
    assert routes[0]["source"] == str(project_dir / "devproxy.toml")


def test_sync_is_idempotent(ctx, env, project_dir, monkeypatch):
    paths, cfg, store, admin = ctx
    (project_dir / "devproxy.toml").write_text(UP_TOML)
    _stub_spawn(monkeypatch, store)
    first = CliRunner().invoke(app, ["up"])
    assert first.exit_code == 0, first.output
    before = (env / "state" / "routes.json").read_text()

    second = CliRunner().invoke(app, ["up"])
    assert second.exit_code == 0, second.output
    assert "Everything is up to date" in second.output
    assert (env / "state" / "routes.json").read_text() == before, (
        "the second run must not change the route table"
    )


def test_sync_reports_conflict_without_touching_anything(ctx, env, project_dir):
    paths, cfg, store, admin = ctx
    (project_dir / "devproxy.toml").write_text(UP_TOML)
    old = Route(
        name="web",
        domain="web.test",
        port=45671,
        mode="up",
        upstream="127.0.0.1:45671",
        command=["npm", "run", "old"],
        pid=4242,
        proc_start="1",
    )
    store.mutate(lambda t: t.upsert(old))
    before = (env / "state" / "routes.json").read_text()

    res = CliRunner().invoke(app, ["up"])
    assert res.exit_code == 1, res.output
    assert "conflicts" in res.output
    assert "devproxy down web" in res.output
    after = _routes_json(env)
    assert after[0]["command"] == ["npm", "run", "old"], "a conflict must not change the command"
    assert after[0]["pid"] == 4242, "a conflict must not touch the existing process"
    assert json.loads(before)["routes"] == after


def test_sync_prune_removes_only_file_declared_routes(ctx, env, project_dir, monkeypatch):
    paths, cfg, store, admin = ctx
    (project_dir / "devproxy.toml").write_text(UP_TOML)
    _stub_spawn(monkeypatch, store)
    mine = Route(
        name="stale",
        domain="stale.test",
        port=1,
        mode="up",
        upstream="127.0.0.1:1",
        source=str(project_dir / "devproxy.toml"),
    )
    manual = Route(
        name="handmade",
        domain="handmade.test",
        port=2,
        mode="expose",
        upstream="127.0.0.1:2",
    )
    store.mutate(lambda t: (t.upsert(mine), t.upsert(manual)))

    res = CliRunner().invoke(app, ["up", "--prune", "--yes"])
    assert res.exit_code == 0, res.output
    names = [r["name"] for r in _routes_json(env)]
    assert "stale" not in names, "routes declared in this file should be pruned"
    assert "handmade" in names, "routes created by manual up/expose must not be pruned"


def test_sync_prune_without_yes_in_non_interactive_exits_4(ctx, env, project_dir):
    paths, cfg, store, admin = ctx
    (project_dir / "devproxy.toml").write_text(UP_TOML)
    mine = Route(
        name="stale",
        domain="stale.test",
        port=1,
        mode="up",
        upstream="127.0.0.1:1",
        source=str(project_dir / "devproxy.toml"),
    )
    store.mutate(lambda t: t.upsert(mine))
    res = CliRunner().invoke(app, ["up", "--prune"])
    assert res.exit_code == 4, res.output
    assert [r["name"] for r in _routes_json(env)] == ["stale"], "nothing changed after declining"


def test_sync_partial_failure_exits_1_and_continues(ctx, env, project_dir, monkeypatch):
    paths, cfg, store, admin = ctx
    (project_dir / "devproxy.toml").write_text(
        '[[routes]]\nname = "web"\ncommand = ["npm", "run", "dev"]\n\n'
        '[[routes]]\nname = "api"\ncommand = ["npm", "run", "api"]\n'
    )
    calls = []

    def spawn(project, declared, log_path, *, port=None, extra_env=None):
        calls.append(declared.name)
        if declared.name == "web":
            # Startup failure: the process exits immediately and the route is not registered
            return FakeProc(1)
        port = 45672
        route = Route(
            name=declared.name,
            domain=f"{declared.name}.test",
            port=port,
            mode="up",
            upstream=f"127.0.0.1:{port}",
            command=list(declared.command or []),
            source=str(project.path),
        )
        store.mutate(lambda t: t.upsert(route))
        return FakeProc()

    monkeypatch.setattr("devproxy.gateway.sync._spawn_worker", spawn)
    res = CliRunner().invoke(app, ["up"])
    assert res.exit_code == 1, res.output
    assert "failed" in res.output
    assert [r["name"] for r in _routes_json(env)] == ["api"], (
        "the remaining routes should still be created"
    )


def test_sync_uses_explicit_file(ctx, env, tmp_path, monkeypatch):
    paths, cfg, store, admin = ctx
    other = tmp_path / "elsewhere.toml"
    other.write_text(EXPOSE_TOML, encoding="utf-8")
    res = CliRunner().invoke(app, ["up", "--file", str(other)])
    assert res.exit_code == 0, res.output
    assert _routes_json(env)[0]["name"] == "legacy"


def test_sync_spawns_real_worker_and_reports_its_failure(ctx, env, project_dir):
    """Really start a background worker (no stub): module path, workload, and log redirection must all be right.

    The command deliberately points at a nonexistent executable, so the worker fails and
    exits. What this pins down is the **plumbing**: the worker really ran, and its error
    went into the route's log file as a readable line rather than a traceback.

    The exact message is environment-dependent: when no Caddy is reachable on the admin
    port the worker stops at that precondition before it ever tries the command (CI has no
    Caddy; a developer machine usually does). So assert on the shape of the report, not on
    which precondition fired -- asserting the specific text made this test pass only on
    machines that happened to run Caddy.
    """
    paths, cfg, store, admin = ctx
    (project_dir / "devproxy.toml").write_text(
        '[[routes]]\nname = "web"\ncommand = ["/nonexistent/devproxy-sync-test-cmd"]\n'
    )
    res = CliRunner().invoke(app, ["up"])
    assert res.exit_code == 1, res.output
    assert "failed" in res.output
    log = paths.log_dir / "routes" / "web.log"
    assert log.exists(), "background process output should be written to the log file"
    log_text = log.read_text(encoding="utf-8")
    assert log_text.startswith("devproxy: "), (
        f"the worker must report a readable error, got: {log_text!r}"
    )
    assert "Traceback" not in log_text, "the worker must not leak a traceback into the log"


# ------------------------------- Background worker


def test_worker_passes_cwd_and_source_to_up_request(monkeypatch, tmp_path):
    from devproxy.cli import _worker

    captured = {}

    class FakeRunner:
        def __init__(self, store, config, admin):
            pass

        def run_request(self, req):
            captured["req"] = req
            return 0

    monkeypatch.setattr(_worker, "UpRunner", FakeRunner)
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    paths.caddyfile.write_text("# test Caddyfile\n", encoding="utf-8")
    monkeypatch.setattr(_worker, "build_context", lambda: (paths, None, None, None))
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    source = str(project_dir / "devproxy.toml")
    monkeypatch.setenv(
        _worker.PAYLOAD_ENV,
        json.dumps(
            {
                "name": "web",
                "command": ["npm", "run", "dev"],
                "cwd": str(project_dir),
                "source": source,
            }
        ),
    )
    assert _worker.main() == 0
    assert captured["req"].cwd == project_dir
    assert captured["req"].source == source
    assert captured["req"].cmd == ["npm", "run", "dev"]


def test_worker_passes_extra_env_to_up_request(monkeypatch, tmp_path):
    """Sibling ports travel through the worker payload into UpRequest."""
    from devproxy.cli import _worker

    captured = {}

    class FakeRunner:
        def __init__(self, store, config, admin):
            pass

        def run_request(self, req):
            captured["req"] = req
            return 0

    monkeypatch.setattr(_worker, "UpRunner", FakeRunner)
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    paths.caddyfile.write_text("# test Caddyfile\n", encoding="utf-8")
    monkeypatch.setattr(_worker, "build_context", lambda: (paths, None, None, None))
    monkeypatch.setenv(
        _worker.PAYLOAD_ENV,
        json.dumps(
            {
                "name": "web",
                "command": ["npm", "run", "dev"],
                "extra_env": {"DEVPROXY_API_PORT": "41001"},
            }
        ),
    )
    assert _worker.main() == 0
    assert captured["req"].extra_env == {"DEVPROXY_API_PORT": "41001"}


def test_worker_runs_app_in_project_dir(monkeypatch, tmp_path):
    """The child process's working directory must be **the directory containing the project file**, not the user's current directory."""
    from devproxy.cli import _worker

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    marker = tmp_path / "cwd.txt"
    code = (
        "import os,pathlib,socket\n"
        "pathlib.Path(os.environ['MARKER']).write_text(os.getcwd())\n"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
        "s.bind(('127.0.0.1',int(os.environ['PORT'])));s.listen(8)\n"
        # Wait for the runner's readiness probe instead of sleeping a fixed
        # 0.5s: on a loaded machine the child can be gone before the runner
        # notices the port is up, and the run fails for a reason that has
        # nothing to do with the behaviour under test.
        "s.settimeout(5)\n"
        "try:\n"
        "    c,_=s.accept();c.close()\n"
        "except OSError:\n"
        "    pass\n"
    )
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    paths.caddyfile.write_text("# test Caddyfile\n", encoding="utf-8")
    cfg = Config(storage=str(paths.data_dir))
    store = RouteStore(paths, cfg, FakeCaddy())
    recorded = []
    real_mutate = store.mutate

    def spy(fn):
        real_mutate(fn)
        recorded.append(json.loads(paths.routes_json.read_text()))

    monkeypatch.setattr(store, "mutate", spy)
    monkeypatch.setattr(_worker, "build_context", lambda: (paths, cfg, store, FakeAdmin()))
    monkeypatch.setenv("MARKER", str(marker))
    source = str(project_dir / "devproxy.toml")
    monkeypatch.setenv(
        _worker.PAYLOAD_ENV,
        json.dumps(
            {
                "name": "web",
                "command": [sys.executable, "-c", code],
                "cwd": str(project_dir),
                "source": source,
            }
        ),
    )
    rc = _worker.main()
    assert rc == 0
    assert marker.read_text(encoding="utf-8") == str(project_dir)
    assert recorded[0]["routes"][0]["source"] == source


# -- App dead -> rebuild instead of skip --------------------------------------


def _up_route(name: str, pid: int | None, proc_start: str | None = None) -> Route:
    return Route(
        name=name,
        domain=f"{name}.test",
        port=45671,
        mode="up",
        upstream="127.0.0.1:45671",
        command=["npm", "run", "dev"],
        pid=pid,
        proc_start=proc_start,
    )


def test_plan_restarts_route_whose_app_died(tmp_path):
    """The app process has exited (pid does not exist) -> rebuild, instead of telling the user "up to date"."""
    import os

    project = _project(tmp_path, UP_TOML)
    dead_pid = 999_999_999  # A pid that does not exist
    assert not os.path.exists(f"/proc/{dead_pid}")
    existing = [_up_route("web", pid=dead_pid)]
    plan = build_sync_plan(project, existing)
    assert [r.name for r in plan.create_up] == ["web"], "rebuild when it is dead"
    assert not plan.skip


def test_plan_skips_route_whose_app_is_alive(tmp_path):
    """The process is alive -> skip as usual."""
    import os

    project = _project(tmp_path, UP_TOML)
    existing = [_up_route("web", pid=os.getpid())]  # This test process itself, definitely alive
    plan = build_sync_plan(project, existing)
    assert plan.skip and plan.skip[0][0].name == "web"
    assert not plan.create_up


def test_plan_restarts_when_pid_was_reused(tmp_path):
    """The pid exists but proc_start does not match = the system reused it for another process -> rebuild."""
    import os

    project = _project(tmp_path, UP_TOML)
    existing = [_up_route("web", pid=os.getpid(), proc_start="1")]
    plan = build_sync_plan(project, existing)
    assert [r.name for r in plan.create_up] == ["web"]
    assert not plan.skip


def test_plan_skips_expose_route_without_pid(tmp_path):
    """An expose route has no managed process -- there is nothing to judge, so do not rebuild."""
    project = _project(tmp_path, EXPOSE_TOML)
    existing = [
        Route(
            name="legacy",
            domain="legacy.test",
            port=3000,
            mode="expose",
            upstream="127.0.0.1:3000",
        )
    ]
    plan = build_sync_plan(project, existing)
    assert plan.skip and plan.skip[0][0].name == "legacy"
    assert not plan.create_expose


SIBLING_TOML = """
[[routes]]
name = "api"
command = ["npm", "run", "api"]

[[routes]]
name = "web"
command = ["sh", "-c", "BACKEND_PORT=$DEVPROXY_API_PORT npm run dev"]
"""


def _env_aware_spawn(monkeypatch, store, recorded):
    """A stub that behaves like the real worker: it records the env the app
    was started with, so drift can be detected later."""

    def spawn(project, declared, log_path, *, port=None, extra_env=None):
        used_port = port or declared.port or 45673
        env_map = {
            "DEVPROXY_" + declared.name.upper().replace("-", "_") + "_PORT": str(used_port),
            **(extra_env or {}),
        }
        recorded[declared.name] = {"port": used_port, "extra_env": env_map}
        store.mutate(
            lambda t: t.upsert(
                Route(
                    name=declared.name,
                    domain=f"{declared.name}.test",
                    port=used_port,
                    mode="up",
                    upstream=f"127.0.0.1:{used_port}",
                    command=list(declared.command or []),
                    source=str(project.path),
                    env=env_map,
                )
            )
        )
        return FakeProc()

    monkeypatch.setattr("devproxy.gateway.sync._spawn_worker", spawn)


def test_sync_reports_conflict_when_a_referenced_sibling_port_changed(
    ctx, env, project_dir, monkeypatch
):
    """The frontend keeps the port the API had when it started. When the API
    is restarted on another port, the frontend points at a dead one -- that
    has to surface as a conflict, not as a silent "up to date"."""
    paths, cfg, store, admin = ctx
    (project_dir / "devproxy.toml").write_text(SIBLING_TOML)
    recorded: dict = {}
    _env_aware_spawn(monkeypatch, store, recorded)
    first = CliRunner().invoke(app, ["up"])
    assert first.exit_code == 0, first.output
    api_port = recorded["api"]["port"]

    # The API is restarted and comes up on another port.
    new_api_port = api_port + 1

    def bump(table):
        route = table.get("api")
        route.port = new_api_port
        route.upstream = f"127.0.0.1:{new_api_port}"

    store.mutate(bump)

    second = CliRunner().invoke(app, ["up"])
    assert second.exit_code == 1, second.output
    assert "sibling port changed" in second.output
    assert f"DEVPROXY_API_PORT: {api_port} -> {new_api_port}" in second.output


def test_sync_reports_the_drift_only_for_the_route_that_uses_the_port(
    ctx, env, project_dir, monkeypatch
):
    """A route that never mentions a sibling's port does not care that the
    sibling was restarted -- only the route that reads the port conflicts."""
    paths, cfg, store, admin = ctx
    (project_dir / "devproxy.toml").write_text(SIBLING_TOML)
    recorded: dict = {}
    _env_aware_spawn(monkeypatch, store, recorded)
    assert CliRunner().invoke(app, ["up"]).exit_code == 0

    def bump(table):
        route = table.get("api")
        route.port = route.port + 1
        route.upstream = f"127.0.0.1:{route.port}"

    store.mutate(bump)
    res = CliRunner().invoke(app, ["up"])
    # The API itself is fine -- it never reads the web route's port -- so
    # exactly one conflict is reported, and it is the web route's.
    assert res.exit_code == 1
    assert res.output.count("sibling port changed") == 1
    assert "conflict web" in res.output.replace("\u26a0\ufe0f  ", "")
