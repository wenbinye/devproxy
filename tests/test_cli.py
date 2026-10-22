import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import DevProxyGroup, app
from devproxy.config import Config, ConfigError
from devproxy.paths import Paths
from devproxy.proxy.routes import Route, RouteTableError
from devproxy.proxy.store import RouteStore, RouteStoreError


class FakeCaddy:
    """假 Caddy：validate/reload 恒成功，避免测试真的去调 caddy 二进制。"""

    def validate(self, caddyfile=None):
        return True, "ok"

    def reload(self, caddyfile=None):
        return True, "ok"


class FakeAdmin:
    """假 Admin API 客户端：alive 可编排，供前置条件测试使用。"""

    def __init__(self, alive=True):
        self.alive = alive
        self.config_payload = {}

    def is_alive(self):
        return self.alive

    def api(self, method, path, body=None):
        return 200, self.config_payload


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


def _patch_ctx(monkeypatch, tmp_path, admin_alive=True):
    """替换 build_context，注入假 Caddy 与假 Admin API。"""
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    store = RouteStore(paths, cfg, FakeCaddy())
    admin = FakeAdmin(alive=admin_alive)
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
    """down 要顺带杀掉 mode=up 路由的残留子进程组（带注册时的进程身份）。"""
    _patch_ctx(monkeypatch, env)
    killed = []

    def fake_kill(pid, proc_start=None):
        killed.append((pid, proc_start))

    monkeypatch.setattr("devproxy.cli.kill_pid_group", fake_kill)
    _seed_route(env / "state", "codeup", 42133, mode="up")
    p = env / "state" / "routes.json"
    data = json.loads(p.read_text())
    data["routes"][0]["pid"] = 4242
    data["routes"][0]["proc_start"] = "12345"
    p.write_text(json.dumps(data))
    res = CliRunner().invoke(app, ["down", "codeup"])
    assert res.exit_code == 0, res.output
    assert killed == [(4242, "12345")]


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


# ---------------------------------------------------------------- I-1: 退出码 3


def test_up_when_caddy_not_running_exits_3(env, monkeypatch):
    """Caddy 未运行是前置条件不满足 → 退出码 3（不是 1）。"""
    _patch_ctx(monkeypatch, env, admin_alive=False)
    res = CliRunner().invoke(app, ["up", "codeup", "--", "sleep", "5"])
    assert res.exit_code == 3, res.output
    assert "Caddy" in res.output


def test_expose_when_caddy_not_running_exits_3(env, monkeypatch):
    """expose 要注册活路由，同样要求 Caddy 在运行 → 退出码 3。"""
    _patch_ctx(monkeypatch, env, admin_alive=False)
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 3, res.output
    assert "Caddy" in res.output
    assert not (env / "state" / "routes.json").exists(), "前置条件不满足时不得写路由表"


# ---------------------------------------------------- I-2: 路由表损坏要干净报错


def _write_routes_file(state_dir: Path, text: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "routes.json").write_text(text, encoding="utf-8")


def test_ls_with_invalid_json_routes_file_is_clean_error(env, monkeypatch):
    """routes.json 不是合法 JSON → 一行错误 + 退出码 1，而不是 Rich traceback。"""
    _patch_ctx(monkeypatch, env)
    _write_routes_file(env / "state", "{ 不是 json")
    res = CliRunner().invoke(app, ["ls"])
    assert res.exit_code == 1, res.output
    assert "devproxy" in res.output and "路由表" in res.output
    assert "Traceback" not in res.output


def test_ls_with_routes_missing_required_key_is_clean_error(env, monkeypatch):
    """routes.json 缺必需键 → 同样是干净的一行错误。"""
    _patch_ctx(monkeypatch, env)
    _write_routes_file(env / "state", '{"version": 1, "routes": [{"name": "x"}]}')
    res = CliRunner().invoke(app, ["ls"])
    assert res.exit_code == 1, res.output
    assert "路由表" in res.output
    assert "Traceback" not in res.output


def test_down_with_corrupt_routes_file_is_clean_error(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    _write_routes_file(env / "state", "[]")  # 顶层是列表而非对象
    res = CliRunner().invoke(app, ["down", "codeup"])
    assert res.exit_code == 1, res.output
    assert "路由表" in res.output
    assert "Traceback" not in res.output


def test_config_with_corrupt_routes_file_is_clean_error(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    _write_routes_file(env / "state", "{ oops")
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 1, res.output
    assert "路由表" in res.output
    assert "Traceback" not in res.output


@pytest.mark.parametrize(
    "exc", [RouteStoreError("boom"), RouteTableError("损坏"), ConfigError("坏")]
)
def test_main_backstop_turns_domain_errors_into_exit_1(monkeypatch, exc):
    """main() 兜底：领域异常绝不能以 traceback 的形式漏到终端。"""
    from devproxy import cli

    def fake_app():
        raise exc

    monkeypatch.setattr(cli, "app", fake_app)
    with pytest.raises(SystemExit) as cm:
        cli.main()
    assert cm.value.code == 1


# ------------------------------------- Rich 标记不得吞掉错误消息里的字符集


def test_invalid_name_error_keeps_charset_in_output(env, monkeypatch):
    """[a-z0-9-] 会被 Rich 当标记吞掉 —— 错误输出必须保留字面字符集。"""
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["expose", "Bad_Name", "--port", "3000"])
    assert res.exit_code == 1
    assert "[a-z0-9-]" in res.output


def test_up_help_text_keeps_charset(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "--help"])
    assert res.exit_code == 0
    assert "[a-z0-9-]" in res.output
