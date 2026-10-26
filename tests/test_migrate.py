"""迁移流程的测试。

铁律：测试里绝不碰真实系统 —— 所有路径都指向 tmp_path，平台一律用 FakePlatform。
"""

import warnings
from pathlib import Path

import pytest

from devproxy.core.backup import load_backup
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.lifecycle.migrate import LegacyState
from devproxy.lifecycle.migrate import MigrationError
from devproxy.lifecycle.migrate import MigrationRequest
from devproxy.lifecycle.migrate import default_dns_route
from devproxy.lifecycle.migrate import detect_legacy
from devproxy.lifecycle.migrate import legacy_storage_path
from devproxy.lifecycle.migrate import load_legacy_storage
from devproxy.lifecycle.migrate import resolve_storage_details
from devproxy.lifecycle.migrate import rollback
from devproxy.lifecycle.migrate import run_migration
from devproxy.lifecycle.migrate import split_caddyfile
from devproxy.lifecycle.migrate import split_caddyfile_blocks
from devproxy.lifecycle.migrate import unit_config_path
from devproxy.lifecycle.migrate import unit_env
from devproxy.lifecycle.migrate import unit_environment_files
from devproxy.lifecycle.migrate import wildcard_addresses
from devproxy.platform.fake import FakePlatform
from tests.support import ElevatedReader
from tests.support import UnreadablePath


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


def _run_migration(platform, paths, config, state, tmp_path):
    """迁移，但把 dnsmasq 配置与 DNS 路由文件指向 tmp_path（绝不碰真实系统）。"""
    return run_migration(
        MigrationRequest(
            platform=platform,
            paths=paths,
            config=config,
            state=state,
            dnsmasq_conf=tmp_path / "dnsmasq.conf",
            dns_route=tmp_path / "resolved.conf",
        )
    )


def _migrate(platform, paths, config, state, tmp_path):
    """只要备份对象的快捷方式。"""
    return _run_migration(platform, paths, config, state, tmp_path).backup


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
        '\trespond "nope" 404\n'
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
        '\trespond "nope" 404\n'
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


def test_split_ignores_braces_inside_inline_comments():
    """`example.test { # 站点 } 说明` 不能把块当场截断。"""
    legacy = "example.test { # 站点 } 说明\n\treverse_proxy 1.2.3.4:80\n}\n"
    personal, dropped = split_caddyfile_blocks(legacy)
    assert dropped == []
    assert len(personal) == 1
    assert "reverse_proxy" in personal[0]
    assert len(personal[0].splitlines()) == 3, "块必须完整，不能被切成两段"


def test_split_ignores_braces_inside_quotes():
    legacy = 'example.test {\n\trespond "a { b" 200\n}\n'
    personal, dropped = split_caddyfile_blocks(legacy)
    assert dropped == []
    assert len(personal) == 1
    assert '"a { b"' in personal[0]


@pytest.mark.parametrize(
    "block",
    [
        "*.test {\n\trespond 404\n}\n",
        "http://*.test {\n\trespond 404\n}\n",
        "https://*.test {\n\trespond 404\n}\n",
        "*.local.test {\n\trespond 404\n}\n",
        "https://*.local.test {\n\trespond 404\n}\n",
        "*.test:8443 {\n\trespond 404\n}\n",
        "foo.test, *.test {\n\trespond 404\n}\n",
        "*.test { # 通配 } 块\n\trespond 404\n}\n",
    ],
)
def test_wildcard_blocks_are_dropped_in_all_forms(block):
    personal, dropped = split_caddyfile_blocks(block)
    assert dropped == [block]
    assert personal == []


@pytest.mark.parametrize(
    "block",
    [
        "*.testing {\n\trespond hi\n}\n",
        "*.local.testing {\n\trespond hi\n}\n",
        "foo.test {\n\trespond hi\n}\n",
        "http://foo.test {\n\trespond hi\n}\n",
        "https://localhost {\n\trespond hi\n}\n",
    ],
)
def test_non_wildcard_or_other_suffix_blocks_are_kept(block):
    personal, dropped = split_caddyfile_blocks(block)
    assert dropped == []
    assert personal == [block]


def test_wildcard_detection_follows_custom_suffix():
    personal, dropped = split_caddyfile_blocks("*.dev {\n}\n", suffix="dev")
    assert dropped and not personal
    personal, dropped = split_caddyfile_blocks("*.test {\n}\n", suffix="dev")
    assert personal and not dropped


def test_wildcard_addresses_reports_scheme_form():
    """doctor 必须能看见 https://*.test —— 漏报会让 TLS 握手整体失败。"""
    assert wildcard_addresses("https://*.test {\n\trespond 404\n}\n") == ["https://*.test"]


# -- 单元解析 -----------------------------------------------------------------


def test_unit_config_path_parsed():
    unit = "[Service]\nExecStart=%h/.local/bin/caddy run --config %h/work/x/Caddyfile\n"
    p = unit_config_path(unit, Path("/home/u"))
    assert p == Path("/home/u/work/x/Caddyfile")


def test_unit_config_path_none_when_absent():
    assert unit_config_path("[Service]\nExecStart=/usr/bin/foo\n", Path("/home/u")) is None


def test_unit_config_path_accepts_equals_form():
    """Caddy/pflag 接受 --config=path —— 这也是一种合法单元写法。"""
    unit = "[Service]\nExecStart=/usr/bin/caddy run --config=/etc/caddy/Caddyfile\n"
    assert unit_config_path(unit, Path("/home/u")) == Path("/etc/caddy/Caddyfile")


def test_unit_config_path_handles_quoted_binary_and_path_with_spaces():
    unit = '[Service]\nExecStart="/usr/bin/caddy" run --config "/opt/my caddy/Caddyfile"\n'
    assert unit_config_path(unit, Path("/home/u")) == Path("/opt/my caddy/Caddyfile")


def test_unit_config_path_expands_home_in_equals_form():
    unit = "[Service]\nExecStart=/usr/bin/caddy run --config=%h/old/Caddyfile\n"
    assert unit_config_path(unit, Path("/home/u")) == Path("/home/u/old/Caddyfile")


def test_unit_config_path_ignores_systemd_dash_prefix():
    unit = "[Service]\nExecStart=-/usr/bin/caddy run --config /old/Caddyfile\n"
    assert unit_config_path(unit, Path("/home/u")) == Path("/old/Caddyfile")


def test_detect_legacy_accepts_equals_form(tmp_path):
    paths = _paths(tmp_path)
    platform = FakePlatform()
    unit_path = tmp_path / "caddy.service"
    unit_path.write_text("[Service]\nExecStart=/bin/caddy run --config=/old/Caddyfile\n")
    state = detect_legacy(platform, paths, unit_dir=tmp_path, home=tmp_path)
    assert state is not None, "--config=<path> 不能被当成没有遗留环境"
    assert state.config_path == Path("/old/Caddyfile")
    assert state.error is None


def test_unit_env_extracted():
    unit = "[Service]\nEnvironment=MINIKUBE_IP=192.168.49.2\nEnvironment=FOO=bar\n"
    assert unit_env(unit) == {"MINIKUBE_IP": "192.168.49.2", "FOO": "bar"}


def test_unit_env_ignores_comments():
    unit = "# Environment=NOT_SET=1\n[Service]\nEnvironment=REAL=1\n"
    assert unit_env(unit) == {"REAL": "1"}


def test_unit_env_handles_quoted_values_with_spaces():
    """systemd 的 Environment= 是 shell 风格分词 —— 带空格的值不能被拆坏。"""
    unit = '[Service]\nEnvironment="GREETING=hello world" "MINIKUBE_IP=1.2.3.4"\n'
    assert unit_env(unit) == {"GREETING": "hello world", "MINIKUBE_IP": "1.2.3.4"}


def test_unit_env_handles_multiple_assignments_per_line():
    unit = "[Service]\nEnvironment=FOO=bar BAZ=qux\n"
    assert unit_env(unit) == {"FOO": "bar", "BAZ": "qux"}


def test_unit_environment_files_detected():
    unit = "[Service]\nEnvironmentFile=/etc/default/caddy\nEnvironmentFile=-/etc/caddy/env\n"
    assert unit_environment_files(unit) == ["/etc/default/caddy", "/etc/caddy/env"]


def test_detect_legacy_refuses_environment_file(tmp_path):
    """EnvironmentFile 的内容无法安全重写 —— 必须拒绝自动迁移并点名。"""
    paths = _paths(tmp_path)
    platform = FakePlatform()
    (tmp_path / "caddy.service").write_text(
        "[Service]\n"
        "EnvironmentFile=/etc/default/caddy\n"
        "ExecStart=/bin/caddy run --config /old/Caddyfile\n"
    )
    state = detect_legacy(platform, paths, unit_dir=tmp_path, home=tmp_path)
    assert state is not None
    assert state.error and "EnvironmentFile" in state.error
    assert "/etc/default/caddy" in state.error


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


def test_detect_legacy_returns_unconfirmed_state_when_config_unparseable(tmp_path):
    """单元存在但解析不出 --config：不能当成“干净机器”。

    返回 None 会让 setup 直接重写用户的单元 —— 没有备份，手工配置全丢。
    必须返回带 error 的状态，让迁移拒绝动手。
    """
    paths = _paths(tmp_path)
    platform = FakePlatform()
    (tmp_path / "caddy.service").write_text("[Service]\nExecStart=/usr/bin/foo\n")
    state = detect_legacy(platform, paths, unit_dir=tmp_path, home=tmp_path)
    assert state is not None
    assert state.config_path is None
    assert state.error and "--config" in state.error


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

    state = LegacyState(unit_path=unit_path, config_path=old_cfg, env={"MINIKUBE_IP": "1.2.3.4"})
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
    outcome = run_migration(
        MigrationRequest(
            platform=platform,
            paths=paths,
            config=Config(),
            state=state,
            dnsmasq_conf=dnsmasq_conf,
            dns_route=tmp_path / "resolved.conf",
        )
    )
    originals = {e.original for e in outcome.backup.entries}
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
    paths.sites_caddy.write_text('mine.example { respond "hi" }\n', encoding="utf-8")
    platform = FakePlatform()
    state = _legacy_state(tmp_path, caddyfile_text="localhost {\n\ttls internal\n}\n")
    outcome = _run_migration(platform, paths, Config(), state, tmp_path)
    assert paths.sites_caddy.read_text() == 'mine.example { respond "hi" }\n'
    assert any("sites.caddy" in n for n in outcome.notes), "必须告诉用户个人站点没被迁走"


def test_migrate_states_that_personal_sites_are_no_longer_served(tmp_path):
    """迁移后旧 Caddyfile 不再被 import —— 里面的站点就此不再被服务。

    这件事必须明说（点名文件与地址）：只留一句"未覆盖"会让人以为
    个人站点还好好的，实际上它们已经 404 了。
    """
    paths = _paths(tmp_path)
    paths.ensure_files()
    paths.sites_caddy.write_text('mine.example { respond "hi" }\n', encoding="utf-8")
    platform = FakePlatform()
    state = _legacy_state(
        tmp_path,
        caddyfile_text=(
            "localhost {\n\ttls internal\n}\n\napi.example.com {\n\treverse_proxy 1.2.3.4:80\n}\n"
        ),
    )
    outcome = _run_migration(platform, paths, Config(), state, tmp_path)
    joined = "\n".join(outcome.notes)
    assert "不再被服务" in joined, f"必须明说站点不再被服务：{outcome.notes}"
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
        unit_text=(f"[Service]\nExecStart={legacy_bin} run --config /old/Caddyfile\n"),
    )
    outcome = _run_migration(
        platform,
        paths,
        Config(caddy_bin=str(tmp_path / "not-downloaded" / "caddy")),
        state,
        tmp_path,
    )
    unit = platform.services["caddy"]
    assert f"ExecStart={legacy_bin} run" in unit, unit
    assert any("沿用遗留" in n for n in outcome.notes), f"必须说明用了哪个二进制：{outcome.notes}"


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
    comp.apply([Action(description="装单元", kind="run", command=["install-service", "caddy"])])
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


def test_migrate_restores_backup_on_keyboard_interrupt(tmp_path, monkeypatch):
    """Ctrl-C 也是失败路径：必须还原，且报错里带上备份位置。

    KeyboardInterrupt 是 BaseException —— 只接 Exception 会把机器留在
    “写了 sites.caddy、还没换单元”的半迁移状态，用户还不知道备份在哪。
    """
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(
        tmp_path,
        caddyfile_text="localhost {\n}\n\n*.test {\n\trespond 404\n}\n",
    )
    original_unit = state.unit_path.read_text(encoding="utf-8")

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(platform, "install_service", interrupt)
    with pytest.raises(MigrationError) as ei:
        _migrate(platform, paths, Config(), state, tmp_path)
    message = str(ei.value)
    assert "中断" in message
    assert "devproxy setup --rollback" in message
    assert state.unit_path.read_text(encoding="utf-8") == original_unit
    assert paths.sites_caddy.read_text(encoding="utf-8").strip() == ""
    assert any("丢弃" in n for n in ei.value.notes), "失败前的提示也必须交给调用方"


def test_migrate_refuses_unconfirmed_legacy_state(tmp_path):
    """无法确认的遗留单元：拒绝迁移，一个文件都不动、也不建备份目录。"""
    paths = _paths(tmp_path)
    platform = FakePlatform()
    unit_path = tmp_path / "caddy.service"
    unit_path.write_text("ORIGINAL\n", encoding="utf-8")
    state = LegacyState(unit_path=unit_path, config_path=None, error="无法确认遗留 caddy 单元")
    with pytest.raises(MigrationError) as ei:
        _migrate(platform, paths, Config(), state, tmp_path)
    assert "无法确认" in str(ei.value)
    assert unit_path.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert platform.services == {}
    assert not paths.backup_dir.exists()


def test_run_migration_returns_notes_in_outcome(tmp_path):
    """notes 是返回值的一部分，不再靠调用方传可变列表。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(
        tmp_path,
        caddyfile_text="localhost {\n}\n\n*.test {\n\trespond 404\n}\n",
    )
    outcome = run_migration(
        MigrationRequest(
            platform=platform,
            paths=paths,
            config=Config(),
            state=state,
            dnsmasq_conf=tmp_path / "dnsmasq.conf",
            dns_route=tmp_path / "resolved.conf",
        )
    )
    assert outcome.backup.entries
    assert any("丢弃" in n for n in outcome.notes)
    assert outcome.notes is not None


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
        _migrate(platform, paths, config, state, tmp_path)
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
    config = Config(caddy_bin=str(tmp_path / "absent"))
    outcome = _run_migration(platform, paths, config, state, tmp_path)
    assert any("跳过配置校验" in n for n in outcome.notes)
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
        unit_text=(f"[Service]\nExecStart={legacy_bin} run --config /old/Caddyfile\n"),
    )
    calls = []

    def fake_run(argv):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr(platform, "run", fake_run)
    outcome = _run_migration(
        platform,
        paths,
        Config(caddy_bin=str(tmp_path / "absent")),
        state,
        tmp_path,
    )
    validate_calls = [c for c in calls if "validate" in c]
    assert validate_calls, "必须真的跑一次 caddy validate"
    assert validate_calls[0][0] == str(legacy_bin)
    assert any("遗留单元" in n for n in outcome.notes)
    assert not any("跳过配置校验" in n for n in outcome.notes)


def test_validate_still_skips_when_no_binary_anywhere(tmp_path):
    """配置与遗留单元里都没有二进制 —— 只能跳过，但必须留下提示。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    outcome = _run_migration(
        platform,
        paths,
        Config(caddy_bin=str(tmp_path / "absent")),
        state,
        tmp_path,
    )
    assert any("跳过配置校验" in n for n in outcome.notes)


def test_unit_exec_start_parsed():
    from devproxy.lifecycle.migrate import unit_exec_start

    assert unit_exec_start("[Service]\nExecStart=/opt/caddy run --config /etc/Caddyfile\n") == [
        "/opt/caddy",
        "run",
        "--config",
        "/etc/Caddyfile",
    ]
    assert unit_exec_start("[Service]\nEnvironment=X=1\n") is None


# -- storage 连续性：CA 不能换地方 -------------------------------------------


def test_legacy_storage_path_parsed():
    legacy = "{\n\tstorage file_system /home/u/.local/share/caddy\n}\n\nlocalhost {\n}\n"
    assert legacy_storage_path(legacy) == Path("/home/u/.local/share/caddy")


def test_legacy_storage_path_none_without_global_block():
    assert legacy_storage_path("localhost {\n}\n") is None


def test_legacy_storage_path_strips_inline_comment():
    """`storage file_system /data # old ca` 的路径是 /data，不是带注释的整串。"""
    assert legacy_storage_path("{\n\tstorage file_system /data # old ca\n}\n") == Path("/data")


def test_legacy_storage_path_keeps_quoted_path_with_spaces():
    text = '{\n\tstorage file_system "/home/u/my caddy" # 旧 CA\n}\n'
    assert legacy_storage_path(text) == Path("/home/u/my caddy")


def test_legacy_storage_path_keeps_hash_inside_quotes():
    text = '{\n\tstorage file_system "/home/u/my # caddy"\n}\n'
    assert legacy_storage_path(text) == Path("/home/u/my # caddy")


def test_migrate_keeps_legacy_storage_so_ca_stays(tmp_path):
    """遗留 storage 与配置不同时必须沿用遗留的 —— 换了 CA 浏览器就不信任了。"""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    legacy_storage = tmp_path / "legacy-caddy-data"
    state = _legacy_state(
        tmp_path,
        caddyfile_text=(
            f"{{\n\tstorage file_system {legacy_storage}\n}}\n\nlocalhost {{\n\ttls internal\n}}\n"
        ),
    )
    outcome = run_migration(
        MigrationRequest(
            platform=platform,
            paths=paths,
            config=Config(storage=str(tmp_path / "config-caddy-data")),
            state=state,
            dnsmasq_conf=tmp_path / "dnsmasq.conf",
            dns_route=tmp_path / "resolved.conf",
        )
    )
    caddyfile = paths.caddyfile.read_text(encoding="utf-8")
    assert f"storage file_system {legacy_storage}" in caddyfile
    assert any("storage" in n and str(legacy_storage) in n for n in outcome.notes), "差异必须说出来"
    # 生效配置必须带回：setup 要用它继续组件流程，不能写回旧 storage
    assert outcome.config.storage_path == legacy_storage
    # 留档：之后的 setup 必须用同一个地方，否则 CA 会重新生成
    assert str(legacy_storage) in paths.caddy_storage.read_text(encoding="utf-8")


def test_resolve_storage_details_keeps_config_when_same(tmp_path):
    state = _legacy_state(
        tmp_path,
        caddyfile_text="{\n\tstorage file_system /same\n}\n\nlocalhost {\n}\n",
    )
    config = Config(storage="/same")
    same, legacy, note = resolve_storage_details(config, state)
    assert note is None
    assert legacy == Path("/same")
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
    outcome = run_migration(
        MigrationRequest(
            platform=platform,
            paths=paths,
            config=Config(),
            state=state,
            dnsmasq_conf=tmp_path / "dnsmasq.conf",
            dns_route=tmp_path / "resolved.conf",
        )
    )
    dropped_notes = [n for n in outcome.notes if "丢弃" in n]
    assert dropped_notes, outcome.notes
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
    _migrate(platform, paths, config, state, tmp_path)
    assert seen.get("missing") == [], "校验时 import 目标必须已经存在"


# -- storage 一次性解析 -------------------------------------------------------


def test_resolve_storage_details_returns_legacy_path(tmp_path):
    """一次解析同时给出路径与提示 —— 调用方不必再读一遍遗留 Caddyfile。"""
    state = _legacy_state(
        tmp_path,
        caddyfile_text="{\n\tstorage file_system /legacy\n}\n\nlocalhost {\n}\n",
    )
    config, legacy, note = resolve_storage_details(Config(storage="/config"), state)
    assert legacy == Path("/legacy")
    assert config.storage_path == Path("/legacy")
    assert note and "/legacy" in note


def test_resolve_storage_details_returns_path_when_already_same(tmp_path):
    state = _legacy_state(
        tmp_path,
        caddyfile_text="{\n\tstorage file_system /same\n}\n\nlocalhost {\n}\n",
    )
    config, legacy, note = resolve_storage_details(Config(storage="/same"), state)
    assert legacy == Path("/same")
    assert config.storage_path == Path("/same")
    assert note is None


# -- 留档：原子写入与损坏告警 -------------------------------------------------


def test_load_extra_env_fails_loudly_on_corrupt_archive(tmp_path):
    """留档存在却解析不了 → 抛异常，绝不静默当成空（会丢 MINIKUBE_IP）。"""
    from devproxy.core.caddy_env import CaddyEnvArchiveError
    from devproxy.core.caddy_env import load_extra_env

    paths = _paths(tmp_path)
    paths.ensure_files()
    paths.caddy_env.write_text("{ 截断", encoding="utf-8")
    with pytest.raises(CaddyEnvArchiveError) as ei:
        load_extra_env(paths)
    message = str(ei.value)
    assert str(paths.caddy_env) in message, "必须点名是哪个文件"
    assert "修复" in message and "删除" in message, "必须给出修复办法"
    assert "MINIKUBE_IP" in message, "必须说明后果"


def test_load_extra_env_rejects_non_object_json(tmp_path):
    """合法 JSON 但不是对象（如数组）同样是坏留档。"""
    from devproxy.core.caddy_env import CaddyEnvArchiveError
    from devproxy.core.caddy_env import load_extra_env

    paths = _paths(tmp_path)
    paths.ensure_files()
    paths.caddy_env.write_text('["MINIKUBE_IP", "1.2.3.4"]', encoding="utf-8")
    with pytest.raises(CaddyEnvArchiveError, match="JSON 对象"):
        load_extra_env(paths)


def test_load_extra_env_missing_file_is_silent(tmp_path):
    from devproxy.core.caddy_env import load_extra_env

    paths = _paths(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert load_extra_env(paths) == {}


def test_load_extra_env_roundtrip(tmp_path):
    from devproxy.core.caddy_env import load_extra_env
    from devproxy.core.caddy_env import save_extra_env

    paths = _paths(tmp_path)
    save_extra_env(paths, {"MINIKUBE_IP": "1.2.3.4"})
    assert load_extra_env(paths) == {"MINIKUBE_IP": "1.2.3.4"}


def test_load_legacy_storage_warns_on_corrupt_archive(tmp_path):
    paths = _paths(tmp_path)
    paths.ensure_files()
    paths.caddy_storage.write_text("{ 截断", encoding="utf-8")
    with pytest.warns(UserWarning, match="storage 留档"):
        assert load_legacy_storage(paths) is None


def test_save_extra_env_is_atomic_when_replace_fails(tmp_path, monkeypatch):
    """写入失败时目标文件保持旧内容，且不留下临时文件。"""
    import devproxy.core.paths as paths_mod
    from devproxy.core.caddy_env import save_extra_env

    paths = _paths(tmp_path)
    save_extra_env(paths, {"A": "1"})
    before = paths.caddy_env.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("磁盘已满")

    monkeypatch.setattr(paths_mod.os, "replace", boom)
    with pytest.raises(OSError):
        save_extra_env(paths, {"A": "2"})
    assert paths.caddy_env.read_text(encoding="utf-8") == before
    leftovers = [p.name for p in paths.caddy_env.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == [], f"临时文件必须清理：{leftovers}"


def test_save_legacy_storage_is_atomic_when_replace_fails(tmp_path, monkeypatch):
    import devproxy.core.paths as paths_mod
    from devproxy.lifecycle.migrate import save_legacy_storage

    paths = _paths(tmp_path)
    paths.ensure_files()
    save_legacy_storage(paths, Path("/old"))
    before = paths.caddy_storage.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("磁盘已满")

    monkeypatch.setattr(paths_mod.os, "replace", boom)
    with pytest.raises(OSError):
        save_legacy_storage(paths, Path("/new"))
    assert paths.caddy_storage.read_text(encoding="utf-8") == before


# -- 注解必须能解析（get_type_hints 不应抛 NameError）--------------------------


def test_public_type_hints_resolve():
    from typing import get_type_hints

    assert get_type_hints(resolve_storage_details)
    assert get_type_hints(run_migration)
    assert get_type_hints(MigrationRequest)
