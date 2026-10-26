"""DNS route component tests: resolved / system backends.

Does not write /etc: apply-related cases redirect target paths to tmp_path via
monkeypatch, and service operations go through FakePlatform.
"""

import os

import pytest

from devproxy.components import dns as dns_module
from devproxy.components.dns import DnsComponent
from devproxy.components.dns import render_resolved_conf
from devproxy.components.dns import render_resolver_file
from devproxy.components.dns import render_system_dnsmasq_conf
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.platform.fake import FakePlatform

# Record the production values before the fixture takes effect -- the guard case uses them to
# assert that test isolation did not also change the production paths.
_REAL_RESOLVED_CONF = dns_module.RESOLVED_CONF
_REAL_SYSTEM_DNSMASQ_CONF = dns_module.SYSTEM_DNSMASQ_CONF
_REAL_RESOLVER_DIR = dns_module.RESOLVER_DIR


@pytest.fixture(autouse=True)
def isolated_dns_paths(tmp_path, monkeypatch):
    """Redirect the absolute paths of the three resolver files into a temp directory.

    `plan()`'s readiness criterion **directly reads** the target file contents, and
    these paths all live under /etc -- conftest's `isolated_home` fixture does not
    cover them (it only redirects HOME/XDG_*). Without isolation, on a machine that
    has **really run `devproxy setup`**, the real file contents happen to match the
    rendered result and `plan()` returns empty, so every case asserting "there are
    actions" goes red together even though the code itself is fine. That is not a
    defect in the code under test; the test simply failed to isolate host state.

    A single case can still override these with its own paths via monkeypatch
    (the later setting wins).
    """
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", tmp_path / "etc" / "devproxy.conf")
    monkeypatch.setattr(dns_module, "SYSTEM_DNSMASQ_CONF", tmp_path / "etc" / "dnsmasq.conf")
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", tmp_path / "etc" / "resolver")


def test_production_paths_point_at_real_system_locations():
    """Isolation applies only to tests: in production all three paths must still be those under /etc."""
    from pathlib import Path

    assert _REAL_RESOLVED_CONF == Path("/etc/systemd/resolved.conf.d/devproxy.conf")
    assert _REAL_SYSTEM_DNSMASQ_CONF == Path("/etc/dnsmasq.d/devproxy.conf")
    assert _REAL_RESOLVER_DIR == Path("/etc/resolver")


def test_dns_route_paths_have_a_single_source():
    """The three route paths are defined only in platform.base; the component and platform implementations reference the same objects.

    Rewriting any one of them back to a literal makes the `is` assertions fail --
    the consequence of drift is "setup writes A, doctor checks B, rollback backs up C".
    """
    from devproxy.platform import base
    from devproxy.platform import linux
    from devproxy.platform import macos

    assert _REAL_RESOLVED_CONF is base.RESOLVED_CONF
    assert _REAL_SYSTEM_DNSMASQ_CONF is base.SYSTEM_DNSMASQ_CONF
    assert _REAL_RESOLVER_DIR is base.RESOLVER_DIR
    assert linux.RESOLVED_CONF is base.RESOLVED_CONF
    assert linux.SYSTEM_DNSMASQ_CONF is base.SYSTEM_DNSMASQ_CONF
    assert macos.RESOLVER_DIR is base.RESOLVER_DIR


def test_render_resolved_conf_shape():
    out = render_resolved_conf(port=5353, suffix="test")
    assert "[Resolve]" in out
    assert "DNS=127.0.0.1:5353" in out
    assert "Domains=~.test" in out


def test_render_resolved_conf_domains_uses_space_separator_not_comma():
    """systemd-resolved's Domains= is space-separated; a comma is not a separator."""
    out = render_resolved_conf(port=5353, suffix=".test")
    assert "Domains=~.test" in out
    assert "," not in out


def test_render_system_dnsmasq_conf_shape():
    out = render_system_dnsmasq_conf(suffix="test")
    assert "address=/.test/127.0.0.1" in out


def test_auto_backend_prefers_resolved(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    cfg = Config()
    platform = FakePlatform()
    platform.resolved_running = True
    comp = DnsComponent(platform, paths, cfg, backend="auto")
    assert comp.resolved_backend is True


def test_auto_backend_picks_system_when_resolved_inactive(tmp_path):
    """A directory existing does not mean resolved is running -- auto must probe process state."""
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    platform = FakePlatform()
    platform.resolved_running = False
    comp = DnsComponent(platform, paths, Config(), backend="auto")
    assert comp.resolved_backend is False


def test_explicit_system_backend(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    comp = DnsComponent(FakePlatform(), paths, Config(), backend="system")
    assert comp.resolved_backend is False


def test_check_reports_missing_route(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    comp = DnsComponent(FakePlatform(), paths, Config(), backend="resolved")
    status = comp.check()
    assert status.ok is False
    assert "DNS" in status.detail or "content" in status.detail


def test_plan_writes_resolved_conf(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    comp = DnsComponent(FakePlatform(), paths, Config(), backend="resolved")
    actions = comp.plan()
    assert any(a.privileged for a in actions), "writing /etc requires elevation"
    assert any(a.kind == "write" for a in actions)


# -- components ---------------------------------------------------------


def _component(tmp_path, backend="resolved", suffix="test", platform=None):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    platform = platform or FakePlatform()
    return (
        DnsComponent(platform, paths, Config(domain_suffix=suffix), backend=backend),
        platform,
    )


class _FailingElevated:
    """Elevated session: writes succeed, commands fail -- simulating a failed systemctl restart."""

    def __init__(self, platform) -> None:
        self.platform = platform

    def run(self, argv):
        self.platform.commands.append(list(argv))
        return 1, "boom"

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def read(self, path):
        return path.read_text(encoding="utf-8")

    def copy(self, src, dest):
        pass


def test_resolved_plan_targets_resolved_conf_and_restarts_resolved(tmp_path):
    comp, _platform = _component(tmp_path, backend="resolved")
    actions = comp.plan()
    write_action = next(a for a in actions if a.kind == "write")
    assert write_action.path == dns_module.RESOLVED_CONF
    assert write_action.privileged is True
    assert "DNS=127.0.0.1:5353" in write_action.content
    assert "Domains=~.test" in write_action.content
    reload_action = next(a for a in actions if a.kind == "run")
    assert reload_action.command == ["systemctl", "restart", "systemd-resolved"]
    assert reload_action.privileged is True


def test_system_plan_targets_system_dnsmasq_and_restarts_dnsmasq(tmp_path):
    comp, _platform = _component(tmp_path, backend="system")
    actions = comp.plan()
    write_action = next(a for a in actions if a.kind == "write")
    assert write_action.path == dns_module.SYSTEM_DNSMASQ_CONF
    assert "address=/.test/127.0.0.1" in write_action.content
    reload_action = next(a for a in actions if a.kind == "run")
    assert reload_action.command == ["systemctl", "restart", "dnsmasq"]
    assert reload_action.privileged is True


def test_apply_writes_via_elevated_session_and_records_commands(tmp_path, monkeypatch):
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    content = target.read_text(encoding="utf-8")
    assert "DNS=127.0.0.1:5353" in content
    assert "Domains=~.test" in content
    assert ["systemctl", "restart", "systemd-resolved"] in platform.commands


def test_check_ok_when_route_installed(tmp_path):
    comp, platform = _component(tmp_path, backend="resolved")
    platform.dns_backends.add("resolved")
    status = comp.check()
    assert status.ok is True
    assert "resolved" in status.detail


def test_verify_ok_when_route_installed(tmp_path):
    comp, platform = _component(tmp_path, backend="resolved")
    platform.dns_backends.add("resolved")
    assert comp.verify().ok is True
    assert platform.dns_queries == ["foo.test"]


def test_verify_fails_when_resolver_does_not_return_loopback(tmp_path):
    comp, platform = _component(tmp_path, backend="resolved")
    platform.dns_backends.add("resolved")
    platform.dns_answer = None
    status = comp.verify()
    assert status.ok is False
    assert "foo.test" in status.detail


def test_verify_fails_when_resolver_returns_wrong_address(tmp_path):
    comp, platform = _component(tmp_path, backend="resolved")
    platform.dns_backends.add("resolved")
    platform.dns_answer = "10.0.0.5"
    assert comp.verify().ok is False


def test_auto_falls_back_to_system_when_resolved_inactive(tmp_path):
    platform = FakePlatform()
    platform.resolved_running = False
    comp, _platform = _component(tmp_path, backend="auto", platform=platform)
    assert comp.resolved_backend is False
    assert comp.target_path == dns_module.SYSTEM_DNSMASQ_CONF


def test_auto_ignores_resolved_conf_dir_when_resolved_inactive(tmp_path):
    """A directory existing is not the criterion -- the directory on this machine was created by hand."""
    (tmp_path / "resolved.conf.d").mkdir()
    platform = FakePlatform()
    platform.resolved_running = False
    comp, _platform = _component(tmp_path, backend="auto", platform=platform)
    assert comp.resolved_backend is False


# -- setup idempotence: plan must be empty when already ready (Important C) ---


def test_plan_empty_when_recorded_hash_matches(tmp_path, monkeypatch):
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    assert comp.plan() != []
    comp.apply(comp.plan())
    # Route written as expected -> a repeated setup must be a no-op (no more sudo, no resolved restart)
    assert comp.plan() == []


def test_plan_non_empty_when_recorded_hash_differs(tmp_path, monkeypatch):
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    # Changed the domain suffix -> content changed -> must rewrite
    other, _p = _component(tmp_path, backend="resolved", suffix="dev")
    assert other.plan() != []


def test_plan_empty_when_file_content_matches_without_state(tmp_path, monkeypatch):
    """The route file content is already the expected content -- do not rewrite even if the state file is lost."""
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    comp.paths.state_dir.joinpath("dns-route.json").unlink()
    assert comp.plan() == []


def test_plan_uses_state_hash_when_file_unreadable(tmp_path, monkeypatch):
    """When the file under /etc is unreadable (regular user), fall back to the hash in the state directory."""
    if os.geteuid() == 0:
        pytest.skip("root can read any file")
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    target.chmod(0o000)
    try:
        assert comp.plan() == []
    finally:
        target.chmod(0o600)


def test_plan_non_empty_when_target_file_removed(tmp_path, monkeypatch):
    """After the user deletes the route file under /etc, setup must not still claim "ready"."""
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    target.unlink()
    assert comp.plan() != []


def test_plan_non_empty_when_target_file_edited(tmp_path, monkeypatch):
    """The user edited the route file under /etc -- the state hash still points at the old content, so it must rewrite."""
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    target.write_text("[Resolve]\nDNS=9.9.9.9\n", encoding="utf-8")
    assert comp.plan() != []


def test_state_not_recorded_when_reload_fails(tmp_path, monkeypatch):
    """A failed reload must not be recorded as "ready" -- otherwise the next plan is empty while nothing took effect."""
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, platform = _component(tmp_path, backend="resolved")
    monkeypatch.setattr(platform, "privilege", lambda: _FailingElevated(platform))
    with pytest.raises(RuntimeError):
        comp.apply(comp.plan())
    assert not (tmp_path / "s" / "dns-route.json").exists()


def test_state_not_recorded_for_other_write_paths(tmp_path):
    """The state file describes only the route itself -- other write actions must not pollute it."""
    from devproxy.platform.base import Action

    comp, _platform = _component(tmp_path, backend="resolved")
    other = tmp_path / "other.conf"
    comp.apply(
        [
            Action(
                description="unrelated write",
                kind="write",
                path=other,
                content="hello",
                privileged=False,
            )
        ]
    )
    assert not (tmp_path / "s" / "dns-route.json").exists()


def test_apply_records_state_in_state_dir(tmp_path, monkeypatch):
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    state = tmp_path / "s" / "dns-route.json"
    assert state.exists()
    import json

    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["backend"] == "resolved"
    assert data["path"] == str(target)
    assert len(data["content_sha256"]) == 64


# -- elevation discipline (two minors) -------------------------------------


def test_apply_without_privileged_actions_does_not_request_privilege(tmp_path, monkeypatch):
    comp, platform = _component(tmp_path, backend="resolved")

    def boom():
        raise AssertionError("must not request elevation when there are no privileged actions")

    monkeypatch.setattr(platform, "privilege", boom)
    comp.apply([])


def test_non_privileged_write_uses_plain_filesystem(tmp_path):
    from devproxy.platform.base import Action

    comp, platform = _component(tmp_path, backend="resolved")
    target = tmp_path / "plain.conf"
    comp.apply(
        [
            Action(
                description="plain write",
                kind="write",
                path=target,
                content="hello",
                privileged=False,
            )
        ]
    )
    assert target.read_text(encoding="utf-8") == "hello"
    assert target not in platform.written


def test_apply_unknown_action_raises(tmp_path):
    from devproxy.platform.base import Action

    comp, _platform = _component(tmp_path, backend="resolved")
    with pytest.raises(RuntimeError):
        comp.apply([Action(description="?", kind="run", command=["bogus"])])


# -- macOS: resolver backend (per-domain resolver files) -------------------


class _MacPlatform(FakePlatform):
    """macOS platform: no systemd-resolved, so use /etc/resolver/<suffix>."""

    name = "mac"
    dns_port = 5300


def test_check_reports_not_ok_when_resolver_file_has_wrong_content(tmp_path, monkeypatch):
    """On macOS /etc/resolver/<suffix> exists but the content is not what we want (missing port).

    `dns_route_installed` answers only by **existence** -- relying on it alone, doctor
    would falsely report "ready", while `*.test` is actually sent to 127.0.0.1:53
    (mDNSResponder), not the tool's dnsmasq (:5300). Real machines have such ancient
    files (a `nameserver 127.0.0.1` left over from 2022).

    check must use the same criterion as plan: wrong content means not ready.
    """
    resolver_dir = tmp_path / "etc" / "resolver"
    resolver_dir.mkdir(parents=True)
    (resolver_dir / "test").write_text("nameserver 127.0.0.1\n", encoding="utf-8")
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", resolver_dir)

    comp, platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    platform.dns_backends = {"resolver"}  # platform layer: "exists means configured"
    status = comp.check()
    assert status.ok is False, "wrong content must not be reported as ready"
    assert "resolved" in status.detail or "content" in status.detail


def test_check_ok_when_resolver_file_content_matches(tmp_path, monkeypatch):
    """Control group: check must be ready when the content is correct -- otherwise the previous case degenerates into "always errors"."""
    resolver_dir = tmp_path / "etc" / "resolver"
    resolver_dir.mkdir(parents=True)
    (resolver_dir / "test").write_text(render_resolver_file(5300), encoding="utf-8")
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", resolver_dir)

    comp, platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    platform.dns_backends = {"resolver"}
    assert comp.check().ok is True


def test_macos_backend_key_is_resolver(tmp_path):
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    assert comp._backend_key() == "resolver"


def test_macos_explicit_resolved_backend_raises(tmp_path):
    """macOS has no systemd-resolved -- an explicit resolved backend must error, not silently downgrade."""
    comp, _platform = _component(tmp_path, backend="resolved", platform=_MacPlatform())
    with pytest.raises(RuntimeError, match="macOS does not support the resolved backend"):
        _ = comp.resolved_backend


def test_macos_explicit_resolved_backend_check_reports_config_error(tmp_path):
    """check never raises: when config and platform disagree, report not ok truthfully."""
    comp, _platform = _component(tmp_path, backend="resolved", platform=_MacPlatform())
    status = comp.check()
    assert status.ok is False
    assert "macOS does not support the resolved backend" in status.detail
    assert "auto" in status.hint


def test_macos_auto_resolved_backend_stays_silent(tmp_path):
    """auto silently uses resolver on macOS -- only an explicit resolved errors."""
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    assert comp.resolved_backend is False


def test_macos_target_path_is_resolver_file(tmp_path, monkeypatch):
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", tmp_path / "etc" / "resolver")
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    assert comp.target_path == tmp_path / "etc" / "resolver" / "test"


def test_macos_plan_writes_resolver_file_with_platform_port(tmp_path, monkeypatch):
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", tmp_path / "etc" / "resolver")
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    actions = comp.plan()
    assert len(actions) == 1, (
        "macOS does not need a system resolver restart -- mDNSResponder reloads itself"
    )
    action = actions[0]
    assert action.kind == "write"
    assert action.privileged is True
    assert action.path == tmp_path / "etc" / "resolver" / "test"
    assert "nameserver 127.0.0.1" in action.content
    assert "port 5300" in action.content


def test_macos_plan_is_empty_when_resolver_file_in_place(tmp_path, monkeypatch):
    """plan must be empty when the route is in place -- a repeated setup must not ask for sudo again."""
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", tmp_path / "etc" / "resolver")
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    assert comp.plan() != []
    comp.apply(comp.plan())
    assert comp.plan() == []


def test_macos_apply_writes_the_resolver_file(tmp_path, monkeypatch):
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", tmp_path / "etc" / "resolver")
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    comp.apply(comp.plan())
    content = (tmp_path / "etc" / "resolver" / "test").read_text(encoding="utf-8")
    assert "nameserver 127.0.0.1" in content
    assert "port 5300" in content


def test_macos_check_reports_resolver_backend(tmp_path):
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    status = comp.check()
    assert status.ok is False
    assert "resolver" in status.detail


def test_macos_state_records_resolver_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", tmp_path / "etc" / "resolver")
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    comp.apply(comp.plan())
    import json

    data = json.loads(comp.state_path.read_text(encoding="utf-8"))
    assert data["backend"] == "resolver"


def test_linux_plan_still_restarts_resolved(tmp_path):
    """The Linux path is unchanged: after writing the drop-in, systemd-resolved must still be restarted."""
    comp, _platform = _component(tmp_path, backend="resolved")
    reload_action = next(a for a in comp.plan() if a.kind == "run")
    assert reload_action.command == ["systemctl", "restart", "systemd-resolved"]


def test_plan_computes_expected_content_once(tmp_path):
    """The two branches of plan should share one content computation, not compute it twice."""
    comp, _platform = _component(tmp_path, backend="resolved")
    calls = []
    original = comp._expected_content
    comp._expected_content = lambda resolved=None: calls.append(resolved) or original(resolved)
    comp.plan()
    assert len(calls) == 1


# -- machine-wide scope disclosure (Finding 8) -----------------------------


def test_global_scope_note_names_machine_wide_dns_and_fallback(tmp_path):
    """resolved's DNS= / FallbackDNS= change **the whole machine** -- this must be stated.

    These two lines go in the global section of the drop-in: DNS= makes every domain
    on this machine ask the user-scope dnsmasq first, and FallbackDNS= is the
    machine-wide fallback. The user should know before confirming.
    """
    comp, _platform = _component(tmp_path, backend="resolved")
    note = comp.global_scope_note()
    assert note is not None
    assert "DNS=127.0.0.1:5353" in note
    assert "FallbackDNS=1.1.1.1 8.8.8.8" in note
    assert "machine-wide" in note
    assert "dnsmasq cannot start" in note, "must spell out the degraded behavior when dnsmasq dies"


def test_global_scope_note_none_for_system_backend(tmp_path):
    """The system backend only writes a dnsmasq fragment for this suffix -- no machine-wide effect, so no disclosure."""
    comp, _platform = _component(tmp_path, backend="system")
    assert comp.global_scope_note() is None


def test_global_scope_note_none_for_macos_resolver_backend(tmp_path):
    """macOS's per-domain resolver file does not affect other domains -- do not disclose machine-wide scope."""
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    assert comp.global_scope_note() is None


def test_resolved_plan_description_marks_global_scope(tmp_path):
    """The change-list table must also make the machine-wide scope visible."""
    comp, _platform = _component(tmp_path, backend="resolved")
    write_action = next(a for a in comp.plan() if a.kind == "write")
    assert "machine-wide" in write_action.description


def test_fallback_dns_constant_matches_rendered_conf(tmp_path):
    """The FallbackDNS in the disclosure text must match what is actually written to the config."""
    content = render_resolved_conf(port=5353, suffix="test")
    assert f"FallbackDNS={dns_module.FALLBACK_DNS}" in content
    assert dns_module.FALLBACK_DNS in dns_module.global_scope_disclosure(5353)


def test_unreadable_route_with_platform_installed_is_in_place_for_check_and_plan(
    tmp_path, monkeypatch
):
    """root:root 0600 + missing state: check and plan must reach the same conclusion.

    When the platform can confirm the file exists, plan must not ask for sudo to
    rewrite it and restart the machine-wide resolver again.
    """
    from tests.support import UnreadablePath

    target = tmp_path / "etc" / "devproxy.conf"
    target.parent.mkdir(parents=True)
    target.write_text(render_resolved_conf(port=5353, suffix="test"), encoding="utf-8")
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", UnreadablePath(str(target)))

    comp, platform = _component(tmp_path, backend="resolved")
    platform.dns_backends.add("resolved")
    assert comp.check().ok is True
    assert comp.plan() == [], (
        "when doctor says ready, setup must not rewrite and restart resolved again"
    )


def test_unreadable_route_without_platform_confirmation_is_not_in_place(tmp_path, monkeypatch):
    """Must not falsely report ready when the platform cannot confirm either -- otherwise a real absence is missed."""
    from tests.support import UnreadablePath

    target = tmp_path / "etc" / "devproxy.conf"
    target.parent.mkdir(parents=True)
    target.write_text(render_resolved_conf(port=5353, suffix="test"), encoding="utf-8")
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", UnreadablePath(str(target)))

    comp, _platform = _component(tmp_path, backend="resolved")
    assert comp.check().ok is False
    assert comp.plan() != []
