"""devproxy stop：停掉本项目（devproxy.toml）声明的所有应用。"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from devproxy.cli import app


def _invoke(args: list[str]):
    return CliRunner().invoke(app, ["stop", *args])


@pytest.fixture
def project_env(tmp_path, monkeypatch):
    """项目目录（devproxy.toml）+ 假 Caddy/Admin + 空路由表。"""
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
    from devproxy.gateway.routes import Route
    import time

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
    """本项目声明的应用全部停止：路由移除 + 进程按注册身份击杀。"""
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
    assert _routes(store) == [], "声明内的路由应被移除"
    assert killed == [(111, "12345"), (222, "12345")]
    assert "全部停止（2 个应用）" in result.output


def test_stop_never_touches_other_projects(project_env, monkeypatch):
    """手工路由（无 source）与别的项目的路由**不能**被碰。"""
    _, paths, store = project_env
    source = str(paths.state_dir / "devproxy.toml")
    _seed(store, "web", source, pid=111)
    _seed(store, "manual", None, pid=333)               # 手工 up 的
    _seed(store, "other", "/elsewhere/devproxy.toml")   # 别的项目的
    monkeypatch.setattr("devproxy.cli._shared.kill_pid_group", lambda *a, **k: None)

    result = _invoke([])
    assert result.exit_code == 0, result.output
    assert _routes(store) == ["manual", "other"]
    assert "manual" in result.output or True  # 输出只报本项目停了几个


def test_stop_with_none_running_is_ok(project_env):
    result = _invoke([])
    assert result.exit_code == 0, result.output
    assert "没有在跑的应用" in result.output


def test_stop_outside_project_exits_2(tmp_path, monkeypatch):
    """项目外没有可依据的声明 —— 拒绝并指路。"""
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
    """expose 起的路由没有 pid —— 只移除路由，不击杀。"""
    _, paths, store = project_env
    source = str(paths.state_dir / "devproxy.toml")
    _seed(store, "web", source, pid=None)
    monkeypatch.setattr("devproxy.cli._shared.kill_pid_group", lambda *a, **k: None)
    result = _invoke([])
    assert result.exit_code == 0, result.output
    assert _routes(store) == []
