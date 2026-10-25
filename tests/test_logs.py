"""日志：轮转写入与 `devproxy logs <路由名>`。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.core.logs import MAX_LOG_BYTES
from devproxy.core.logs import RotatingLogWriter
from devproxy.core.logs import history_path
from devproxy.core.logs import rotate_at_start


def _invoke(args: list[str]):
    return CliRunner().invoke(app, args)


# -- 轮转 ----------------------------------------------------------------


def test_writer_writes_to_file(tmp_path):
    log = tmp_path / "svc.log"
    writer = RotatingLogWriter(log)
    writer.write(b"hello\n")
    writer.close()
    assert log.read_text(encoding="utf-8") == "hello\n"


def test_writer_rotates_over_the_cap(tmp_path):
    """超过上限就轮转：当前文件从头写，旧的变成 .1。"""
    log = tmp_path / "svc.log"
    writer = RotatingLogWriter(log, max_bytes=100)
    writer.write(b"x" * 80)
    writer.write(b"y" * 80)  # 这一写越过上限 → 轮转
    writer.close()

    assert history_path(log, 1).exists(), "上一份应被保留成 .1"
    assert log.stat().st_size < 100, "当前文件应是轮转后的新文件"


def test_rotation_keeps_only_one_history(tmp_path):
    log = tmp_path / "svc.log"
    writer = RotatingLogWriter(log, max_bytes=50)
    for _ in range(4):
        writer.write(b"z" * 60)
    writer.close()
    assert history_path(log, 1).exists()
    assert not history_path(log, 2).exists(), "只保留一份历史"


def test_writer_survives_disk_error(tmp_path):
    """写失败（磁盘满）不能抛 —— 丢弃内容，但不能拖垮子进程。"""

    class _Full:
        def write(self, data):  # noqa: D102
            raise OSError(28, "No space left on device")

        def flush(self):
            pass

        def tell(self):
            return 0

        def close(self):
            pass

    writer = RotatingLogWriter(tmp_path / "svc.log")
    writer._handle = _Full()
    writer.write(b"still fine\n")  # 不抛
    writer.close()


def test_rotate_at_start_keeps_previous_session(tmp_path):
    """启动时轮转（超过上限才转）：上一轮挪成 .1，当前文件清空。"""
    log = tmp_path / "svc.log"
    log.write_bytes(b"old session" * 100)
    rotate_at_start(log, max_bytes=100)
    assert history_path(log, 1).exists()
    # 当前文件此刻还不存在（等应用第一次写才创建）或为空 —— 都不该再有旧内容
    assert not log.exists() or log.stat().st_size == 0, "当前文件不该再有旧内容"


def test_rotate_at_start_keeps_small_file(tmp_path):
    """小文件不轮转 —— 免得频繁重启把有用的尾巴弄丢。"""
    log = tmp_path / "svc.log"
    log.write_bytes(b"tiny")
    rotate_at_start(log, max_bytes=100)
    assert not history_path(log, 1).exists()
    assert log.read_bytes() == b"tiny"


def test_rotate_at_start_missing_file_is_noop(tmp_path):
    rotate_at_start(tmp_path / "nope.log")  # 不抛


# -- devproxy logs <路由名> ----------------------------------------------


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """一个只含日志目录的假状态目录。"""
    from devproxy.core.paths import Paths

    paths = Paths(tmp_path / "cfg", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    routes = paths.log_dir / "routes"
    routes.mkdir(parents=True, exist_ok=True)
    (routes / "myapp.log").write_text("app output\n", encoding="utf-8")
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (paths, None, None, None),
    )
    return paths


def test_logs_rejects_unknown_name(log_dir):
    result = _invoke(["logs", "nope"])
    assert result.exit_code == 2, result.output
    assert "myapp" in result.output, "应列出可用的路由日志"


def test_logs_lists_available_route_logs(log_dir):
    result = _invoke(["logs", "nope"])
    assert "可用的路由日志" in result.output
    assert "myapp" in result.output


def test_logs_follows_route_log(log_dir, monkeypatch):
    """跟路由日志：调 follow_log 并把路径记下来。"""
    seen: list[Path] = []

    def fake_follow(path: Path) -> None:
        seen.append(path)

    monkeypatch.setattr("devproxy.cli.logs.follow_log", fake_follow)
    result = _invoke(["logs", "myapp"])
    assert result.exit_code == 0, result.output
    assert seen == [log_dir.log_dir / "routes" / "myapp.log"]


def test_logs_still_supports_services(monkeypatch):
    """caddy / dnsmasq 的老行为不变。"""
    called: list[str] = []

    class _FakePlatform:
        def tail_service_log(self, name: str) -> None:
            called.append(name)

    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: _FakePlatform())
    result = _invoke(["logs", "caddy"])
    assert result.exit_code == 0, result.output
    assert called == ["caddy"]


def test_logs_service_unknown_still_exits_2(monkeypatch):
    """不是路由名、也不是服务名 → 仍按参数错误退 2。"""
    result = _invoke(["logs", "nginx"])
    assert result.exit_code == 2, result.output


def test_max_log_bytes_is_sane():
    """上限必须是「有意义的大小」—— 太小会截断正常日志，太大没意义。"""
    assert 1 * 1024 * 1024 <= MAX_LOG_BYTES <= 100 * 1024 * 1024


def test_logs_list_shows_services_and_routes(log_dir):
    """--list：列出所有能跟的目标，不跟随。"""
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    assert "caddy" in result.output
    assert "dnsmasq" in result.output
    assert "myapp" in result.output
    assert "路由日志" in result.output


def test_logs_list_does_not_follow(log_dir, monkeypatch):
    """--list 不能去跟随日志。"""
    called: list[str] = []

    class _FakePlatform:
        def tail_service_log(self, name: str) -> None:
            called.append(name)

    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: _FakePlatform())
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    assert not called


def test_logs_list_shows_size(log_dir):
    """每个路由日志带大小 —— 挑哪个看时有用。"""
    result = _invoke(["logs", "--list"])
    assert "B" in result.output or "KB" in result.output


def test_logs_list_without_any_route_log(tmp_path, monkeypatch):
    """一个路由日志都没有时也给得出结论，不报错。"""
    from devproxy.core.paths import Paths

    paths = Paths(tmp_path / "cfg", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (paths, None, None, None),
    )
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    assert "没有" in result.output


# -- --list 的项目作用域 -------------------------------------------------


@pytest.fixture
def project_scope(tmp_path, monkeypatch):
    """项目目录：devproxy.toml 声明 web；日志目录里有 web 和 other 的。"""
    from devproxy.core.paths import Paths

    project = tmp_path / "proj"
    project.mkdir()
    (project / "devproxy.toml").write_text(
        '[[routes]]\nname = "web"\ncommand = ["npm", "run", "dev"]\n',
        encoding="utf-8",
    )
    (project / ".git").mkdir()  # 让项目根 = 这个目录
    paths = Paths(tmp_path / "cfg", tmp_path / "state", tmp_path / "data")
    routes = paths.log_dir / "routes"
    routes.mkdir(parents=True, exist_ok=True)
    (routes / "web.log").write_text("w\n", encoding="utf-8")
    (routes / "other.log").write_text("o\n", encoding="utf-8")  # 别的项目的

    monkeypatch.chdir(project)
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (paths, None, None, None),
    )
    return project, paths


def test_logs_list_in_project_shows_only_project_routes(project_scope):
    """项目里 --list 只列本项目声明的路由，别的项目的不混进来。"""
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    assert "web" in result.output
    assert "other" not in result.output, "别的项目的路由不该混进来"
    assert "本项目" in result.output


def test_logs_list_in_project_mentions_undeclared(project_scope):
    """声明了但还没起过（没有日志文件）的路由也要提示。"""
    toml = project_scope[0] / "devproxy.toml"
    toml.write_text(
        '[[routes]]\nname = "web"\ncommand = ["npm", "run", "dev"]\n'
        '[[routes]]\nname = "ghost"\ncommand = ["npm", "run", "dev"]\n',
        encoding="utf-8",
    )
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    assert "ghost" in result.output
    assert "还没有日志" in result.output


def test_logs_one_target_shows_path_and_follow_hint(project_scope):
    result = _invoke(["logs", "web", "--list"])
    assert result.exit_code == 0, result.output
    assert "web.log" in result.output
    assert "devproxy logs web" in result.output


def test_logs_bare_in_project_follows_project_apps(project_scope, monkeypatch):
    """裸 `devproxy logs` 在项目里 = 跟本项目所有应用（一个时就是 tail -f）。"""
    followed: list[Path] = []
    monkeypatch.setattr(
        "devproxy.cli.logs.follow_log", lambda path: followed.append(path)
    )
    result = _invoke(["logs"])
    assert result.exit_code == 0, result.output
    assert followed == [project_scope[1].log_dir / "routes" / "web.log"]


def test_logs_bare_outside_project_still_defaults_to_caddy(tmp_path, monkeypatch):
    """项目外裸 `devproxy logs` 仍跟 caddy（老行为）。"""
    from devproxy.core.paths import Paths

    paths = Paths(tmp_path / "cfg", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    monkeypatch.chdir(tmp_path)  # 没有 devproxy.toml
    called: list[str] = []

    class _FakePlatform:
        def tail_service_log(self, name: str) -> None:
            called.append(name)

    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: _FakePlatform())
    result = _invoke(["logs"])
    assert result.exit_code == 0, result.output
    assert called == ["caddy"]


def test_logs_bare_multi_app_uses_colored_tags(project_scope, monkeypatch):
    """多个应用：加标签 + 图例，走 follow_many。"""
    toml = project_scope[0] / "devproxy.toml"
    toml.write_text(
        '[[routes]]\nname = "web"\ncommand = ["npm", "run", "dev"]\n'
        '[[routes]]\nname = "api"\ncommand = ["npm", "run", "serve"]\n',
        encoding="utf-8",
    )
    routes = project_scope[1].log_dir / "routes"
    (routes / "api.log").write_text("a\n", encoding="utf-8")

    seen: list[list[tuple[str, Path]]] = []
    monkeypatch.setattr(
        "devproxy.cli.logs.follow_many",
        lambda targets: seen.append(list(targets)) or iter([]),
    )
    result = _invoke(["logs"])
    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    tags = [t for t, _ in seen[0]]
    assert tags == ["web", "api"], "标签就是 devproxy.toml 里的应用名"
    assert "跟随中" in result.output
    assert "[web]" in result.output and "[api]" in result.output


def test_logs_list_with_broken_project_file_falls_back(tmp_path, monkeypatch):
    """devproxy.toml 坏了：提示一声，按全部路由列出（而不是拒绝服务）。"""
    from devproxy.core.paths import Paths

    project = tmp_path / "proj"
    project.mkdir()
    (project / "devproxy.toml").write_text("[[routes]\nbroken", encoding="utf-8")
    (project / ".git").mkdir()
    paths = Paths(tmp_path / "cfg", tmp_path / "state", tmp_path / "data")
    routes = paths.log_dir / "routes"
    routes.mkdir(parents=True, exist_ok=True)
    (routes / "web.log").write_text("w\n", encoding="utf-8")

    monkeypatch.chdir(project)
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (paths, None, None, None),
    )
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    assert "按全部路由列出" in result.output
    assert "web" in result.output
