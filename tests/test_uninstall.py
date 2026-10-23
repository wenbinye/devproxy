"""uninstall 的行为测试：平台能力、计划顺序、CA 规则、幂等与 CLI。"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.lifecycle import uninstall as uninstall_mod
from devproxy.lifecycle.uninstall import (
    UninstallTargets,
    apply_plan,
    build_plan,
    latest_dnsmasq_backup,
    latest_migration_backup,
)
from devproxy.platform import linux as linux_mod
from devproxy.platform import macos as macos_mod
from devproxy.platform.base import Platform
from devproxy.platform.fake import FakePlatform
from devproxy.platform.linux import LinuxPlatform
from devproxy.platform.macos import MacOSPlatform


# ---------------------------------------------------------------------------
# 测试夹具与小工具
# ---------------------------------------------------------------------------


def cert_pem(seed: str) -> str:
    """造一个 PEM 证书块：指纹判据只关心 DER 字节，不需要真的 X.509。"""
    payload = base64.b64encode(seed.encode("utf-8")).decode("ascii")
    return f"-----BEGIN CERTIFICATE-----\n{payload}\n-----END CERTIFICATE-----\n"


def make_config(paths: Paths) -> Config:
    return Config(
        storage=str(paths.data_dir),
        caddy_bin=str(paths.config_dir / "bin" / "caddy"),
    )


def make_targets(tmp_path: Path) -> UninstallTargets:
    """把所有系统路径指到 tmp_path —— 永不碰真实 /etc 与钥匙串。"""
    return UninstallTargets(
        resolved_conf=tmp_path / "etc" / "systemd" / "resolved.conf.d" / "devproxy.conf",
        system_dnsmasq_conf=tmp_path / "etc" / "dnsmasq.d" / "devproxy.conf",
        resolver_dir=tmp_path / "etc" / "resolver",
        system_ca_dest=tmp_path / "usr" / "local" / "share" / "ca-certificates" / "devproxy-local.crt",
        system_keychain=str(tmp_path / "System.keychain"),
        nss_dir=tmp_path / "nssdb",
        dnsmasq_conf=tmp_path / "home" / ".config" / "dnsmasq" / "dnsmasq.conf",
    )


def write_ca(config: Config, seed: str = "devproxy-ca") -> Path:
    ca = config.storage_path / "pki" / "authorities" / "local" / "root.crt"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text(cert_pem(seed), encoding="utf-8")
    return ca


def write_routes(paths: Paths, routes: list[dict]) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.routes_json.write_text(
        json.dumps(
            {"version": 1, "domain_suffix": "test", "routes": routes},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def route(
    name: str,
    *,
    mode: str = "up",
    pid: int | None = None,
    proc_start: str | None = None,
) -> dict:
    data = {
        "name": name,
        "domain": f"{name}.test",
        "port": 4000,
        "mode": mode,
        "upstream": "127.0.0.1:4000",
    }
    if pid is not None:
        data["pid"] = pid
    if proc_start is not None:
        data["proc_start"] = proc_start
    return data


def seed_service(platform: FakePlatform, name: str) -> None:
    platform.install_service(name, exec_start=["/bin/true"], env={})
    platform.enable_service(name)


def seed_state(paths: Paths, *, include_storage_archive: bool = False) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "sites.caddy").write_text("# 我的站点\n", encoding="utf-8")
    (paths.config_dir / "config.toml").write_text("", encoding="utf-8")
    targets = {
        paths.caddyfile: "# main\n",
        paths.routes_caddy: "# routes\n",
        paths.lock_path: "",
        paths.state_dir / "dns-route.json": "{}",
        paths.caddy_env: "{}",
    }
    if not paths.routes_json.exists():
        targets[paths.routes_json] = "{}"
    if include_storage_archive:
        targets[paths.caddy_storage] = "{}"
    for path, content in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    (paths.log_dir / "caddy.log").write_text("log\n", encoding="utf-8")


def commands(plan) -> list[list[str]]:
    return [action.command for action in plan.actions if action.command]


def delete_paths(plan) -> set[Path]:
    return {action.path for action in plan.actions if action.kind == "delete"}


# ---------------------------------------------------------------------------
# Platform.remove_service
# ---------------------------------------------------------------------------


def test_linux_remove_service_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_mod, "_ALLOW_MUTATION", True)
    calls: list[list[str]] = []

    def fake_run(argv, timeout=None):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    platform = LinuxPlatform(user_home=tmp_path)
    unit = platform.service_unit_path("caddy")
    unit.parent.mkdir(parents=True)
    unit.write_text("unit", encoding="utf-8")

    platform.remove_service("caddy")
    assert not unit.exists()
    assert ["systemctl", "--user", "disable", "--now", "caddy"] in calls
    assert ["systemctl", "--user", "daemon-reload"] in calls

    calls.clear()
    platform.remove_service("caddy")  # 第二次：单元已不存在，仍须成功
    assert calls == [
        ["systemctl", "--user", "disable", "--now", "caddy"],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_linux_remove_service_ignores_missing_service(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_mod, "_ALLOW_MUTATION", True)

    def fake_run(argv, timeout=None):
        if "disable" in argv:
            return 1, "Unit caddy.service not loaded."
        if "is-active" in argv:
            return 3, "inactive"
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    platform = LinuxPlatform(user_home=tmp_path)
    platform.remove_service("caddy")  # 不抛异常
    assert not platform.service_unit_path("caddy").exists()


def test_linux_remove_service_removes_stale_unit_when_disable_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_mod, "_ALLOW_MUTATION", True)

    def fake_run(argv, timeout=None):
        if "disable" in argv:
            return 1, "Unit caddy.service not loaded."
        if "is-active" in argv:
            return 3, "inactive"
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    platform = LinuxPlatform(user_home=tmp_path)
    unit = platform.service_unit_path("caddy")
    unit.parent.mkdir(parents=True)
    unit.write_text("stale", encoding="utf-8")

    platform.remove_service("caddy")
    assert not unit.exists()


def test_linux_remove_service_raises_when_service_still_active(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_mod, "_ALLOW_MUTATION", True)

    def fake_run(argv, timeout=None):
        if "disable" in argv:
            return 1, "permission denied"
        if "is-active" in argv:
            return 0, "active"
        return 0, ""

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    platform = LinuxPlatform(user_home=tmp_path)
    with pytest.raises(RuntimeError):
        platform.remove_service("caddy")


def test_macos_remove_service_user_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(macos_mod, "_ALLOW_MUTATION", True)
    calls: list[list[str]] = []

    def fake_run(argv, timeout=None):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    platform = MacOSPlatform(user_home=tmp_path)
    plist = platform.service_unit_path("dnsmasq")
    plist.parent.mkdir(parents=True)
    plist.write_text("plist", encoding="utf-8")

    platform.remove_service("dnsmasq")
    assert ["launchctl", "bootout", f"gui/{os.getuid()}/com.devproxy.dnsmasq"] in calls
    assert not plist.exists()


def test_macos_remove_service_system_scope_uses_privilege(tmp_path, monkeypatch):
    monkeypatch.setattr(macos_mod, "_ALLOW_MUTATION", True)
    calls: list[list[str]] = []
    target = tmp_path / "LaunchDaemons" / "com.devproxy.caddy.plist"

    def fake_run(argv, timeout=None):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(macos_mod, "_run", fake_run)
    platform = MacOSPlatform(user_home=tmp_path)
    monkeypatch.setattr(platform, "service_unit_path", lambda name: target)

    platform.remove_service("caddy")
    assert ["sudo", "launchctl", "bootout", "system/com.devproxy.caddy"] in calls
    assert ["sudo", "rm", "-f", str(target)] in calls


def test_fake_platform_remove_service_clears_state():
    platform = FakePlatform()
    seed_service(platform, "caddy")
    assert platform.services and platform.active

    platform.remove_service("caddy")
    assert "caddy" not in platform.services
    assert "caddy" not in platform.active
    assert not platform.service_unit_path("caddy").exists()
    assert ["remove-service", "caddy"] in platform.commands

    platform.remove_service("caddy")  # 幂等


def test_platform_protocol_includes_remove_service(tmp_path):
    assert isinstance(FakePlatform(), Platform)
    assert isinstance(LinuxPlatform(user_home=tmp_path), Platform)
    assert isinstance(MacOSPlatform(user_home=tmp_path), Platform)


# ---------------------------------------------------------------------------
# build_plan：顺序与内容
# ---------------------------------------------------------------------------


def test_plan_order_kill_services_dns_ca_state(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    targets = make_targets(tmp_path)
    platform.nss_db = targets.nss_dir
    write_routes(
        paths,
        [
            route("demo", pid=4242, proc_start="start-1"),
            route("expose-only", mode="expose", pid=9999, proc_start="start-2"),
        ],
    )
    seed_service(platform, "caddy")
    seed_service(platform, "dnsmasq")
    targets.resolved_conf.parent.mkdir(parents=True)
    targets.resolved_conf.write_text("dns\n", encoding="utf-8")
    targets.system_dnsmasq_conf.parent.mkdir(parents=True)
    targets.system_dnsmasq_conf.write_text("dns\n", encoding="utf-8")
    ca = write_ca(config)
    platform.trusted.add(ca)
    platform.nss_entries_db[(platform.nss_db, "任意昵称")] = (ca, "C,,")
    seed_state(paths)

    plan = build_plan(
        platform, paths, config, purge=False, remove_ca=False, targets=targets
    )
    executed = commands(plan)
    assert executed[0] == ["kill-pid-group", "4242", "start-1"]
    assert executed[1:3] == [
        ["remove-service", "caddy"],
        ["remove-service", "dnsmasq"],
    ]
    assert ["systemctl", "restart", "systemd-resolved"] in executed
    assert ["systemctl", "restart", "dnsmasq"] in executed
    assert any(c[:1] == ["remove-nss-ca"] for c in executed)
    assert any(c[:1] == ["remove-linux-system-ca"] for c in executed)

    # 顺序：进程 → 服务 → DNS → CA → 状态文件
    def index_of(predicate):
        return next(i for i, action in enumerate(plan.actions) if predicate(action))

    assert index_of(lambda a: a.command[:1] == ["kill-pid-group"]) < index_of(
        lambda a: a.command[:1] == ["remove-service"]
    )
    assert index_of(lambda a: a.command[:1] == ["remove-service"]) < index_of(
        lambda a: a.command == ["systemctl", "restart", "systemd-resolved"]
    )
    assert index_of(
        lambda a: a.command == ["systemctl", "restart", "systemd-resolved"]
    ) < index_of(lambda a: a.command[:1] == ["remove-nss-ca"])
    assert index_of(lambda a: a.command[:1] == ["remove-nss-ca"]) < index_of(
        lambda a: a.path == paths.caddyfile
    )


def test_plan_skips_processes_without_proc_start(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    write_routes(paths, [route("demo", pid=4242)])
    plan = build_plan(
        platform,
        paths,
        config,
        purge=False,
        remove_ca=False,
        targets=make_targets(tmp_path),
    )
    assert not any(c[:1] == ["kill-pid-group"] for c in commands(plan))
    assert any("proc_start" in note for note in plan.notes)


def test_plan_dns_linux_removes_both_dropins(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    targets = make_targets(tmp_path)
    for path in (targets.resolved_conf, targets.system_dnsmasq_conf):
        path.parent.mkdir(parents=True)
        path.write_text("dns\n", encoding="utf-8")

    plan = build_plan(
        platform, paths, config, purge=False, remove_ca=False, targets=targets
    )
    assert targets.resolved_conf in delete_paths(plan)
    assert targets.system_dnsmasq_conf in delete_paths(plan)
    assert ["systemctl", "restart", "systemd-resolved"] in commands(plan)
    assert ["systemctl", "restart", "dnsmasq"] in commands(plan)


def test_plan_dns_macos_removes_resolver_without_restart(paths, tmp_path, monkeypatch):
    platform = FakePlatform()
    monkeypatch.setattr(platform, "name", "mac")
    config = make_config(paths)
    targets = make_targets(tmp_path)
    resolver = targets.resolver_dir / config.domain_suffix
    resolver.parent.mkdir(parents=True)
    resolver.write_text("nameserver 127.0.0.1\nport 5300\n", encoding="utf-8")

    plan = build_plan(
        platform, paths, config, purge=False, remove_ca=False, targets=targets
    )
    assert resolver in delete_paths(plan)
    assert not any("systemctl" in c for c in commands(plan))


def test_plan_dnsmasq_restores_latest_backup(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    targets = make_targets(tmp_path)
    targets.dnsmasq_conf.parent.mkdir(parents=True)
    targets.dnsmasq_conf.write_text("merged\n", encoding="utf-8")
    paths.backup_dir.mkdir(parents=True)
    old = paths.backup_dir / "dnsmasq.conf.20240101T000000Z.bak"
    new = paths.backup_dir / "dnsmasq.conf.20240101T000000Z.1.bak"
    old.write_text("old\n", encoding="utf-8")
    new.write_text("original\n", encoding="utf-8")
    assert latest_dnsmasq_backup(paths.backup_dir) == new

    plan = build_plan(
        platform, paths, config, purge=False, remove_ca=False, targets=targets
    )
    writes = [a for a in plan.actions if a.kind == "write"]
    assert len(writes) == 1
    assert writes[0].path == targets.dnsmasq_conf
    assert writes[0].content == "original\n"


def test_plan_dnsmasq_without_backup_keeps_file(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    targets = make_targets(tmp_path)
    targets.dnsmasq_conf.parent.mkdir(parents=True)
    targets.dnsmasq_conf.write_text("merged\n", encoding="utf-8")

    plan = build_plan(
        platform, paths, config, purge=False, remove_ca=False, targets=targets
    )
    assert not any(a.kind == "write" for a in plan.actions)
    assert any(str(targets.dnsmasq_conf) in item for item in plan.kept)


# ---------------------------------------------------------------------------
# CA 的两条规则
# ---------------------------------------------------------------------------


def test_plan_keeps_migrated_ca_by_default(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    targets = make_targets(tmp_path)
    write_ca(config)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.caddy_storage.write_text("{}", encoding="utf-8")

    plan = build_plan(
        platform, paths, config, purge=False, remove_ca=False, targets=targets
    )
    assert not any(
        c[:1] in (["remove-nss-ca"], ["remove-linux-system-ca"], ["remove-macos-ca"])
        for c in commands(plan)
    )
    assert any("storage 留档" in note for note in plan.notes)
    assert any("CA 根证书" in item for item in plan.kept)


def test_plan_remove_ca_forces_migrated_ca_removal(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    targets = make_targets(tmp_path)
    write_ca(config)
    platform.trusted.add(write_ca(config))
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.caddy_storage.write_text("{}", encoding="utf-8")

    plan = build_plan(
        platform, paths, config, purge=False, remove_ca=True, targets=targets
    )
    assert any(c[:1] == ["remove-linux-system-ca"] for c in commands(plan))
    assert any("--remove-ca" in note for note in plan.notes)


def test_plan_ca_matches_fingerprint_not_nickname(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    targets = make_targets(tmp_path)
    platform.nss_db = targets.nss_dir
    ca = write_ca(config, "our-ca")
    other = tmp_path / "other.crt"
    other.write_text(cert_pem("someone-else"), encoding="utf-8")
    platform.nss_entries_db[(platform.nss_db, "devproxy Local CA")] = (other, "C,,")
    platform.nss_entries_db[(platform.nss_db, "用户自己的别名")] = (ca, "C,,")

    plan = build_plan(
        platform, paths, config, purge=False, remove_ca=False, targets=targets
    )
    nss_actions = [c for c in commands(plan) if c[:1] == ["remove-nss-ca"]]
    assert nss_actions == [
        ["remove-nss-ca", str(ca), str(targets.nss_dir), "用户自己的别名"]
    ]

    results = apply_plan(plan)
    assert all(result.ok for result in results), [r for r in results if not r.ok]
    assert (platform.nss_db, "devproxy Local CA") in platform.nss_entries_db
    assert (platform.nss_db, "用户自己的别名") not in platform.nss_entries_db


def test_apply_system_ca_only_removes_matching_fingerprint(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    targets = make_targets(tmp_path)
    ca = write_ca(config, "our-ca")
    platform.trusted.add(ca)
    targets.system_ca_dest.parent.mkdir(parents=True)
    targets.system_ca_dest.write_text(cert_pem("someone-else"), encoding="utf-8")

    plan = build_plan(
        platform, paths, config, purge=False, remove_ca=False, targets=targets
    )
    results = apply_plan(plan)
    assert all(result.ok for result in results), [r for r in results if not r.ok]
    assert targets.system_ca_dest.exists()
    assert not any(c[:1] == ["rm"] for c in platform.commands)
    assert any("指纹不匹配" in result.detail for result in results)


def test_macos_plan_removes_ca_by_sha1_fingerprint(paths, tmp_path, monkeypatch):
    import hashlib
    import ssl

    platform = FakePlatform()
    monkeypatch.setattr(platform, "name", "mac")
    config = make_config(paths)
    targets = make_targets(tmp_path)
    ca = write_ca(config)
    platform.trusted.add(ca)

    plan = build_plan(
        platform, paths, config, purge=False, remove_ca=False, targets=targets
    )
    mac_actions = [c for c in commands(plan) if c[:1] == ["remove-macos-ca"]]
    assert mac_actions == [
        ["remove-macos-ca", str(ca), targets.system_keychain]
    ]
    assert not any(c[:1] == ["remove-nss-ca"] for c in commands(plan))

    results = apply_plan(plan)
    assert all(result.ok for result in results), [r for r in results if not r.ok]
    security = next(c for c in platform.commands if c[:1] == ["security"])
    der = ssl.PEM_cert_to_DER_cert(ca.read_text(encoding="utf-8"))
    assert security == [
        "security",
        "delete-certificate",
        "-Z",
        hashlib.sha1(der).hexdigest().upper(),
        "-t",
        targets.system_keychain,
    ]


def test_macos_ca_not_found_is_idempotent(paths, tmp_path, monkeypatch):
    platform = FakePlatform()
    config = make_config(paths)
    ca = write_ca(config)
    monkeypatch.setattr(
        platform.privilege(),
        "run",
        lambda argv: (1, "security: The specified item could not be found."),
    )
    detail = uninstall_mod._remove_macos_ca(
        platform, ca, str(tmp_path / "System.keychain")
    )
    assert "未找到" in detail


def test_apply_system_ca_removes_matching_fingerprint(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    targets = make_targets(tmp_path)
    ca = write_ca(config, "our-ca")
    platform.trusted.add(ca)
    targets.system_ca_dest.parent.mkdir(parents=True)
    targets.system_ca_dest.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    plan = build_plan(
        platform, paths, config, purge=False, remove_ca=False, targets=targets
    )
    results = apply_plan(plan)
    assert all(result.ok for result in results), [r for r in results if not r.ok]
    assert not targets.system_ca_dest.exists()
    assert ["rm", "-f", "--", str(targets.system_ca_dest)] in platform.commands
    assert ["update-ca-certificates"] in platform.commands


# ---------------------------------------------------------------------------
# --purge / 保留项 / dry-run
# ---------------------------------------------------------------------------


def test_plan_purge_deletes_storage_backups_and_binary(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    config.storage_path.mkdir(parents=True)
    config.caddy_bin_path.parent.mkdir(parents=True)
    config.caddy_bin_path.write_text("binary", encoding="utf-8")
    paths.backup_dir.mkdir(parents=True)
    (paths.backup_dir / "dnsmasq.conf.20240101T000000Z.bak").write_text(
        "x", encoding="utf-8"
    )

    plan = build_plan(
        platform,
        paths,
        config,
        purge=True,
        remove_ca=False,
        targets=make_targets(tmp_path),
    )
    deleted = delete_paths(plan)
    assert config.storage_path in deleted
    assert paths.backup_dir in deleted
    assert config.caddy_bin_path in deleted


def test_plan_default_keeps_user_assets(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    plan = build_plan(
        platform,
        paths,
        config,
        purge=False,
        remove_ca=False,
        targets=make_targets(tmp_path),
    )
    kept = "\n".join(plan.kept)
    assert str(paths.sites_caddy) in kept
    assert str(paths.config_toml) in kept
    assert str(config.storage_path) in kept
    assert str(config.caddy_bin_path) in kept
    assert str(paths.backup_dir) in kept
    assert "enable-linger" in kept
    assert str(config.storage_path) not in {str(p) for p in delete_paths(plan)}


def test_plan_dry_run_never_probes_or_runs_commands(paths, tmp_path, monkeypatch):
    platform = FakePlatform()
    config = make_config(paths)
    targets = make_targets(tmp_path)
    seed_service(platform, "caddy")
    targets.resolved_conf.parent.mkdir(parents=True)
    targets.resolved_conf.write_text("dns\n", encoding="utf-8")
    write_ca(config)
    seed_state(paths)

    def boom(*args, **kwargs):
        raise AssertionError("dry-run 不允许起子进程 / 探测服务")

    monkeypatch.setattr(platform, "run", boom)
    monkeypatch.setattr(platform, "service_active", boom)
    monkeypatch.setattr(platform, "ca_trusted", boom)
    monkeypatch.setattr(platform, "nss_entries", boom)
    monkeypatch.setattr(platform, "privilege", boom)

    plan = build_plan(
        platform,
        paths,
        config,
        purge=False,
        remove_ca=False,
        dry_run=True,
        targets=targets,
    )
    assert plan.actions
    assert platform.commands == []


def test_plan_migration_hint_points_at_backup(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    backup = paths.backup_dir / "2024-01-01T00-00-00Z"
    backup.mkdir(parents=True)
    (backup / "manifest.json").write_text("{}", encoding="utf-8")
    assert latest_migration_backup(paths.backup_dir) == backup

    plan = build_plan(
        platform,
        paths,
        config,
        purge=False,
        remove_ca=False,
        targets=make_targets(tmp_path),
    )
    assert plan.migration_backup == backup


def test_plan_purge_warns_migration_backup_deleted(paths, tmp_path):
    platform = FakePlatform()
    config = make_config(paths)
    backup = paths.backup_dir / "2024-01-01T00-00-00Z"
    backup.mkdir(parents=True)
    (backup / "manifest.json").write_text("{}", encoding="utf-8")

    plan = build_plan(
        platform,
        paths,
        config,
        purge=True,
        remove_ca=False,
        targets=make_targets(tmp_path),
    )
    assert plan.migration_backup is None
    assert any("无法再用" in note for note in plan.notes)


# ---------------------------------------------------------------------------
# apply_plan：真实文件操作与失败语义
# ---------------------------------------------------------------------------


def _full_plan(paths, tmp_path, platform, monkeypatch):
    config = make_config(paths)
    targets = make_targets(tmp_path)
    platform.nss_db = targets.nss_dir
    write_routes(paths, [route("demo", pid=4242, proc_start="start-1")])
    seed_service(platform, "caddy")
    seed_service(platform, "dnsmasq")
    targets.resolved_conf.parent.mkdir(parents=True)
    targets.resolved_conf.write_text("dns\n", encoding="utf-8")
    targets.dnsmasq_conf.parent.mkdir(parents=True)
    targets.dnsmasq_conf.write_text("merged\n", encoding="utf-8")
    paths.backup_dir.mkdir(parents=True)
    (paths.backup_dir / "dnsmasq.conf.20240101T000000Z.bak").write_text(
        "original\n", encoding="utf-8"
    )
    ca = write_ca(config)
    platform.trusted.add(ca)
    platform.nss_entries_db[(platform.nss_db, "devproxy Local CA")] = (ca, "C,,")
    targets.system_ca_dest.parent.mkdir(parents=True)
    targets.system_ca_dest.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")
    seed_state(paths)

    killed: list[tuple[int, str | None]] = []
    monkeypatch.setattr(
        uninstall_mod,
        "kill_pid_group",
        lambda pid, proc_start=None: killed.append((pid, proc_start)) or True,
    )
    plan = build_plan(
        platform, paths, config, purge=False, remove_ca=False, targets=targets
    )
    return plan, config, targets, ca, killed


def test_apply_full_flow_removes_tool_state_and_keeps_user_files(
    paths, tmp_path, monkeypatch
):
    platform = FakePlatform()
    plan, config, targets, ca, killed = _full_plan(
        paths, tmp_path, platform, monkeypatch
    )
    results = apply_plan(plan)
    assert all(result.ok for result in results), [r for r in results if not r.ok]
    assert killed == [(4242, "start-1")]

    # 服务
    assert "caddy" not in platform.active and "dnsmasq" not in platform.active
    assert not platform.service_unit_path("caddy").exists()
    assert not platform.service_unit_path("dnsmasq").exists()

    # DNS 路由与 dnsmasq 配置
    assert not targets.resolved_conf.exists()
    assert targets.dnsmasq_conf.read_text(encoding="utf-8") == "original\n"

    # CA：NSS 条目与系统信任库文件按指纹移除
    assert (platform.nss_db, "devproxy Local CA") not in platform.nss_entries_db
    assert not targets.system_ca_dest.exists()

    # 工具状态文件删除，用户资产保留
    for gone in (
        paths.caddyfile,
        paths.routes_json,
        paths.routes_caddy,
        paths.lock_path,
        paths.state_dir / "dns-route.json",
        paths.caddy_env,
        paths.log_dir,
    ):
        assert not gone.exists(), gone
    assert paths.sites_caddy.exists()
    assert paths.config_toml.exists()
    assert config.storage_path.exists()  # 非 --purge 保留 Caddy 存储


def test_apply_continues_after_one_step_fails(paths, tmp_path, monkeypatch):
    platform = FakePlatform()
    plan, config, targets, ca, killed = _full_plan(
        paths, tmp_path, platform, monkeypatch
    )

    def boom(name):
        raise RuntimeError("service boom")

    monkeypatch.setattr(platform, "remove_service", boom)
    results = apply_plan(plan)
    assert any(not result.ok for result in results)
    assert any("service boom" in result.detail for result in results if not result.ok)
    # 单步失败不阻断后续：状态文件仍被删掉
    assert not paths.caddyfile.exists()


def test_apply_is_idempotent_second_plan_is_noop(paths, tmp_path, monkeypatch):
    platform = FakePlatform()
    plan, config, targets, ca, _killed = _full_plan(
        paths, tmp_path, platform, monkeypatch
    )
    results = apply_plan(plan)
    assert all(result.ok for result in results)

    # 模拟系统信任库真的不再包含该 CA（假平台不解析 update-ca-certificates）
    platform.trusted.discard(ca)
    second = build_plan(
        platform, paths, config, purge=False, remove_ca=False, targets=targets
    )
    assert second.is_noop, [a.description for a in second.actions]
    assert apply_plan(second) == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _patch_cli(monkeypatch, paths, config, targets, platform):
    from devproxy.lifecycle.uninstall import build_plan as real_build_plan

    def build(platform_, paths_, config_, **kwargs):
        return real_build_plan(platform_, paths_, config_, targets=targets, **kwargs)

    monkeypatch.setattr("devproxy.cli._shared.build_uninstall_plan", build)
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *args, **kwargs: (paths, config, None, None),
    )
    return platform


def test_cli_unknown_option_exits_2():
    result = CliRunner().invoke(app, ["uninstall", "--bogus"])
    assert result.exit_code == 2, result.output


def test_cli_uninstall_help_lists_options():
    result = CliRunner().invoke(app, ["uninstall", "--help"])
    assert result.exit_code == 0, result.output
    for option in ("--dry-run", "--yes", "--purge", "--remove-ca"):
        assert option in result.output

    top = CliRunner().invoke(app, ["--help"])
    assert top.exit_code == 0, top.output
    assert "uninstall" in top.output


def test_cli_dry_run_has_no_side_effects(
    paths, tmp_path, monkeypatch, isolated_cli_platform
):
    config = make_config(paths)
    targets = make_targets(tmp_path)
    seed_service(isolated_cli_platform, "caddy")
    seed_state(paths)
    _patch_cli(monkeypatch, paths, config, targets, isolated_cli_platform)

    result = CliRunner().invoke(app, ["uninstall", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "预览" in result.output
    assert "以下内容将保留（不会删除）" in result.output
    assert paths.caddyfile.exists()
    assert isolated_cli_platform.service_unit_path("caddy").exists()
    assert isolated_cli_platform.commands == []


def test_cli_refuses_noninteractive_without_yes(
    paths, tmp_path, monkeypatch, isolated_cli_platform
):
    config = make_config(paths)
    targets = make_targets(tmp_path)
    seed_service(isolated_cli_platform, "caddy")
    seed_state(paths)
    _patch_cli(monkeypatch, paths, config, targets, isolated_cli_platform)
    monkeypatch.setattr("devproxy.cli._shared._interactive", lambda: False)

    result = CliRunner().invoke(app, ["uninstall"])
    assert result.exit_code == 4, result.output
    assert "非交互环境" in result.output
    assert paths.caddyfile.exists()
    assert not isolated_cli_platform.commands


def test_cli_declined_confirmation_exits_4(
    paths, tmp_path, monkeypatch, isolated_cli_platform
):
    config = make_config(paths)
    targets = make_targets(tmp_path)
    seed_service(isolated_cli_platform, "caddy")
    seed_state(paths)
    _patch_cli(monkeypatch, paths, config, targets, isolated_cli_platform)
    monkeypatch.setattr("devproxy.cli._shared._interactive", lambda: True)
    monkeypatch.setattr("devproxy.cli._shared.typer.confirm", lambda *a, **k: False)

    result = CliRunner().invoke(app, ["uninstall"])
    assert result.exit_code == 4, result.output
    assert "已取消" in result.output
    assert paths.caddyfile.exists()
    assert not isolated_cli_platform.commands


def test_cli_yes_uninstalls_and_prints_kept(
    paths, tmp_path, monkeypatch, isolated_cli_platform
):
    config = make_config(paths)
    targets = make_targets(tmp_path)
    seed_service(isolated_cli_platform, "caddy")
    seed_state(paths)
    _patch_cli(monkeypatch, paths, config, targets, isolated_cli_platform)

    result = CliRunner().invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert "✔" in result.output
    assert "以下内容已保留" in result.output
    assert not paths.caddyfile.exists()
    assert not isolated_cli_platform.service_unit_path("caddy").exists()
    assert paths.sites_caddy.exists()
    assert paths.config_toml.exists()


def test_cli_second_run_says_nothing_to_uninstall(
    paths, tmp_path, monkeypatch, isolated_cli_platform
):
    config = make_config(paths)
    targets = make_targets(tmp_path)
    seed_service(isolated_cli_platform, "caddy")
    seed_state(paths)
    _patch_cli(monkeypatch, paths, config, targets, isolated_cli_platform)

    first = CliRunner().invoke(app, ["uninstall", "--yes"])
    assert first.exit_code == 0, first.output

    isolated_cli_platform.commands.clear()
    second = CliRunner().invoke(app, ["uninstall", "--yes"])
    assert second.exit_code == 0, second.output
    assert "没有可卸载的东西" in second.output
    assert not isolated_cli_platform.commands


def test_cli_failure_exits_1(
    paths, tmp_path, monkeypatch, isolated_cli_platform
):
    config = make_config(paths)
    targets = make_targets(tmp_path)
    seed_service(isolated_cli_platform, "caddy")
    _patch_cli(monkeypatch, paths, config, targets, isolated_cli_platform)

    def boom(name):
        raise RuntimeError("service boom")

    monkeypatch.setattr(isolated_cli_platform, "remove_service", boom)
    result = CliRunner().invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 1, result.output
    assert "✗" in result.output
    assert "service boom" in result.output


def test_cli_purge_deletes_storage_backups_and_binary(
    paths, tmp_path, monkeypatch, isolated_cli_platform
):
    config = make_config(paths)
    targets = make_targets(tmp_path)
    config.storage_path.mkdir(parents=True)
    (config.storage_path / "certificates").mkdir()
    config.caddy_bin_path.parent.mkdir(parents=True)
    config.caddy_bin_path.write_text("binary", encoding="utf-8")
    paths.backup_dir.mkdir(parents=True)
    (paths.backup_dir / "dnsmasq.conf.20240101T000000Z.bak").write_text(
        "x", encoding="utf-8"
    )
    _patch_cli(monkeypatch, paths, config, targets, isolated_cli_platform)

    result = CliRunner().invoke(app, ["uninstall", "--yes", "--purge"])
    assert result.exit_code == 0, result.output
    assert not config.storage_path.exists()
    assert not paths.backup_dir.exists()
    assert not config.caddy_bin_path.exists()
