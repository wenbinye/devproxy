"""devproxy stop: stop every app declared by this project (devproxy.toml)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from devproxy.cli import app


def _invoke(args: list[str]):
    return CliRunner().invoke(app, ["down", *args])


@pytest.fixture
def project_env(tmp_path, monkeypatch):
    """Project directory (devproxy.toml) + fake Caddy/Admin + empty route table."""
    from devproxy.core.config import Config
    from devproxy.core.paths import Paths
    from devproxy.gateway.store import RouteStore

    project = tmp_path / "proj"
    project.mkdir()
    (project / "devproxy.toml").write_text(
        '[[routes]]\nname = "web"\ncommand = ["npm", "run", "dev"]\n'
        '[[routes]]\nname = "api"\ncommand = ["npm", "run", "serve"]\n',
        encoding="utf-8",
    )
    (project / ".git").mkdir()
    monkeypatch.chdir(project)

    paths = Paths(tmp_path / "cfg", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    store = RouteStore(paths, cfg, _FakeCaddy())

    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (paths, cfg, store, None),
    )
    monkeypatch.setattr(
        "devproxy.cli._shared.find_project_file", lambda start=None: project / "devproxy.toml"
    )
    return project, paths, store


class _FakeCaddy:
    def validate(self, caddyfile=None):
        return True, ""

    def reload(self, caddyfile=None):
        return True, ""


def _seed(store, name: str, source: str | None, pid: int | None = None) -> None:
    import time

    from devproxy.gateway.routes import Route

    route = Route(
        name=name,
        domain=f"{name}.test",
        port=10000 + len(name),
        mode="up",
        upstream=f"127.0.0.1:{10000 + len(name)}",
        command=["npm", "run", "dev"],
        pid=pid,
        proc_start="12345" if pid else None,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        source=source,
    )
    store.mutate(lambda t: t.upsert(route))


def _routes(store) -> list[str]:
    return [r.name for r in store.list()]


def test_stop_stops_all_project_apps(project_env, monkeypatch):
    """Stop every app declared by this project: remove routes + kill processes by registered identity."""
    _, paths, store = project_env
    source = str(paths.state_dir / "devproxy.toml")
    _seed(store, "web", source, pid=111)
    _seed(store, "api", source, pid=222)
    killed: list[tuple[int, str | None]] = []
    monkeypatch.setattr(
        "devproxy.cli._shared.kill_pid_group",
        lambda pid, proc_start=None: killed.append((pid, proc_start)),
    )

    result = _invoke([])
    assert result.exit_code == 0, result.output
    assert _routes(store) == [], "routes inside the declaration must be removed"
    assert killed == [(111, "12345"), (222, "12345")]
    assert "All stopped (2 apps)" in result.output


def test_stop_never_touches_other_projects(project_env, monkeypatch):
    """Manual routes (no source) and other projects' routes must **not** be touched."""
    _, paths, store = project_env
    source = str(paths.state_dir / "devproxy.toml")
    _seed(store, "web", source, pid=111)
    _seed(store, "manual", None, pid=333)  # started manually with up
    _seed(store, "other", "/elsewhere/devproxy.toml")  # another project's
    monkeypatch.setattr("devproxy.cli._shared.kill_pid_group", lambda *a, **k: None)

    result = _invoke([])
    assert result.exit_code == 0, result.output
    assert _routes(store) == ["manual", "other"]
    # The output reports only what this project stopped -- one app, not the two others
    assert "All stopped (1 apps)" in result.output


def test_stop_with_none_running_is_ok(project_env):
    result = _invoke([])
    assert result.exit_code == 0, result.output
    assert "No running apps." in result.output


def test_stop_outside_project_exits_2(tmp_path, monkeypatch):
    """Outside a project there is no declaration to rely on -- refuse and point the way."""
    from devproxy.core.config import Config
    from devproxy.core.paths import Paths
    from devproxy.gateway.store import RouteStore

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    paths = Paths(tmp_path / "cfg", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (paths, cfg, RouteStore(paths, cfg, _FakeCaddy()), None),
    )
    result = _invoke([])
    assert result.exit_code == 2, result.output
    assert "devproxy down" in result.output


def test_stop_route_with_no_pid_is_removed_silently(project_env, monkeypatch):
    """Routes started by expose have no pid -- remove the route only, never kill."""
    _, paths, store = project_env
    source = str(paths.state_dir / "devproxy.toml")
    _seed(store, "web", source, pid=None)
    monkeypatch.setattr("devproxy.cli._shared.kill_pid_group", lambda *a, **k: None)
    result = _invoke([])
    assert result.exit_code == 0, result.output
    assert _routes(store) == []
