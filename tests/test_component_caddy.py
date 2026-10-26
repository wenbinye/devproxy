import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from devproxy.components.base import has_localhost_site
from devproxy.components.base import probe_url
from devproxy.components.caddy import CADDY_SHA256
from devproxy.components.caddy import CADDY_VERSION
from devproxy.components.caddy import CaddyComponent
from devproxy.components.caddy import _binary_version
from devproxy.components.caddy import caddy_asset_name
from devproxy.components.caddy import current_platform_key
from devproxy.components.caddy import download_caddy
from devproxy.core.caddy_env import CaddyEnvArchiveError
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.platform.fake import FakePlatform
from tests.support import ElevatedReader
from tests.support import UnreadablePath


def _paths(tmp_path):
    return Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")


def _fake_downloader(dest: Path) -> None:
    """Fake downloader: only writes an executable placeholder file and never touches the network."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("#!/bin/sh\n")
    dest.chmod(0o755)


def _component(tmp_path, platform=None, downloader=None):
    paths = _paths(tmp_path)
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir), caddy_bin=str(tmp_path / "bin" / "caddy"))
    comp = CaddyComponent(platform or FakePlatform(), paths, cfg, downloader or _fake_downloader)
    return comp, paths, cfg


def test_corrupt_extra_env_archive_fails_at_construction(tmp_path):
    """Construction must fail: treating a corrupt archive as empty silently drops MINIKUBE_IP."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    paths.caddy_env.write_text("{ truncated", encoding="utf-8")
    with pytest.raises(CaddyEnvArchiveError, match="environment variable archive"):
        CaddyComponent(
            FakePlatform(),
            paths,
            Config(storage=str(paths.data_dir)),
            _fake_downloader,
        )


def test_missing_extra_env_archive_is_normal(tmp_path):
    """A missing file is normal (no migration archive); construction must not fail."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    comp = CaddyComponent(
        FakePlatform(), paths, Config(storage=str(paths.data_dir)), _fake_downloader
    )
    assert comp.extra_env == {}


def _make_ready(comp, platform, cfg) -> None:
    """Put the binary, main Caddyfile, unit, service, and linger all into the ready state."""
    binary = Path(cfg.caddy_bin)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    comp.paths.caddyfile.write_text(comp._main_caddyfile(), encoding="utf-8")
    platform.install_service(
        "caddy",
        exec_start=[
            str(binary),
            "run",
            "--config",
            str(comp.paths.caddyfile),
        ],
        exec_reload=[
            str(binary),
            "reload",
            "--config",
            str(comp.paths.caddyfile),
            "--address",
            cfg.admin_addr,
        ],
        exec_stop=[str(binary), "stop"],
        env={},
    )
    platform.active.add("caddy")
    platform.linger = True


def test_asset_name_uses_mac_not_darwin():
    assert caddy_asset_name("mac", "arm64") == f"caddy_{CADDY_VERSION}_mac_arm64.tar.gz"
    assert caddy_asset_name("linux", "amd64") == f"caddy_{CADDY_VERSION}_linux_amd64.tar.gz"


def test_has_localhost_site_matches_address_tokens_not_substrings(tmp_path):
    site = tmp_path / "sites.caddy"
    site.write_text('https://localhost:8443, example.test { respond "hi" }\n', encoding="utf-8")
    assert has_localhost_site(site) is True

    site.write_text('foo.localhost.test { respond "hi" }\n', encoding="utf-8")
    assert has_localhost_site(site) is False


def test_probe_url_prefers_route_over_localhost(tmp_path):
    site = tmp_path / "sites.caddy"
    site.write_text('localhost { respond "hi" }\n', encoding="utf-8")
    routes = tmp_path / "routes.json"
    routes.write_text(
        json.dumps({"version": 1, "routes": [{"domain": "codeup.test"}]}),
        encoding="utf-8",
    )
    assert probe_url(routes, site) == "https://codeup.test"


def test_probe_url_none_without_route_or_localhost_site(tmp_path):
    site = tmp_path / "sites.caddy"
    site.write_text('codeup.test { respond "hi" }\n', encoding="utf-8")
    assert probe_url(tmp_path / "missing.json", site) is None


def test_sha256_covers_four_platforms():
    for key in [
        ("linux", "amd64"),
        ("linux", "arm64"),
        ("mac", "arm64"),
        ("mac", "amd64"),
    ]:
        assert key in CADDY_SHA256
        assert len(CADDY_SHA256[key]) == 64


def test_check_ok_when_binary_and_service_ready(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    status = comp.check()
    assert status.ok is True, status.detail


def test_check_not_ok_when_caddyfile_missing(tmp_path):
    """The binary/service are fine but the main Caddyfile is missing -- must not report "ready"."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    paths.caddyfile.unlink()
    status = comp.check()
    assert status.ok is False
    assert "Caddyfile" in status.detail
    assert status.hint
    actions = comp.plan()
    assert any(a.kind == "write" and a.path == paths.caddyfile for a in actions), (
        "plan must include a generate action when the Caddyfile is missing"
    )


def test_check_not_ok_when_caddyfile_stale(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    paths.caddyfile.write_text("stale config\n", encoding="utf-8")
    assert comp.check().ok is False
    assert any(a.kind == "write" and a.path == paths.caddyfile for a in comp.plan())


def test_plan_reinstalls_unit_when_execstart_points_elsewhere(tmp_path):
    """Operational leftover: the unit still points at the old Caddyfile while the service is active."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    platform.install_service(
        "caddy",
        exec_start=[
            str(Path(cfg.caddy_bin)),
            "run",
            "--config",
            str(tmp_path / "old" / "Caddyfile"),
        ],
        env={},
    )
    assert comp.check().ok is False
    actions = comp.plan()
    assert any(a.command[:1] == ["install-service"] for a in actions)


def test_plan_is_empty_when_everything_ready(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    assert comp.plan() == []


def test_check_missing_binary_reports_hint(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    status = comp.check()
    assert status.ok is False
    assert "caddy" in status.detail.lower()
    assert status.hint, "must provide a fix hint when missing"


def test_plan_includes_download_when_binary_missing(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    actions = comp.plan()
    kinds = [a.description for a in actions]
    assert any("download" in d for d in kinds)
    assert any(a.privileged for a in actions), "setcap requires elevation"


def test_apply_writes_unit_and_enables_service(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    comp.apply(comp.plan())
    assert "caddy" in platform.services
    unit = platform.services["caddy"]
    assert "ExecStart=" in unit
    assert str(paths.caddyfile) in unit
    assert "caddy" in platform.active


def test_apply_creates_main_caddyfile(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    comp.apply(comp.plan())
    content = paths.caddyfile.read_text()
    assert "admin 127.0.0.1:2019" in content
    assert f"import {paths.routes_caddy}" in content
    assert f"import {paths.sites_caddy}" in content


def test_sites_caddy_is_not_overwritten(tmp_path):
    """sites.caddy belongs to the user; the tool only imports it and never overwrites it."""
    comp, paths, cfg = _component(tmp_path)
    paths.sites_caddy.write_text('localhost { respond "hi" }\n')
    comp.apply(comp.plan())
    assert paths.sites_caddy.read_text() == 'localhost { respond "hi" }\n'


def test_verify_fails_when_service_inactive(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    comp.apply(comp.plan())
    # The service was installed but did not come up (setcap declined, port taken, etc.) -- caddy is not active
    platform.active.discard("caddy")
    status = comp.verify()
    assert status.ok is False
    assert "systemctl" in status.hint


# -- downloads belong to the component apply -------------------------------


def test_default_downloader_is_the_real_download_caddy(tmp_path):
    """The default uses the real downloader; injection is only for offline test runs."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir), caddy_bin=str(tmp_path / "bin" / "caddy"))
    comp = CaddyComponent(FakePlatform(), paths, cfg)
    assert comp._downloader is download_caddy


def test_apply_downloads_binary_when_missing(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    calls: list[Path] = []

    def recorder(dest: Path) -> None:
        calls.append(dest)
        _fake_downloader(dest)

    comp = CaddyComponent(FakePlatform(), paths, cfg, recorder)
    comp.apply(comp.plan())
    assert calls == [Path(cfg.caddy_bin)]
    assert Path(cfg.caddy_bin).exists()


def test_apply_propagates_download_failure_and_skips_service_install(tmp_path):
    comp, paths, cfg = _component(tmp_path)

    def boom(dest: Path) -> None:
        raise RuntimeError("caddy package verification failed")

    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, boom)
    with pytest.raises(RuntimeError):
        comp.apply(comp.plan())
    assert "caddy" not in platform.services


# -- setcap self-healing ----------------------------------------------------


def test_plan_replans_setcap_when_binary_present_but_service_inactive(tmp_path):
    """The binary is in place but the service is not running: the previous setcap may have failed, so re-authorize."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    binary = Path(cfg.caddy_bin)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    actions = comp.plan()
    descriptions = [a.description for a in actions]
    assert any("setcap" in d for d in descriptions)
    assert any(a.privileged for a in actions)
    assert any(a.command[:1] == ["install-service"] for a in actions)


# -- lower-bound checks for unit, platform, and download --------------------


def test_current_platform_key_rejects_unknown_arch(monkeypatch):
    import devproxy.components.caddy as caddy_mod

    monkeypatch.setattr(caddy_mod._platform, "system", lambda: "Linux")
    monkeypatch.setattr(caddy_mod._platform, "machine", lambda: "ppc64le")
    with pytest.raises(RuntimeError, match="unsupported platform"):
        current_platform_key()


def test_current_platform_key_rejects_unknown_os(monkeypatch):
    import devproxy.components.caddy as caddy_mod

    monkeypatch.setattr(caddy_mod._platform, "system", lambda: "Windows")
    monkeypatch.setattr(caddy_mod._platform, "machine", lambda: "AMD64")
    with pytest.raises(RuntimeError, match="unsupported platform"):
        current_platform_key()


def test_binary_version_parses_real_binary_output(tmp_path):
    binary = tmp_path / "caddy"
    binary.write_text('#!/bin/sh\necho "v2.11.7 h1:abc="\n')
    binary.chmod(0o755)
    assert _binary_version(binary) == "v2.11.7"


def test_binary_version_empty_when_binary_not_executable(tmp_path):
    binary = tmp_path / "caddy"
    binary.write_text("not executable\n")
    binary.chmod(0o644)
    assert _binary_version(binary) == ""


class _FakeTar:
    """Minimal usable tar context: provides only getmember / extractfile."""

    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getmember(self, name):
        return name

    def extractfile(self, member):
        return io.BytesIO(self.payload)


def _patch_download(monkeypatch, payload: bytes):
    import devproxy.components.caddy as caddy_mod

    monkeypatch.setattr(caddy_mod, "current_platform_key", lambda: ("linux", "amd64"))
    monkeypatch.setattr(
        caddy_mod.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b"tgz")
    )
    monkeypatch.setattr(caddy_mod, "_sha256_file", lambda path: CADDY_SHA256[("linux", "amd64")])
    monkeypatch.setattr(caddy_mod.tarfile, "open", lambda path: _FakeTar(payload))


def test_download_caddy_verifies_binary_version_and_replaces_atomically(tmp_path, monkeypatch):
    _patch_download(monkeypatch, b'#!/bin/sh\necho "v' + CADDY_VERSION.encode() + b'"\n')
    dest = tmp_path / "bin" / "caddy"
    download_caddy(dest)
    assert dest.exists()
    assert dest.stat().st_mode & 0o111, "the binary must be executable"
    # No temp file may be left behind in the same directory
    assert list(dest.parent.glob(".caddy-download-*")) == []


def test_download_caddy_rejects_wrong_binary_version(tmp_path, monkeypatch):
    _patch_download(monkeypatch, b'#!/bin/sh\necho "v0.0.1"\n')
    dest = tmp_path / "bin" / "caddy"
    with pytest.raises(RuntimeError, match="version mismatch"):
        download_caddy(dest)
    assert not dest.exists(), "a version mismatch must not leave the target file behind"
    assert list(dest.parent.glob(".caddy-download-*")) == []


def test_setcap_failure_raises_with_libcap_hint(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()

    class FailingElevated:
        def run(self, argv):
            return 1, "setcap: command not found"

        def write(self, path, content):
            pass

        def read(self, path):
            return ""

        def copy(self, src, dest):
            pass

    platform.privilege = lambda: FailingElevated()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    with pytest.raises(RuntimeError, match="libcap2-bin"):
        comp.apply(comp.plan())


def test_plan_includes_enable_linger_when_missing(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    platform.linger = False
    actions = comp.plan()
    linger = [a for a in actions if a.command[:1] == ["enable-linger"]]
    assert linger and linger[0].privileged is True


def test_apply_enables_linger(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    platform.linger = False
    comp.apply(comp.plan())
    assert platform.linger is True


def test_verify_checks_real_tls_handshake(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    paths.sites_caddy.write_text('localhost { respond "hi" }\n', encoding="utf-8")
    status = comp.verify()
    assert status.ok is True, status.detail
    assert "https://localhost" in platform.http_probes


def test_verify_fails_when_tls_probe_fails(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    paths.sites_caddy.write_text('localhost { respond "hi" }\n', encoding="utf-8")
    platform.http_result = False
    status = comp.verify()
    assert status.ok is False
    assert "TLS" in status.detail
    assert status.hint


def test_verify_skips_tls_probe_when_no_site_configured(tmp_path):
    """A clean machine has no sites yet -- TLS is not executable, so stay ok + hint."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    status = comp.verify()
    assert status.ok is True
    assert "no sites yet" in status.detail
    assert platform.http_probes == []


# -- N1: the probe target must not be hardcoded to localhost ----------------


def test_verify_probes_first_route_domain(tmp_path):
    """A machine with only *.test sites has no localhost site -- probing localhost would fail falsely."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    paths.sites_caddy.write_text('codeup.test { respond "hi" }\n', encoding="utf-8")
    paths.routes_json.parent.mkdir(parents=True, exist_ok=True)
    paths.routes_json.write_text(
        json.dumps(
            {
                "version": 1,
                "routes": [
                    {
                        "name": "codeup",
                        "domain": "codeup.test",
                        "port": 42133,
                        "mode": "up",
                        "upstream": "127.0.0.1:42133",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    status = comp.verify()
    assert status.ok is True, status.detail
    assert "https://codeup.test" in platform.http_probes
    assert "https://localhost" not in platform.http_probes


def test_verify_without_route_and_without_localhost_site_is_ok(tmp_path):
    """Neither a registered route nor a localhost site -- nothing to probe, return ok + hint."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    paths.sites_caddy.write_text('codeup.test { respond "hi" }\n', encoding="utf-8")
    status = comp.verify()
    assert status.ok is True, status.detail
    assert platform.http_probes == []


# -- N5: check and plan must use the same version criterion ----------------


def test_check_and_plan_agree_when_version_unreadable(tmp_path):
    """When the version is unreadable, one must not say "ready" while the other says "download"."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    platform.binary_version = lambda binary: ""
    assert comp.check().ok is False, "an unreadable version must not be judged ready"
    assert any("download" in a.description for a in comp.plan())


def test_check_and_plan_agree_when_version_matches(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    assert comp.check().ok is True
    assert not any("download" in a.description for a in comp.plan())


def test_plan_restarts_service_when_only_main_caddyfile_changed(tmp_path):
    """Changing only the main Caddyfile (e.g. https_port) leaves unit and service unchanged -- still must restart.

    Writing the file alone changes nothing: the running Caddy still holds the old
    config, and a later `devproxy up` connects to the admin API using the new config
    and reports a completely unrelated error.
    """
    from dataclasses import replace

    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)

    new_cfg = replace(cfg, https_port=8443)
    comp = CaddyComponent(platform, paths, new_cfg, _fake_downloader)
    actions = comp.plan()

    assert not any(a.command[:1] == ["install-service"] for a in actions)
    assert any(a.command[:2] == ["restart-service", "caddy"] for a in actions), actions
    idx_write = next(
        i for i, a in enumerate(actions) if a.kind == "write" and a.path == paths.caddyfile
    )
    idx_restart = next(
        i for i, a in enumerate(actions) if a.command[:2] == ["restart-service", "caddy"]
    )
    assert idx_write < idx_restart, "must write the file before restarting"


def test_apply_restarts_service_for_caddyfile_only_change(tmp_path):
    """The restart-service action in the Caddyfile-only scenario must really restart."""
    from dataclasses import replace

    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    comp = CaddyComponent(platform, paths, replace(cfg, https_port=8443), _fake_downloader)
    restarted = []
    platform.restart_service = lambda name: restarted.append(name)
    comp.apply(comp.plan())
    assert restarted == ["caddy"]


def test_check_not_ok_when_linger_disabled(tmp_path):
    """With linger disabled the user service does not start at boot -- check and plan must agree."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    platform.linger = False

    status = comp.check()
    assert status.ok is False
    assert "linger" in status.detail
    assert any(a.command[:1] == ["enable-linger"] for a in comp.plan())


def test_plan_probes_binary_version_and_service_only_once(tmp_path):
    """One plan should probe the version once and the service state once (avoid self-contradiction within a round)."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)

    version_calls = []
    active_calls = []
    platform.binary_version = lambda binary: version_calls.append(binary) or f"v{CADDY_VERSION}"
    original_active = platform.service_active
    platform.service_active = lambda name: active_calls.append(name) or original_active(name)

    comp.plan()
    assert len(version_calls) == 1
    assert active_calls.count("caddy") == 1


def test_unit_current_checks_exec_reload_address(tmp_path):
    """When admin_addr changed but ExecStart did not, the old unit's --address is stale."""
    from dataclasses import replace

    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)

    comp = CaddyComponent(
        platform, paths, replace(cfg, admin_addr="127.0.0.1:2020"), _fake_downloader
    )
    assert comp._unit_current() is False
    assert any(a.command[:1] == ["install-service"] for a in comp.plan())


def test_plan_restarts_service_when_binary_is_replaced(tmp_path):
    """Replacing the binary without restarting leaves the old version running; "version pinned" only takes effect on the next restart."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    # Version mismatch (but path and unit unchanged) -> only replace the binary
    platform.binary_version = lambda binary: "v2.10.0"
    actions = comp.plan()
    assert any(a.command[:1] == ["download-caddy"] for a in actions)
    assert any(a.command[:2] == ["restart-service", "caddy"] for a in actions), actions
    # The restart must come after setcap -- otherwise the restarted process cannot bind 80/443
    idx_cap = next(i for i, a in enumerate(actions) if a.command[:1] == ["setcap"])
    idx_restart = next(
        i for i, a in enumerate(actions) if a.command[:2] == ["restart-service", "caddy"]
    )
    assert idx_cap < idx_restart


def test_apply_restarts_service_after_replacing_binary(tmp_path):
    """The restart action must really restart the service."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    platform.binary_version = lambda binary: "v2.10.0"
    actions = comp.plan()
    restarted = []
    platform.restart_service = lambda name: restarted.append(name)
    comp.apply(actions)
    assert "caddy" in restarted


# -- N6: the unit criterion must look at the binary path in ExecStart -------


def test_plan_reinstalls_unit_when_binary_path_changed(tmp_path):
    """After changing caddy_bin in the config, the installed unit still points at the old binary -- must be judged stale."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    platform.install_service(
        "caddy",
        exec_start=["/old/path/caddy", "run", "--config", str(paths.caddyfile)],
        env={},
    )
    assert comp.check().ok is False
    actions = comp.plan()
    assert any(a.command[:1] == ["install-service"] for a in actions)


# -- macOS: no setcap; the unit is a launchd plist --------------------------


class _MacPlatform(FakePlatform):
    """macOS platform: the unit is a launchd plist (ProgramArguments is an array)."""

    name = "mac"
    dns_port = 5300

    def linger_enabled(self) -> bool:
        """macOS has no systemd-linger -- always "no need to enable"."""
        return True

    def install_service(
        self, name, *, exec_start, exec_reload=None, exec_stop=None, env, description=""
    ):
        import plistlib

        data = {
            "Label": f"com.devproxy.{name}",
            "ProgramArguments": list(exec_start),
            "EnvironmentVariables": dict(env),
            "RunAtLoad": True,
            "KeepAlive": True,
        }
        text = plistlib.dumps(data).decode("utf-8")
        self.services[name] = text
        path = self.service_unit_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_macos_plan_has_no_setcap(tmp_path):
    """macOS has no setcap -- binding 80/443 relies on a root LaunchDaemon."""
    comp, paths, cfg = _component(tmp_path)
    platform = _MacPlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    actions = comp.plan()
    assert not any(a.command[:1] == ["setcap"] for a in actions)


def test_macos_install_service_action_is_privileged(tmp_path):
    """On macOS caddy is a root daemon under /Library/LaunchDaemons.

    An action's `privileged` flag determines two things: whether the sudo list
    names it and whether a non-interactive environment refuses to run because of
    it. Omitting the flag makes setup falsely claim "1 item needs sudo" and, when
    only this step remains, **bypass the non-interactive guard**
    (`plan.needs_sudo` is empty), then pop a password prompt midway -- which in a
    no-TTY scenario is a direct failure.
    """
    comp, paths, cfg = _component(tmp_path)
    platform = _MacPlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    installs = [a for a in comp.plan() if a.command[:1] == ["install-service"]]
    assert installs, "the plan should include an action to install the LaunchDaemon"
    assert all(a.privileged for a in installs)


def test_linux_install_service_action_is_not_privileged(tmp_path):
    """On Linux caddy is a **user** unit (~/.config/systemd/user) -- no root needed.

    Paired with the previous case: hardcoding the macOS fix as "always elevate"
    would add a phantom item to the Linux sudo list and make non-interactive
    environments exit 4 incorrectly.
    """
    comp, paths, cfg = _component(tmp_path)
    comp = CaddyComponent(FakePlatform(), paths, cfg, _fake_downloader)
    installs = [a for a in comp.plan() if a.command[:1] == ["install-service"]]
    assert installs, "the plan should include an action to install the user unit"
    assert not any(a.privileged for a in installs)


def test_macos_plan_has_no_linger_action(tmp_path):
    """macOS has no systemd-linger -- the platform reports "enabled", so no linger action may be planned."""
    comp, paths, cfg = _component(tmp_path)
    platform = _MacPlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    actions = comp.plan()
    assert not any(a.command[:1] == ["enable-linger"] for a in actions)


def test_macos_unit_current_accepts_plist(tmp_path):
    """In a launchd plist ProgramArguments is an array -- the unit criterion must understand it."""
    comp, paths, cfg = _component(tmp_path)
    platform = _MacPlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    assert comp._unit_current() is True
    assert comp.check().ok is True


def test_macos_unit_current_detects_stale_plist(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = _MacPlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    platform.install_service(
        "caddy",
        exec_start=["/old/path/caddy", "run", "--config", str(paths.caddyfile)],
        env={},
    )
    assert comp._unit_current() is False


# ---------- an unreadable unit is not a stale unit ----------


class _UnreadableUnitPlatform(_MacPlatform):
    """The unit file is unreadable to a regular user (macOS system-scope 0600 root case)."""

    def service_unit_path(self, name):
        return UnreadablePath(str(super().service_unit_path(name)))

    def privilege(self):
        raise AssertionError("reading the unit may only use non-interactive privilege_read")

    def privilege_read(self, path):
        return ElevatedReader(self).read(path)


def test_unit_current_survives_permission_denied_read(tmp_path):
    """An unreadable unit never means "stale unit".

    macOS system-scope units live in /Library/LaunchDaemons -- with wrong permissions
    or ownership a regular user cannot read them. If judged "stale", doctor would
    stay red forever and every setup would reinstall the unit and ask for a password again.
    """
    comp, paths, cfg = _component(tmp_path)
    platform = _UnreadableUnitPlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    assert comp._unit_current() is True
    status = comp.check()
    assert status.ok is True, status.detail


class _NoPromptPlatform(_UnreadableUnitPlatform):
    """Explode as soon as anyone requests an elevated session -- doctor must never pop a password prompt."""

    def privilege(self):
        raise AssertionError("doctor must not trigger elevation (it pops a password prompt)")

    def privilege_read(self, path):
        return None


def test_doctor_never_asks_for_password(tmp_path):
    """doctor promises to be read-only: it must not ask for a password even when the unit is unreadable.

    Constructing an elevated session runs `sudo -v` -- that pops a password prompt in
    the user's face, violating doctor's "read-only" promise and hanging doctor in scripts.
    """
    comp, paths, cfg = _component(tmp_path)
    platform = _NoPromptPlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    # Passing means no exception: _NoPromptPlatform.privilege() raises AssertionError directly
    assert comp._unit_current() is True
    comp.check()


def test_unit_current_is_false_when_argv_does_not_match(tmp_path):
    """The criterion is the whole command line -- a missing argument or wrong order counts as stale."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    platform.install_service(
        "caddy",
        exec_start=[
            str(Path(cfg.caddy_bin)),
            "--config",
            str(paths.caddyfile),
        ],
        env={},
    )
    assert comp._unit_current() is False, "missing run means it is not the current unit"


def test_unit_current_is_false_when_unit_missing(tmp_path):
    """The unit was never installed -> of course it is stale."""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    Path(platform.service_unit_path("caddy")).unlink()
    assert comp._unit_current() is False
