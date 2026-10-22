import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import DevProxyGroup, app
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.proxy.caddy import CaddyAdmin
from devproxy.proxy.routes import Route
from devproxy.proxy.store import RouteStore


class FakeCaddy:
    """假 Caddy：validate/reload 恒成功，避免测试真的去调 caddy 二进制。"""

    def validate(self, caddyfile=None):
        return True, "ok"

    def reload(self, caddyfile=None):
        return True, "ok"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把 devproxy 的三棵目录指向 tmp_path。"""
    monkeypatch.setenv("DEVPROXY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DEVPROXY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DEVPROXY_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def _patch_ctx(monkeypatch, tmp_path):
    """替换 build_context，注入假 Caddy。"""
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    store = RouteStore(paths, cfg, FakeCaddy())
    admin = CaddyAdmin(base="http://127.0.0.1:1")
    monkeypatch.setattr(
        "devproxy.cli.build_context", lambda *a, **k: (paths, cfg, store, admin)
    )
    return paths, cfg, store


def _seed_route(state_dir: Path, name="codeup", port=42133, mode="up"):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "routes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "domain_suffix": "test",
                "routes": [
                    {
                        "name": name,
                        "domain": f"{name}.test",
                        "port": port,
                        "mode": mode,
                        "upstream": f"127.0.0.1:{port}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_separator_is_split_and_passed_through(env, monkeypatch):
    """-- 之后的参数原样交给子命令，不被 devproxy 的选项解析器吞掉。"""
    _patch_ctx(monkeypatch, env)
    captured = {}

    def fake_run(self, name, cmd, port=None, ready_timeout=None, on_ready=None):
        captured["name"] = name
        captured["cmd"] = cmd
        captured["port"] = port
        return 0

    monkeypatch.setattr("devproxy.cli.UpRunner.run", fake_run)
    res = CliRunner().invoke(
        app, ["up", "codeup", "--port", "8080", "--", "npx", "vite", "--port", "3000"]
    )
    assert res.exit_code == 0, res.output
    assert captured["name"] == "codeup"
    assert captured["port"] == 8080
    assert captured["cmd"] == ["npx", "vite", "--port", "3000"]


def test_up_without_separator_is_rejected(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "codeup", "npx", "vite"])
    assert res.exit_code != 0
    assert "--" in res.output


def test_up_without_command_is_rejected(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "codeup", "--"])
    assert res.exit_code != 0


def test_expose_registers_route(env, monkeypatch):
    """expose 是一次性的：注册完立即返回，路由常驻。"""
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 0, res.output
    table = json.loads((env / "state" / "routes.json").read_text())
    assert table["routes"][0]["name"] == "legacy"
    assert table["routes"][0]["mode"] == "expose"
    assert table["routes"][0]["domain"] == "legacy.test"


def test_down_removes_route(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    _seed_route(env / "state")
    res = CliRunner().invoke(app, ["down", "codeup"])
    assert res.exit_code == 0, res.output
    table = json.loads((env / "state" / "routes.json").read_text())
    assert table["routes"] == []


def test_down_unknown_name_is_ok(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["down", "nope"])
    assert res.exit_code == 0


def test_down_kills_leftover_process_group(env, monkeypatch):
    """down 要顺带杀掉 mode=up 路由的残留子进程组。"""
    _patch_ctx(monkeypatch, env)
    killed = []
    monkeypatch.setattr("devproxy.cli.kill_pid_group", lambda pid: killed.append(pid))
    _seed_route(env / "state", "codeup", 42133, mode="up")
    p = env / "state" / "routes.json"
    data = json.loads(p.read_text())
    data["routes"][0]["pid"] = 4242
    p.write_text(json.dumps(data))
    res = CliRunner().invoke(app, ["down", "codeup"])
    assert res.exit_code == 0, res.output
    assert killed == [4242]


def test_ls_lists_routes(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    _seed_route(env / "state", "codeup", 42133)
    res = CliRunner().invoke(app, ["ls"])
    assert res.exit_code == 0, res.output
    assert "codeup.test" in res.output


def test_config_shows_effective_config(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 0, res.output
    assert "domain_suffix" in res.output
