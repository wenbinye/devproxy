import hashlib
import tarfile
import io
from pathlib import Path

import pytest

from devproxy.components.caddy import (
    CADDY_SHA256,
    CADDY_VERSION,
    CaddyComponent,
    caddy_asset_name,
)
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.fake import FakePlatform


def _paths(tmp_path):
    return Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")


def _component(tmp_path, platform=None):
    paths = _paths(tmp_path)
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir), caddy_bin=str(tmp_path / "bin" / "caddy"))
    return CaddyComponent(platform or FakePlatform(), paths, cfg), paths, cfg


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
    comp = CaddyComponent(platform, paths, cfg)
    binary = Path(cfg.caddy_bin)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    platform.active.add("caddy")
    status = comp.check()
    assert status.ok is True, status.detail


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


def test_plan_is_empty_when_everything_ready(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg)
    binary = Path(cfg.caddy_bin)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    platform.active.add("caddy")
    assert comp.plan() == []


def test_apply_writes_unit_and_enables_service(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg)
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
    comp = CaddyComponent(platform, paths, cfg)
    comp.apply(comp.plan())
    status = comp.verify()
    assert status.ok is False
    assert "systemctl" in status.hint
