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


def test_build_report_isolates_check_exception():
    """某个组件 check() 抛异常：记为失败行，其余组件照常列出。"""

    class Boom:
        name = "boom"

        def check(self):
            raise PermissionError("二进制不可执行")

    reports = build_report([Boom(), C("good", True)])
    assert [r.name for r in reports] == ["boom", "good"]
    assert reports[0].status.ok is False
    assert "二进制不可执行" in reports[0].status.detail
    assert reports[1].status.ok is True


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


def test_cli_doctor_handles_check_exception_without_traceback(tmp_path, monkeypatch):
    """组件 check() 抛异常只能变成一行失败报告，不能是 traceback。"""
    from typer.testing import CliRunner

    from devproxy.cli import app

    class BoomCheck:
        name = "boom"

        def check(self):
            raise PermissionError("二进制不可执行")

    _patch_cli(monkeypatch, tmp_path, [BoomCheck(), FakeCliComponent("good", True)])
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 3, result.output
    assert "boom" in result.stdout
    assert "检查失败" in result.stdout
    assert "Traceback" not in result.output


def _patch_cli_with_config(monkeypatch, tmp_path, cfg):
    from devproxy.paths import Paths

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    monkeypatch.setattr(
        "devproxy.cli.build_context", lambda *a, **k: (paths, cfg, None, None)
    )
    return paths


def test_cli_doctor_uses_config_dns_backend(tmp_path, monkeypatch):
    """doctor 必须和 setup 用同一个 DNS 后端，否则 setup 成功后 doctor 仍报缺失。"""
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.config import Config

    cfg = Config(storage=str(tmp_path / "data"), dns_backend="system")
    _patch_cli_with_config(monkeypatch, tmp_path, cfg)
    seen = {}

    def fake_components(paths, config, dns_backend=None):
        seen["backend"] = dns_backend
        return [FakeCliComponent("dns", True)]

    monkeypatch.setattr("devproxy.cli._components", fake_components)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    assert seen["backend"] == "system"


def test_cli_doctor_accepts_dns_backend_override(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.config import Config

    cfg = Config(storage=str(tmp_path / "data"), dns_backend="system")
    _patch_cli_with_config(monkeypatch, tmp_path, cfg)
    seen = {}

    def fake_components(paths, config, dns_backend=None):
        seen["backend"] = dns_backend
        return [FakeCliComponent("dns", True)]

    monkeypatch.setattr("devproxy.cli._components", fake_components)
    result = CliRunner().invoke(app, ["doctor", "--dns-backend", "resolved"])
    assert result.exit_code == 0, result.stdout
    assert seen["backend"] == "resolved"


def test_cli_doctor_rejects_invalid_dns_backend(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.config import Config

    cfg = Config(storage=str(tmp_path / "data"))
    _patch_cli_with_config(monkeypatch, tmp_path, cfg)
    result = CliRunner().invoke(app, ["doctor", "--dns-backend", "foo"])
    assert result.exit_code == 2


# -- 现役配置里的通配站点块 -------------------------------------------------


def test_find_wildcard_sites_detects_test_wildcard(tmp_path):
    """工具的设计前提是"精确站点块、没有通配"。"""
    from devproxy.config import Config
    from devproxy.doctor import find_wildcard_sites
    from devproxy.paths import Paths

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    paths.sites_caddy.write_text(
        "*.test {\n\ttls {\n\t\ton_demand\n\t}\n}\n", encoding="utf-8"
    )
    found = find_wildcard_sites(paths, Config())
    assert found, "必须发现 *.test 通配块"
    assert any("*.test" in a for _, addrs in found for a in addrs)


def test_find_wildcard_sites_ignores_other_suffix(tmp_path):
    """别的后缀的通配块与本工具无关（*.local.yikuaigao.com 不算）。"""
    from devproxy.config import Config
    from devproxy.doctor import find_wildcard_sites
    from devproxy.paths import Paths

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    paths.sites_caddy.write_text(
        "*.local.yikuaigao.com {\n\treverse_proxy 1.2.3.4:80\n}\n", encoding="utf-8"
    )
    assert find_wildcard_sites(paths, Config()) == []


def test_cli_doctor_reports_wildcard_and_exits_3(tmp_path, monkeypatch):
    """现役配置里还有通配块 → doctor 必须报出来并给修复动作。

    通配块与工具生成的精确块同后缀时，Caddy 的 TLS 握手会整体失败；
    迁移正是移除它的动作，所以修复提示就是"跑 setup 完成迁移"。
    """
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.config import Config

    cfg = Config(storage=str(tmp_path / "data"))
    paths = _patch_cli_with_config(monkeypatch, tmp_path, cfg)
    paths.sites_caddy.write_text(
        "*.test {\n\ttls {\n\t\ton_demand\n\t}\n}\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "devproxy.cli._components", lambda *a, **k: [FakeCliComponent("a", True)]
    )
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 3, result.stdout
    assert "通配" in result.stdout
    assert "devproxy setup" in result.stdout


def test_cli_doctor_ok_when_no_wildcard(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.config import Config

    cfg = Config(storage=str(tmp_path / "data"))
    paths = _patch_cli_with_config(monkeypatch, tmp_path, cfg)
    paths.sites_caddy.write_text("localhost {\n\ttls internal\n}\n", encoding="utf-8")
    monkeypatch.setattr(
        "devproxy.cli._components", lambda *a, **k: [FakeCliComponent("a", True)]
    )
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
