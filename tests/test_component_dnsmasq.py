"""Dnsmasq component tests: incremental config merge + user-scope service unit.

Never touches the real system: config paths are injected from tmp_path and
service operations go through FakePlatform.
"""

from pathlib import Path

import pytest

from devproxy.components import dnsmasq as dnsmasq_module
from devproxy.components.dnsmasq import DnsmasqComponent
from devproxy.components.dnsmasq import merge_dnsmasq_config
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.platform.fake import FakePlatform
from tests.support import UnreadablePath


@pytest.fixture(autouse=True)
def _isolated_dnsmasq_binary(tmp_path, monkeypatch):
    """check verifies the executable exists -- point it at a temp fake file and never depend on the host.

    macOS cases can also find an "installed" dnsmasq by default; a case that needs to
    simulate a missing install overrides the candidate paths with nonexistent ones.
    """
    binary = tmp_path / "fake-bin" / "dnsmasq"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(dnsmasq_module, "LINUX_DNSMASQ_BINARY", str(binary))
    monkeypatch.setattr(dnsmasq_module, "MACOS_DNSMASQ_CANDIDATES", (str(binary),))


def test_merge_adds_port_and_addresses_to_empty():
    out = merge_dnsmasq_config("", port=5353, suffix="test")
    assert "port=5353" in out
    assert "address=/.test/127.0.0.1" in out
    assert "address=/.test/::1" in out


def test_merge_preserves_existing_address_entries():
    existing = "address=/.develop/127.0.0.1\naddress=/.develop/::1\nno-resolv\nserver=223.5.5.5\nfilter-AAAA\n"
    out = merge_dnsmasq_config(existing, port=5353, suffix="test")
    assert "address=/.develop/127.0.0.1" in out
    assert "address=/.develop/::1" in out
    assert "no-resolv" in out
    assert "server=223.5.5.5" in out
    assert "filter-AAAA" in out


def test_merge_replaces_existing_port():
    out = merge_dnsmasq_config("port=53\n", port=5353, suffix="test")
    # Note: do not test with "port=53" not in out -- "port=5353" itself contains that substring.
    # The old port must disappear as a whole line and only one port= line may remain.
    lines = out.splitlines()
    assert "port=53" not in lines
    assert "port=5353" in lines
    assert out.count("port=") == 1


def test_merge_is_idempotent():
    once = merge_dnsmasq_config("", port=5353, suffix="test")
    twice = merge_dnsmasq_config(once, port=5353, suffix="test")
    assert once == twice


def test_merge_does_not_duplicate_address():
    existing = "address=/.test/127.0.0.1\naddress=/.test/::1\n"
    out = merge_dnsmasq_config(existing, port=5353, suffix="test")
    assert out.count("address=/.test/127.0.0.1") == 1
    assert out.count("address=/.test/::1") == 1


def test_merge_handles_leading_dot_suffix():
    out = merge_dnsmasq_config("", port=5353, suffix=".test")
    assert "address=/.test/127.0.0.1" in out
    assert "address=/.test/::1" in out


# -- upstreams and resolution-loop protection (Important B) ----------------


def test_merge_empty_adds_no_resolv_and_default_upstreams():
    """An empty config must include upstreams: otherwise dnsmasq uses /etc/resolv.conf (the resolved stub)
    as its upstream while resolved points back at the local dnsmasq -- a resolution loop."""
    out = merge_dnsmasq_config("", port=5353, suffix="test")
    assert "no-resolv" in out.splitlines()
    servers = [ln for ln in out.splitlines() if ln.startswith("server=")]
    assert len(servers) >= 1
    # The upstream must not be the local stub
    assert all("127.0.0.53" not in s for s in servers)


def test_merge_keeps_user_upstream_untouched():
    out = merge_dnsmasq_config("server=223.5.5.5\n", port=5353, suffix="test")
    servers = [ln for ln in out.splitlines() if ln.startswith("server=")]
    assert servers == ["server=223.5.5.5"]


# -- global upstreams vs domain-scoped upstreams (dnsmasq manual: server= does not stop reading resolv.conf) --


def _global_servers(lines):
    """A global upstream = a server= value with no domain part."""
    out = []
    for line in lines:
        s = line.strip()
        if not s.startswith("server="):
            continue
        value = s[len("server=") :].strip()
        if value and "/" not in value and "," not in value:
            out.append(s)
    return out


def test_merge_domain_scoped_server_is_not_a_global_upstream():
    """server=/corp/10.0.0.1 serves only that domain -- it does not count as a global upstream.

    Otherwise the config has neither a global upstream nor no-resolv, so ordinary
    domains go to /etc/resolv.conf (on a resolved machine that is the 127.0.0.53
    stub), while resolved is pointed back at the local dnsmasq by the tool's
    drop-in -> machine-wide resolution fails.
    """
    out = merge_dnsmasq_config("server=/corp.example.com/10.0.0.1\n", port=5353, suffix="test")
    lines = out.splitlines()
    assert "no-resolv" in lines
    assert _global_servers(lines)
    # The user's domain-scoped upstream is preserved verbatim
    assert "server=/corp.example.com/10.0.0.1" in lines


def test_merge_global_server_keeps_it_and_adds_no_resolv():
    """server= never suppresses reading /etc/resolv.conf -- the manual explicitly says --no-resolv."""
    out = merge_dnsmasq_config("server=223.5.5.5\n", port=5353, suffix="test")
    lines = out.splitlines()
    assert "no-resolv" in lines
    assert [ln for ln in lines if ln.startswith("server=")] == ["server=223.5.5.5"]


def test_merge_resolv_file_prevents_no_resolv():
    """An explicit resolv-file= is the user's chosen resolver file -- mutually exclusive with no-resolv, so do not add it."""
    out = merge_dnsmasq_config("resolv-file=/etc/resolv.d/custom\n", port=5353, suffix="test")
    assert "no-resolv" not in out.splitlines()


def test_merge_idempotent_for_domain_scoped_server():
    once = merge_dnsmasq_config("server=/corp.example.com/10.0.0.1\n", port=5353, suffix="test")
    twice = merge_dnsmasq_config(once, port=5353, suffix="test")
    assert once == twice


def test_merge_idempotent_for_global_server():
    once = merge_dnsmasq_config("server=223.5.5.5\n", port=5353, suffix="test")
    twice = merge_dnsmasq_config(once, port=5353, suffix="test")
    assert once == twice


def test_merge_idempotent_for_resolv_file():
    once = merge_dnsmasq_config("resolv-file=/etc/resolv.d/custom\n", port=5353, suffix="test")
    twice = merge_dnsmasq_config(once, port=5353, suffix="test")
    assert once == twice


# -- listen on loopback only, avoiding becoming an open recursive resolver on the LAN --


def test_merge_ensures_loopback_only_listener():
    out = merge_dnsmasq_config("", port=5353, suffix="test")
    lines = out.splitlines()
    assert "listen-address=127.0.0.1" in lines
    assert "bind-interfaces" in lines


def test_merge_keeps_user_listen_address():
    out = merge_dnsmasq_config(
        "listen-address=127.0.0.2\nbind-interfaces\n", port=5353, suffix="test"
    )
    lines = out.splitlines()
    assert "listen-address=127.0.0.2" in lines
    assert "listen-address=127.0.0.1" not in lines
    assert lines.count("bind-interfaces") == 1


def test_merge_does_not_duplicate_existing_no_resolv():
    out = merge_dnsmasq_config("no-resolv\n", port=5353, suffix="test")
    assert out.splitlines().count("no-resolv") == 1
    servers = [ln for ln in out.splitlines() if ln.startswith("server=")]
    assert len(servers) >= 1


def test_merge_with_upstream_is_idempotent():
    once = merge_dnsmasq_config("", port=5353, suffix="test")
    twice = merge_dnsmasq_config(once, port=5353, suffix="test")
    assert once == twice
    assert twice.splitlines().count("no-resolv") == 1
    assert len([ln for ln in twice.splitlines() if ln.startswith("server=")]) == len(
        [ln for ln in once.splitlines() if ln.startswith("server=")]
    )


# -- components ---------------------------------------------------------


def _component(tmp_path, conf=None, platform=None):
    platform = platform or FakePlatform()
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    conf_path = tmp_path / "dnsmasq" / "dnsmasq.conf"
    if conf is not None:
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        conf_path.write_text(conf, encoding="utf-8")
    return DnsmasqComponent(platform, paths, Config(), conf_path=conf_path), platform, conf_path


def test_plan_keeps_user_entries_and_writes_to_conf_path(tmp_path):
    existing = "address=/.develop/127.0.0.1\nno-resolv\nserver=223.5.5.5\n"
    comp, _platform, conf_path = _component(tmp_path, conf=existing)
    actions = comp.plan()
    write_actions = [a for a in actions if a.kind == "write"]
    assert len(write_actions) == 1
    action = write_actions[0]
    assert action.path == conf_path
    assert "address=/.develop/127.0.0.1" in action.content
    assert "no-resolv" in action.content
    assert "server=223.5.5.5" in action.content
    assert "port=5353" in action.content
    assert "address=/.test/127.0.0.1" in action.content
    assert "address=/.test/::1" in action.content


def test_plan_contains_install_service_action(tmp_path):
    comp, _platform, _conf = _component(tmp_path)
    actions = comp.plan()
    assert any(a.command[:1] == ["install-service"] for a in actions)


def test_apply_writes_conf_and_installs_user_service(tmp_path):
    comp, platform, conf_path = _component(tmp_path)
    comp.apply(comp.plan())
    content = conf_path.read_text(encoding="utf-8")
    assert "port=5353" in content
    assert "address=/.test/127.0.0.1" in content
    # systemd user unit (not system scope)
    unit = platform.services["dnsmasq"]
    assert "[Service]" in unit
    assert "dnsmasq" in unit
    assert "dnsmasq" in platform.active


def test_unit_keeps_dnsmasq_in_foreground(tmp_path):
    """dnsmasq forks and exits by default -- Type=simple would immediately mark the unit inactive."""
    comp, platform, _conf = _component(tmp_path)
    comp.apply(comp.plan())
    unit = platform.services["dnsmasq"]
    assert "Type=simple" in unit
    assert "--keep-in-foreground" in unit
    assert "--pid-file" not in unit
    assert "ExecReload=/bin/kill -HUP $MAINPID" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit


def test_apply_preserves_existing_entries_on_disk(tmp_path):
    comp, _platform, conf_path = _component(
        tmp_path, conf="address=/.develop/127.0.0.1\nno-resolv\n"
    )
    comp.apply(comp.plan())
    content = conf_path.read_text(encoding="utf-8")
    assert "address=/.develop/127.0.0.1" in content
    assert "no-resolv" in content


def test_generated_conf_has_upstream_to_avoid_resolution_loop(tmp_path):
    """A config generated on a new machine must include upstreams -- otherwise resolved <-> dnsmasq loops forever."""
    comp, _platform, conf_path = _component(tmp_path)
    comp.apply(comp.plan())
    lines = conf_path.read_text(encoding="utf-8").splitlines()
    assert "no-resolv" in lines
    assert [ln for ln in lines if ln.startswith("server=")]


def test_generated_conf_only_listens_on_loopback(tmp_path):
    """Without listen-address/bind-interfaces dnsmasq binds 0.0.0.0:5353 --
    becoming an open recursive resolver on the LAN."""
    comp, _platform, conf_path = _component(tmp_path)
    comp.apply(comp.plan())
    lines = conf_path.read_text(encoding="utf-8").splitlines()
    assert "listen-address=127.0.0.1" in lines
    assert "bind-interfaces" in lines


def test_unit_conf_dir_is_absolute_for_relative_conf_path(tmp_path, monkeypatch):
    """systemd resolves relative paths against / -- the unit must use an absolute path or %h/... ."""
    monkeypatch.chdir(tmp_path)
    platform = FakePlatform()
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    comp = DnsmasqComponent(platform, paths, Config(), conf_path=Path("rel/dnsmasq.conf"))
    comp.apply(comp.plan())
    unit = platform.services["dnsmasq"]
    conf_dir = unit.split("--conf-dir=")[1].split()[0]
    # A relative path is resolved by systemd against / -- it must be absolute or %h/...
    assert conf_dir.startswith("/") or conf_dir.startswith("%h/")
    assert conf_dir != "rel" and not conf_dir.startswith("rel/")


def test_check_reports_service_not_running(tmp_path):
    comp, _platform, _conf = _component(tmp_path)
    status = comp.check()
    assert status.ok is False
    assert "dnsmasq" in status.detail


def test_check_reports_missing_conf(tmp_path):
    comp, platform, _conf = _component(tmp_path)
    platform.active.add("dnsmasq")
    status = comp.check()
    assert status.ok is False
    assert "config" in status.detail


def test_check_ok_after_apply(tmp_path):
    comp, _platform, _conf = _component(tmp_path)
    comp.apply(comp.plan())
    status = comp.check()
    assert status.ok is True


def test_plan_empty_when_already_in_place(tmp_path):
    """plan must be empty when already in place -- then setup can determine it is a no-op."""
    comp, _platform, _conf = _component(tmp_path)
    comp.apply(comp.plan())
    assert comp.plan() == []


def test_plan_non_empty_when_service_down(tmp_path):
    comp, platform, _conf = _component(tmp_path)
    comp.apply(comp.plan())
    platform.active.discard("dnsmasq")
    assert comp.plan() != []


def test_plan_non_empty_when_conf_changed(tmp_path):
    comp, _platform, conf_path = _component(tmp_path)
    comp.apply(comp.plan())
    conf_path.write_text("port=5353\n", encoding="utf-8")
    assert comp.plan() != []


def test_apply_unknown_action_raises(tmp_path):
    from devproxy.platform.base import Action

    comp, _platform, _conf = _component(tmp_path)
    with pytest.raises(RuntimeError):
        comp.apply([Action(description="?", kind="run", command=["bogus"])])


# -- verify: resolution of *.test actually works ---------------------------


def test_verify_ok_when_dns_query_returns_loopback(tmp_path):
    comp, platform, _conf = _component(tmp_path)
    platform.active.add("dnsmasq")
    status = comp.verify()
    assert status.ok is True
    assert platform.dns_queries == ["foo.test"]


def test_verify_fails_when_dns_query_returns_nothing(tmp_path):
    comp, platform, _conf = _component(tmp_path)
    platform.active.add("dnsmasq")
    platform.dns_answer = None
    status = comp.verify()
    assert status.ok is False
    assert "foo.test" in status.detail
    assert status.hint


def test_verify_fails_when_dns_query_returns_wrong_address(tmp_path):
    comp, platform, _conf = _component(tmp_path)
    platform.active.add("dnsmasq")
    platform.dns_answer = "10.0.0.5"
    assert comp.verify().ok is False


# -- backups: back up the user's existing config before rewriting it ---------


def test_apply_backs_up_changed_user_config(tmp_path):
    original = "port=53\naddress=/.develop/127.0.0.1\n"
    comp, _platform, conf_path = _component(tmp_path, conf=original)
    comp.apply(comp.plan())

    backups = sorted((tmp_path / "s" / "backups").glob("dnsmasq.conf.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_apply_does_not_backup_when_content_unchanged(tmp_path):
    """Do not write a backup when the content is unchanged -- otherwise every setup piles up garbage."""
    comp, platform, conf_path = _component(tmp_path)
    merged = merge_dnsmasq_config("", port=platform.dns_port, suffix=Config().domain_suffix)
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(merged, encoding="utf-8")

    from devproxy.platform.base import Action

    comp.apply(
        [
            Action(
                description="write the same content",
                kind="write",
                path=conf_path,
                content=merged,
            )
        ]
    )
    backups = list((tmp_path / "s" / "backups").glob("*.bak"))
    assert backups == []


def test_apply_creates_no_backup_for_new_config(tmp_path):
    comp, _platform, _conf_path = _component(tmp_path)
    comp.apply(comp.plan())
    assert list((tmp_path / "s" / "backups").glob("*.bak")) == []


# -- macOS: launchd has no %h and no ExecReload ----------------------------


class _MacPlatform(FakePlatform):
    """macOS platform: the platform renders the unit as a launchd plist."""

    name = "mac"
    dns_port = 5300

    def __init__(self):
        super().__init__()
        self.installed: dict = {}

    def install_service(
        self, name, *, exec_start, exec_reload=None, exec_stop=None, env, description=""
    ):
        self.installed = {
            "name": name,
            "exec_start": list(exec_start),
            "exec_reload": exec_reload,
            "env": dict(env),
            "description": description,
        }


def test_macos_exec_start_uses_absolute_conf_dir(tmp_path):
    """launchd does not understand systemd's %h -- the config directory must be an absolute path."""
    comp, platform, _conf = _component(tmp_path, platform=_MacPlatform())
    comp.apply(comp.plan())
    exec_start = platform.installed["exec_start"]
    conf_dir = [a for a in exec_start if a.startswith("--conf-dir=")]
    assert conf_dir, "the unit must include the config directory"
    path = conf_dir[0].split("=", 1)[1]
    assert path.startswith("/"), f"launchd requires an absolute path: {path}"
    assert "%h" not in path


def test_macos_exec_start_program_is_absolute(tmp_path):
    """launchd's ProgramArguments must be absolute paths."""
    comp, platform, _conf = _component(tmp_path, platform=_MacPlatform())
    comp.apply(comp.plan())
    assert Path(platform.installed["exec_start"][0]).is_absolute()


def test_macos_has_no_exec_reload(tmp_path):
    """launchd's plist has no ExecReload -- reloading relies on a kickstart restart."""
    comp, platform, _conf = _component(tmp_path, platform=_MacPlatform())
    comp.apply(comp.plan())
    assert platform.installed["exec_reload"] is None


def test_macos_service_description_mentions_launchagent(tmp_path):
    comp, platform, _conf = _component(tmp_path, platform=_MacPlatform())
    comp.apply(comp.plan())
    assert "LaunchAgent" in platform.installed["description"]


def test_linux_exec_reload_still_uses_mainpid(tmp_path):
    """The Linux path is unchanged: ExecReload still sends HUP to dnsmasq."""
    comp, platform, _conf = _component(tmp_path)
    comp.apply(comp.plan())
    unit = platform.services["dnsmasq"]
    assert "ExecReload=/bin/kill -HUP $MAINPID" in unit


# ---------- hints and binary paths: chosen per platform ----------


def test_macos_check_hint_mentions_launchctl(tmp_path):
    """On macOS, do not tell the user to run systemctl -- give the launchctl command."""
    comp, _platform, _conf = _component(tmp_path, platform=_MacPlatform())
    status = comp.check()
    assert status.ok is False
    assert "launchctl" in status.hint
    assert "systemctl" not in status.hint


def test_macos_verify_hint_mentions_launchctl(tmp_path):
    comp, platform, _conf = _component(tmp_path, platform=_MacPlatform())
    platform.active.add("dnsmasq")
    platform.dns_answers["foo.test"] = None
    status = comp.verify()
    assert status.ok is False
    assert "launchctl" in status.hint
    assert "systemctl" not in status.hint


def test_linux_check_hint_still_mentions_systemctl(tmp_path):
    """The Linux path is unchanged."""
    comp, _platform, _conf = _component(tmp_path)
    status = comp.check()
    assert "systemctl" in status.hint


def test_macos_dnsmasq_binary_prefers_homebrew_path(tmp_path, monkeypatch):
    """The dnsmasq on PATH may not be the Homebrew one -- prefer the fixed path."""
    from devproxy.components import dnsmasq as dnsmasq_module

    brew = tmp_path / "opt" / "homebrew" / "sbin" / "dnsmasq"
    brew.parent.mkdir(parents=True)
    brew.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(dnsmasq_module, "MACOS_DNSMASQ_CANDIDATES", (str(brew),))
    monkeypatch.setattr(dnsmasq_module.shutil, "which", lambda name: "/usr/local/bin/dnsmasq")
    comp, _platform, _conf = _component(tmp_path, platform=_MacPlatform())
    assert comp._dnsmasq_binary() == str(brew)


def test_macos_dnsmasq_binary_falls_back_to_which(tmp_path, monkeypatch):
    """Only when none of the fixed paths exist, use the one on PATH."""
    from devproxy.components import dnsmasq as dnsmasq_module

    monkeypatch.setattr(dnsmasq_module, "MACOS_DNSMASQ_CANDIDATES", ("/nope/dnsmasq",))
    monkeypatch.setattr(dnsmasq_module.shutil, "which", lambda name: "/usr/local/bin/dnsmasq")
    comp, _platform, _conf = _component(tmp_path, platform=_MacPlatform())
    assert comp._dnsmasq_binary() == "/usr/local/bin/dnsmasq"


# -- I1: after a config change, an already-active service must restart ------


class _TrackingPlatform(FakePlatform):
    """Records restart_service calls -- FakePlatform only updates the active set by default."""

    def __init__(self):
        super().__init__()
        self.restarts: list[str] = []

    def restart_service(self, name):
        self.restarts.append(name)
        super().restart_service(name)


def test_apply_restarts_active_service_after_config_change(tmp_path):
    """enable --now does not restart an already-active unit -- the new config must take effect via an explicit restart."""
    platform = _TrackingPlatform()
    comp, _platform, conf_path = _component(tmp_path, platform=platform)
    comp.apply(comp.plan())  # first install: the service was not running, no restart needed
    assert platform.restarts == []

    conf_path.write_text("port=53\n", encoding="utf-8")
    comp.apply(comp.plan())  # service already active: must restart after writing the config
    assert platform.restarts == ["dnsmasq"]


# -- I2: check's content criterion must agree with plan -------------------


def test_check_reports_config_drift_when_service_running(tmp_path):
    """The service is running but the config was changed -- doctor must not report green."""
    comp, _platform, conf_path = _component(tmp_path)
    comp.apply(comp.plan())
    conf_path.write_text("port=53\n", encoding="utf-8")

    status = comp.check()
    assert status.ok is False
    assert "missing or deviates" in status.detail
    assert "setup" in status.hint


def test_check_skips_content_validation_when_conf_unreadable(tmp_path):
    """When the config is unreadable it cannot be disproven, so fall back to the existence criterion (same as the dns component)."""
    comp, platform, conf_path = _component(tmp_path, conf="port=53\n")
    platform.active.add("dnsmasq")
    comp.conf_path = UnreadablePath(str(conf_path))

    status = comp.check()
    assert status.ok is True
    assert "unreadable" in status.detail


# -- I7: a missing binary must surface with the right message in check/plan --


def test_macos_missing_binary_raises_instead_of_fake_path(tmp_path, monkeypatch):
    """Never return a fabricated /opt/homebrew path when it cannot be found."""
    monkeypatch.setattr(dnsmasq_module, "MACOS_DNSMASQ_CANDIDATES", ("/nope/dnsmasq",))
    monkeypatch.setattr(dnsmasq_module.shutil, "which", lambda name: None)
    comp, _platform, _conf = _component(tmp_path, platform=_MacPlatform())

    with pytest.raises(RuntimeError, match="brew install dnsmasq"):
        comp._dnsmasq_binary()


def test_macos_check_reports_missing_binary_with_brew_hint(tmp_path, monkeypatch):
    """When dnsmasq is not installed, do not suggest launchctl -- the service does not exist."""
    monkeypatch.setattr(dnsmasq_module, "MACOS_DNSMASQ_CANDIDATES", ("/nope/dnsmasq",))
    monkeypatch.setattr(dnsmasq_module.shutil, "which", lambda name: None)
    comp, _platform, _conf = _component(tmp_path, platform=_MacPlatform())

    status = comp.check()
    assert status.ok is False
    assert "brew install dnsmasq" in status.hint
    assert "launchctl" not in status.hint


def test_linux_check_reports_missing_binary_with_install_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(dnsmasq_module, "LINUX_DNSMASQ_BINARY", "/nope/dnsmasq")
    comp, _platform, _conf = _component(tmp_path)

    status = comp.check()
    assert status.ok is False
    assert "not found" in status.detail
    assert "install dnsmasq" in status.hint


# -- M7: a failed backup read blocks the write; backups in the same second must not overwrite each other --


class _UnreadableBackupSource(UnreadablePath):
    """read_bytes is denied too -- simulating a permission change after plan (TOCTOU)."""

    def read_bytes(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))


def test_apply_refuses_to_overwrite_when_backup_read_fails(tmp_path):
    from devproxy.platform.base import Action

    comp, _platform, conf_path = _component(tmp_path, conf="port=53\n")
    comp.conf_path = _UnreadableBackupSource(str(conf_path))

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        comp.apply(
            [
                Action(
                    description="write the new config",
                    kind="write",
                    path=comp.conf_path,
                    content="port=5353\n",
                )
            ]
        )
    # The original file must remain untouched
    assert conf_path.read_text(encoding="utf-8") == "port=53\n"


def test_backups_do_not_collide_within_same_second(tmp_path, monkeypatch):
    from devproxy.platform.base import Action

    monkeypatch.setattr(dnsmasq_module.time, "strftime", lambda *args, **kwargs: "20260101T000000Z")
    comp, _platform, conf_path = _component(tmp_path, conf="port=53\n")
    first = merge_dnsmasq_config("port=53\n", port=5353, suffix="test")
    comp.apply([Action(description="first", kind="write", path=conf_path, content=first)])

    conf_path.write_text("port=5300\n", encoding="utf-8")
    second = merge_dnsmasq_config("port=5300\n", port=5353, suffix="test")
    comp.apply([Action(description="second", kind="write", path=conf_path, content=second)])

    backups = sorted((tmp_path / "s" / "backups").glob("dnsmasq.conf.*.bak"))
    assert len(backups) == 2, "a second backup within the same second must not overwrite the first"
    assert {p.read_text(encoding="utf-8") for p in backups} == {
        "port=53\n",
        "port=5300\n",
    }
