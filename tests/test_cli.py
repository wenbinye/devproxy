import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.cli._shared import DevProxyGroup
from devproxy.core.config import Config, ConfigError
from devproxy.core.paths import Paths
from devproxy.gateway.routes import Route, RouteTableError
from devproxy.gateway.store import RouteStore, RouteStoreError


class FakeCaddy:
    """假 Caddy：validate/reload 恒成功，避免测试真的去调 caddy 二进制。"""

    def validate(self, caddyfile=None):
        return True, "ok"

    def reload(self, caddyfile=None):
        return True, "ok"


class FakeAdmin:
    """假 Admin API 客户端：alive 可编排，供前置条件测试使用。"""

    def __init__(self, alive=True):
        self.alive = alive
        self.config_payload = {}

    def is_alive(self):
        return self.alive

    def api(self, method, path, body=None):
        return 200, self.config_payload


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把 devproxy 的三棵目录指向 tmp_path。"""
    monkeypatch.setenv("DEVPROXY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DEVPROXY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DEVPROXY_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def _patch_ctx(monkeypatch, tmp_path, admin_alive=True):
    """替换 build_context，注入假 Caddy 与假 Admin API。"""
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    # up/expose 以主 Caddyfile 存在为前置条件（缺了应先 devproxy setup）
    paths.caddyfile.write_text("# test Caddyfile\n", encoding="utf-8")
    cfg = Config(storage=str(paths.data_dir))
    store = RouteStore(paths, cfg, FakeCaddy())
    admin = FakeAdmin(alive=admin_alive)
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context", lambda *a, **k: (paths, cfg, store, admin)
    )
    return paths, cfg, store


def _seed_route(state_dir: Path, name="codeup", port=42133, mode="up"):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "routes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "domain_suffix": "test",
                "routes": [
                    {
                        "name": name,
                        "domain": f"{name}.test",
                        "port": port,
                        "mode": mode,
                        "upstream": f"127.0.0.1:{port}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_separator_is_split_and_passed_through(env, monkeypatch):
    """-- 之后的参数原样交给子命令，不被 devproxy 的选项解析器吞掉。"""
    _patch_ctx(monkeypatch, env)
    captured = {}

    def fake_run(self, name, cmd, port=None, ready_timeout=None, on_ready=None):
        captured["name"] = name
        captured["cmd"] = cmd
        captured["port"] = port
        return 0

    monkeypatch.setattr("devproxy.cli._shared.UpRunner.run", fake_run)
    res = CliRunner().invoke(
        app, ["up", "codeup", "--port", "8080", "--", "npx", "vite", "--port", "3000"]
    )
    assert res.exit_code == 0, res.output
    assert captured["name"] == "codeup"
    assert captured["port"] == 8080
    assert captured["cmd"] == ["npx", "vite", "--port", "3000"]


def test_up_without_separator_is_rejected(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "codeup", "npx", "vite"])
    # 命令写法错误 → 参数错误（2），不是运行失败
    assert res.exit_code == 2, res.output
    assert "--" in res.output


def test_up_without_command_is_rejected(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "codeup", "--"])
    assert res.exit_code == 2, res.output


def test_expose_registers_route(env, monkeypatch):
    """expose 是一次性的：注册完立即返回，路由常驻。"""
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 0, res.output
    table = json.loads((env / "state" / "routes.json").read_text())
    assert table["routes"][0]["name"] == "legacy"
    assert table["routes"][0]["mode"] == "expose"
    assert table["routes"][0]["domain"] == "legacy.test"


def test_down_removes_route(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    _seed_route(env / "state")
    res = CliRunner().invoke(app, ["down", "codeup"])
    assert res.exit_code == 0, res.output
    table = json.loads((env / "state" / "routes.json").read_text())
    assert table["routes"] == []


def test_down_unknown_name_is_ok(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["down", "nope"])
    assert res.exit_code == 0


def test_down_kills_leftover_process_group(env, monkeypatch):
    """down 要顺带杀掉 mode=up 路由的残留子进程组（带注册时的进程身份）。"""
    _patch_ctx(monkeypatch, env)
    killed = []

    def fake_kill(pid, proc_start=None):
        killed.append((pid, proc_start))

    monkeypatch.setattr("devproxy.cli._shared.kill_pid_group", fake_kill)
    _seed_route(env / "state", "codeup", 42133, mode="up")
    p = env / "state" / "routes.json"
    data = json.loads(p.read_text())
    data["routes"][0]["pid"] = 4242
    data["routes"][0]["proc_start"] = "12345"
    p.write_text(json.dumps(data))
    res = CliRunner().invoke(app, ["down", "codeup"])
    assert res.exit_code == 0, res.output
    assert killed == [(4242, "12345")]


def test_ls_lists_routes(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    _seed_route(env / "state", "codeup", 42133)
    res = CliRunner().invoke(app, ["ls"])
    assert res.exit_code == 0, res.output
    assert "codeup.test" in res.output


def test_config_shows_effective_config(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 0, res.output
    assert "domain_suffix" in res.output


def test_config_marks_effective_storage_and_labels_default_dir(env, monkeypatch):
    """config 只该说清"哪条 storage 真正生效"。

    Paths.data_dir（只受 DEVPROXY_DATA_DIR 影响）与 config.toml 里的
    storage 可能不是同一个 —— 两个都叫 storage 会让人以为 Caddy 的数据
    在错的地方。
    """
    paths, cfg, _ = _patch_ctx(monkeypatch, env)
    # 让两处真的不一致：config.toml 指向别处
    other = env / "other-storage"
    cfg = replace(cfg, storage=str(other))
    store = RouteStore(paths, cfg, FakeCaddy())
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context", lambda *a, **k: (paths, cfg, store, FakeAdmin())
    )
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 0, res.output
    assert str(other) in res.output, "生效的 storage 必须打出来"
    assert "data_dir_default" in res.output, "默认目录必须换个不误导的名字"
    assert "storage 才是生效的" in res.output


# ---------------------------------------------- 注册路由后的信任提示


def _write_ca(storage: Path) -> Path:
    """在 Caddy storage 下放一份 CA 根证书（模拟"首条路由已生成 CA"）。"""
    ca = storage / "pki" / "authorities" / "local" / "root.crt"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", encoding="utf-8")
    return ca


def test_expose_prints_trust_hint_when_ca_not_trusted(env, monkeypatch):
    """CA 是第一条路由注册时才生成的 —— 那一刻必须告诉用户去 trust。

    不说的话，用户只会看到"浏览器报证书不安全"，根本不知道还差一步。
    """
    paths, cfg, _ = _patch_ctx(monkeypatch, env)
    _write_ca(Path(cfg.storage))
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 0, res.output
    assert "devproxy trust" in res.output


def test_expose_does_not_print_trust_hint_when_ca_trusted(env, monkeypatch):
    """CA 已被信任时不再重复提示 —— 每次都冒出来就是噪音。"""
    paths, cfg, _ = _patch_ctx(monkeypatch, env)
    ca = _write_ca(Path(cfg.storage))
    platform = monkeypatch.undo  # noqa: F841 —— 只是让下面读起来清楚
    from devproxy.platform.fake import FakePlatform

    fake = FakePlatform()
    fake.trusted.add(ca)
    fake.nss.add(ca)
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: fake)
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 0, res.output
    assert "devproxy trust" not in res.output


def test_expose_does_not_print_trust_hint_without_ca(env, monkeypatch):
    """还没有 CA（没生成）就没什么可信任的 —— 不提示。"""
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 0, res.output
    assert "devproxy trust" not in res.output


def test_up_prints_trust_hint_when_ca_not_trusted(env, monkeypatch):
    paths, cfg, _ = _patch_ctx(monkeypatch, env)
    _write_ca(Path(cfg.storage))

    def fake_run(self, name, cmd, port=None, ready_timeout=None, on_ready=None):
        if on_ready is not None:
            on_ready("codeup.test", 8080)
        return 0

    monkeypatch.setattr("devproxy.cli._shared.UpRunner.run", fake_run)
    res = CliRunner().invoke(app, ["up", "codeup", "--", "sleep", "1"])
    assert res.exit_code == 0, res.output
    assert "devproxy trust" in res.output


# ---------------------------------------------------------------- I-1: 退出码 3


def test_up_when_caddy_not_running_exits_3(env, monkeypatch):
    """Caddy 未运行是前置条件不满足 → 退出码 3（不是 1）。"""
    _patch_ctx(monkeypatch, env, admin_alive=False)
    res = CliRunner().invoke(app, ["up", "codeup", "--", "sleep", "5"])
    assert res.exit_code == 3, res.output
    assert "Caddy" in res.output


def test_expose_when_caddy_not_running_exits_3(env, monkeypatch):
    """expose 要注册活路由，同样要求 Caddy 在运行 → 退出码 3。"""
    _patch_ctx(monkeypatch, env, admin_alive=False)
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 3, res.output
    assert "Caddy" in res.output
    assert not (env / "state" / "routes.json").exists(), "前置条件不满足时不得写路由表"


def test_up_without_main_caddyfile_exits_3_and_points_at_setup(env, monkeypatch):
    """还没 setup（主 Caddyfile 不存在）是前置条件问题，不是 reload 运行失败。"""
    paths, _, _ = _patch_ctx(monkeypatch, env)
    paths.caddyfile.unlink()
    res = CliRunner().invoke(app, ["up", "codeup", "--", "sleep", "5"])
    assert res.exit_code == 3, res.output
    assert "devproxy setup" in res.output


def test_expose_without_main_caddyfile_exits_3(env, monkeypatch):
    paths, _, _ = _patch_ctx(monkeypatch, env)
    paths.caddyfile.unlink()
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 3, res.output
    assert "devproxy setup" in res.output
    assert not (env / "state" / "routes.json").exists()


# ---------------------------------------------------- I-2: 路由表损坏要干净报错


def _write_routes_file(state_dir: Path, text: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "routes.json").write_text(text, encoding="utf-8")


def test_ls_with_invalid_json_routes_file_is_clean_error(env, monkeypatch):
    """routes.json 不是合法 JSON → 一行错误 + 退出码 1，而不是 Rich traceback。"""
    _patch_ctx(monkeypatch, env)
    _write_routes_file(env / "state", "{ 不是 json")
    res = CliRunner().invoke(app, ["ls"])
    assert res.exit_code == 1, res.output
    assert "devproxy" in res.output and "路由表" in res.output
    assert "Traceback" not in res.output


def test_ls_with_routes_missing_required_key_is_clean_error(env, monkeypatch):
    """routes.json 缺必需键 → 同样是干净的一行错误。"""
    _patch_ctx(monkeypatch, env)
    _write_routes_file(env / "state", '{"version": 1, "routes": [{"name": "x"}]}')
    res = CliRunner().invoke(app, ["ls"])
    assert res.exit_code == 1, res.output
    assert "路由表" in res.output
    assert "Traceback" not in res.output


def test_down_with_corrupt_routes_file_is_clean_error(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    _write_routes_file(env / "state", "[]")  # 顶层是列表而非对象
    res = CliRunner().invoke(app, ["down", "codeup"])
    assert res.exit_code == 1, res.output
    assert "路由表" in res.output
    assert "Traceback" not in res.output


def test_config_with_corrupt_routes_file_is_clean_error(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    _write_routes_file(env / "state", "{ oops")
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 1, res.output
    assert "路由表" in res.output
    assert "Traceback" not in res.output


@pytest.mark.parametrize(
    "exc", [RouteStoreError("boom"), RouteTableError("损坏"), ConfigError("坏")]
)
def test_main_backstop_turns_domain_errors_into_exit_1(monkeypatch, exc):
    """main() 兜底：领域异常绝不能以 traceback 的形式漏到终端。"""
    from devproxy import cli

    def fake_app():
        raise exc

    monkeypatch.setattr(cli, "app", fake_app)
    with pytest.raises(SystemExit) as cm:
        cli.main()
    assert cm.value.code == 1


# ------------------------------------- 迁移与回滚（setup）


def test_cli_never_builds_the_real_platform(env, monkeypatch, tmp_path):
    """回归防线：CLI 路径绝不能构造真实平台。

    真实平台会去看 ~/.config/systemd/user/ 下的真实单元，迁移一旦跑起来
    就会改写用户机器上的 caddy.service（曾因此真的覆盖过一次）。
    conftest 的 autouse fixture 负责兜底，这里把"真实平台不许被构造"
    变成显式断言，防止兜底被摘掉。
    """
    import devproxy.platform.linux as linux_mod

    def boom(*args, **kwargs):
        raise AssertionError("测试里不允许构造真实平台")

    monkeypatch.setattr(linux_mod, "LinuxPlatform", boom)
    monkeypatch.setattr("devproxy.cli._shared._components", lambda *a, **k: [])
    res = CliRunner().invoke(app, ["setup", "--yes"])
    assert res.exit_code == 0, res.output


def _fake_setup_platform(monkeypatch, tmp_path, patch_dns_route=True):
    """把 setup 依赖的一切换成 tmp_path 里的假东西 —— 绝不碰真实系统。"""
    from devproxy.platform.fake import FakePlatform

    platform = FakePlatform()
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    # 不装配任何组件：迁移本身与组件流程无关，测试只盯迁移与回滚
    monkeypatch.setattr("devproxy.cli._shared._components", lambda *a, **k: [])
    monkeypatch.setattr("devproxy.cli._shared.DNSMASQ_CONF", tmp_path / "dnsmasq.conf")
    if patch_dns_route:
        monkeypatch.setattr(
            "devproxy.cli._shared.default_dns_route", lambda *a, **k: tmp_path / "resolved.conf"
        )
    return platform


def _seed_legacy_unit(platform, tmp_path, caddyfile_text=None):
    """造一套遗留环境：单元 + 旧 Caddyfile。"""
    old_cfg = tmp_path / "old" / "Caddyfile"
    old_cfg.parent.mkdir(parents=True, exist_ok=True)
    old_cfg.write_text(
        caddyfile_text
        or "localhost {\n\ttls internal\n}\n\n*.test {\n\trespond 404\n}\n",
        encoding="utf-8",
    )
    unit = platform.service_unit_path("caddy")
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Service]\n"
        "Environment=MINIKUBE_IP=1.2.3.4\n"
        f"ExecStart=/bin/caddy run --config {old_cfg}\n",
        encoding="utf-8",
    )
    return unit, old_cfg


def test_setup_migrates_legacy_caddy(env, monkeypatch, tmp_path):
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    unit, _old = _seed_legacy_unit(platform, tmp_path)

    res = CliRunner().invoke(app, ["setup", "--yes"])
    assert res.exit_code == 0, res.output
    assert "遗留" in res.output
    sites = (env / "config" / "sites.caddy").read_text(encoding="utf-8")
    assert "localhost" in sites
    assert "*.test" not in sites
    assert "MINIKUBE_IP=1.2.3.4" in platform.services["caddy"]
    # 备份目录必须真的建出来了
    backups = list((env / "state" / "backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "MANIFEST.txt").exists()


def test_setup_refuses_migration_without_yes_in_non_interactive(env, monkeypatch, tmp_path):
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    unit, _old = _seed_legacy_unit(platform, tmp_path)
    res = CliRunner().invoke(app, ["setup"])
    assert res.exit_code != 0
    assert "--yes" in res.output
    assert "MINIKUBE_IP" not in platform.services.get("caddy", "")


def test_setup_rollback_restores_files(env, monkeypatch, tmp_path):
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    unit, _old = _seed_legacy_unit(platform, tmp_path)
    assert CliRunner().invoke(app, ["setup", "--yes"]).exit_code == 0
    backup_dir = next((env / "state" / "backups").iterdir())

    unit.write_text("CLOBBERED\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["setup", "--rollback", str(backup_dir)])
    assert res.exit_code == 0, res.output
    assert "已回滚" in res.output
    assert "MINIKUBE_IP=1.2.3.4" in unit.read_text(encoding="utf-8")


def test_setup_rollback_reports_partial_failure(env, monkeypatch, tmp_path):
    """部分条目还原失败时，已还原清单必须如实报出来（不能随异常丢掉）。"""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    unit, _old = _seed_legacy_unit(platform, tmp_path)
    assert CliRunner().invoke(app, ["setup", "--yes"]).exit_code == 0
    backup_dir = next((env / "state" / "backups").iterdir())

    # 删掉单元的备份副本 → 还原该条目必然失败（与 uid 无关）
    (backup_dir / "caddy.service.bak").unlink()
    res = CliRunner().invoke(app, ["setup", "--rollback", str(backup_dir)])
    assert res.exit_code == 1, res.output
    assert "已还原" in res.output
    assert "Traceback" not in res.output


def test_setup_rollback_with_bad_directory_is_clean_error(env, monkeypatch, tmp_path):
    _fake_setup_platform(monkeypatch, tmp_path)
    res = CliRunner().invoke(app, ["setup", "--rollback", str(tmp_path / "nope")])
    assert res.exit_code == 1, res.output
    assert "Traceback" not in res.output


# ------------------------------------- Rich 标记不得吞掉错误消息里的字符集


def test_invalid_name_error_keeps_charset_in_output(env, monkeypatch):
    """[a-z0-9-] 会被 Rich 当标记吞掉 —— 错误输出必须保留字面字符集。"""
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["expose", "Bad_Name", "--port", "3000"])
    # 非法路由名是参数错误（约定 2），不是运行失败
    assert res.exit_code == 2
    assert "[a-z0-9-]" in res.output


def test_up_help_text_keeps_charset(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "--help"])
    assert res.exit_code == 0
    assert "[a-z0-9-]" in res.output


def test_setup_backup_targets_follow_dns_backend(env, monkeypatch, tmp_path):
    """--dns-backend system 时，备份的必须是 /etc/dnsmasq.d 那个文件。

    组件写的是 dnsmasq.d，备份的却是 resolved —— 被改动的文件不在备份里，
    回滚就删不掉它。
    """
    platform = _fake_setup_platform(monkeypatch, tmp_path, patch_dns_route=False)
    _seed_legacy_unit(platform, tmp_path)
    res = CliRunner().invoke(app, ["setup", "--yes", "--dns-backend", "system"])
    assert res.exit_code == 0, res.output
    manifest = json.loads(
        (next((env / "state" / "backups").iterdir()) / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    originals = [e["original"] for e in manifest["entries"]]
    assert "/etc/dnsmasq.d/devproxy.conf" in originals
    assert "/etc/systemd/resolved.conf.d/devproxy.conf" not in originals


def test_setup_migration_failure_still_prints_notes(env, monkeypatch, tmp_path):
    """迁移失败时，已经收集到的提示（丢了哪些块等）必须照样打出来。"""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    _seed_legacy_unit(platform, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("写单元失败")

    monkeypatch.setattr(platform, "install_service", boom)
    res = CliRunner().invoke(app, ["setup", "--yes"])
    assert res.exit_code == 1, res.output
    assert "已丢弃" in res.output
    assert "devproxy: " in res.output
    assert "Traceback" not in res.output


def test_setup_persists_legacy_storage_for_later_setups(env, monkeypatch, tmp_path):
    """沿用遗留 storage 之后必须留档 —— 否则下一次 setup 会把 CA 换地方。

    迁移时换了 storage 位置，Caddy 会重新生成 CA：浏览器里的信任与个人
    站点已有的 tls internal 证书全部失效。留档之后，每次 setup 都要用
    同一个地方。
    """
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    legacy_storage = tmp_path / "legacy-caddy-data"
    _seed_legacy_unit(
        platform,
        tmp_path,
        caddyfile_text=(
            "{\n"
            f"\tstorage file_system {legacy_storage}\n"
            "}\n\n"
            "localhost {\n\ttls internal\n}\n"
        ),
    )
    res = CliRunner().invoke(app, ["setup", "--yes"])
    assert res.exit_code == 0, res.output
    saved = env / "state" / "caddy-storage.json"
    assert saved.exists(), "沿用的 storage 必须留档，否则下次 setup 会换地方"
    assert str(legacy_storage) in saved.read_text(encoding="utf-8")
    # Rich 会按终端宽度折行（长路径甚至会被拆断）—— 比对前把空白全去掉
    squashed = "".join(res.output.split())
    assert "".join(str(legacy_storage).split()) in squashed, "storage 的差异必须对用户说出来"


# ------------------------------------- 留档的 storage 覆盖 config.toml 时必须说


def _write_storage_pair(env, config_storage: str, stored_storage: str):
    from devproxy.core.paths import default_paths

    paths = default_paths()
    paths.config_toml.parent.mkdir(parents=True, exist_ok=True)
    paths.config_toml.write_text(
        f'storage = "{config_storage}"\n', encoding="utf-8"
    )
    paths.caddy_storage.parent.mkdir(parents=True, exist_ok=True)
    paths.caddy_storage.write_text(
        json.dumps({"storage": stored_storage}), encoding="utf-8"
    )
    return paths


def test_build_context_says_so_when_stored_storage_overrides_config(env, capsys):
    """留档的 storage 覆盖了 config.toml —— 必须说出来，并给出退出路径。

    用户改了 config.toml 却被无声忽略，是最难查的一类问题。
    """
    from devproxy.cli._shared import build_context

    paths = _write_storage_pair(env, "/tmp/from-config-toml", "/tmp/from-legacy")
    _paths, config, _store, _admin = build_context()
    assert config.storage == "/tmp/from-legacy", "留档必须继续生效（CA 不能换地方）"
    err = capsys.readouterr().err
    assert "/tmp/from-legacy" in err, "必须说出实际生效的路径"
    assert "/tmp/from-config-toml" in err, "必须说出被忽略的配置值"
    # Rich 会按终端宽度折行（长路径甚至会被拆断）—— 比对前把空白全去掉
    squashed = "".join(err.split())
    assert "".join(str(paths.caddy_storage).split()) in squashed, "必须给出退出路径（留档文件）"


def test_build_context_quiet_when_storage_agrees(env, capsys):
    """两边一致时不啰嗦。"""
    from devproxy.cli._shared import build_context

    _write_storage_pair(env, "/tmp/same", "/tmp/same")
    _paths, config, _store, _admin = build_context()
    assert config.storage == "/tmp/same"
    assert "storage" not in capsys.readouterr().err


def test_cli_setup_reports_the_storage_override(env, monkeypatch, tmp_path):
    """CLI 路径上也要说出来 —— 用户看到的是终端输出，不是源码。"""
    from devproxy.cli._shared import build_context

    _write_storage_pair(env, "/tmp/from-config-toml", "/tmp/from-legacy")
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 0, res.output
    assert "/tmp/from-legacy" in res.output
    assert "/tmp/from-config-toml" in res.output


# ------------------------------------- setup 结束提示里的示例命令


def test_setup_example_command_actually_uses_port():
    """setup 结束时的示例命令必须真的用上 $PORT。

    `python3 -m http.server` 不读 PORT（永远绑 8000）：照抄示例的用户会
    等满 60 秒后看到「就绪超时」。示例必须是跑得通的那种写法。
    """
    from devproxy.cli._shared import UP_EXAMPLE

    assert "sh -c" in UP_EXAMPLE, "示例必须用 sh -c 把 $PORT 推迟展开"
    assert "$PORT" in UP_EXAMPLE, "示例必须真的把 PORT 交给应用"


def test_setup_prints_the_working_example(env, monkeypatch):
    """终端上打出来的示例必须是同一个能跑通的写法。"""
    from devproxy.cli._shared import UP_EXAMPLE
    from devproxy.platform.base import Action, Status
    from devproxy.lifecycle.setup import ComponentReport, SetupPlan

    report = ComponentReport(
        name="caddy",
        status=Status(ok=True, detail="已就绪"),
        actions=[Action(description="noop", kind="run", command=["noop"])],
    )
    monkeypatch.setattr("devproxy.cli._shared._components", lambda *a, **k: [])
    monkeypatch.setattr(
        "devproxy.cli._shared.build_plan", lambda comps: SetupPlan(reports=[report])
    )
    monkeypatch.setattr(
        "devproxy.cli._shared.apply_plan",
        lambda plan: [("caddy", Status(ok=True, detail="已就绪"))],
    )
    res = CliRunner().invoke(app, ["setup", "--yes"])
    assert res.exit_code == 0, res.output
    assert UP_EXAMPLE in res.output
    assert "$PORT" in res.output


# ------------------------------------- I7：参数错误一律退出 2


def test_up_invalid_name_exits_2(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "Bad_Name", "--", "sleep", "1"])
    assert res.exit_code == 2, res.output
    assert "[a-z0-9-]" in res.output


def test_up_invalid_port_exits_2(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "codeup", "--port", "0", "--", "sleep", "1"])
    assert res.exit_code == 2, res.output
    assert "1..65535" in res.output


def test_up_invalid_ready_timeout_exits_2(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(
        app, ["up", "codeup", "--ready-timeout", "0", "--", "sleep", "1"]
    )
    assert res.exit_code == 2, res.output
    assert "就绪超时" in res.output


def test_expose_invalid_port_exits_2(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "65536"])
    assert res.exit_code == 2, res.output


def test_down_invalid_name_exits_2(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["down", "Bad_Name"])
    assert res.exit_code == 2, res.output


# ------------------------------------- I1：只读命令不产生写副作用


@pytest.mark.parametrize("command", ["ls", "config", "doctor"])
def test_readonly_commands_do_not_create_config_dir(env, monkeypatch, command):
    """查询命令不得 mkdir/touch —— 配置目录父路径是普通文件时也不能炸。"""
    blocker = env / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    config_dir = blocker / "devproxy"
    monkeypatch.setenv("DEVPROXY_CONFIG_DIR", str(config_dir))
    if command == "doctor":
        # doctor 会做真实组件探测（读宿主 /etc 等）—— 这里只关心 build_context
        monkeypatch.setattr("devproxy.cli._shared._components", lambda *a, **k: [])
    res = CliRunner().invoke(app, [command])
    assert "Traceback" not in res.output, res.output
    assert res.exit_code in (0, 3), res.output
    assert not config_dir.exists(), "只读命令不得创建配置目录"


# ------------------------------------- I2：storage 留档损坏不得静默回退


def test_corrupt_storage_archive_is_precondition_error(env):
    """留档存在但读不懂 → 退出 3，提示删除留档，绝不静默换 storage。"""
    from devproxy.core.paths import default_paths

    paths = default_paths()
    paths.caddy_storage.parent.mkdir(parents=True, exist_ok=True)
    paths.caddy_storage.write_text("{ 截断", encoding="utf-8")
    with pytest.warns(UserWarning, match="storage 留档不可读"):
        res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 3, res.output
    assert "留档损坏" in res.output
    assert "Traceback" not in res.output


def test_missing_storage_archive_is_not_an_error(env):
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 0, res.output


def test_resolve_storage_reports_warnings_without_side_effects(env, capsys):
    """M2：查询函数只返回警告，不直接写 stderr。"""
    from devproxy.cli._shared import _resolve_storage

    paths = _write_storage_pair(env, "/tmp/from-config-toml", "/tmp/from-legacy")
    config, warnings = _resolve_storage(Config(), paths)
    assert config.storage == "/tmp/from-legacy"
    assert warnings and "/tmp/from-legacy" in warnings[0]
    assert capsys.readouterr().err == "", "解析函数不应有 stderr 副作用"


# ------------------------------------- C1：回滚后重启失败不得静默


def test_setup_rollback_restart_failure_is_reported(env, monkeypatch, tmp_path):
    """文件已还原但 caddy 重启失败：必须报出来，且不能说成回滚失败。"""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    unit, _old = _seed_legacy_unit(platform, tmp_path)
    assert CliRunner().invoke(app, ["setup", "--yes"]).exit_code == 0
    backup_dir = next((env / "state" / "backups").iterdir())

    def boom(name):
        raise RuntimeError("systemctl restart caddy 失败: unit 坏了")

    monkeypatch.setattr(platform, "restart_service", boom)
    res = CliRunner().invoke(app, ["setup", "--rollback", str(backup_dir)])
    assert res.exit_code == 1, res.output
    assert "已回滚" in res.output, "文件确实还原了，必须如实说"
    assert "Caddy 可能仍在运行迁移后的配置" in res.output
    assert "Traceback" not in res.output


def test_setup_rollback_restart_permission_error_exits_4(env, monkeypatch, tmp_path):
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    _seed_legacy_unit(platform, tmp_path)
    assert CliRunner().invoke(app, ["setup", "--yes"]).exit_code == 0
    backup_dir = next((env / "state" / "backups").iterdir())

    def boom(name):
        raise PermissionError("提权失败：sudo 不可用")

    monkeypatch.setattr(platform, "restart_service", boom)
    res = CliRunner().invoke(app, ["setup", "--rollback", str(backup_dir)])
    assert res.exit_code == 4, res.output
    assert "已还原" in res.output
    assert "Traceback" not in res.output


def test_setup_rollback_skips_restart_when_service_was_absent(
    env, monkeypatch, tmp_path
):
    """迁移前没有服务单元 → 回滚无需重启，也不该报错。"""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    _seed_legacy_unit(platform, tmp_path)
    assert CliRunner().invoke(app, ["setup", "--yes"]).exit_code == 0
    backup_dir = next((env / "state" / "backups").iterdir())

    monkeypatch.setattr(
        platform, "service_unit_path", lambda name: tmp_path / "no-such-unit.service"
    )
    monkeypatch.setattr(
        platform, "restart_service", lambda name: pytest.fail("不应尝试重启")
    )
    res = CliRunner().invoke(app, ["setup", "--rollback", str(backup_dir)])
    assert res.exit_code == 0, res.output
    assert "跳过重启" in res.output


# ------------------------------------- I5 / I6：回滚参数的边界与恢复路径


def test_setup_rejects_empty_rollback_dir(env, monkeypatch, tmp_path):
    """--rollback "" 是参数错误，绝不能落进完整部署分支。"""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    res = CliRunner().invoke(app, ["setup", "--rollback", ""])
    assert res.exit_code == 2, res.output
    assert "空字符串" in res.output
    # 没做迁移、也没开始部署
    assert platform.services == {}


def test_setup_rejects_empty_domain_suffix(env, monkeypatch, tmp_path):
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    res = CliRunner().invoke(app, ["setup", "--domain-suffix", ""])
    assert res.exit_code == 2, res.output
    assert platform.services == {}


def test_setup_rollback_works_with_corrupt_config_toml(env, monkeypatch, tmp_path):
    """配置损坏正是最需要回滚的时刻 —— 恢复路径不能先加载配置。"""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    unit, _old = _seed_legacy_unit(platform, tmp_path)
    assert CliRunner().invoke(app, ["setup", "--yes"]).exit_code == 0
    backup_dir = next((env / "state" / "backups").iterdir())

    (env / "config" / "config.toml").write_text("storage = [", encoding="utf-8")
    unit.write_text("CLOBBERED\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["setup", "--rollback", str(backup_dir)])
    assert res.exit_code == 0, res.output
    assert "MINIKUBE_IP=1.2.3.4" in unit.read_text(encoding="utf-8")


# ------------------------------------- M7：交互判定统一走 _shared 接缝


def test_setup_migration_uses_interactive_seam(env, monkeypatch, tmp_path):
    """打桩 `_shared._interactive` 必须能驱动迁移的交互确认分支。"""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    _seed_legacy_unit(platform, tmp_path)
    monkeypatch.setattr("devproxy.cli._shared._interactive", lambda: True)
    monkeypatch.setattr("devproxy.cli._shared.typer.confirm", lambda *a, **k: True)
    res = CliRunner().invoke(app, ["setup"])
    assert res.exit_code == 0, res.output
    assert "MINIKUBE_IP=1.2.3.4" in platform.services["caddy"]


# ------------------------------------- 平台构造失败（如 sudo/root）


def test_up_does_not_mask_programming_errors(env, monkeypatch):
    """M1：UpRunner 只该把已知运行错误转成退出码；bug 不能被伪装成运行失败。"""
    _patch_ctx(monkeypatch, env)

    def boom(self, *a, **k):
        raise AttributeError("runner 内部 bug")

    monkeypatch.setattr("devproxy.cli._shared.UpRunner.run", boom)
    res = CliRunner().invoke(app, ["up", "codeup", "--", "sleep", "1"])
    assert isinstance(res.exception, AttributeError), res.output


def test_trust_lets_programming_errors_escape(env, monkeypatch):
    """M6：trust 只该捕获契约内的 PermissionError / RuntimeError。"""
    from devproxy.platform.base import Action
    from devproxy.platform.fake import FakePlatform

    _patch_ctx(monkeypatch, env)
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: FakePlatform())

    class BoomCert:
        def __init__(self, *a, **k):
            pass

        def plan(self):
            return [Action(description="装 CA")]

        def apply(self, actions):
            raise AttributeError("组件内部 bug")

    monkeypatch.setattr("devproxy.cli._shared.CertComponent", BoomCert)
    with pytest.raises(AttributeError):
        CliRunner().invoke(app, ["trust"], catch_exceptions=False)


def test_platform_construction_failure_is_clean_precondition(env, monkeypatch):
    """sudo devproxy ... 在构造平台时被拒绝 → 一行可读错误 + 退出 3。"""
    from devproxy.cli import _context

    _patch_ctx(monkeypatch, env)
    monkeypatch.setattr("devproxy.cli._shared._platform", _context._platform)

    def refuse():
        raise RuntimeError("请以普通用户运行 devproxy：以 root 运行会装错家目录")

    monkeypatch.setattr("devproxy.platform.get_platform", refuse)
    res = CliRunner().invoke(app, ["trust"])
    assert res.exit_code == 3, res.output
    assert "普通用户" in res.output
    assert "Traceback" not in res.output
