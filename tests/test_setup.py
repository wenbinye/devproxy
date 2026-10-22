from devproxy.platform.base import Action, Status
from devproxy.setup import apply_plan, build_plan


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


def _patch_cli(monkeypatch, tmp_path, components):
    from devproxy.config import Config
    from devproxy.paths import Paths

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    monkeypatch.setattr(
        "devproxy.cli.build_context", lambda *a, **k: (paths, cfg, None, None)
    )
    monkeypatch.setattr("devproxy.cli._components", lambda *a, **k: components)
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

    def fake_components(paths, config, dns_backend="auto"):
        seen["suffix"] = config.domain_suffix
        seen["backend"] = dns_backend
        return [FakeCliComponent("caddy")]

    _patch_cli(monkeypatch, tmp_path, [])
    monkeypatch.setattr("devproxy.cli._components", fake_components)
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
