from pathlib import Path

from devproxy.lifecycle.setup import apply_plan
from devproxy.lifecycle.setup import build_plan
from devproxy.platform.base import Action
from devproxy.platform.base import Status


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
        Status(ok=False, detail="missing"),
        [Action(description="setcap", privileged=True), Action(description="write file")],
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
            raise RuntimeError("boom")

    good = FakeComponent("good", Status(ok=True), [Action(description="x")])
    bad = Boom("bad", Status(ok=True), [Action(description="y")])
    results = apply_plan(build_plan([good, bad]))
    statuses = dict(results)
    assert statuses["bad"].ok is False
    assert "boom" in statuses["bad"].detail
    assert statuses["good"].ok is True


def test_apply_plan_uses_verify_when_available():
    class WithVerify(FakeComponent):
        def verify(self):
            return Status(ok=True, detail="verified")

    c = WithVerify("a", Status(ok=False, detail="missing"), [Action(description="x")])
    results = apply_plan(build_plan([c]))
    assert dict(results)["a"].detail == "verified"


def test_build_plan_keeps_component_reference():
    c = FakeComponent("a", Status(ok=True))
    plan = build_plan([c])
    assert plan.reports[0].component is c


def test_build_plan_isolates_check_exception():
    """One component's check() raising must not take down the whole setup."""

    class BoomCheck(FakeComponent):
        def check(self):
            raise RuntimeError("check boom")

    bad = BoomCheck("bad", Status(ok=True))
    good = FakeComponent("good", Status(ok=True))
    plan = build_plan([bad, good])
    assert [r.name for r in plan.reports] == ["bad", "good"]
    assert plan.reports[0].status.ok is False
    assert "check boom" in plan.reports[0].status.detail
    assert plan.reports[1].status.ok is True


def test_build_plan_isolates_plan_exception():
    class BoomPlan(FakeComponent):
        def plan(self):
            raise RuntimeError("plan boom")

    bad = BoomPlan("bad", Status(ok=True))
    good = FakeComponent("good", Status(ok=True))
    plan = build_plan([bad, good])
    assert plan.reports[0].status.ok is False
    assert "plan boom" in plan.reports[0].status.detail
    assert plan.reports[0].actions == []
    assert plan.reports[1].status.ok is True


def test_apply_plan_keeps_plan_failure_even_if_verify_would_pass():
    """When plan() raises the component did nothing -- a successful verify() must not mask the failure."""

    class PlanBoom(FakeComponent):
        def plan(self):
            raise RuntimeError("cannot read config")

        def verify(self):
            return Status(ok=True, detail="service is running")

    c = PlanBoom("bad", Status(ok=True))
    results = apply_plan(build_plan([c]))
    status = dict(results)["bad"]
    assert status.ok is False
    assert "cannot read config" in status.detail


def test_apply_plan_keeps_check_failure_even_if_verify_would_pass():
    class CheckBoom(FakeComponent):
        def check(self):
            raise PermissionError("binary not executable")

        def verify(self):
            return Status(ok=True, detail="service is running")

    c = CheckBoom("bad", Status(ok=True))
    results = apply_plan(build_plan([c]))
    status = dict(results)["bad"]
    assert status.ok is False
    assert "binary not executable" in status.detail


# -- CLI: setup orchestration -------------------------------------------------


class FakeCliComponent:
    """Fake component for the CLI: records apply calls."""

    def __init__(self, name, status=None, actions=(), fail=False):
        self.name = name
        self._status = status or Status(ok=True, detail="ready")
        self._actions = list(actions)
        self._fail = fail
        self.applied = None

    def check(self):
        return self._status

    def plan(self):
        return list(self._actions)

    def apply(self, actions):
        if self._fail:
            raise RuntimeError("boom")
        self.applied = actions

    def verify(self):
        return Status(ok=True, detail="in effect")


def _fake_components_seam(components):
    """Simulate the `_components` seam, including the filtering semantics of `include_cert=False`."""

    def build(paths, config, dns_backend=None, platform=None, *, include_cert=True):
        selected = list(components)
        if not include_cert:
            # The production implementation filters by CertComponent type; the test
            # double has no type to recognize, so simulate the same semantics by name
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
    monkeypatch.setattr("devproxy.cli._shared._components", _fake_components_seam(components))
    # The platform must also be swapped for a fake one: setup's migration detection
    # looks at the real unit directory, and once migration runs it rewrites the user
    # machine's caddy.service
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: FakePlatform())
    return paths, cfg


def test_cli_setup_noop_exits_ok(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [FakeCliComponent("caddy")]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup"])
    assert result.exit_code == 0, result.stdout
    assert "All components are ready; nothing to change." in result.stdout
    assert comps[0].applied is None


def test_cli_setup_refuses_privileged_without_yes(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCliComponent(
            "caddy",
            Status(ok=False, detail="missing"),
            [Action(description="write /etc", privileged=True)],
        )
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup"])
    assert result.exit_code == 4
    assert "write /etc" in result.stdout
    assert comps[0].applied is None


def test_cli_setup_applies_with_yes(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCliComponent(
            "caddy",
            Status(ok=False, detail="missing"),
            [Action(description="write /etc", privileged=True)],
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
            "bad", Status(ok=False, detail="missing"), [Action(description="x")], fail=True
        ),
        FakeCliComponent("good", Status(ok=False, detail="missing"), [Action(description="y")]),
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 1
    assert "boom" in result.stdout
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

    def fake_components(paths, config, dns_backend="auto", platform=None, *, include_cert=True):
        seen["suffix"] = config.domain_suffix
        seen["backend"] = dns_backend
        return [FakeCliComponent("caddy")]

    _patch_cli(monkeypatch, tmp_path, [])
    monkeypatch.setattr("devproxy.cli._shared._components", fake_components)
    result = CliRunner().invoke(
        app, ["setup", "--domain-suffix", "example", "--dns-backend", "resolved"]
    )
    assert result.exit_code == 0, result.stdout
    assert seen["suffix"] == "example"
    assert seen["backend"] == "resolved"


def test_cli_setup_rejects_invalid_domain_suffix(tmp_path, monkeypatch):
    """--domain-suffix flows into the Caddyfile and config files under /etc -- it must pass config validation."""
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


def test_cli_setup_noop_with_unhealthy_component_exits_precondition(tmp_path, monkeypatch):
    """No executable actions, but the component itself is unhealthy -- must not falsely report "ready"."""
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [FakeCliComponent("caddy", Status(ok=False, detail="broken"))]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup"])
    assert result.exit_code == 3
    assert "broken" in result.stdout or "broken" in result.stderr


class FakeCertComponent(FakeCliComponent):
    """Fake certificate component with a structured ca_path (`_status_cell` uses it to decide the hint state)."""

    def __init__(self, name, status, ca_path):
        super().__init__(name, status)
        self.ca_path = ca_path


def _write_ca(storage: Path) -> Path:
    ca = Path(storage) / "pki" / "authorities" / "local" / "root.crt"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text(
        "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", encoding="utf-8"
    )
    return ca


def test_cli_setup_shows_cert_note_without_useless_trust_hint(tmp_path, monkeypatch):
    """When the CA has not been generated yet the table shows a hint; with no CA there is nothing to trust, so no trust hint."""
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCertComponent(
            "cert",
            Status(
                ok=True,
                detail="no CA root certificate yet (Caddy will generate it on first route registration)",
            ),
            tmp_path / "missing" / "root.crt",
        )
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "no CA root certificate yet" in result.stdout
    assert "devproxy trust" not in result.stdout


def test_cli_setup_prints_trust_hint_when_ca_exists_but_untrusted(tmp_path, monkeypatch):
    """Only the second setup (CA generated but untrusted) shows the trust hint."""
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
    """Do not repeat the hint when the CA is already fully trusted (M8)."""
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
    """Elevation failure during apply = exit code 4 (same as trust), not an ordinary failure of 1."""
    from typer.testing import CliRunner

    from devproxy.cli import app

    class SudoFail(FakeCliComponent):
        def apply(self, actions):
            raise PermissionError("sudo needs a password and is currently unavailable")

    comps = [
        SudoFail("caddy", Status(ok=False, detail="missing"), [Action(description="write /etc")])
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 4, result.output
    assert "elevation failed" in result.output
    assert "sudo needs a password and is currently unavailable" in result.output
    assert "Traceback" not in result.output


def test_components_include_cert_flag_controls_cert_component(tmp_path):
    """M4: --no-trust must not rely on the caller removing the component; `include_cert` must really take effect."""
    from devproxy.cli._shared import _components
    from devproxy.core.config import Config
    from devproxy.core.paths import Paths
    from devproxy.platform.fake import FakePlatform

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    config = Config(storage=str(paths.data_dir))
    platform = FakePlatform()

    without = _components(paths, config, "auto", platform, include_cert=False)
    assert [type(c).__name__ for c in without] == [
        "CaddyComponent",
        "DnsmasqComponent",
        "DnsComponent",
    ]
    with_cert = _components(paths, config, "auto", platform, include_cert=True)
    assert [type(c).__name__ for c in with_cert][-1] == "CertComponent"


def test_cli_setup_noop_unhealthy_prints_fix_hint(tmp_path, monkeypatch):
    """M5: even with no executable actions, give a fix hint; do not just say "broken"."""
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCliComponent(
            "caddy",
            Status(ok=False, detail="service not running", hint="devproxy setup"),
        )
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 3, result.output
    assert "Fix: devproxy setup" in result.output


def test_cli_setup_scope_note_exception_does_not_crash(tmp_path, monkeypatch):
    """M9: a component failing to probe machine-wide scope may only degrade to a warning, not crash setup."""
    from typer.testing import CliRunner

    from devproxy.cli import app

    class BoomNote(FakeCliComponent):
        def global_scope_note(self):
            raise RuntimeError("probe boom")

    comps = [
        BoomNote(
            "dns",
            Status(ok=False, detail="missing"),
            [Action(description="write /etc/resolv.conf")],
        )
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0, result.output
    assert "cannot get the machine-wide scope note for dns" in result.output
    assert "Traceback" not in result.output


def test_cli_setup_macos_rejects_resolved_backend(tmp_path, monkeypatch):
    """macOS + explicit resolved is an argument error (exit code 2); do not wait for the component to raise a traceback."""
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [FakeCliComponent("caddy")]
    _patch_cli(monkeypatch, tmp_path, comps)
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: SimpleNamespace(name="mac"))
    result = CliRunner().invoke(app, ["setup", "--dns-backend", "resolved", "--yes"])
    assert result.exit_code == 2, result.output
    assert "macOS does not support the resolved backend" in result.output
    assert "Traceback" not in result.output
    assert comps[0].applied is None


def test_cli_setup_rejects_explicit_empty_dns_backend(tmp_path, monkeypatch):
    """--dns-backend "" is an argument error; do not silently fall back to auto because the value is falsy."""
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
        FakeCliComponent(
            "caddy", Status(ok=False, detail="missing"), [Action(description="write file")]
        )
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
    assert asked, "interactive mode must confirm first"
    assert comps[0].applied is not None


def test_cli_setup_aborts_when_user_declines(tmp_path, monkeypatch):
    """The user declining confirmation means "nothing installed" -- must never end with exit code 0.

    `devproxy setup && devproxy up ...` would treat "not installed" as "installed" and continue.
    """
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCliComponent(
            "caddy", Status(ok=False, detail="missing"), [Action(description="write file")]
        )
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    monkeypatch.setattr("devproxy.cli._shared._interactive", lambda: True)
    monkeypatch.setattr("devproxy.cli._shared.typer.confirm", lambda *a, **k: False)
    result = CliRunner().invoke(app, ["setup"])
    assert result.exit_code == 4, result.stdout
    assert comps[0].applied is None
    assert "Cancelled" in result.stdout
    assert "nothing was changed" in result.stdout


# -- Machine-wide scope disclosure (Finding 8) --------------------------------


class FakeScopeComponent(FakeCliComponent):
    """Fake component with a machine-wide scope disclosure."""

    def __init__(self, name, note, status=None, actions=()):
        super().__init__(name, status, actions)
        self._note = note

    def global_scope_note(self):
        return self._note


def test_cli_setup_prints_global_scope_disclosure(tmp_path, monkeypatch):
    """The change list must name machine-wide scope -- it does not fit in the table, so print it separately."""
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeScopeComponent(
            "dns",
            "⚠ machine-wide scope (all domains on this machine, not just *.test)\n"
            "  · DNS=127.0.0.1:5353: machine-wide resolution asks the user-scope dnsmasq first\n"
            "  · FallbackDNS=1.1.1.1 8.8.8.8: machine-wide fallback\n"
            "    if dnsmasq would not start, machine-wide resolution degrades to public DNS",
            Status(ok=False, detail="missing"),
            [Action(description="write /etc/systemd/resolved.conf.d/devproxy.conf")],
        )
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "machine-wide scope" in result.stdout
    assert "DNS=127.0.0.1:5353" in result.stdout
    assert "FallbackDNS=1.1.1.1 8.8.8.8" in result.stdout
    assert "dnsmasq would not start" in result.stdout


def test_cli_setup_without_global_scope_component_prints_no_disclosure(tmp_path, monkeypatch):
    """Do not add noise when the component has no machine-wide effect."""
    from typer.testing import CliRunner

    from devproxy.cli import app

    comps = [
        FakeCliComponent("caddy", Status(ok=False, detail="missing"), [Action(description="x")])
    ]
    _patch_cli(monkeypatch, tmp_path, comps)
    result = CliRunner().invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "machine-wide scope" not in result.stdout
