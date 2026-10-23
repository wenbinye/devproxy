"""迁移流程的测试。

铁律：测试里绝不碰真实系统 —— 所有路径都指向 tmp_path，平台一律用 FakePlatform。
"""

import json
from pathlib import Path

import pytest

from devproxy.core.backup import load_backup
from devproxy.core.config import Config
from devproxy.lifecycle.migrate import (
    LegacyState,
    MigrationError,
    default_dns_route,
    detect_legacy,
    legacy_storage_path,
    migrate,
    resolve_storage,
    rollback,
    split_caddyfile,
    unit_config_path,
    unit_env,
)
from devproxy.core.paths import Paths
from devproxy.platform.fake import FakePlatform
from tests.support import ElevatedReader, UnreadablePath


class _UnreadableUnitPlatform(FakePlatform):
    """单元文件普通用户读不了（macOS system scope 的 0600 root 单元）。"""

    def service_unit_path(self, name):
        return UnreadablePath(str(super().service_unit_path(name)))

    def privilege(self):
        return ElevatedReader(self)


def _paths(tmp_path):
    return Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")


def _legacy_state(tmp_path, unit_text=None, caddyfile_text=None):
    """造一套遗留环境：单元 + 旧 Caddyfile，全在 tmp_path 下。"""
    unit_path = tmp_path / "caddy.service"
    unit_path.write_text(
        unit_text
        or (
            "[Service]\n"
            "Environment=MINIKUBE_IP=1.2.3.4\n"
            "ExecStart=caddy run --config /old/Caddyfile\n"
        ),
        encoding="utf-8",
    )
    old_cfg = tmp_path / "old" / "Caddyfile"
    old_cfg.parent.mkdir(parents=True, exist_ok=True)
    old_cfg.write_text(
        caddyfile_text if caddyfile_text is not None else "localhost {\n\ttls internal\n}\n",
        encoding="utf-8",
    )
    return LegacyState(unit_path=unit_path, config_path=old_cfg, env={"MINIKUBE_IP": "1.2.3.4"})


def _migrate(platform, paths, config, state, tmp_path, notes=None):
    """迁移，但把 dnsmasq 配置与 DNS 路由文件指向 tmp_path（绝不碰真实系统）。"""
    return migrate(
        platform,
        paths,
        config,
        state,
        dnsmasq_conf=tmp_path / "dnsmasq.conf",
        dns_route=tmp_path / "resolved.conf",
        notes=notes,
    )


# -- Caddyfile 拆分 ------------------------------------------------------------


def test_split_keeps_personal_sites_and_drops_test_blocks():
    legacy = (
        "{\n"
        "\tstorage file_system /data\n"
        "}\n"
        "\n"
        "http://localhost {\n"
        "\troot * /site\n"
        "\tfile_server\n"
        "}\n"
        "\n"
        "*.local.yikuaigao.com {\n"
        "\ttls internal\n"
        "\treverse_proxy 1.2.3.4:30800\n"
        "}\n"
        "\n"
        "*.test {\n"
        "\ttls {\n"
        "\t\ton_demand\n"
        "\t\tissuer internal\n"
        "\t}\n"
        "\trespond \"nope\" 404\n"
        "}\n"
        "\n"
        "http://*.test {\n"
        "\tredir https://{host}{uri} permanent\n"
        "}\n"
    )
    personal, dropped = split_caddyfile(legacy)
    assert "http://localhost" in personal
    assert "*.local.yikuaigao.com" in personal
    assert "*.test" not in personal
    assert len(dropped) == 2
    assert all(".test" in d for d in dropped)


def test_split_preserves_nested_braces():
    legacy = "*.test {\n\ttls {\n\t\ton_demand\n\t}\n}\n\nlocalhost {\n\ttls internal\n}\n"
    personal, dropped = split_caddyfile(legacy)
    assert "localhost" in personal
    assert len(dropped) == 1


def test_split_drops_global_block():
    """全局块不能进 sites.caddy —— 导入文件里出现第二个全局块，Caddy 直接报错。"""
    legacy = "{\n\tstorage file_system /data\n}\n\nlocalhost {\n\ttls internal\n}\n"
    personal, dropped = split_caddyfile(legacy)
    assert "storage" not in personal
    assert dropped == []


def test_split_keeps_comments_with_their_site():
    """站点块前面的注释属于用户自己的文档 —— 不能在拆分时丢掉。

    真实用户的 Caddyfile 里每个站点前面都有说明；按"首行是不是注释"
    判块的话，*.test 块会被当成个人站点留下来，与工具生成的站点冲突。
    """
    legacy = (
        "# 本地网关\n"
        "*.local.yikuaigao.com {\n"
        "\ttls internal\n"
        "\treverse_proxy {$MINIKUBE_IP:192.168.49.2}:30800\n"
        "}\n"
        "\n"
        "# 动态路由（由 devp 管理）\n"
        "*.test {\n"
        "\ttls {\n"
        "\t\ton_demand\n"
        "\t}\n"
        "\trespond \"nope\" 404\n"
        "}\n"
    )
    personal, dropped = split_caddyfile(legacy)
    assert "*.local.yikuaigao.com" in personal
    assert "# 本地网关" in personal
    assert "*.test" not in personal
    assert len(dropped) == 1


def test_split_ignores_braces_inside_comments():
    legacy = "# 注释里有 { 和 } 也不能影响切分\nlocalhost {\n\ttls internal\n}\n"
    personal, dropped = split_caddyfile(legacy)
    assert "localhost" in personal
    assert dropped == []


# -- 单元解析 -----------------------------------------------------------------


def test_unit_config_path_parsed():
    unit = "[Service]\nExecStart=%h/.local/bin/caddy run --config %h/work/x/Caddyfile\n"
    p = unit_config_path(unit, Path("/home/u"))
    assert p == Path("/home/u/work/x/Caddyfile")


def test_unit_config_path_none_when_absent():
    assert unit_config_path("[Service]\nExecStart=/usr/bin/foo\n", Path("/home/u")) is None


def test_unit_env_extracted():
    unit = "[Service]\nEnvironment=MINIKUBE_IP=192.168.49.2\nEnvironment=FOO=bar\n"
    assert unit_env(unit) == {"MINIKUBE_IP": "192.168.49.2", "FOO": "bar"}


def test_unit_env_ignores_comments():
    unit = "# Environment=NOT_SET=1\n[Service]\nEnvironment=REAL=1\n"
    assert unit_env(unit) == {"REAL": "1"}


# -- 遗留检测 -----------------------------------------------------------------


def test_detect_legacy_returns_none_without_unit(tmp_path):
    paths = _paths(tmp_path)
    platform = FakePlatform()
    assert detect_legacy(platform, paths) is None


def test_detect_legacy_finds_existing_unit(tmp_path):
    paths = _paths(tmp_path)
    platform = FakePlatform()
    unit_path = tmp_path / "caddy.service"
    unit_path.write_text(
        "[Service]\nExecStart=%h/.local/bin/caddy run --config %h/old/Caddyfile\n"
        "Environment=MINIKUBE_IP=1.2.3.4\n"
    )
    state = detect_legacy(platform, paths, unit_dir=tmp_path, home=tmp_path)
    assert state is not None
    assert state.unit_path == unit_path
    assert state.config_path == tmp_path / "old" / "Caddyfile"
    assert state.env == {"MINIKUBE_IP": "1.2.3.4"}


def test_detect_legacy_none_when_unit_already_managed(tmp_path):
    """单元已经指向工具的 Caddyfile —— 不是遗留环境，不能再迁一遍。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    unit_path = tmp_path / "caddy.service"
    unit_path.write_text(
        f"[Service]\nExecStart=/bin/caddy run --config {paths.caddyfile}\n", encoding="utf-8"
    )
    assert detect_legacy(platform, paths, unit_dir=tmp_path, home=tmp_path) is None


def test_detect_legacy_none_when_unit_has_no_config(tmp_path):
    paths = _paths(tmp_path)
    platform = FakePlatform()
    (tmp_path / "caddy.service").write_text("[Service]\nExecStart=/usr/bin/foo\n")
    assert detect_legacy(platform, paths, unit_dir=tmp_path, home=tmp_path) is None


def test_detect_legacy_reads_unreadable_unit_through_elevation(tmp_path):
    """单元普通用户读不了 —— 必须走提权会话去读。

    macOS 的 system scope 单元在 /Library/LaunchDaemons，权限或归属不对时
    普通用户读不了。读不到就当"没有遗留环境"，等于把用户手工维护的
    Caddyfile 与环境变量悄悄接管掉 —— 那是迁移最不能犯的错。
    """
    paths = _paths(tmp_path)
    platform = _UnreadableUnitPlatform()
    unit_path = Path(str(platform.service_unit_path("caddy")))
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(
        "[Service]\nExecStart=/bin/caddy run --config /old/Caddyfile\n"
        "Environment=MINIKUBE_IP=1.2.3.4\n",
        encoding="utf-8",
    )
    state = detect_legacy(platform, paths, home=tmp_path)
    assert state is not None, "读不到单元不能当成没有遗留环境"
    assert state.env == {"MINIKUBE_IP": "1.2.3.4"}


# -- 迁移 ---------------------------------------------------------------------


def test_migrate_backs_up_before_writing(tmp_path):
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    unit_path = tmp_path / "caddy.service"
    unit_path.write_text("[Service]\nExecStart=caddy run --config /old/Caddyfile\n")
    old_cfg = tmp_path / "old" / "Caddyfile"
    old_cfg.parent.mkdir(parents=True)
    old_cfg.write_text("localhost {\n\ttls internal\n}\n")

    state = LegacyState(
        unit_path=unit_path, config_path=old_cfg, env={"MINIKUBE_IP": "1.2.3.4"}
    )
    backup = _migrate(platform, paths, Config(), state, tmp_path)
    assert backup.entries, "迁移必须产生备份"
    assert (backup.directory / "MANIFEST.txt").exists()
    assert (backup.directory / "manifest.json").exists()
    # 原文件仍在备份里
    assert any(e.original == unit_path and e.existed for e in backup.entries)


def test_migrate_backup_covers_every_file_to_be_touched(tmp_path):
    """每一个将被改动的既有文件都必须在备份里 —— 否则回滚留残留。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    dnsmasq_conf = tmp_path / "dnsmasq.conf"
    dnsmasq_conf.write_text("port=5353\n", encoding="utf-8")
    backup = migrate(
        platform,
        paths,
        Config(),
        state,
        dnsmasq_conf=dnsmasq_conf,
        dns_route=tmp_path / "resolved.conf",
    )
    originals = {e.original for e in backup.entries}
    assert state.unit_path in originals, "单元会被改写"
    assert paths.sites_caddy in originals, "个人站点会被写入"
    assert paths.caddyfile in originals, "主 Caddyfile 会被生成"
    assert dnsmasq_conf in originals, "dnsmasq 配置会被合并改写"
    assert tmp_path / "resolved.conf" in originals, "DNS 路由文件会被改写"


def test_migrate_writes_sites_caddy_from_legacy(tmp_path):
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    old_cfg = tmp_path / "old" / "Caddyfile"
    old_cfg.parent.mkdir(parents=True)
    old_cfg.write_text("localhost {\n\ttls internal\n}\n\n*.test {\n\trespond 404\n}\n")

    state = LegacyState(unit_path=tmp_path / "caddy.service", config_path=old_cfg, env={})
    _migrate(platform, paths, Config(), state, tmp_path)
    assert "localhost" in paths.sites_caddy.read_text()
    assert "*.test" not in paths.sites_caddy.read_text()


def test_migrate_does_not_overwrite_existing_sites_caddy(tmp_path):
    """工具只 import sites.caddy —— 用户自己写的内容一个字都不能动。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    paths.sites_caddy.write_text("mine.example { respond \"hi\" }\n", encoding="utf-8")
    platform = FakePlatform()
    state = _legacy_state(
        tmp_path, caddyfile_text="localhost {\n\ttls internal\n}\n"
    )
    notes: list[str] = []
    migrate(
        platform,
        paths,
        Config(),
        state,
        dnsmasq_conf=tmp_path / "dnsmasq.conf",
        dns_route=tmp_path / "resolved.conf",
        notes=notes,
    )
    assert paths.sites_caddy.read_text() == "mine.example { respond \"hi\" }\n"
    assert any("sites.caddy" in n for n in notes), "必须告诉用户个人站点没被迁走"


def test_migrate_states_that_personal_sites_are_no_longer_served(tmp_path):
    """迁移后旧 Caddyfile 不再被 import —— 里面的站点就此不再被服务。

    这件事必须明说（点名文件与地址）：只留一句"未覆盖"会让人以为
    个人站点还好好的，实际上它们已经 404 了。
    """
    paths = _paths(tmp_path)
    paths.ensure_files()
    paths.sites_caddy.write_text("mine.example { respond \"hi\" }\n", encoding="utf-8")
    platform = FakePlatform()
    state = _legacy_state(
        tmp_path,
        caddyfile_text=(
            "localhost {\n\ttls internal\n}\n"
            "\n"
            "api.example.com {\n\treverse_proxy 1.2.3.4:80\n}\n"
        ),
    )
    notes: list[str] = []
    _migrate(platform, paths, Config(), state, tmp_path, notes=notes)
    joined = "\n".join(notes)
    assert "不再被服务" in joined, f"必须明说站点不再被服务：{notes}"
    assert "localhost" in joined and "api.example.com" in joined, "必须点名涉及的地址"
    assert str(state.config_path) in joined, "必须点名要从哪个文件合并"
    assert str(paths.sites_caddy) in joined, "必须点名要合并到哪个文件"


def test_migrate_unit_falls_back_to_legacy_binary_when_config_binary_missing(tmp_path):
    """二进制还没下载时，单元不能指向一个不存在的文件。

    迁移发生在组件流程（下载）之前 —— 照抄 config 里的路径会让正在跑的
    Caddy 被改坏（单元指向不存在的二进制），而且没有东西能把它拉起来。
    """
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    legacy_bin = tmp_path / "legacy" / "caddy"
    legacy_bin.parent.mkdir(parents=True, exist_ok=True)
    legacy_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    state = _legacy_state(
        tmp_path,
        unit_text=(
            "[Service]\n"
            f"ExecStart={legacy_bin} run --config /old/Caddyfile\n"
        ),
    )
    notes: list[str] = []
    _migrate(
        platform,
        paths,
        Config(caddy_bin=str(tmp_path / "not-downloaded" / "caddy")),
        state,
        tmp_path,
        notes=notes,
    )
    unit = platform.services["caddy"]
    assert f"ExecStart={legacy_bin} run" in unit, unit
    assert any("沿用遗留" in n for n in notes), f"必须说明用了哪个二进制：{notes}"


def test_migrate_preserves_unit_env(tmp_path):
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    old_cfg = tmp_path / "old" / "Caddyfile"
    old_cfg.parent.mkdir(parents=True)
    old_cfg.write_text("localhost {\n}\n")

    state = LegacyState(
        unit_path=tmp_path / "caddy.service", config_path=old_cfg, env={"MINIKUBE_IP": "9.9.9.9"}
    )
    _migrate(platform, paths, Config(), state, tmp_path)
    unit = platform.services["caddy"]
    assert "MINIKUBE_IP=9.9.9.9" in unit


def test_migrate_persists_unit_env_for_later_setup(tmp_path):
    """后续 setup 重装单元时也必须带上 MINIKUBE_IP —— 个人站点依赖它。"""
    from devproxy.components.caddy import CaddyComponent
    from devproxy.platform.base import Action

    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    _migrate(platform, paths, Config(), state, tmp_path)

    comp = CaddyComponent(platform, paths, Config())
    assert comp.extra_env == {"MINIKUBE_IP": "1.2.3.4"}
    platform.services.clear()
    comp.apply(
        [
            Action(
                description="装单元", kind="run", command=["install-service", "caddy"]
            )
        ]
    )
    assert "MINIKUBE_IP=1.2.3.4" in platform.services["caddy"]


def test_migrate_writes_tool_caddyfile_and_points_unit_at_it(tmp_path):
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    _migrate(platform, paths, Config(), state, tmp_path)
    caddyfile = paths.caddyfile.read_text(encoding="utf-8")
    assert f"import {paths.sites_caddy}" in caddyfile
    assert f"import {paths.routes_caddy}" in caddyfile
    assert "admin 127.0.0.1:2019" in caddyfile
    assert f"--config {paths.caddyfile}" in platform.services["caddy"]


def test_migrate_leaves_legacy_caddyfile_untouched(tmp_path):
    """旧 Caddyfile 只被备份，不被改写 —— 它是回滚时要还原的东西。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path, caddyfile_text="localhost {\n\ttls internal\n}\n")
    _migrate(platform, paths, Config(), state, tmp_path)
    assert state.config_path.read_text(encoding="utf-8") == "localhost {\n\ttls internal\n}\n"


def test_migrate_aborts_when_backup_target_is_not_a_regular_file(tmp_path):
    """备份失败 → 迁移中止，一个文件都不改。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    # 把旧 Caddyfile 换成符号链接 —— 备份器必须拒绝，而不是记成"原本不存在"
    state.config_path.unlink()
    state.config_path.symlink_to(tmp_path / "target.txt")
    with pytest.raises(MigrationError):
        _migrate(platform, paths, Config(), state, tmp_path)
    assert platform.services == {}, "备份失败时不得改写单元"
    assert paths.sites_caddy.read_text(encoding="utf-8") == ""


def test_migrate_restores_backup_when_rewrite_fails(tmp_path, monkeypatch):
    """改写中途失败 → 按备份还原，机器不能停在半迁移状态。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    original_unit = state.unit_path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("写单元失败")

    monkeypatch.setattr(platform, "install_service", boom)
    with pytest.raises(MigrationError):
        _migrate(platform, paths, Config(), state, tmp_path)
    assert state.unit_path.read_text(encoding="utf-8") == original_unit


# -- 回滚 ---------------------------------------------------------------------


def test_rollback_restores_unit_and_caddyfile(tmp_path):
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    unit_path = tmp_path / "caddy.service"
    unit_path.write_text("ORIGINAL UNIT\n")
    old_cfg = tmp_path / "old" / "Caddyfile"
    old_cfg.parent.mkdir(parents=True)
    old_cfg.write_text("ORIGINAL CADDYFILE\n")

    state = LegacyState(unit_path=unit_path, config_path=old_cfg, env={})
    backup = _migrate(platform, paths, Config(), state, tmp_path)
    unit_path.write_text("CLOBBERED\n")
    old_cfg.write_text("CLOBBERED\n")
    rollback(backup)
    assert unit_path.read_text() == "ORIGINAL UNIT\n"
    assert old_cfg.read_text() == "ORIGINAL CADDYFILE\n"


def test_rollback_removes_files_that_did_not_exist_before(tmp_path):
    """原本不存在 → 还原时删除 —— 工具造的文件不能留在机器上。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    backup = _migrate(platform, paths, Config(), state, tmp_path)
    # 主 Caddyfile 迁移前不存在，迁移后被生成
    assert paths.caddyfile.exists()
    rollback(backup)
    assert not paths.caddyfile.exists(), "工具生成的 Caddyfile 必须被删掉"


def test_rollback_restores_sites_caddy_to_empty(tmp_path):
    """sites.caddy 迁移前是空的（工具建的）→ 还原后不能留个人站点残留。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path, caddyfile_text="localhost {\n\ttls internal\n}\n")
    backup = _migrate(platform, paths, Config(), state, tmp_path)
    assert "localhost" in paths.sites_caddy.read_text(encoding="utf-8")
    rollback(backup)
    assert paths.sites_caddy.read_text(encoding="utf-8").strip() == ""


def test_rollback_from_loaded_backup(tmp_path):
    """回滚目录可以事后重新加载 —— manifest.json 是机器可读的。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    backup = _migrate(platform, paths, Config(), state, tmp_path)
    loaded = load_backup(backup.directory)
    state.unit_path.write_text("CLOBBERED\n")
    rollback(loaded)
    assert "ExecStart" in state.unit_path.read_text(encoding="utf-8")


# -- DNS 后端一致性（备份的必须是真正会被改动的那个文件）--------------------


def test_default_dns_route_follows_explicit_backend(tmp_path):
    """用户显式指定后端时，备份的 drop-in 必须跟着换。

    组件写 /etc/dnsmasq.d/devproxy.conf，备份的却是 resolved 那个文件 ——
    被改动的文件不在备份里，回滚就删不掉它。
    """
    paths = _paths(tmp_path)
    platform = FakePlatform()
    config = Config()
    system = default_dns_route(platform, paths, config, "system")
    resolved = default_dns_route(platform, paths, config, "resolved")
    assert system.as_posix().endswith("/etc/dnsmasq.d/devproxy.conf")
    assert resolved.as_posix().endswith("/etc/systemd/resolved.conf.d/devproxy.conf")
    assert system != resolved


# -- 配置校验：换单元之前必须让 caddy 自己验一遍 ------------------------------


def test_migrate_aborts_when_caddy_validate_fails(tmp_path, monkeypatch):
    """个人站点里有语法错误时，迁移必须中止 —— 单元一个字都不能改。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    original_unit = state.unit_path.read_text(encoding="utf-8")

    # 造一个"存在但校验不过"的 caddy 二进制
    binary = tmp_path / "caddy"
    binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    config = Config(caddy_bin=str(binary))

    def fake_run(argv):
        if "validate" in argv:
            return 1, "Caddyfile:3 - Error during parsing: unexpected token"
        return 0, ""

    monkeypatch.setattr(platform, "run", fake_run)

    with pytest.raises(MigrationError) as ei:
        migrate(
            platform,
            paths,
            config,
            state,
            dnsmasq_conf=tmp_path / "dnsmasq.conf",
            dns_route=tmp_path / "resolved.conf",
        )
    assert "校验" in str(ei.value)
    assert state.unit_path.read_text(encoding="utf-8") == original_unit
    assert not paths.caddyfile.exists(), "校验不过就不该写主 Caddyfile"
    assert paths.sites_caddy.read_text(encoding="utf-8").strip() == "", "sites.caddy 必须被还原"


def test_migrate_skips_validation_when_binary_missing(tmp_path):
    """二进制还没下载时不能因此判定迁移失败 —— setup 会下载后再校验。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    notes: list[str] = []
    config = Config(caddy_bin=str(tmp_path / "absent"))
    migrate(
        platform,
        paths,
        config,
        state,
        dnsmasq_conf=tmp_path / "dnsmasq.conf",
        dns_route=tmp_path / "resolved.conf",
        notes=notes,
    )
    assert any("跳过配置校验" in n for n in notes)
    assert paths.caddyfile.exists()


def test_validate_falls_back_to_legacy_unit_binary(tmp_path, monkeypatch):
    """config.toml 里的 caddy 不存在时，改用遗留单元里的二进制校验。

    "换单元前必校验"不能因为配置里的路径还没下载就变成空话 ——
    遗留单元里的 ExecStart 指向的正是这台机器上真正在跑的 caddy。
    """
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    legacy_bin = tmp_path / "legacy" / "caddy"
    legacy_bin.parent.mkdir(parents=True, exist_ok=True)
    legacy_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    state = _legacy_state(
        tmp_path,
        unit_text=(
            "[Service]\n"
            f"ExecStart={legacy_bin} run --config /old/Caddyfile\n"
        ),
    )
    calls = []

    def fake_run(argv):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(platform, "run", fake_run)
    notes: list[str] = []
    migrate(
        platform,
        paths,
        Config(caddy_bin=str(tmp_path / "absent")),
        state,
        dnsmasq_conf=tmp_path / "dnsmasq.conf",
        dns_route=tmp_path / "resolved.conf",
        notes=notes,
    )
    validate_calls = [c for c in calls if "validate" in c]
    assert validate_calls, "必须真的跑一次 caddy validate"
    assert validate_calls[0][0] == str(legacy_bin)
    assert any("遗留单元" in n for n in notes)
    assert not any("跳过配置校验" in n for n in notes)


def test_validate_still_skips_when_no_binary_anywhere(tmp_path):
    """配置与遗留单元里都没有二进制 —— 只能跳过，但必须留下提示。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    notes: list[str] = []
    migrate(
        platform,
        paths,
        Config(caddy_bin=str(tmp_path / "absent")),
        state,
        dnsmasq_conf=tmp_path / "dnsmasq.conf",
        dns_route=tmp_path / "resolved.conf",
        notes=notes,
    )
    assert any("跳过配置校验" in n for n in notes)


def test_unit_exec_start_parsed():
    from devproxy.lifecycle.migrate import unit_exec_start

    assert unit_exec_start(
        "[Service]\nExecStart=/opt/caddy run --config /etc/Caddyfile\n"
    ) == ["/opt/caddy", "run", "--config", "/etc/Caddyfile"]
    assert unit_exec_start("[Service]\nEnvironment=X=1\n") is None


# -- storage 连续性：CA 不能换地方 -------------------------------------------


def test_legacy_storage_path_parsed():
    legacy = "{\n\tstorage file_system /home/u/.local/share/caddy\n}\n\nlocalhost {\n}\n"
    assert legacy_storage_path(legacy) == Path("/home/u/.local/share/caddy")


def test_legacy_storage_path_none_without_global_block():
    assert legacy_storage_path("localhost {\n}\n") is None


def test_migrate_keeps_legacy_storage_so_ca_stays(tmp_path):
    """遗留 storage 与配置不同时必须沿用遗留的 —— 换了 CA 浏览器就不信任了。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    legacy_storage = tmp_path / "legacy-caddy-data"
    state = _legacy_state(
        tmp_path,
        caddyfile_text=(
            "{\n"
            f"\tstorage file_system {legacy_storage}\n"
            "}\n\n"
            "localhost {\n\ttls internal\n}\n"
        ),
    )
    notes: list[str] = []
    migrate(
        platform,
        paths,
        Config(storage=str(tmp_path / "config-caddy-data")),
        state,
        dnsmasq_conf=tmp_path / "dnsmasq.conf",
        dns_route=tmp_path / "resolved.conf",
        notes=notes,
    )
    caddyfile = paths.caddyfile.read_text(encoding="utf-8")
    assert f"storage file_system {legacy_storage}" in caddyfile
    assert any("storage" in n and str(legacy_storage) in n for n in notes), "差异必须说出来"
    # 留档：之后的 setup 必须用同一个地方，否则 CA 会重新生成
    assert str(legacy_storage) in paths.caddy_storage.read_text(encoding="utf-8")


def test_resolve_storage_keeps_config_when_same(tmp_path):
    paths = _paths(tmp_path)
    state = _legacy_state(
        tmp_path,
        caddyfile_text="{\n\tstorage file_system /same\n}\n\nlocalhost {\n}\n",
    )
    config = Config(storage="/same")
    same, note = resolve_storage(config, state)
    assert note is None
    assert same.storage_path == config.storage_path


# -- 失败路径要给出备份位置（I5）--------------------------------------------


def test_failure_message_points_at_backup_dir(tmp_path, monkeypatch):
    """还原失败时用户最需要知道的是：备份在哪、怎么手动还原。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("写单元失败")

    monkeypatch.setattr(platform, "install_service", boom)
    with pytest.raises(MigrationError) as ei:
        _migrate(platform, paths, Config(), state, tmp_path)
    message = str(ei.value)
    assert "backups" in message, message
    assert "devproxy setup --rollback" in message, message


# -- 备份目标与丢弃提示 -------------------------------------------------------


def test_backup_covers_caddy_env_and_storage(tmp_path):
    """迁移留下的留档（环境变量、storage）也必须进备份 —— 否则回滚后还在。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    backup = _migrate(platform, paths, Config(), state, tmp_path)
    originals = {e.original for e in backup.entries}
    assert paths.caddy_env in originals
    assert paths.caddy_storage in originals


def test_dropped_blocks_note_lists_addresses(tmp_path):
    """丢弃哪些块必须点名 —— 用户得能看出里面有没有自己手写的 *.test 块。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(
        tmp_path,
        caddyfile_text=(
            "localhost {\n\ttls internal\n}\n\n"
            "*.test {\n\trespond 404\n}\n\n"
            "http://*.test {\n\tredir https://{host}{uri} permanent\n}\n"
        ),
    )
    notes: list[str] = []
    _migrate(platform, paths, Config(), state, tmp_path, notes=notes)
    dropped_notes = [n for n in notes if "丢弃" in n]
    assert dropped_notes, notes
    assert "*.test" in dropped_notes[0]
    assert "http://*.test" in dropped_notes[0]
    assert "备份" in dropped_notes[0]


def test_migrate_creates_import_targets_before_validation(tmp_path, monkeypatch):
    """校验之前 import 目标必须已经存在 —— caddy 遇到缺失的 import 会直接拒绝。

    迁移是 API，不能假设调用方已经建好 sites.caddy / routes.caddy：
    校验用的主 Caddyfile 里就 import 这两个文件，缺一个 caddy 必然报
    "file not found"，迁移会被自己的校验拦死。
    """
    paths = _paths(tmp_path)  # 故意不 ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    binary = tmp_path / "caddy"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    config = Config(caddy_bin=str(binary))
    seen: dict[str, list[str]] = {}

    def fake_run(argv):
        if "validate" in argv:
            text = Path(argv[-1]).read_text(encoding="utf-8")
            targets = [
                line.split(None, 1)[1].strip()
                for line in text.splitlines()
                if line.strip().startswith("import ")
            ]
            seen["missing"] = [t for t in targets if not Path(t).exists()]
            return (1, "import 目标不存在") if seen["missing"] else (0, "")
        return 0, ""

    monkeypatch.setattr(platform, "run", fake_run)
    migrate(
        platform,
        paths,
        config,
        state,
        dnsmasq_conf=tmp_path / "dnsmasq.conf",
        dns_route=tmp_path / "resolved.conf",
    )
    assert seen.get("missing") == [], "校验时 import 目标必须已经存在"
