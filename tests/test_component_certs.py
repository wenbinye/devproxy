from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.components import certs as certs_module
from devproxy.components.certs import CertComponent
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.platform.base import Action
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
    # "探测工具坏了"不等于"CA 未被信任"：文案必须区分，避免诱导用户重复提权
    assert "无法判定" in status.detail
    assert "平台炸了" in status.detail
    assert "根证书未被信任" not in status.detail


def test_verify_reports_unknown_trust_probe_not_as_untrusted(tmp_path):
    """verify 同样不能把"判不了"说成"仍未被完全信任"。"""

    class Broken(FakePlatform):
        def ca_trusted(self, ca):
            raise RuntimeError("钥匙串被锁")

        def nss_trusted(self, ca):
            return False

    comp, platform, paths = _component(tmp_path, Broken())
    _write_ca(paths)
    status = comp.verify()
    assert status.ok is False
    assert "无法判定" in status.detail
    assert "钥匙串被锁" in status.detail
    assert "仍未被完全信任" not in status.detail


def test_check_degrades_when_platform_lacks_nss_trusted(tmp_path):
    """平台协议里没有 nss_trusted（如未来的 macOS 实现）时按“未信任”降级。"""

    class NoNss:
        name = "no-nss"

        def ca_trusted(self, ca):
            return True

    comp, _platform, paths = _component(tmp_path, NoNss())
    _write_ca(paths)
    status = comp.check()
    assert status.ok is False
    assert "NSS" in status.detail


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
    # 平台复查证书时默认会用 HOME 下的 NSS 库；这里把假平台的默认库
    # 同步成组件注入的库，否则 `_require_same_nss_db` 会（正确地）拒绝。
    platform.nss_db = tmp_path / "nssdb"
    comp = CertComponent(
        platform,
        paths,
        cfg,
        system_dest=tmp_path / "ca" / "devproxy-local.crt",
        nss_dir=tmp_path / "nssdb",
    )
    return comp, paths, cfg


def _certutil_installed(monkeypatch):
    monkeypatch.setattr(
        "devproxy.components.certs.shutil.which", lambda name: "/usr/bin/certutil"
    )


def _certutil_missing(monkeypatch):
    monkeypatch.setattr("devproxy.components.certs.shutil.which", lambda name: None)


def test_apply_writes_only_to_injected_targets(tmp_path, monkeypatch):
    _certutil_installed(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())
    assert (tmp_path / "ca" / "devproxy-local.crt").read_text() == "FAKE CA\n"
    assert ["update-ca-certificates"] in platform.commands
    assert any(c[:1] == ["certutil"] and "-A" in c for c in platform.commands)
    for written in platform.written:
        assert tmp_path in written.parents, f"写到了 tmp_path 之外: {written}"


def test_apply_runs_nss_before_system_ca(tmp_path, monkeypatch):
    _certutil_installed(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())
    names = [c[0] for c in platform.commands]
    assert names.index("certutil") < names.index("update-ca-certificates")


def test_apply_detects_certutil_by_binary_not_exit_code(tmp_path, monkeypatch):
    """certutil -V 需要 -n，真实机器上永远非零 —— 探测只能看二进制在不在。"""
    _certutil_installed(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())
    assert ["certutil", "-V"] not in platform.commands
    assert any(c[:1] == ["certutil"] and "-A" in c for c in platform.commands)


def test_apply_raises_when_certutil_missing(tmp_path, monkeypatch):
    _certutil_missing(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(comp.plan())
    assert "缺少 certutil" in str(ei.value)


def test_apply_still_installs_system_ca_when_nss_fails(tmp_path, monkeypatch):
    """两步互相独立：没有 certutil 不该连累 curl/Go 的系统信任库。"""
    _certutil_missing(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(comp.plan())
    assert "缺少 certutil" in str(ei.value)
    # NSS 失败之后，系统信任库那步仍然跑完了
    assert (tmp_path / "ca" / "devproxy-local.crt").exists()
    assert ["update-ca-certificates"] in platform.commands


def test_apply_still_installs_nss_when_system_ca_fails(tmp_path, monkeypatch):
    """拒绝 sudo / 提权失败时，不需要 root 的 NSS 步仍要跑完。"""
    _certutil_installed(monkeypatch)
    from devproxy.platform.fake import FakeElevated

    class CopyBoom(FakePlatform):
        def privilege(self):
            if self._elevated is None:

                class Elev(FakeElevated):
                    def copy(self, src, dest):
                        raise RuntimeError("copy 失败")

                self._elevated = Elev(self)
            return self._elevated

    platform = CopyBoom()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError, match="copy 失败"):
        comp.apply(comp.plan())
    assert any(c[:1] == ["certutil"] and "-A" in c for c in platform.commands)


def test_apply_preserves_permission_error(tmp_path, monkeypatch):
    """sudo 被拒是"提权失败"（退出码 4），不能被改写成普通 RuntimeError。"""
    _certutil_installed(monkeypatch)
    from devproxy.platform.fake import FakeElevated

    class SudoDenied(FakePlatform):
        def privilege(self):
            if self._elevated is None:

                class Elev(FakeElevated):
                    def copy(self, src, dest):
                        raise PermissionError("sudo: 密码错误")

                self._elevated = Elev(self)
            return self._elevated

    platform = SudoDenied()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(PermissionError) as ei:
        comp.apply(comp.plan())
    message = str(ei.value)
    assert "系统信任库" in message and "sudo: 密码错误" in message
    assert "其余 1 个步骤已执行" in message or "已执行" in message
    # NSS 那步照样装上了
    assert any(c[:1] == ["certutil"] and "-A" in c for c in platform.commands)


def test_apply_reports_every_failed_step(tmp_path, monkeypatch):
    _certutil_missing(monkeypatch)
    from devproxy.platform.fake import FakeElevated

    class AllBoom(FakePlatform):
        def privilege(self):
            if self._elevated is None:

                class Elev(FakeElevated):
                    def copy(self, src, dest):
                        raise RuntimeError("copy 失败")

                self._elevated = Elev(self)
            return self._elevated

    platform = AllBoom()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(comp.plan())
    message = str(ei.value)
    assert "certutil" in message
    assert "copy 失败" in message


def test_apply_raises_when_update_ca_certificates_fails(tmp_path, monkeypatch):
    """update-ca-certificates 失败必须报错 —— 否则 CA 只是躺在暂存目录里。"""
    _certutil_installed(monkeypatch)
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
    with pytest.raises(RuntimeError, match="update-ca-certificates"):
        comp.apply(comp.plan())


# -- NSS 安装：必须复查，不能只信 certutil -A 的返回码 ----------------------
#
# 真机现象：NSS 按 (issuer, serial) 去重 —— 同一张证书换个昵称入库时，
# `certutil -A` **静默无操作**（返回 0，条目并不新增）。只信返回码，
# 工具就会一直以为装好了，而库里根本没有自己那条。


class _StalePlatform(FakePlatform):
    """自有昵称下有陈旧条目，`-D` 执行成功后消失（删除后复验才通得过）。"""

    def __init__(self):
        super().__init__()
        self.stale = True

    def nss_stale_nickname(self, ca, nickname, nss_dir=None):
        return self.stale

    def nss_has_nickname(self, nickname, nss_dir=None):
        return self.stale

    def run(self, argv):
        result = super().run(argv)
        if argv[:1] == ["certutil"] and "-D" in argv:
            self.stale = False
        return result


class _NssFlagPlatform(FakePlatform):
    """库里有这张 CA，但缺 SSL 信任标志 —— 只有 -M 能补上。"""

    def __init__(self):
        super().__init__()
        self.flagged = False
        self.entries: list[tuple[str, str]] = []

    def nss_trusted(self, ca):
        return self.flagged

    def nss_entries(self, ca):
        return self.entries

    def run(self, argv):
        result = super().run(argv)
        if argv[:1] == ["certutil"] and "-M" in argv:
            self.flagged = True
        return result


def test_apply_adds_trust_flag_to_existing_entry(tmp_path, monkeypatch):
    """库里有这张 CA 但没信任标志 → 用 certutil -M 补上标志。"""
    _certutil_installed(monkeypatch)
    platform = _NssFlagPlatform()
    platform.entries = [("Caddy Local Authority", ",,")]
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())
    mods = [c for c in platform.commands if c[:1] == ["certutil"] and "-M" in c]
    assert mods, "缺信任标志时必须用 certutil -M 补标志"
    assert "Caddy Local Authority" in mods[0], "补的是库里那条的昵称"
    assert mods[0][mods[0].index("-t") + 1] == "C,,"


def test_apply_raises_when_cert_never_becomes_trusted(tmp_path, monkeypatch):
    """装完复查仍不是"已信任" → 必须报错，不能默默说成功。"""

    class NeverTrusted(_NssFlagPlatform):
        def run(self, argv):
            FakePlatform.run(self, argv)
            return 0, ""

    _certutil_installed(monkeypatch)
    platform = NeverTrusted()
    platform.entries = [("Caddy Local Authority", ",,")]
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(comp.plan())
    assert "信任标志" in str(ei.value)


def test_apply_skips_nss_install_when_already_trusted(tmp_path, monkeypatch):
    """已经信任时不该再动用户的库 —— 一条 certutil 命令都不该发。"""
    _certutil_installed(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    platform.nss.add(ca)
    comp.apply(
        [
            Action(
                description="安装 CA 根证书到 NSS 库",
                kind="run",
                command=["install-nss-ca", str(ca), str(tmp_path / "nssdb")],
                privileged=False,
            )
        ]
    )
    assert not [c for c in platform.commands if c[:1] == ["certutil"]], (
        "已信任的库不该被反复重装"
    )


# -- fix round 1：偏差点 ----------------------------------------------------


def test_apply_fails_loud_when_component_nss_dir_differs(tmp_path, monkeypatch):
    """组件写入的库与平台复查的库不一致 → 显式报错，不许静默查另一个库。

    复查是"`certutil -A` 说谎"那张安全网；查错库等于安全网失效，
    而且会静默地"安装成功"。
    """
    _certutil_installed(monkeypatch)

    class WithDbPath(FakePlatform):
        def nss_db_path(self):
            return Path("/platform/nssdb")

    platform = WithDbPath()
    comp = CertComponent(
        platform,
        Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data"),
        Config(storage=str(tmp_path / "data")),
        system_dest=tmp_path / "ca" / "devproxy-local.crt",
        nss_dir=tmp_path / "nssdb",
    )
    comp.paths.ensure_files()
    _write_ca(comp.paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(
            [
                Action(
                    description="安装 CA 根证书到 NSS 库",
                    kind="run",
                    command=["install-nss-ca", str(comp.ca_path), str(tmp_path / "nssdb")],
                    privileged=False,
                )
            ]
        )
    message = str(ei.value)
    assert "不一致" in message
    assert "/platform/nssdb" in message and str(tmp_path / "nssdb") in message
    assert platform.commands == [], "报错前不该往任何一个库写东西"


def test_apply_skips_when_platform_db_path_matches(tmp_path, monkeypatch):
    """两边一致（同一个库）时照常走，不误报。"""
    _certutil_installed(monkeypatch)

    class WithDbPath(FakePlatform):
        def nss_db_path(self):
            return tmp_path / "nssdb"

    platform = WithDbPath()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())  # 不抛异常即可
    assert any(c[:1] == ["certutil"] and "-A" in c for c in platform.commands)


def test_apply_repair_error_keeps_last_modify_output(tmp_path, monkeypatch):
    """补标志失败时，错误信息必须带上**最后一次 -M** 的输出。

    `-A` 返回 0 时它的输出是空的，只留它会报成 "certutil: "，
    等于什么都没说。
    """
    _certutil_installed(monkeypatch)

    class ModifyFails(_NssFlagPlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            if argv[:1] == ["certutil"] and "-M" in argv:
                return 255, "certutil: unable to modify trust"
            return 0, ""

        def nss_trusted(self, ca):
            return False  # 永远修不好

    platform = ModifyFails()
    platform.entries = [("Caddy Local Authority", ",,")]
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(comp.plan())
    message = str(ei.value)
    assert "unable to modify trust" in message, "-M 的真实输出不能被丢掉"
    assert "Caddy Local Authority" in message, "错误信息要报出库里的实际条目"


def test_apply_repair_uses_platform_flag_helper(tmp_path, monkeypatch):
    """补标志时用平台给的三列标志（保留 S/MIME / JAR），不是硬编码 C,,。"""
    _certutil_installed(monkeypatch)

    class Flags(_NssFlagPlatform):
        def nss_trust_flags(self, flags):
            parts = flags.split(",")
            return ",".join(["C", *parts[1:]])

    platform = Flags()
    platform.entries = [("Caddy Local Authority", ",C,")]
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())
    mods = [c for c in platform.commands if c[:1] == ["certutil"] and "-M" in c]
    assert mods and mods[0][mods[0].index("-t") + 1] == "C,C,", (
        "只补 SSL 列，S/MIME 的信任必须保留"
    )


def test_check_fails_loud_when_stale_state_is_unknown(tmp_path):
    """三态原样透传：判不了就不能报"没有陈旧条目"，也不能静默跳过清理。"""

    class UnknownStale(FakePlatform):
        def nss_stale_nickname(self, ca, nickname, nss_dir=None):
            return None

    platform = UnknownStale()
    comp, _platform, paths = _component(tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)

    status = comp.check()
    assert status.ok is False
    assert "无法判定" in status.detail
    actions = comp.plan()
    assert [a.command[0] for a in actions] == ["remove-stale-nss-ca"], (
        "判不了时 plan 必须产出清理动作，让 apply 显式报错而不是静默跳过"
    )


def test_plan_offers_stale_cleanup_when_ca_trusted(tmp_path, monkeypatch):
    """CA 已信任但自有昵称下残留旧 CA → plan() 产出非特权的清理动作。

    这个场景下 apply() 只有在 plan() 非空时才会被调用 ——
    清理动作必须由 plan() 自己产出，否则生产上永远执行不到。
    """
    _certutil_installed(monkeypatch)

    platform = _StalePlatform()
    comp, _platform, paths = _component(tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)

    status = comp.check()
    assert status.ok is True
    assert "陈旧" in status.detail
    assert "devproxy trust" in status.hint, "修复列要能直接给出动作"

    actions = comp.plan()
    assert [a.command[0] for a in actions] == ["remove-stale-nss-ca"]
    assert actions[0].privileged is False
    assert str(ca) in actions[0].command


def test_remove_stale_action_runs_certutil_delete(tmp_path, monkeypatch):
    _certutil_installed(monkeypatch)

    platform = _StalePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    comp.apply(
        [
            Action(
                description="清理 NSS 库中工具昵称下的陈旧 CA 根证书",
                kind="run",
                command=["remove-stale-nss-ca", str(ca), str(tmp_path / "nssdb")],
                privileged=False,
            )
        ]
    )
    deletes = [c for c in platform.commands if c[:1] == ["certutil"] and "-D" in c]
    assert deletes and certs_module.CA_NAME in deletes[0]


def test_remove_stale_action_raises_when_delete_fails(tmp_path, monkeypatch):
    """`certutil -D` 失败且条目仍在 → 必须报错，不能静默吞掉。

    旧根会被浏览器永久信任；吞掉失败就等于工具报"已就绪"而旧根还留着。
    """
    _certutil_installed(monkeypatch)

    class DeleteFails(FakePlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            if argv[:1] == ["certutil"] and "-D" in argv:
                return 255, "certutil: unable to delete (database locked)"
            return 0, ""

        def nss_stale_nickname(self, ca, nickname, nss_dir=None):
            return True

        def nss_has_nickname(self, nickname, nss_dir=None):
            return True  # 条目确实还在

    platform = DeleteFails()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(
            [
                Action(
                    description="清理 NSS 库中工具昵称下的陈旧 CA 根证书",
                    kind="run",
                    command=["remove-stale-nss-ca", str(ca), str(tmp_path / "nssdb")],
                    privileged=False,
                )
            ]
        )
    assert "database locked" in str(ei.value)


def test_remove_stale_action_raises_when_delete_unverifiable(tmp_path, monkeypatch):
    """库被锁 / 不可读时 `-L` 也非零 —— 判不了就必须报错，不能当"条目不存在"。"""
    _certutil_installed(monkeypatch)

    class Locked(FakePlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            if argv[:1] == ["certutil"] and "-D" in argv:
                return 255, "certutil: function failed: SEC_ERROR_BAD_DATABASE"
            return 0, ""

        def nss_stale_nickname(self, ca, nickname, nss_dir=None):
            return True

        def nss_has_nickname(self, nickname, nss_dir=None):
            return None  # 判不了

    platform = Locked()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(
            [
                Action(
                    description="清理 NSS 库中工具昵称下的陈旧 CA 根证书",
                    kind="run",
                    command=["remove-stale-nss-ca", str(ca), str(tmp_path / "nssdb")],
                    privileged=False,
                )
            ]
        )
    message = str(ei.value)
    assert "无法确认" in message
    assert "SEC_ERROR_BAD_DATABASE" in message


def test_remove_stale_action_raises_when_delete_reports_success_but_fails(
    tmp_path, monkeypatch
):
    """`-D` 返回 0 但条目仍在（库目录只读）→ 必须报错。"""
    _certutil_installed(monkeypatch)

    class SilentNoop(FakePlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            return 0, ""  # 假装成功，但下面 stale 仍是 True

        def nss_stale_nickname(self, ca, nickname, nss_dir=None):
            return True

        def nss_has_nickname(self, nickname, nss_dir=None):
            return True

    platform = SilentNoop()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(
            [
                Action(
                    description="清理 NSS 库中工具昵称下的陈旧 CA 根证书",
                    kind="run",
                    command=["remove-stale-nss-ca", str(ca), str(tmp_path / "nssdb")],
                    privileged=False,
                )
            ]
        )
    assert "未生效" in str(ei.value)


def test_remove_stale_action_tolerates_missing_entry(tmp_path, monkeypatch):
    """并发下条目已不存在 → 目标状态已达成，幂等成功。"""
    _certutil_installed(monkeypatch)

    class AlreadyGone(FakePlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            if argv[:1] == ["certutil"] and "-D" in argv:
                return 255, 'certutil: could not find certificate named "x"'
            return 0, ""

        def nss_stale_nickname(self, ca, nickname, nss_dir=None):
            return True

        def nss_has_nickname(self, nickname, nss_dir=None):
            return False

    platform = AlreadyGone()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    comp.apply(
        [
            Action(
                description="清理 NSS 库中工具昵称下的陈旧 CA 根证书",
                kind="run",
                command=["remove-stale-nss-ca", str(ca), str(tmp_path / "nssdb")],
                privileged=False,
            )
        ]
    )  # 不抛异常


def test_remove_stale_action_uses_message_when_platform_cannot_probe(
    tmp_path, monkeypatch
):
    """平台不提供 nss_has_nickname 时按 certutil 的输出判断"不存在"。"""
    _certutil_installed(monkeypatch)

    class NoProbe(FakePlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            if argv[:1] == ["certutil"] and "-D" in argv:
                return 255, 'certutil: could not find certificate named "x"'
            return 0, ""

        def nss_stale_nickname(self, ca, nickname, nss_dir=None):
            return True

    platform = NoProbe()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    comp.apply(
        [
            Action(
                description="清理 NSS 库中工具昵称下的陈旧 CA 根证书",
                kind="run",
                command=["remove-stale-nss-ca", str(ca), str(tmp_path / "nssdb")],
                privileged=False,
            )
        ]
    )


def test_repair_flags_raises_when_platform_lacks_flag_helper(tmp_path, monkeypatch):
    """平台给不出完整标志时就报错 —— 绝不退回 "C,," 抹掉 S/MIME/JAR 列。"""
    _certutil_installed(monkeypatch)

    class NoFlagHelper(_NssFlagPlatform):
        nss_trust_flags = None

    platform = NoFlagHelper()
    platform.entries = [("Caddy Local Authority", ",,")]
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)

    with pytest.raises(RuntimeError, match="nss_trust_flags"):
        comp.apply(comp.plan())
    mods = [c for c in platform.commands if c[:1] == ["certutil"] and "-M" in c]
    assert mods == [], "标志算不出来时不能发 -M，更不能按 C,, 覆盖"


def test_signature_probe_does_not_swallow_internal_type_error(tmp_path):
    """平台实现内部抛出的 TypeError 不能被误读成"签名不匹配"。"""
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)

    calls = []

    def nss_entries(ca):
        calls.append(ca)
        raise TypeError("平台内部错误")

    platform.nss_entries = nss_entries
    assert comp._nss_entries(ca, nss_dir=tmp_path / "nssdb") == []
    assert len(calls) == 1, "签名探测失败不能导致重复调用"


def test_apply_unknown_action_is_a_clean_error(tmp_path):
    comp, _platform, _paths = _component(tmp_path)
    with pytest.raises(RuntimeError, match="未知动作"):
        comp.apply(
            [Action(description="未知步骤", kind="run", command=["bogus-action"])]
        )


def test_apply_drops_stale_own_entry_when_trusted_elsewhere(tmp_path, monkeypatch):
    """已信任但自有昵称下残留**旧 CA** → 定向删除那一条。"""
    _certutil_installed(monkeypatch)

    platform = _StalePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    platform.nss.add(ca)  # 平台判定已信任（异昵称）
    comp.apply(
        [
            Action(
                description="安装 CA 根证书到 NSS 库",
                kind="run",
                command=["install-nss-ca", str(ca), str(tmp_path / "nssdb")],
                privileged=False,
            )
        ]
    )
    deletes = [c for c in platform.commands if c[:1] == ["certutil"] and "-D" in c]
    assert deletes, "陈旧的自有条目必须删掉"
    assert certs_module.CA_NAME in deletes[0]
    assert not [c for c in platform.commands if c[:1] == ["certutil"] and "-A" in c], (
        "已信任时不该重装"
    )


def test_verify_ok_after_trust(tmp_path, monkeypatch):
    _certutil_installed(monkeypatch)
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


def test_verify_checks_real_https_request(tmp_path, monkeypatch):
    """信任库装好还不够 —— 真发一次 HTTPS 请求确认端到端可用。"""
    _certutil_installed(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    comp.apply(comp.plan())
    _trust_both(platform, ca)
    paths.sites_caddy.write_text("localhost { respond \"hi\" }\n", encoding="utf-8")
    assert comp.verify().ok is True
    assert "https://localhost" in platform.http_probes


def test_verify_not_ok_when_https_probe_fails(tmp_path):
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    paths.sites_caddy.write_text("localhost { respond \"hi\" }\n", encoding="utf-8")
    platform.http_result = False
    status = comp.verify()
    assert status.ok is False
    assert "https://localhost" in status.detail
    assert status.hint


def test_verify_skips_https_probe_when_no_target(tmp_path):
    """没有 localhost 站点也没有路由时，不该拿 localhost 硬探。"""
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    status = comp.verify()
    assert status.ok is True
    assert "暂无可探测站点" in status.detail
    assert platform.http_probes == []


def test_verify_does_not_treat_localhost_substring_as_site(tmp_path):
    """`foo.localhost.test` 不是 localhost 站点 —— 不能被当成可探目标。"""
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    paths.sites_caddy.write_text(
        "foo.localhost.test { respond \"hi\" }\n", encoding="utf-8"
    )
    status = comp.verify()
    assert status.ok is True
    assert "暂无可探测站点" in status.detail
    assert platform.http_probes == []


def test_verify_probes_first_registered_route(tmp_path):
    """有路由时必须探测真实域名 —— localhost 没有站点，验的是匹配不是信任。"""
    import json

    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    paths.routes_json.write_text(
        json.dumps({"version": 1, "routes": [{"domain": "codeup.test"}]}),
        encoding="utf-8",
    )
    # 即便机器上同时有 localhost 站点，也应优先探真实路由
    paths.sites_caddy.write_text("localhost { respond \"hi\" }\n", encoding="utf-8")
    assert comp.verify().ok is True
    assert platform.http_probes == ["https://codeup.test"]


# -- CLI：trust / logs ------------------------------------------------------


def _patch_cli(monkeypatch, tmp_path, platform):
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context", lambda *a, **k: (paths, cfg, None, None)
    )
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    return paths, cfg


def _patch_cert_component(monkeypatch, tmp_path):
    def build(p, paths_, cfg_):
        # 组件注入的 NSS 库必须同时成为假平台的默认库，
        # 否则 `_require_same_nss_db` 会判定“写入库 A、复查库 B”。
        p.nss_db = tmp_path / "nssdb"
        return CertComponent(
            p,
            paths_,
            cfg_,
            system_dest=tmp_path / "ca" / "devproxy-local.crt",
            nss_dir=tmp_path / "nssdb",
        )

    monkeypatch.setattr("devproxy.cli._shared.CertComponent", build)


def test_cli_trust_is_idempotent(tmp_path, monkeypatch):
    platform = FakePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    result = CliRunner().invoke(app, ["trust"])
    assert result.exit_code == 0
    assert "CA 根证书已信任，无需变更。" in result.stdout
    assert platform.commands == []


def test_cli_trust_reports_cleanup_not_install(tmp_path, monkeypatch):
    """只做清理时不能说"已安装 CA 根证书" —— 那是两回事。"""
    _certutil_installed(monkeypatch)
    platform = _StalePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)

    result = CliRunner().invoke(app, ["trust", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "已清理" in result.stdout
    assert "已安装" not in result.stdout


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
    _certutil_installed(monkeypatch)
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
    _certutil_missing(monkeypatch)
    platform = FakePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    _write_ca(paths)
    _patch_cert_component(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["trust", "--yes"])
    assert result.exit_code == 1
    assert "certutil" in result.stderr
    assert "Traceback" not in result.stdout


def test_cli_trust_exits_4_when_sudo_denied(tmp_path, monkeypatch):
    """sudo 被拒是提权问题（退出码 4），不是普通运行失败。"""
    from devproxy.platform.fake import FakeElevated

    class SudoDenied(FakePlatform):
        def privilege(self):
            if self._elevated is None:

                class Elev(FakeElevated):
                    def copy(self, src, dest):
                        raise PermissionError("sudo: 密码错误")

                self._elevated = Elev(self)
            return self._elevated

    _certutil_installed(monkeypatch)
    platform = SudoDenied()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    _write_ca(paths)
    _patch_cert_component(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["trust", "--yes"])
    assert result.exit_code == 4
    assert "sudo: 密码错误" in result.stderr


def test_cli_logs_rejects_unknown_service(tmp_path, monkeypatch):
    platform = FakePlatform()
    _patch_cli(monkeypatch, tmp_path, platform)
    result = CliRunner().invoke(app, ["logs", "nginx"])
    # 参数错误按全局约定退出 2（§9.1），不是运行失败 1
    assert result.exit_code == 2


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


def test_cli_logs_missing_journalctl_is_clean_error(tmp_path, monkeypatch):
    """journalctl 不存在时不能把 FileNotFoundError 漏成 traceback。"""

    class MissingJournal(FakePlatform):
        def tail_service_log(self, name):
            raise FileNotFoundError("journalctl")

    platform = MissingJournal()
    _patch_cli(monkeypatch, tmp_path, platform)
    result = CliRunner().invoke(app, ["logs", "caddy"])
    assert result.exit_code == 1
    assert "journalctl" in result.stderr
    assert "Traceback" not in result.output


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


# -- macOS：CA 进系统钥匙串，没有 NSS 这一步 --------------------------------


class _MacPlatform(FakePlatform):
    """macOS 平台：没有 NSS 库，CA 只装进系统钥匙串。"""

    name = "mac"
    dns_port = 5300

    def nss_trusted(self, ca):
        return True


def test_macos_plan_installs_ca_into_system_keychain(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    _write_ca(paths)
    actions = comp.plan()
    assert len(actions) == 1, "macOS 没有 NSS 库 —— 只装系统钥匙串"
    assert actions[0].command[0] == "install-macos-ca"
    assert actions[0].privileged is True


def test_macos_plan_skips_nss_step(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    _write_ca(paths)
    actions = comp.plan()
    assert not any(a.command[:1] == ["install-nss-ca"] for a in actions)


def test_macos_plan_is_empty_when_keychain_trusts(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    ca = _write_ca(paths)
    platform.trusted.add(ca)
    assert comp.plan() == []


def test_macos_apply_adds_trusted_cert_to_system_keychain(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    ca = _write_ca(paths)
    comp.apply(comp.plan())
    assert [
        "security",
        "add-trusted-cert",
        "-d",
        "-r",
        "trustRoot",
        "-k",
        certs_module.SYSTEM_KEYCHAIN,
        str(ca),
    ] in platform.commands


def test_macos_apply_reports_keychain_failure(tmp_path):
    """钥匙串写失败必须报错 —— 静默失败会让用户以为已经信任了。"""

    class Failing(_MacPlatform):
        def privilege(self):
            class _S:
                def run(self, argv):
                    return 1, "boom"

                def write(self, path, content):
                    pass

                def read(self, path):
                    return ""

                def copy(self, src, dest):
                    pass

            return _S()

    comp, platform, paths = _component(tmp_path, platform=Failing())
    _write_ca(paths)
    with pytest.raises(RuntimeError, match="钥匙串"):
        comp.apply(comp.plan())


def test_macos_check_reports_keychain(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    _write_ca(paths)
    status = comp.check()
    assert status.ok is False
    assert "钥匙串" in status.detail


def test_macos_check_ok_when_keychain_trusts(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    ca = _write_ca(paths)
    platform.trusted.add(ca)
    status = comp.check()
    assert status.ok is True
    assert "钥匙串" in status.detail


def test_macos_verify_ok_when_keychain_trusts(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    ca = _write_ca(paths)
    platform.trusted.add(ca)
    assert comp.verify().ok is True


def test_linux_plan_still_has_two_steps(tmp_path):
    """Linux 路径不变：系统信任库 + NSS 两步都在。"""
    comp, platform, paths = _component(tmp_path)
    _write_ca(paths)
    actions = comp.plan()
    assert len(actions) == 2
    assert {a.command[0] for a in actions} == {"install-nss-ca", "install-system-ca"}


class _DeniedKeychainPlatform(_MacPlatform):
    """提权会话能建立，但 `security add-trusted-cert` 被系统拒绝。

    真机（macOS 26，SSH 会话）实测的原文：
    `SecTrustSettingsSetTrustSettings: The authorization was denied since no
    user interaction was possible.`
    """

    def privilege(self):
        platform = self

        class _Denied:
            def run(self, argv):
                platform.commands.append(list(argv))
                return 1, (
                    "SecTrustSettingsSetTrustSettings: The authorization was "
                    "denied since no user interaction was possible."
                )

            def write(self, path, content):  # pragma: no cover - 本用例不走
                raise AssertionError("不应走到这里")

            def read(self, path):  # pragma: no cover
                raise AssertionError("不应走到这里")

            def copy(self, src, dest):  # pragma: no cover
                raise AssertionError("不应走到这里")

        return _Denied()


def test_macos_ca_denied_without_gui_raises_permission_error(tmp_path):
    """SSH 会话里 macOS 拒绝修改系统信任设置 —— 必须是可操作的中文提示。

    原先这里抛的是 RuntimeError（信息只有系统原文），CLI 映射成退出码 1，
    用户既不知道原因也不知道该去哪做。这类失败的本质是"需要人去那台
    Mac 的图形界面点一下"，所以应当抛 PermissionError（CLI 据此退 4），
    并在信息里直接给出该在哪运行什么。
    """
    comp, _platform, paths = _component(tmp_path, platform=_DeniedKeychainPlatform())
    _write_ca(paths)
    actions = comp.plan()
    assert actions and actions[0].command[0] == "install-macos-ca"

    with pytest.raises(PermissionError) as cm:
        comp.apply(actions)
    message = str(cm.value)
    assert "图形界面" in message or "终端" in message, "必须说清该在哪做"
    assert "devproxy trust" in message, "必须给出可执行的具体命令"
    assert "no user interaction" in message, "系统原文要保留，便于查证"


def test_cli_trust_exits_4_when_macos_denies_without_gui(tmp_path, monkeypatch):
    """CLI 层：这类失败必须是退出码 4（需要人工交互），不是 1。

    退出码 4 是给脚本/CI 看的信号："不是你命令写错了，是需要有人在
    那台机器的图形界面里点一下"。给 1 会被当成程序故障。
    """
    platform = _DeniedKeychainPlatform()
    paths, _cfg = _patch_cli(monkeypatch, tmp_path, platform)
    _write_ca(paths)

    result = CliRunner().invoke(app, ["trust", "--yes"])
    assert result.exit_code == 4, result.stdout
    # 提示走 err_console（stderr）—— 成功信息才走 stdout
    shown = (result.stdout or "") + (result.stderr or "")
    assert "终端" in shown or "图形界面" in shown
    assert "devproxy trust" in shown


# -- Firefox：有自己的证书库，NSS/系统信任库对它无效 -----------------------


def _make_firefox_profile(home: Path, name: str = "abc.default-release") -> Path:
    """造一个用过的 Firefox profile（含证书库 cert9.db）。"""
    profile = home / ".mozilla" / "firefox" / name
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "cert9.db").write_bytes(b"")
    return profile


def test_firefox_profile_dirs_empty_without_firefox(tmp_path):
    assert certs_module.firefox_profile_dirs(tmp_path) == []


def test_firefox_profile_dirs_finds_profile(tmp_path):
    profile = _make_firefox_profile(tmp_path)
    assert certs_module.firefox_profile_dirs(tmp_path) == [profile]


def test_firefox_profile_dirs_ignores_non_profile_dirs(tmp_path):
    """~/.mozilla/firefox 下的杂物目录（如 Crash Reports）不是 profile。"""
    (tmp_path / ".mozilla" / "firefox" / "Crash Reports").mkdir(parents=True)
    assert certs_module.firefox_profile_dirs(tmp_path) == []


def test_firefox_profile_dirs_probe_failure_is_not_an_error(tmp_path, monkeypatch):
    """探不到就当没有 Firefox —— 不能抛异常，更不能当成"有 Firefox"。"""

    def boom(*args, **kwargs):
        raise OSError("权限不足")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert certs_module.firefox_profile_dirs(tmp_path) == []


def test_check_hints_firefox_when_installed(tmp_path, isolated_home):
    """本机有 Firefox：hint 必须给出可操作的手动信任指引。"""
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    _make_firefox_profile(isolated_home)
    status = comp.check()
    assert status.ok is True, "Firefox 没装 CA 不是缺陷，不能因此判失败"
    assert "Firefox" in status.hint
    assert "security.enterprise_roots.enabled" in status.hint


def test_check_does_not_mention_firefox_when_absent(tmp_path):
    """没装 Firefox 就别提 —— 不制造噪音。"""
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    status = comp.check()
    assert status.ok is True
    assert "Firefox" not in status.hint


def test_check_firefox_hint_survives_missing_trust(tmp_path, isolated_home):
    """CA 还没被信任时，hint 要同时给出 trust 与 Firefox 的解法。"""
    comp, platform, paths = _component(tmp_path)
    _write_ca(paths)
    _make_firefox_profile(isolated_home)
    status = comp.check()
    assert status.ok is False
    assert "devproxy trust" in status.hint
    assert "Firefox" in status.hint


def test_firefox_note_none_without_ca(tmp_path, isolated_home):
    """还没有 CA 时没什么可导入的 —— 不提示。"""
    comp, _platform, _paths = _component(tmp_path)
    _make_firefox_profile(isolated_home)
    assert comp.firefox_note() is None


def test_firefox_note_points_at_ca_path(tmp_path, isolated_home):
    """提示要说清导入哪个文件。"""
    comp, _platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    _make_firefox_profile(isolated_home)
    note = comp.firefox_note()
    assert note is not None
    assert str(ca) in note
    assert "security.enterprise_roots.enabled" in note


def test_check_never_raises_when_firefox_probe_breaks(tmp_path, isolated_home, monkeypatch):
    """探测炸了也不能让 check() 抛异常 —— doctor 依赖它一次列完。"""

    def boom(*args, **kwargs):
        raise RuntimeError("探不了")

    monkeypatch.setattr(certs_module, "firefox_profile_dirs", boom)
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    status = comp.check()
    assert status.ok is True
    assert "Firefox" not in status.hint
