import hashlib
import json
import tarfile
import io
from pathlib import Path

import pytest

from devproxy.components.caddy import (
    CADDY_SHA256,
    CADDY_VERSION,
    CaddyComponent,
    _binary_version,
    caddy_asset_name,
    current_platform_key,
    download_caddy,
)
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.fake import FakePlatform
from tests.support import ElevatedReader, UnreadablePath


def _paths(tmp_path):
    return Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")


def _fake_downloader(dest: Path) -> None:
    """假下载器：只落一个可执行占位文件，绝不触网。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("#!/bin/sh\n")
    dest.chmod(0o755)


def _component(tmp_path, platform=None, downloader=None):
    paths = _paths(tmp_path)
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir), caddy_bin=str(tmp_path / "bin" / "caddy"))
    comp = CaddyComponent(
        platform or FakePlatform(), paths, cfg, downloader or _fake_downloader
    )
    return comp, paths, cfg


def _make_ready(comp, platform, cfg) -> None:
    """把二进制、主 Caddyfile、单元、服务、linger 全部置为就绪。"""
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
    """二进制/服务都对，但主 Caddyfile 不存在 —— 不能报“已就绪”。"""
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
    assert any(
        a.kind == "write" and a.path == paths.caddyfile for a in actions
    ), "Caddyfile 缺失时 plan 必须包含生成动作"


def test_check_not_ok_when_caddyfile_stale(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    paths.caddyfile.write_text("旧的配置\n", encoding="utf-8")
    assert comp.check().ok is False
    assert any(
        a.kind == "write" and a.path == paths.caddyfile for a in comp.plan()
    )


def test_plan_reinstalls_unit_when_execstart_points_elsewhere(tmp_path):
    """运维遗留：单元还指向旧 Caddyfile，服务却是 active。"""
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
    assert status.hint, "缺失时必须给出修复提示"


def test_plan_includes_download_when_binary_missing(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    actions = comp.plan()
    kinds = [a.description for a in actions]
    assert any("下载" in d for d in kinds)
    assert any(a.privileged for a in actions), "setcap 需要提权"


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
    """sites.caddy 是用户的，工具只 import 不覆盖。"""
    comp, paths, cfg = _component(tmp_path)
    paths.sites_caddy.write_text("localhost { respond \"hi\" }\n")
    comp.apply(comp.plan())
    assert paths.sites_caddy.read_text() == "localhost { respond \"hi\" }\n"


def test_verify_fails_when_service_inactive(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    comp.apply(comp.plan())
    # 服务装上了却没起来（setcap 被拒、端口被占等）—— active 里没有 caddy
    platform.active.discard("caddy")
    status = comp.verify()
    assert status.ok is False
    assert "systemctl" in status.hint


# -- 下载归属组件 apply ----------------------------------------------------


def test_default_downloader_is_the_real_download_caddy(tmp_path):
    """默认走真实下载器；注入只用于测试离线运行。"""
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
        raise RuntimeError("caddy 包校验失败")

    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, boom)
    with pytest.raises(RuntimeError):
        comp.apply(comp.plan())
    assert "caddy" not in platform.services


# -- setcap 自愈 ------------------------------------------------------------


def test_plan_replans_setcap_when_binary_present_but_service_inactive(tmp_path):
    """二进制已就位但服务没跑：上次 setcap 可能失败过，需要重新授权。"""
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


# -- 单元、平台、下载的下限校验 ---------------------------------------------


def test_current_platform_key_rejects_unknown_arch(monkeypatch):
    import devproxy.components.caddy as caddy_mod

    monkeypatch.setattr(caddy_mod._platform, "system", lambda: "Linux")
    monkeypatch.setattr(caddy_mod._platform, "machine", lambda: "ppc64le")
    with pytest.raises(RuntimeError, match="不支持的平台"):
        current_platform_key()


def test_current_platform_key_rejects_unknown_os(monkeypatch):
    import devproxy.components.caddy as caddy_mod

    monkeypatch.setattr(caddy_mod._platform, "system", lambda: "Windows")
    monkeypatch.setattr(caddy_mod._platform, "machine", lambda: "AMD64")
    with pytest.raises(RuntimeError, match="不支持的平台"):
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
    """最小可用的 tar 上下文：只提供 getmember / extractfile。"""

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


def test_download_caddy_verifies_binary_version_and_replaces_atomically(
    tmp_path, monkeypatch
):
    _patch_download(monkeypatch, b'#!/bin/sh\necho "v' + CADDY_VERSION.encode() + b'"\n')
    dest = tmp_path / "bin" / "caddy"
    download_caddy(dest)
    assert dest.exists()
    assert dest.stat().st_mode & 0o111, "二进制必须可执行"
    # 同目录临时文件不得残留
    assert list(dest.parent.glob(".caddy-download-*")) == []


def test_download_caddy_rejects_wrong_binary_version(tmp_path, monkeypatch):
    _patch_download(monkeypatch, b'#!/bin/sh\necho "v0.0.1"\n')
    dest = tmp_path / "bin" / "caddy"
    with pytest.raises(RuntimeError, match="版本不符"):
        download_caddy(dest)
    assert not dest.exists(), "版本不符时不能留下目标文件"
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
    paths.sites_caddy.write_text("localhost { respond \"hi\" }\n", encoding="utf-8")
    status = comp.verify()
    assert status.ok is True, status.detail
    assert "https://localhost" in platform.http_probes


def test_verify_fails_when_tls_probe_fails(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    paths.sites_caddy.write_text("localhost { respond \"hi\" }\n", encoding="utf-8")
    platform.http_result = False
    status = comp.verify()
    assert status.ok is False
    assert "TLS" in status.detail
    assert status.hint


def test_verify_skips_tls_probe_when_no_site_configured(tmp_path):
    """干净机器上还没有任何站点 —— TLS 不可执行，保持 ok + 提示。"""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    status = comp.verify()
    assert status.ok is True
    assert "尚无站点" in status.detail
    assert platform.http_probes == []


# -- N1: 探测目标不能硬编码 localhost ---------------------------------------


def test_verify_probes_first_route_domain(tmp_path):
    """只有 *.test 站点的机器上没有 localhost 站点 —— 探 localhost 会假失败。"""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    paths.sites_caddy.write_text("codeup.test { respond \"hi\" }\n", encoding="utf-8")
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
    """既没有注册路由、也没有 localhost 站点 —— 无从探测，返回 ok + 提示。"""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    paths.sites_caddy.write_text("codeup.test { respond \"hi\" }\n", encoding="utf-8")
    status = comp.verify()
    assert status.ok is True, status.detail
    assert platform.http_probes == []


# -- N5: check 与 plan 的版本判据必须一致 ----------------------------------


def test_check_and_plan_agree_when_version_unreadable(tmp_path):
    """版本读不出来时，不能一个说"已就绪"、另一个说"要下载"。"""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    platform.binary_version = lambda binary: ""
    assert comp.check().ok is False, "版本读不出来不能判就绪"
    assert any("下载" in a.description for a in comp.plan())


def test_check_and_plan_agree_when_version_matches(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    assert comp.check().ok is True
    assert not any("下载" in a.description for a in comp.plan())


def test_plan_restarts_service_when_binary_is_replaced(tmp_path):
    """换了二进制却不重启 —— 跑着的还是旧版本，"已固定版本"只在下次重启兑现。"""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    # 版本不符（但路径没变、单元也没变）→ 只换二进制
    platform.binary_version = lambda binary: "v2.10.0"
    actions = comp.plan()
    assert any(a.command[:1] == ["download-caddy"] for a in actions)
    assert any(a.command[:2] == ["restart-service", "caddy"] for a in actions), actions
    # 重启必须排在 setcap 之后 —— 否则重启后绑不上 80/443
    idx_cap = next(
        i for i, a in enumerate(actions) if a.command[:1] == ["setcap"]
    )
    idx_restart = next(
        i for i, a in enumerate(actions) if a.command[:2] == ["restart-service", "caddy"]
    )
    assert idx_cap < idx_restart


def test_apply_restarts_service_after_replacing_binary(tmp_path):
    """重启动作必须真的重启服务。"""
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


# -- N6: 单元判据要看 ExecStart 里的二进制路径 -----------------------------


def test_plan_reinstalls_unit_when_binary_path_changed(tmp_path):
    """改了配置里的 caddy_bin 后，已装单元仍指旧二进制 —— 必须判为单元过期。"""
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


# -- macOS：没有 setcap，单元是 launchd plist ------------------------------


class _MacPlatform(FakePlatform):
    """macOS 平台：单元是 launchd plist（ProgramArguments 是数组）。"""

    name = "mac"
    dns_port = 5300

    def linger_enabled(self) -> bool:
        """macOS 没有 systemd-linger —— 恒为"无需启用"。"""
        return True

    def install_service(self, name, *, exec_start, exec_reload=None, exec_stop=None,
                        env, description=""):
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
    """macOS 没有 setcap —— 绑 80/443 靠 root 的 LaunchDaemon。"""
    comp, paths, cfg = _component(tmp_path)
    platform = _MacPlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    actions = comp.plan()
    assert not any(a.command[:1] == ["setcap"] for a in actions)


def test_macos_install_service_action_is_privileged(tmp_path):
    """macOS 的 caddy 是 /Library/LaunchDaemons 下的 root 守护进程。

    动作的 `privileged` 决定两件事：sudo 清单要不要点名它、非交互环境下
    要不要因此拒绝执行。漏标会让 setup 谎报"需要 sudo 共 1 项"，并在
    只剩这一步时**绕过非交互护栏**（`plan.needs_sudo` 为空），然后中途
    弹密码框 —— 在无 TTY 的场景下就是直接失败。
    """
    comp, paths, cfg = _component(tmp_path)
    platform = _MacPlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    installs = [a for a in comp.plan() if a.command[:1] == ["install-service"]]
    assert installs, "计划里应有安装 LaunchDaemon 的动作"
    assert all(a.privileged for a in installs)


def test_linux_install_service_action_is_not_privileged(tmp_path):
    """Linux 的 caddy 是 **user** unit（~/.config/systemd/user）—— 不需要 root。

    与上一条成对：把 macOS 的修法写死成"永远提权"会让 Linux 的 sudo 清单
    凭空多一项，非交互环境还会因此误退 4。
    """
    comp, paths, cfg = _component(tmp_path)
    comp = CaddyComponent(FakePlatform(), paths, cfg, _fake_downloader)
    installs = [a for a in comp.plan() if a.command[:1] == ["install-service"]]
    assert installs, "计划里应有安装 user unit 的动作"
    assert not any(a.privileged for a in installs)


def test_macos_plan_has_no_linger_action(tmp_path):
    """macOS 无 systemd-linger —— 平台报"已启用"，不该计划任何 linger 动作。"""
    comp, paths, cfg = _component(tmp_path)
    platform = _MacPlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    actions = comp.plan()
    assert not any(a.command[:1] == ["enable-linger"] for a in actions)


def test_macos_unit_current_accepts_plist(tmp_path):
    """launchd plist 里 ProgramArguments 是数组 —— 单元判据必须认它。"""
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


# ---------- 单元读不到 ≠ 单元过期 ----------


class _UnreadableUnitPlatform(_MacPlatform):
    """单元文件普通用户读不了（macOS system scope 的 0600 root 情形）。"""

    def service_unit_path(self, name):
        return UnreadablePath(str(super().service_unit_path(name)))

    def privilege(self):
        return ElevatedReader(self)


def test_unit_current_survives_permission_denied_read(tmp_path):
    """读不到单元，绝不等于"单元过期"。

    macOS 的 system scope 单元在 /Library/LaunchDaemons —— 权限或归属
    不对时普通用户读不了。这时若判成"过期"，doctor 会永远报红，
    每次 setup 都要重装单元并再要一次密码。
    """
    comp, paths, cfg = _component(tmp_path)
    platform = _UnreadableUnitPlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    assert comp._unit_current() is True
    status = comp.check()
    assert status.ok is True, status.detail


class _NoPromptPlatform(_UnreadableUnitPlatform):
    """一旦有人要提权会话就炸 —— doctor 绝不能弹密码框。"""

    def privilege(self):
        raise AssertionError("doctor 不得触发提权（会弹密码框）")

    def privilege_read(self, path):
        return None


def test_doctor_never_asks_for_password(tmp_path):
    """doctor 承诺只读：单元读不到时也不许索要密码。

    提权会话的构造会跑 `sudo -v` —— 那会把密码框弹到用户脸上，
    既违反 doctor 的"只读"承诺，也会让脚本里跑的 doctor 卡住。
    """
    comp, paths, cfg = _component(tmp_path)
    platform = _NoPromptPlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    # 不抛异常即通过：_NoPromptPlatform.privilege() 会直接 AssertionError
    assert comp._unit_current() is True
    comp.check()


def test_unit_current_is_false_when_argv_does_not_match(tmp_path):
    """判据是整条命令行 —— 参数缺失或顺序不对都算过期。"""
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
    assert comp._unit_current() is False, "少了 run 就算不是当前单元"


def test_unit_current_is_false_when_unit_missing(tmp_path):
    """单元压根没装 → 当然是过期。"""
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg, _fake_downloader)
    _make_ready(comp, platform, cfg)
    Path(platform.service_unit_path("caddy")).unlink()
    assert comp._unit_current() is False

