"""Logs: rotating writes and `devproxy logs <route name>`."""

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


# -- rotation ------------------------------------------------------------


def test_writer_writes_to_file(tmp_path):
    log = tmp_path / "svc.log"
    writer = RotatingLogWriter(log)
    writer.write(b"hello\n")
    writer.close()
    assert log.read_text(encoding="utf-8") == "hello\n"


def test_writer_rotates_over_the_cap(tmp_path):
    """Rotate past the cap: the current file starts from scratch, the old one becomes .1."""
    log = tmp_path / "svc.log"
    writer = RotatingLogWriter(log, max_bytes=100)
    writer.write(b"x" * 80)
    writer.write(b"y" * 80)  # this write crosses the cap -> rotate
    writer.close()

    assert history_path(log, 1).exists(), "the previous file must be kept as .1"
    assert log.stat().st_size < 100, "the current file must be the new file after rotation"


def test_rotation_keeps_only_one_history(tmp_path):
    log = tmp_path / "svc.log"
    writer = RotatingLogWriter(log, max_bytes=50)
    for _ in range(4):
        writer.write(b"z" * 60)
    writer.close()
    assert history_path(log, 1).exists()
    assert not history_path(log, 2).exists(), "keep only one history file"


def test_writer_survives_disk_error(tmp_path):
    """A failed write (disk full) must not raise -- drop the content, but never take down the child process."""

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
    writer.write(b"still fine\n")  # must not raise
    writer.close()


def test_rotate_at_start_keeps_previous_session(tmp_path):
    """Rotate at startup (only past the cap): the previous session moves to .1, the current file is emptied."""
    log = tmp_path / "svc.log"
    log.write_bytes(b"old session" * 100)
    rotate_at_start(log, max_bytes=100)
    assert history_path(log, 1).exists()
    # The current file does not exist yet at this point (it is created when the app
    # first writes) or is empty -- either way it must no longer contain old content
    assert not log.exists() or log.stat().st_size == 0, (
        "the current file must no longer contain old content"
    )


def test_rotate_at_start_keeps_small_file(tmp_path):
    """Do not rotate small files -- avoid losing a useful tail on frequent restarts."""
    log = tmp_path / "svc.log"
    log.write_bytes(b"tiny")
    rotate_at_start(log, max_bytes=100)
    assert not history_path(log, 1).exists()
    assert log.read_bytes() == b"tiny"


def test_rotate_at_start_missing_file_is_noop(tmp_path):
    rotate_at_start(tmp_path / "nope.log")  # must not raise


# -- devproxy logs <route name> ------------------------------------------


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """A fake state directory containing only the log directory."""
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
    assert "myapp" in result.output, "should list the available route logs"


def test_logs_lists_available_route_logs(log_dir):
    result = _invoke(["logs", "nope"])
    assert "available route logs" in result.output
    assert "myapp" in result.output


def test_logs_follows_route_log(log_dir, monkeypatch):
    """Following a route log: call follow_log and record the path."""
    seen: list[Path] = []

    def fake_follow(path: Path) -> None:
        seen.append(path)

    monkeypatch.setattr("devproxy.cli.logs.follow_log", fake_follow)
    result = _invoke(["logs", "myapp"])
    assert result.exit_code == 0, result.output
    assert seen == [log_dir.log_dir / "routes" / "myapp.log"]


def test_logs_still_supports_services(monkeypatch):
    """The existing caddy / dnsmasq behavior is unchanged."""
    called: list[str] = []

    class _FakePlatform:
        def tail_service_log(self, name: str) -> None:
            called.append(name)

    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: _FakePlatform())
    result = _invoke(["logs", "caddy"])
    assert result.exit_code == 0, result.output
    assert called == ["caddy"]


def test_logs_service_unknown_still_exits_2(monkeypatch):
    """Neither a route name nor a service name -> still a usage error, exit 2."""
    result = _invoke(["logs", "nginx"])
    assert result.exit_code == 2, result.output


def test_max_log_bytes_is_sane():
    """The cap must be a "meaningful size" -- too small truncates normal logs, too large is pointless."""
    assert 1 * 1024 * 1024 <= MAX_LOG_BYTES <= 100 * 1024 * 1024


def test_logs_list_shows_services_and_routes(log_dir):
    """--list: list every target that can be followed, without following."""
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    assert "caddy" in result.output
    assert "dnsmasq" in result.output
    assert "myapp" in result.output
    assert "Route logs:" in result.output


def test_logs_list_does_not_follow(log_dir, monkeypatch):
    """--list must not follow any log."""
    called: list[str] = []

    class _FakePlatform:
        def tail_service_log(self, name: str) -> None:
            called.append(name)

    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: _FakePlatform())
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    assert not called


def test_logs_list_shows_size(log_dir):
    """Each route log comes with its size -- useful when choosing which one to read."""
    result = _invoke(["logs", "--list"])
    assert "B" in result.output or "KB" in result.output


def test_logs_list_without_any_route_log(tmp_path, monkeypatch):
    """With no route logs at all, still reach a conclusion without error."""
    from devproxy.core.paths import Paths

    paths = Paths(tmp_path / "cfg", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (paths, None, None, None),
    )
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    assert "Route logs: (none" in result.output


# -- --list project scope -------------------------------------------------


@pytest.fixture
def project_scope(tmp_path, monkeypatch):
    """Project directory: devproxy.toml declares web; the log directory has logs for web and other."""
    from devproxy.core.paths import Paths

    project = tmp_path / "proj"
    project.mkdir()
    (project / "devproxy.toml").write_text(
        '[[routes]]\nname = "web"\ncommand = ["npm", "run", "dev"]\n',
        encoding="utf-8",
    )
    (project / ".git").mkdir()  # make the project root = this directory
    paths = Paths(tmp_path / "cfg", tmp_path / "state", tmp_path / "data")
    routes = paths.log_dir / "routes"
    routes.mkdir(parents=True, exist_ok=True)
    (routes / "web.log").write_text("w\n", encoding="utf-8")
    (routes / "other.log").write_text("o\n", encoding="utf-8")  # another project's

    monkeypatch.chdir(project)
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (paths, None, None, None),
    )
    return project, paths


def test_logs_list_in_project_shows_only_project_routes(project_scope):
    """Inside a project, --list lists only this project's declared routes; other projects never mix in."""
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    assert "web" in result.output
    assert "other" not in result.output, "another project's routes must not mix in"
    assert "This project" in result.output


def test_logs_list_in_project_mentions_undeclared(project_scope):
    """Routes that are declared but have never run (no log file) must also be mentioned."""
    toml = project_scope[0] / "devproxy.toml"
    toml.write_text(
        '[[routes]]\nname = "web"\ncommand = ["npm", "run", "dev"]\n'
        '[[routes]]\nname = "ghost"\ncommand = ["npm", "run", "dev"]\n',
        encoding="utf-8",
    )
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    assert "ghost" in result.output
    assert "declared but no logs yet" in result.output


def test_logs_one_target_shows_path_and_follow_hint(project_scope):
    result = _invoke(["logs", "web", "--list"])
    assert result.exit_code == 0, result.output
    assert "web.log" in result.output
    assert "devproxy logs web" in result.output


def test_logs_bare_in_project_follows_project_apps(project_scope, monkeypatch):
    """Bare `devproxy logs` inside a project = follow every app of this project (tail -f when there is one)."""
    followed: list[Path] = []
    monkeypatch.setattr("devproxy.cli.logs.follow_log", lambda path: followed.append(path))
    result = _invoke(["logs"])
    assert result.exit_code == 0, result.output
    assert followed == [project_scope[1].log_dir / "routes" / "web.log"]


def test_logs_bare_outside_project_still_defaults_to_caddy(tmp_path, monkeypatch):
    """Bare `devproxy logs` outside a project still follows caddy (the old behavior)."""
    from devproxy.core.paths import Paths

    paths = Paths(tmp_path / "cfg", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    monkeypatch.chdir(tmp_path)  # no devproxy.toml
    called: list[str] = []

    class _FakePlatform:
        def tail_service_log(self, name: str) -> None:
            called.append(name)

    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: _FakePlatform())
    result = _invoke(["logs"])
    assert result.exit_code == 0, result.output
    assert called == ["caddy"]


def test_logs_bare_multi_app_uses_colored_tags(project_scope, monkeypatch):
    """Multiple apps: add tags + legend and go through follow_many."""
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
    assert tags == ["web", "api"], "the tags are the app names in devproxy.toml"
    assert "Following (Ctrl-C to exit)" in result.output
    assert "[web]" in result.output and "[api]" in result.output


def test_logs_list_with_broken_project_file_falls_back(tmp_path, monkeypatch):
    """Broken devproxy.toml: print a note and list every route (rather than refusing service)."""
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
    assert "web" in result.output, "the fallback must list every route, not refuse service"
    # 那条「按全部路由列出」的提示**不能**拿来做断言：它是一整行经 Rich 按宽度
    # 折行的文本，换行位置取决于路径长度 —— 在 CI 的路径下这个词组恰好被折开。
    # 回退行为本身（exit 0 + 列出了 web）才是这个用例要守住的东西。


# -- full paths in --list --------------------------------------------------


def test_logs_list_shows_full_path(log_dir):
    """--list shows the full path of every log file directly -- no need to assemble it yourself."""
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    expected = str(log_dir.log_dir / "routes" / "myapp.log")
    assert expected in result.output, f"should show the full path:\n{result.output}"


def test_logs_list_in_project_shows_full_path(project_scope):
    """Project mode also gives full paths (the heading names devproxy.toml, not the log directory)."""
    result = _invoke(["logs", "--list"])
    assert result.exit_code == 0, result.output
    expected = str(project_scope[1].log_dir / "routes" / "web.log")
    assert expected in result.output, f"should show the full path:\n{result.output}"


# -- --edit ---------------------------------------------------------------


class _RecordingPlatform:
    """Fake platform that only records open_in_editor calls."""

    def __init__(self, code: int = 0, error: Exception | None = None) -> None:
        self.calls: list[tuple[Path, str]] = []
        self.code = code
        self.error = error

    def open_in_editor(self, path: Path, editor: str) -> int:
        self.calls.append((path, editor))
        if self.error is not None:
            raise self.error
        return self.code


def test_logs_edit_opens_route_log_with_editor(log_dir, monkeypatch):
    """--edit opens the route log file with $EDITOR."""
    platform = _RecordingPlatform()
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    monkeypatch.setenv("EDITOR", "vim")
    monkeypatch.delenv("VISUAL", raising=False)

    result = _invoke(["logs", "myapp", "--edit"])
    assert result.exit_code == 0, result.output
    assert platform.calls == [(log_dir.log_dir / "routes" / "myapp.log", "vim")]


def test_logs_edit_falls_back_to_visual(log_dir, monkeypatch):
    """When EDITOR is unset, use VISUAL."""
    platform = _RecordingPlatform()
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("VISUAL", "nano")

    result = _invoke(["logs", "myapp", "--edit"])
    assert result.exit_code == 0, result.output
    assert platform.calls == [(log_dir.log_dir / "routes" / "myapp.log", "nano")]


def test_logs_edit_without_editor_exits_2(log_dir, monkeypatch):
    """Neither EDITOR nor VISUAL is set and no candidate editor is in PATH -> usage error, exit 2."""
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr("devproxy.cli.logs.which", lambda name: None)

    result = _invoke(["logs", "myapp", "--edit"])
    assert result.exit_code == 2, result.output
    assert "EDITOR" in result.output
    assert "vim" in result.output, "should spell out the candidate editors"


def test_logs_edit_falls_back_to_first_available_editor(log_dir, monkeypatch):
    """EDITOR unset: probe vim / vi / nano in order and use the first one in PATH."""
    platform = _RecordingPlatform()
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    # vim not installed, vi not installed, nano available -> use nano
    monkeypatch.setattr(
        "devproxy.cli.logs.which",
        lambda name: "/usr/bin/nano" if name == "nano" else None,
    )

    result = _invoke(["logs", "myapp", "-e"])
    assert result.exit_code == 0, result.output
    assert platform.calls == [(log_dir.log_dir / "routes" / "myapp.log", "nano")]


def test_logs_edit_falls_back_to_vim_when_available(log_dir, monkeypatch):
    """EDITOR unset and vim available -> use vim (first in the candidate order)."""
    platform = _RecordingPlatform()
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr("devproxy.cli.logs.which", lambda name: "/usr/bin/" + name)

    result = _invoke(["logs", "myapp", "--edit"])
    assert result.exit_code == 0, result.output
    assert platform.calls == [(log_dir.log_dir / "routes" / "myapp.log", "vim")]


def test_logs_edit_rejects_service_logs(monkeypatch):
    """A service log is not a file an editor can open -- report clearly, do not guess."""
    monkeypatch.setenv("EDITOR", "vim")
    result = _invoke(["logs", "caddy", "--edit"])
    assert result.exit_code == 2, result.output
    assert "to follow it" in result.output


def test_logs_edit_rejects_unknown_route(log_dir, monkeypatch):
    """No log for this route -> exit 2."""
    monkeypatch.setenv("EDITOR", "vim")
    result = _invoke(["logs", "nope", "--edit"])
    assert result.exit_code == 2, result.output
    assert "myapp" in result.output, "should list the available route logs"


def test_logs_edit_without_name_exits_2(log_dir, monkeypatch):
    """--edit must specify which log to open."""
    monkeypatch.setenv("EDITOR", "vim")
    result = _invoke(["logs", "--edit"])
    assert result.exit_code == 2, result.output
    assert "--edit" in result.output


def test_logs_edit_rejects_list_together(log_dir, monkeypatch):
    """--list and --edit are mutually exclusive."""
    monkeypatch.setenv("EDITOR", "vim")
    result = _invoke(["logs", "--list", "--edit"])
    assert result.exit_code == 2, result.output


def test_logs_edit_reports_editor_failure(log_dir, monkeypatch):
    """Editor exits non-zero -> report it (exit 1, runtime failure)."""
    platform = _RecordingPlatform(code=3)
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    monkeypatch.setenv("EDITOR", "vim")

    result = _invoke(["logs", "myapp", "--edit"])
    assert result.exit_code == 1, result.output
    assert "3" in result.output


def test_logs_edit_reports_missing_editor(log_dir, monkeypatch):
    """Editor does not exist -> clean error, no traceback."""
    platform = _RecordingPlatform(error=FileNotFoundError("no such file: 'vim'"))
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    monkeypatch.setenv("EDITOR", "vim")

    result = _invoke(["logs", "myapp", "--edit"])
    assert result.exit_code == 1, result.output
    assert "vim" in result.output
