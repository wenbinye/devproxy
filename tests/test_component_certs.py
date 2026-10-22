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


def _write_ca(paths: Paths) -> Path:
    ca = paths.data_dir / "pki" / "authorities" / "local" / "root.crt"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text("FAKE CA\n", encoding="utf-8")
    return ca


def _trust_both(platform, ca: Path) -> None:
    platform.trusted.add(ca)
    platform.nss.add(ca)


# -- check -----------------------------------------------------------------


def test_check_ok_when_trusted(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    status = comp.check()
    assert status.ok is True
    assert "已信任" in status.detail


def test_check_missing_ca_is_a_note_not_a_failure(tmp_path):
    """CA 由 Caddy 在首条路由注册时生成 —— 此刻还不存在不是缺陷。"""
    comp, platform, paths = _component(tmp_path)
    status = comp.check()
    assert status.ok is True
    assert "尚无 CA 根证书" in status.detail


def test_check_requires_nss_too(tmp_path):
    """系统信任库装了、NSS 没装 → Chrome 仍不信任，必须报出来。"""
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    platform.trusted.add(ca)
    status = comp.check()
    assert status.ok is False
    assert "NSS" in status.detail
    assert status.hint


def test_check_requires_system_trust_too(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    platform.nss.add(ca)
    status = comp.check()
    assert status.ok is False
    assert "系统信任库" in status.detail


def test_check_never_raises(tmp_path):
    """check() 永不抛异常 —— doctor 依赖它一次列出全部问题。"""

    class Broken(FakePlatform):
        def ca_trusted(self, ca):
            raise RuntimeError("平台炸了")

        def nss_trusted(self, ca):
            raise RuntimeError("平台炸了")

    comp, platform, paths = _component(tmp_path, Broken())
    _write_ca(paths)
    status = comp.check()
    assert status.ok is False
    assert status.detail


# -- plan ------------------------------------------------------------------


def test_plan_is_empty_when_trusted(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    assert comp.plan() == []


def test_plan_is_empty_when_ca_missing(tmp_path):
    """CA 还没生成 → 无事可做，也不该失败。"""
    comp, platform, paths = _component(tmp_path)
    assert comp.plan() == []


def test_plan_only_nss_step_when_system_trusted(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    platform.trusted.add(ca)
    actions = comp.plan()
    assert len(actions) == 1
    assert actions[0].command[0] == "install-nss-ca"
    assert actions[0].privileged is False


def test_plan_only_system_step_when_nss_trusted(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    platform.nss.add(ca)
    actions = comp.plan()
    assert len(actions) == 1
    assert actions[0].command[0] == "install-system-ca"
    assert actions[0].privileged is True


def test_plan_includes_privileged_steps(tmp_path):
    """只有系统信任库需要 root；NSS 库在用户家目录，不需要提权。"""
    comp, platform, paths = _component(tmp_path)
    _write_ca(paths)
    actions = comp.plan()
    assert len(actions) == 2
    privileged = [a for a in actions if a.privileged]
    assert len(privileged) == 1
    assert privileged[0].command[0] == "install-system-ca"
    nss_action = [a for a in actions if a.command[0] == "install-nss-ca"][0]
    assert nss_action.privileged is False, "NSS 库属于当前用户，不需要 root"


def test_plan_puts_root_free_step_first(tmp_path):
    """用户若拒绝 sudo，NSS 这步仍能跑完 —— 所以它排在前面。"""
    comp, platform, paths = _component(tmp_path)
    _write_ca(paths)
    actions = comp.plan()
    assert actions[0].command[0] == "install-nss-ca"
    assert actions[0].privileged is False
    assert actions[1].command[0] == "install-system-ca"


# -- apply：目标路径可注入，测试期间绝不碰真实系统 ---------------------------


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


def test_apply_runs_nss_before_system_ca(tmp_path):
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())
    names = [c[0] for c in platform.commands]
    assert names.index("certutil") < names.index("update-ca-certificates")


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


def test_apply_raises_when_update_ca_certificates_fails(tmp_path):
    """update-ca-certificates 失败必须报错 —— 否则 CA 只是躺在暂存目录里。"""
    from devproxy.platform.fake import FakeElevated

    class NoUpdateCa(FakePlatform):
        def privilege(self):
            if self._elevated is None:
                platform = self

                class Elev(FakeElevated):
                    def run(self, argv):
                        platform.commands.append(list(argv))
                        if argv[:1] == ["update-ca-certificates"]:
                            return 127, "命令不存在: update-ca-certificates"
                        return 0, ""

                self._elevated = Elev(self)
            return self._elevated

    platform = NoUpdateCa()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    try:
        comp.apply(comp.plan())
    except RuntimeError as e:
        assert "update-ca-certificates" in str(e)
    else:
        raise AssertionError("update-ca-certificates 失败时应当报错")


def test_verify_ok_after_trust(tmp_path):
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    comp.apply(comp.plan())
    _trust_both(platform, ca)
    assert comp.verify().ok is True


def test_verify_not_ok_when_only_system_trusted(tmp_path):
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    platform.trusted.add(ca)
    assert comp.verify().ok is False


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


def _patch_cert_component(monkeypatch, tmp_path):
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


def test_cli_trust_is_idempotent(tmp_path, monkeypatch):
    platform = FakePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    result = CliRunner().invoke(app, ["trust"])
    assert result.exit_code == 0
    assert "CA 根证书已信任，无需变更。" in result.stdout
    assert platform.commands == []


def test_cli_trust_without_ca_is_a_note(tmp_path, monkeypatch):
    platform = FakePlatform()
    _patch_cli(monkeypatch, tmp_path, platform)
    result = CliRunner().invoke(app, ["trust"])
    assert result.exit_code == 0
    assert "尚无 CA 根证书" in result.stdout


def test_cli_trust_refuses_privileged_without_yes(tmp_path, monkeypatch):
    """非交互环境 + 需要 root → 退出码 4，不能挂在密码提示上。"""
    platform = FakePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    _write_ca(paths)
    _patch_cert_component(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["trust"])
    assert result.exit_code == 4
    assert platform.commands == []


def test_cli_trust_installs_into_injected_targets(tmp_path, monkeypatch):
    platform = FakePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    _write_ca(paths)
    _patch_cert_component(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["trust", "--yes"])
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
    _patch_cert_component(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["trust", "--yes"])
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


def test_cli_logs_does_not_create_files_in_home(tmp_path, monkeypatch):
    """logs 是只读命令，不该在用户家目录里凭空造文件。"""
    platform = FakePlatform()
    _patch_cli(monkeypatch, tmp_path, platform)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    result = CliRunner().invoke(app, ["logs", "caddy"])
    assert result.exit_code == 0, result.stdout
    assert list(home.iterdir()) == []
