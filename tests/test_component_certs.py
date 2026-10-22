from pathlib import Path

from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.components.certs import CertComponent
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.fake import FakePlatform


def _component(tmp_path, platform=None):
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    platform = platform or FakePlatform()
    return CertComponent(platform, paths, cfg), platform, paths


def test_check_ok_when_trusted(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = paths.data_dir / "pki" / "authorities" / "local" / "root.crt"
    platform.trusted.add(ca)
    assert comp.check().ok is True


def test_check_missing_ca_reports_hint(tmp_path):
    comp, platform, paths = _component(tmp_path)
    status = comp.check()
    assert status.ok is False
    assert "root.crt" in status.detail
    assert status.hint


def test_plan_is_empty_when_trusted(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = paths.data_dir / "pki" / "authorities" / "local" / "root.crt"
    platform.trusted.add(ca)
    assert comp.plan() == []


def test_plan_includes_privileged_steps(tmp_path):
    comp, platform, paths = _component(tmp_path)
    actions = comp.plan()
    assert actions
    assert all(a.privileged for a in actions), "装信任库需要提权"


# -- apply：目标路径可注入，测试期间绝不碰真实系统 ---------------------------


def _write_ca(paths: Paths) -> Path:
    ca = paths.data_dir / "pki" / "authorities" / "local" / "root.crt"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text("FAKE CA\n", encoding="utf-8")
    return ca


def _injected(tmp_path, platform):
    """把系统信任库与 NSS 库指向 tmp_path，测试不碰真实系统。"""
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    comp = CertComponent(
        platform,
        paths,
        cfg,
        system_dest=tmp_path / "ca" / "devproxy-local.crt",
        nss_dir=tmp_path / "nssdb",
    )
    return comp, paths, cfg


def test_apply_writes_only_to_injected_targets(tmp_path):
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())
    assert (tmp_path / "ca" / "devproxy-local.crt").read_text() == "FAKE CA\n"
    assert ["update-ca-certificates"] in platform.commands
    assert any(c[:1] == ["certutil"] and "-A" in c for c in platform.commands)
    for written in platform.written:
        assert tmp_path in written.parents, f"写到了 tmp_path 之外: {written}"


def test_apply_raises_when_certutil_missing(tmp_path):
    class NoCertutil(FakePlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            if argv[:1] == ["certutil"]:
                return 127, "命令不存在: certutil"
            return 0, ""

    platform = NoCertutil()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    try:
        comp.apply(comp.plan())
    except RuntimeError as e:
        assert "certutil" in str(e)
    else:
        raise AssertionError("缺少 certutil 时应当报错")


def test_verify_ok_after_trust(tmp_path):
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    comp.apply(comp.plan())
    platform.trusted.add(ca)
    assert comp.verify().ok is True


# -- CLI：trust / logs ------------------------------------------------------


def _patch_cli(monkeypatch, tmp_path, platform):
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    monkeypatch.setattr(
        "devproxy.cli.build_context", lambda *a, **k: (paths, cfg, None, None)
    )
    monkeypatch.setattr("devproxy.cli._platform", lambda: platform)
    return paths, cfg


def test_cli_trust_is_idempotent(tmp_path, monkeypatch):
    platform = FakePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    ca = paths.data_dir / "pki" / "authorities" / "local" / "root.crt"
    platform.trusted.add(ca)
    result = CliRunner().invoke(app, ["trust"])
    assert result.exit_code == 0
    assert "CA 根证书已信任，无需变更。" in result.stdout
    assert platform.commands == []


def test_cli_trust_installs_into_injected_targets(tmp_path, monkeypatch):
    platform = FakePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    _write_ca(paths)
    monkeypatch.setattr(
        "devproxy.cli.CertComponent",
        lambda p, paths_, cfg_: CertComponent(
            p,
            paths_,
            cfg_,
            system_dest=tmp_path / "ca" / "devproxy-local.crt",
            nss_dir=tmp_path / "nssdb",
        ),
    )
    result = CliRunner().invoke(app, ["trust"])
    assert result.exit_code == 0, result.stdout
    assert "已安装 CA 根证书" in result.stdout
    assert (tmp_path / "ca" / "devproxy-local.crt").exists()
    for written in platform.written:
        assert tmp_path in written.parents


def test_cli_trust_failure_exits_1_without_traceback(tmp_path, monkeypatch):
    class NoCertutil(FakePlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            if argv[:1] == ["certutil"]:
                return 127, "命令不存在: certutil"
            return 0, ""

    platform = NoCertutil()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    _write_ca(paths)
    monkeypatch.setattr(
        "devproxy.cli.CertComponent",
        lambda p, paths_, cfg_: CertComponent(
            p,
            paths_,
            cfg_,
            system_dest=tmp_path / "ca" / "devproxy-local.crt",
            nss_dir=tmp_path / "nssdb",
        ),
    )
    result = CliRunner().invoke(app, ["trust"])
    assert result.exit_code == 1
    assert "certutil" in result.stderr
    assert "Traceback" not in result.stdout


def test_cli_logs_rejects_unknown_service(tmp_path, monkeypatch):
    platform = FakePlatform()
    _patch_cli(monkeypatch, tmp_path, platform)
    result = CliRunner().invoke(app, ["logs", "nginx"])
    assert result.exit_code == 1


def test_cli_logs_tails_service_log(tmp_path, monkeypatch):
    class RecordingPlatform(FakePlatform):
        def __init__(self):
            super().__init__()
            self.tailed = []

        def tail_service_log(self, name):
            self.tailed.append(name)

    platform = RecordingPlatform()
    _patch_cli(monkeypatch, tmp_path, platform)
    result = CliRunner().invoke(app, ["logs", "caddy"])
    assert result.exit_code == 0, result.stdout
    assert platform.tailed == ["caddy"]
