from devproxy.lifecycle.doctor import build_report
from devproxy.lifecycle.doctor import report_is_healthy
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
    """A component's check() raises: record a failure row and list the other components as usual."""

    class Boom:
        name = "boom"

        def check(self):
            raise PermissionError("binary is not executable")

    reports = build_report([Boom(), C("good", True)])
    assert [r.name for r in reports] == ["boom", "good"]
    assert reports[0].status.ok is False
    assert "binary is not executable" in reports[0].status.detail
    assert reports[1].status.ok is True


# -- CLI: doctor is read-only -------------------------------------------------


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
        raise AssertionError("doctor must not execute any action")


def _patch_cli(monkeypatch, tmp_path, components):
    from devproxy.core.config import Config
    from devproxy.core.paths import Paths

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context", lambda *a, **k: (paths, cfg, None, None)
    )
    monkeypatch.setattr("devproxy.cli._shared._components", lambda *a, **k: components)
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
    """A component's check() raises: it may become one failure row, never a traceback."""
    from typer.testing import CliRunner

    from devproxy.cli import app

    class BoomCheck:
        name = "boom"

        def check(self):
            raise PermissionError("binary is not executable")

    _patch_cli(monkeypatch, tmp_path, [BoomCheck(), FakeCliComponent("good", True)])
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 3, result.output
    assert "boom" in result.stdout
    assert "check failed" in result.stdout
    assert "Traceback" not in result.output


def _patch_cli_with_config(monkeypatch, tmp_path, cfg):
    from devproxy.core.paths import Paths

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context", lambda *a, **k: (paths, cfg, None, None)
    )
    return paths


def test_cli_doctor_uses_config_dns_backend(tmp_path, monkeypatch):
    """doctor must use the same DNS backend as setup; otherwise doctor keeps reporting missing after setup succeeds."""
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.core.config import Config

    cfg = Config(storage=str(tmp_path / "data"), dns_backend="system")
    _patch_cli_with_config(monkeypatch, tmp_path, cfg)
    seen = {}

    def fake_components(paths, config, dns_backend=None):
        seen["backend"] = dns_backend
        return [FakeCliComponent("dns", True)]

    monkeypatch.setattr("devproxy.cli._shared._components", fake_components)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    assert seen["backend"] == "system"


def test_cli_doctor_accepts_dns_backend_override(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.core.config import Config

    cfg = Config(storage=str(tmp_path / "data"), dns_backend="system")
    _patch_cli_with_config(monkeypatch, tmp_path, cfg)
    seen = {}

    def fake_components(paths, config, dns_backend=None):
        seen["backend"] = dns_backend
        return [FakeCliComponent("dns", True)]

    monkeypatch.setattr("devproxy.cli._shared._components", fake_components)
    result = CliRunner().invoke(app, ["doctor", "--dns-backend", "resolved"])
    assert result.exit_code == 0, result.stdout
    assert seen["backend"] == "resolved"


def test_cli_doctor_macos_rejects_resolved_backend(tmp_path, monkeypatch):
    """macOS with an explicit resolved backend: doctor must return an argument error, not a component traceback."""
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.core.config import Config

    cfg = Config(storage=str(tmp_path / "data"))
    _patch_cli_with_config(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr("devproxy.cli._shared._components", lambda *a, **k: [])
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: SimpleNamespace(name="mac"))
    result = CliRunner().invoke(app, ["doctor", "--dns-backend", "resolved"])
    assert result.exit_code == 2, result.output
    assert "macOS does not support the resolved backend" in result.output
    assert "Traceback" not in result.output


def test_cli_doctor_rejects_invalid_dns_backend(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.core.config import Config

    cfg = Config(storage=str(tmp_path / "data"))
    _patch_cli_with_config(monkeypatch, tmp_path, cfg)
    result = CliRunner().invoke(app, ["doctor", "--dns-backend", "foo"])
    assert result.exit_code == 2


# -- wildcard site blocks in the live config ---------------------------------


def test_find_wildcard_sites_detects_test_wildcard(tmp_path):
    """The tool's design premise is "exact site blocks, no wildcards"."""
    from devproxy.core.config import Config
    from devproxy.core.paths import Paths
    from devproxy.lifecycle.doctor import find_wildcard_sites

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    paths.sites_caddy.write_text("*.test {\n\ttls {\n\t\ton_demand\n\t}\n}\n", encoding="utf-8")
    found = find_wildcard_sites(paths, Config())
    assert found, "must detect the *.test wildcard block"
    assert any("*.test" in a for _, addrs in found for a in addrs)


def test_find_wildcard_sites_ignores_other_suffix(tmp_path):
    """Wildcard blocks under other suffixes are unrelated to this tool (*.local.yikuaigao.com does not count)."""
    from devproxy.core.config import Config
    from devproxy.core.paths import Paths
    from devproxy.lifecycle.doctor import find_wildcard_sites

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    paths.sites_caddy.write_text(
        "*.local.yikuaigao.com {\n\treverse_proxy 1.2.3.4:80\n}\n", encoding="utf-8"
    )
    assert find_wildcard_sites(paths, Config()) == []


def test_cli_doctor_reports_wildcard_and_exits_3(tmp_path, monkeypatch):
    """A wildcard block still present in the live config -> doctor must report it and offer a fix.

    When a wildcard block shares the suffix with the exact blocks generated by
    the tool, Caddy's TLS handshake fails entirely; migration is the action
    that removes it, so the fix hint is "run setup to finish the migration".
    """
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.core.config import Config

    cfg = Config(storage=str(tmp_path / "data"))
    paths = _patch_cli_with_config(monkeypatch, tmp_path, cfg)
    paths.sites_caddy.write_text("*.test {\n\ttls {\n\t\ton_demand\n\t}\n}\n", encoding="utf-8")
    monkeypatch.setattr(
        "devproxy.cli._shared._components", lambda *a, **k: [FakeCliComponent("a", True)]
    )
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 3, result.stdout
    assert "wildcard" in result.stdout
    assert "devproxy setup" in result.stdout


def test_cli_doctor_ok_when_no_wildcard(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.core.config import Config

    cfg = Config(storage=str(tmp_path / "data"))
    paths = _patch_cli_with_config(monkeypatch, tmp_path, cfg)
    paths.sites_caddy.write_text("localhost {\n\ttls internal\n}\n", encoding="utf-8")
    monkeypatch.setattr(
        "devproxy.cli._shared._components", lambda *a, **k: [FakeCliComponent("a", True)]
    )
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout


# -- Firefox certificate-store note (a note, not a defect) --------------------


class FakeFirefoxComponent(FakeCliComponent):
    """Fake component with a Firefox note."""

    def firefox_note(self):
        return "Firefox has its own certificate store (cert9.db); import the CA separately"


def test_cli_doctor_prints_firefox_note(tmp_path, monkeypatch):
    """Firefox present on this machine: doctor must state the manual-trust workaround."""
    from typer.testing import CliRunner

    from devproxy.cli import app

    _patch_cli(monkeypatch, tmp_path, [FakeFirefoxComponent("a", True)])
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    assert "Firefox" in result.stdout
    assert "cert9.db" in result.stdout


def test_cli_doctor_without_firefox_note_is_silent(tmp_path, monkeypatch):
    """A component without this capability is treated as absent -- that must not cause an error."""
    from typer.testing import CliRunner

    from devproxy.cli import app

    _patch_cli(monkeypatch, tmp_path, [FakeCliComponent("a", True)])
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    assert "Firefox" not in result.stdout


def test_cli_doctor_survives_broken_firefox_note(tmp_path, monkeypatch):
    """When a component's note method raises, doctor must still finish."""
    from typer.testing import CliRunner

    from devproxy.cli import app

    class BoomNote(FakeCliComponent):
        def firefox_note(self):
            raise RuntimeError("probe failed")

    _patch_cli(monkeypatch, tmp_path, [BoomNote("a", True)])
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    assert "Traceback" not in result.stdout


def test_cli_doctor_with_real_cert_component_hints_firefox(tmp_path, monkeypatch, isolated_home):
    """Real component + real Firefox profile: both the hint and the table must show the Firefox guidance."""
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.components.certs import CertComponent
    from devproxy.core.config import Config
    from devproxy.platform.fake import FakePlatform

    cfg = Config(storage=str(tmp_path / "data"))
    paths = _patch_cli_with_config(monkeypatch, tmp_path, cfg)
    ca = cfg.storage_path / "pki" / "authorities" / "local" / "root.crt"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text("FAKE CA\n", encoding="utf-8")
    platform = FakePlatform()
    platform.trusted.add(ca)
    platform.nss.add(ca)
    monkeypatch.setattr(
        "devproxy.cli._shared._components",
        lambda *a, **k: [CertComponent(platform, paths, cfg)],
    )
    profile = isolated_home / ".mozilla" / "firefox" / "abc.default-release"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "cert9.db").write_bytes(b"")
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    assert "Firefox" in result.stdout
    assert "security.enterprise_roots.enabled" in result.stdout
