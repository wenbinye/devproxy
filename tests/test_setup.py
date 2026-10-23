from pathlib import Path

from devproxy.platform.base import Action, Status
from devproxy.lifecycle.setup import apply_plan, build_plan


class FakeComponent:
    def __init__(self, name, status, actions=()):
        self.name = name
        self._status = status
        self._actions = list(actions)
        self.applied = None

    def check(self):
        return self._status

    def plan(self):
        return list(self._actions)

    def apply(self, actions):
        self.applied = actions


def test_build_plan_collects_reports():
    c = FakeComponent("caddy", Status(ok=True))
    plan = build_plan([c])
    assert len(plan.reports) == 1
    assert plan.reports[0].name == "caddy"
    assert plan.is_noop is True


def test_plan_needs_sudo_lists_privileged_actions():
    c = FakeComponent(
        "caddy",
        Status(ok=False, detail="缺"),
        [Action(description="setcap", privileged=True), Action(description="写文件")],
    )
    plan = build_plan([c])
    assert len(plan.needs_sudo) == 1
    assert plan.needs_sudo[0].description == "setcap"
    assert plan.is_noop is False


def test_apply_plan_applies_each_component():
    c1 = FakeComponent("a", Status(ok=True), [Action(description="x")])
    c2 = FakeComponent("b", Status(ok=True), [Action(description="y")])
    plan = build_plan([c1, c2])
    results = apply_plan(plan)
    assert c1.applied is not None and c2.applied is not None
    assert {name for name, _ in results} == {"a", "b"}


def test_apply_plan_continues_after_failure():
    class Boom(FakeComponent):
        def apply(self, actions):
            raise RuntimeError("炸了")

    good = FakeComponent("good", Status(ok=True), [Action(description="x")])
    bad = Boom("bad", Status(ok=True), [Action(description="y")])
    results = apply_plan(build_plan([good, bad]))
    statuses = dict(results)
    assert statuses["bad"].ok is False
    assert "炸了" in statuses["bad"].detail
    assert statuses["good"].ok is True


def test_apply_plan_uses_verify_when_available():
    class WithVerify(FakeComponent):
        def verify(self):
            return Status(ok=True, detail="verified")

    c = WithVerify("a", Status(ok=False, detail="缺"), [Action(description="x")])
    results = apply_plan(build_plan([c]))
    assert dict(results)["a"].detail == "verified"


def test_build_plan_keeps_component_reference():
    c = FakeComponent("a", Status(ok=True))
    plan = build_plan([c])
    assert plan.reports[0].component is c


def test_build_plan_isolates_check_exception():
    """某个组件的 check() 抛异常，不能带崩整个 setup。"""

    class BoomCheck(FakeComponent):
        def check(self):
            raise RuntimeError("检查炸了")

    bad = BoomCheck("bad", Status(ok=True))
    good = FakeComponent("good", Status(ok=True))
    plan = build_plan([bad, good])
    assert [r.name for r in plan.reports] == ["bad", "good"]
    assert plan.reports[0].status.ok is False
    assert "检查炸了" in plan.reports[0].status.detail
    assert plan.reports[1].status.ok is True


def test_build_plan_isolates_plan_exception():
    class BoomPlan(FakeComponent):
        def plan(self):
            raise RuntimeError("算计划炸了")

    bad = BoomPlan("bad", Status(ok=True))
    good = FakeComponent("good", Status(ok=True))
    plan = build_plan([bad, good])
    assert plan.reports[0].status.ok is False
    assert "算计划炸了" in plan.reports[0].status.detail
    assert plan.reports[0].actions == []
    assert plan.reports[1].status.ok is True


def test_apply_plan_keeps_plan_failure_even_if_verify_would_pass():
    """plan() 抛异常时组件什么都没做 —— verify() 成功也不能覆盖失败。"""

    class PlanBoom(FakeComponent):
        def plan(self):
            raise RuntimeError("配置读不出来")

        def verify(self):
            return Status(ok=True, detail="服务在跑")

    c = PlanBoom("bad", Status(ok=True))
    results = apply_plan(build_plan([c]))
    status = dict(results)["bad"]
    assert status.ok is False
    assert "配置读不出来" in status.detail


def test_apply_plan_keeps_check_failure_even_if_verify_would_pass():
    class CheckBoom(FakeComponent):
        def check(self):
            raise PermissionError("二进制不可执行")

        def verify(self):
            return Status(ok=True, detail="服务在跑")

    c = CheckBoom("bad", Status(ok=True))
    results = apply_plan(build_plan([c]))
    status = dict(results)["bad"]
    assert status.ok is False
    assert "二进制不可执行" in status.detail


# -- CLI：setup 编排 --------------------------------------------------------


class FakeCliComponent:
    """CLI 用的假组件：记录 apply 调用。"""

    def __init__(self, name, status=None, actions=(), fail=False):
        self.name = name
        self._status = status or Status(ok=True, detail="就绪")
        self._actions = list(actions)
        self._fail = fail
        self.applied = None

    def check(self):
        return self._status

    def plan(self):
        return list(self._actions)

    def apply(self, actions):
        if self._fail:
            raise RuntimeError("炸了")
        self.applied = actions

    def verify(self):
        return Status(ok=True, detail="已生效")


def _fake_components_seam(components):
    """模拟 `_components` 的接缝，包括 `include_cert=False` 的过滤语义。"""

    def build(paths, config, dns_backend=None, platform=None, *, include_cert=True):
        selected = list(components)
        if not include_cert:
            # 生产实现按 CertComponent 类型过滤；测试替身没有类型可认，
            # 用 name 模拟同一语义
            selected = [c for c in selected if getattr(c, "name", None) != "cert"]
        return selected

    return build


def _patch_cli(monkeypatch, tmp_path, components):
    from devproxy.core.config import Config
    from devproxy.core.paths import Paths
    from devproxy.platform.fake import FakePlatform

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context", lambda *a, **k: (paths, cfg, None, None)
    )
    monkeypatch.setattr(
        "devproxy.cli._shared._components", _fake_components_seam(components)
    )
    # 平台也必须换成假的：setup 的迁移检测会去看真实单元目录，
    # 迁移一旦跑起来就会改写用户机器上的 caddy.service
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: FakePlatform())
    return paths, cfg


def test_cli_setup_noop_exits_ok(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [FakeCliComponent("caddy")]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup"])
    assert result.exit_code == 0, result.stdout
    assert "所有组件已就绪，无需变更。" in result.stdout
    assert comps[0].applied is None


def test_cli_setup_refuses_privileged_without_yes(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCliComponent(
            "caddy",
            Status(ok=False, detail="缺"),
            [Action(description="写 /etc", privileged=True)],
        )
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup"])
    assert result.exit_code == 4
    assert "写 /etc" in result.stdout
    assert comps[0].applied is None


def test_cli_setup_applies_with_yes(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCliComponent(
            "caddy",
            Status(ok=False, detail="缺"),
            [Action(description="写 /etc", privileged=True)],
        )
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert comps[0].applied is not None
    assert "caddy" in result.stdout


def test_cli_setup_reports_failure_with_exit_1(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCliComponent(
            "bad", Status(ok=False, detail="缺"), [Action(description="x")], fail=True
        ),
        FakeCliComponent(
            "good", Status(ok=False, detail="缺"), [Action(description="y")]
        ),
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 1
    assert "炸了" in result.stdout
    assert comps[1].applied is not None


def test_cli_setup_no_trust_skips_cert_component(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [FakeCliComponent("cert"), FakeCliComponent("caddy")]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--no-trust", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "cert" not in result.stdout


def test_cli_setup_domain_suffix_override(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    seen = {}

    def fake_components(
        paths, config, dns_backend="auto", platform=None, *, include_cert=True
    ):
        seen["suffix"] = config.domain_suffix
        seen["backend"] = dns_backend
        return [FakeCliComponent("caddy")]

    _patch_cli(monkeypatch, tmp_path, [])
    monkeypatch.setattr("devproxy.cli._shared._components", fake_components)
    result = CliRunner().invoke(
        app, ["setup", "--domain-suffix", "local", "--dns-backend", "resolved"]
    )
    assert result.exit_code == 0, result.stdout
    assert seen["suffix"] == "local"
    assert seen["backend"] == "resolved"


def test_cli_setup_rejects_invalid_domain_suffix(tmp_path, monkeypatch):
    """--domain-suffix 会流进 Caddyfile 与 /etc 下的配置 —— 必须过配置校验。"""
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [FakeCliComponent("caddy")]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--domain-suffix", "bad\nsuffix"])
    assert result.exit_code == 2
    assert comps[0].applied is None


def test_cli_setup_rejects_invalid_dns_backend(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [FakeCliComponent("caddy")]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--dns-backend", "foo"])
    assert result.exit_code == 2
    assert comps[0].applied is None


def test_cli_setup_noop_with_unhealthy_component_exits_precondition(
    tmp_path, monkeypatch
):
    """没有可执行的动作、但组件本身不健康 —— 不能谎报「已就绪」。"""
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [FakeCliComponent("caddy", Status(ok=False, detail="坏了"))]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup"])
    assert result.exit_code == 3
    assert "坏了" in result.stdout or "坏了" in result.stderr


class FakeCertComponent(FakeCliComponent):
    """带结构化 ca_path 的假证书组件（`_status_cell` 用它判断提示状态）。"""

    def __init__(self, name, status, ca_path):
        super().__init__(name, status)
        self.ca_path = ca_path


def _write_ca(storage: Path) -> Path:
    ca = Path(storage) / "pki" / "authorities" / "local" / "root.crt"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", encoding="utf-8")
    return ca


def test_cli_setup_shows_cert_note_without_useless_trust_hint(tmp_path, monkeypatch):
    """CA 尚未生成时表格展示提示；但没有 CA 就没什么可 trust，不该提示。"""
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCertComponent(
            "cert",
            Status(ok=True, detail="尚无 CA 根证书（将在首条路由注册时由 Caddy 生成）"),
            tmp_path / "missing" / "root.crt",
        )
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "尚无 CA" in result.stdout
    assert "devproxy trust" not in result.stdout


def test_cli_setup_prints_trust_hint_when_ca_exists_but_untrusted(tmp_path, monkeypatch):
    """第二次 setup（CA 已生成但未信任）才提示 trust。"""
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.platform.fake import FakePlatform

    _paths, cfg = _patch_cli(monkeypatch, tmp_path, [FakeCliComponent("caddy")])
    platform = FakePlatform()
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    _write_ca(cfg.storage_path)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "devproxy trust" in result.stdout


def test_cli_setup_no_trust_hint_when_ca_already_trusted(tmp_path, monkeypatch):
    """CA 已完全信任时不再重复提示（M8）。"""
    from typer.testing import CliRunner

    from devproxy.cli import app
    from devproxy.platform.fake import FakePlatform

    _paths, cfg = _patch_cli(monkeypatch, tmp_path, [FakeCliComponent("caddy")])
    platform = FakePlatform()
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    ca = _write_ca(cfg.storage_path)
    platform.trusted.add(ca)
    platform.nss.add(ca)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "devproxy trust" not in result.stdout


def test_cli_setup_permission_error_during_apply_exits_4(tmp_path, monkeypatch):
    """apply 阶段提权失败 = 退出码 4（与 trust 一致），不是普通失败 1。"""
    from typer.testing import CliRunner

    from devproxy.cli import app

    class SudoFail(FakeCliComponent):
        def apply(self, actions):
            raise PermissionError("sudo 需要密码且当前不可用")

    comps = [
        SudoFail("caddy", Status(ok=False, detail="缺"), [Action(description="写 /etc")])
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 4, result.output
    assert "提权失败" in result.output
    assert "sudo 需要密码且当前不可用" in result.output
    assert "Traceback" not in result.output


def test_components_include_cert_flag_controls_cert_component(tmp_path):
    """M4：--no-trust 不能靠调用方自己删组件；`include_cert` 必须真的生效。"""
    from devproxy.cli._shared import _components
    from devproxy.core.config import Config
    from devproxy.core.paths import Paths
    from devproxy.platform.fake import FakePlatform

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    config = Config(storage=str(paths.data_dir))
    platform = FakePlatform()

    without = _components(
        paths, config, "auto", platform, include_cert=False
    )
    assert [type(c).__name__ for c in without] == [
        "CaddyComponent",
        "DnsmasqComponent",
        "DnsComponent",
    ]
    with_cert = _components(paths, config, "auto", platform, include_cert=True)
    assert [type(c).__name__ for c in with_cert][-1] == "CertComponent"


def test_cli_setup_noop_unhealthy_prints_fix_hint(tmp_path, monkeypatch):
    """M5：没有可执行动作时也要给出修复提示，不能只说“坏了”。"""
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCliComponent(
            "caddy",
            Status(ok=False, detail="服务未运行", hint="devproxy setup"),
        )
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 3, result.output
    assert "修复: devproxy setup" in result.output


def test_cli_setup_scope_note_exception_does_not_crash(tmp_path, monkeypatch):
    """M9：组件探测整机作用域失败只能降级为警告，不能带崩 setup。"""
    from typer.testing import CliRunner

    from devproxy.cli import app

    class BoomNote(FakeCliComponent):
        def global_scope_note(self):
            raise RuntimeError("探测炸了")

    comps = [
        BoomNote(
            "dns",
            Status(ok=False, detail="缺"),
            [Action(description="写 /etc/resolv.conf")],
        )
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0, result.output
    assert "无法获取 dns 的整机作用域说明" in result.output
    assert "Traceback" not in result.output


def test_cli_setup_macos_rejects_resolved_backend(tmp_path, monkeypatch):
    """macOS + 显式 resolved 是参数错误（退出码 2），不能等组件抛 traceback。"""
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [FakeCliComponent("caddy")]
    _patch_cli(monkeypatch, tmp_path, comps)
    monkeypatch.setattr(
        "devproxy.cli._shared._platform", lambda: SimpleNamespace(name="mac")
    )
    result = CliRunner().invoke(app, ["setup", "--dns-backend", "resolved", "--yes"])
    assert result.exit_code == 2, result.output
    assert "macOS 不支持 resolved" in result.output
    assert "Traceback" not in result.output
    assert comps[0].applied is None


def test_cli_setup_rejects_explicit_empty_dns_backend(tmp_path, monkeypatch):
    """--dns-backend "" 是参数错误，不能靠 falsy 悄悄退回 auto。"""
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [FakeCliComponent("caddy")]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--dns-backend", ""])
    assert result.exit_code == 2
    assert comps[0].applied is None


def test_cli_setup_asks_for_confirmation_interactively(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCliComponent("caddy", Status(ok=False, detail="缺"), [Action(description="写文件")])
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    monkeypatch.setattr("devproxy.cli._shared._interactive", lambda: True)
    asked = []
    monkeypatch.setattr(
        "devproxy.cli._shared.typer.confirm",
        lambda prompt, default=True: asked.append(prompt) or True,
    )
    result = CliRunner().invoke(app, ["setup"])
    assert result.exit_code == 0, result.stdout
    assert asked, "交互模式必须先确认"
    assert comps[0].applied is not None


def test_cli_setup_aborts_when_user_declines(tmp_path, monkeypatch):
    """用户拒绝确认 = "什么都没装" —— 绝不能以退出码 0 结束。

    `devproxy setup && devproxy up …` 会把"没装"当成"装好了"继续跑。
    """
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCliComponent("caddy", Status(ok=False, detail="缺"), [Action(description="写文件")])
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    monkeypatch.setattr("devproxy.cli._shared._interactive", lambda: True)
    monkeypatch.setattr("devproxy.cli._shared.typer.confirm", lambda *a, **k: False)
    result = CliRunner().invoke(app, ["setup"])
    assert result.exit_code == 4, result.stdout
    assert comps[0].applied is None
    assert "已取消" in result.stdout
    assert "什么都没改" in result.stdout


# -- 整机作用域的披露（Finding 8） -----------------------------------------


class FakeScopeComponent(FakeCliComponent):
    """带整机作用域披露的假组件。"""

    def __init__(self, name, note, status=None, actions=()):
        super().__init__(name, status, actions)
        self._note = note

    def global_scope_note(self):
        return self._note


def test_cli_setup_prints_global_scope_disclosure(tmp_path, monkeypatch):
    """变更清单必须点名整机作用域 —— 表格放不下，就单独打一遍。"""
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeScopeComponent(
            "dns",
            "⚠ 整机作用域（这台机器全部域名，不只是 *.test）\n"
            "  · DNS=127.0.0.1:5353：全机解析先问用户级 dnsmasq\n"
            "  · FallbackDNS=1.1.1.1 8.8.8.8：整机兜底\n"
            "    dnsmasq 起不来时，全机解析退化为公共 DNS",
            Status(ok=False, detail="缺"),
            [Action(description="写 /etc/systemd/resolved.conf.d/devproxy.conf")],
        )
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "整机作用域" in result.stdout
    assert "DNS=127.0.0.1:5353" in result.stdout
    assert "FallbackDNS=1.1.1.1 8.8.8.8" in result.stdout
    assert "dnsmasq 起不来" in result.stdout


def test_cli_setup_without_global_scope_component_prints_no_disclosure(
    tmp_path, monkeypatch
):
    """组件不涉及整机时不多嘴。"""
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [FakeCliComponent("caddy", Status(ok=False, detail="缺"), [Action(description="x")])]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "整机作用域" not in result.stdout
