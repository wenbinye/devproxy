from devproxy.doctor import build_report, report_is_healthy
from devproxy.platform.base import Status


class C:
    def __init__(self, name, ok):
        self.name = name
        self._ok = ok

    def check(self):
        return Status(ok=self._ok, detail="d", hint="h")


def test_report_is_healthy_when_all_ok():
    assert report_is_healthy(build_report([C("a", True), C("b", True)])) is True


def test_report_is_unhealthy_when_any_fails():
    assert report_is_healthy(build_report([C("a", True), C("b", False)])) is False


# -- CLI：doctor 只读 -------------------------------------------------------


class FakeCliComponent:
    def __init__(self, name, ok, detail="d", hint="h"):
        self.name = name
        self._ok = ok
        self._detail = detail
        self._hint = hint

    def check(self):
        return Status(ok=self._ok, detail=self._detail, hint=self._hint)

    def plan(self):
        return []

    def apply(self, actions):
        raise AssertionError("doctor 不应执行任何动作")


def _patch_cli(monkeypatch, tmp_path, components):
    from devproxy.config import Config
    from devproxy.paths import Paths

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    monkeypatch.setattr(
        "devproxy.cli.build_context", lambda *a, **k: (paths, cfg, None, None)
    )
    monkeypatch.setattr("devproxy.cli._components", lambda *a, **k: components)
    return paths, cfg


def test_cli_doctor_exits_precondition_when_unhealthy(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    _patch_cli(
        monkeypatch,
        tmp_path,
        [FakeCliComponent("a", True), FakeCliComponent("b", False)],
    )
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 3
    assert "b" in result.stdout


def test_cli_doctor_ok_when_healthy(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    _patch_cli(monkeypatch, tmp_path, [FakeCliComponent("a", True)])
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    assert "a" in result.stdout
