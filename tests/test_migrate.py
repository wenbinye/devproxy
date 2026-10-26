"""Tests for the migration flow.

Iron rule: tests never touch the real system -- every path points at tmp_path and the platform is always FakePlatform.
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
    """The unit file is unreadable by a normal user (a 0600 root unit in the macOS system scope)."""

    def service_unit_path(self, name):
        return UnreadablePath(str(super().service_unit_path(name)))

    def privilege(self):
        return ElevatedReader(self)


def _paths(tmp_path):
    return Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")


def _legacy_state(tmp_path, unit_text=None, caddyfile_text=None):
    """Build a legacy environment: unit + old Caddyfile, all under tmp_path."""
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
    """Migrate, but point the dnsmasq config and DNS route file at tmp_path (never touch the real system)."""
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
    """Convenience shortcut that returns only the backup object."""
    return _run_migration(platform, paths, config, state, tmp_path).backup


# -- Caddyfile splitting ------------------------------------------------------


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
    """The global block must not go into sites.caddy -- a second global block in the imported file makes Caddy fail outright."""
    legacy = "{\n\tstorage file_system /data\n}\n\nlocalhost {\n\ttls internal\n}\n"
    personal, dropped = split_caddyfile(legacy)
    assert "storage" not in personal
    assert dropped == []


def test_split_keeps_comments_with_their_site():
    """Comments before a site block are the user's own documentation -- they must not be dropped during the split.

    Real users put an explanation before every site in their Caddyfile; if blocks were
    detected by "is the first line a comment", the *.test block would be kept as a
    personal site and conflict with the tool-generated sites.
    """
    legacy = (
        "# local gateway\n"
        "*.local.yikuaigao.com {\n"
        "\ttls internal\n"
        "\treverse_proxy {$MINIKUBE_IP:192.168.49.2}:30800\n"
        "}\n"
        "\n"
        "# dynamic routes (managed by devp)\n"
        "*.test {\n"
        "\ttls {\n"
        "\t\ton_demand\n"
        "\t}\n"
        '\trespond "nope" 404\n'
        "}\n"
    )
    personal, dropped = split_caddyfile(legacy)
    assert "*.local.yikuaigao.com" in personal
    assert "# local gateway" in personal
    assert "*.test" not in personal
    assert len(dropped) == 1


def test_split_ignores_braces_inside_comments():
    legacy = "# braces { and } inside comments must not affect the split\nlocalhost {\n\ttls internal\n}\n"
    personal, dropped = split_caddyfile(legacy)
    assert "localhost" in personal
    assert dropped == []


def test_split_ignores_braces_inside_inline_comments():
    """`example.test { # site } explanation` must not truncate the block."""
    legacy = "example.test { # site } explanation\n\treverse_proxy 1.2.3.4:80\n}\n"
    personal, dropped = split_caddyfile_blocks(legacy)
    assert dropped == []
    assert len(personal) == 1
    assert "reverse_proxy" in personal[0]
    assert len(personal[0].splitlines()) == 3, "the block must be intact and not split in two"


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
        "*.test { # wildcard } block\n\trespond 404\n}\n",
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
    """doctor must be able to see https://*.test -- missing it makes every TLS handshake fail."""
    assert wildcard_addresses("https://*.test {\n\trespond 404\n}\n") == ["https://*.test"]


# -- Unit parsing -------------------------------------------------------------


def test_unit_config_path_parsed():
    unit = "[Service]\nExecStart=%h/.local/bin/caddy run --config %h/work/x/Caddyfile\n"
    p = unit_config_path(unit, Path("/home/u"))
    assert p == Path("/home/u/work/x/Caddyfile")


def test_unit_config_path_none_when_absent():
    assert unit_config_path("[Service]\nExecStart=/usr/bin/foo\n", Path("/home/u")) is None


def test_unit_config_path_accepts_equals_form():
    """Caddy/pflag accept --config=path -- that is also a valid unit form."""
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
    assert state is not None, "--config=<path> must not be treated as no legacy environment"
    assert state.config_path == Path("/old/Caddyfile")
    assert state.error is None


def test_unit_env_extracted():
    unit = "[Service]\nEnvironment=MINIKUBE_IP=192.168.49.2\nEnvironment=FOO=bar\n"
    assert unit_env(unit) == {"MINIKUBE_IP": "192.168.49.2", "FOO": "bar"}


def test_unit_env_ignores_comments():
    unit = "# Environment=NOT_SET=1\n[Service]\nEnvironment=REAL=1\n"
    assert unit_env(unit) == {"REAL": "1"}


def test_unit_env_handles_quoted_values_with_spaces():
    """systemd's Environment= uses shell-style word splitting -- values with spaces must not be broken apart."""
    unit = '[Service]\nEnvironment="GREETING=hello world" "MINIKUBE_IP=1.2.3.4"\n'
    assert unit_env(unit) == {"GREETING": "hello world", "MINIKUBE_IP": "1.2.3.4"}


def test_unit_env_handles_multiple_assignments_per_line():
    unit = "[Service]\nEnvironment=FOO=bar BAZ=qux\n"
    assert unit_env(unit) == {"FOO": "bar", "BAZ": "qux"}


def test_unit_environment_files_detected():
    unit = "[Service]\nEnvironmentFile=/etc/default/caddy\nEnvironmentFile=-/etc/caddy/env\n"
    assert unit_environment_files(unit) == ["/etc/default/caddy", "/etc/caddy/env"]


def test_detect_legacy_refuses_environment_file(tmp_path):
    """The content of EnvironmentFile cannot be rewritten safely -- automatic migration must be refused and named."""
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


# -- Legacy detection ---------------------------------------------------------


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
    """The unit already points at the tool's Caddyfile -- not a legacy environment; do not migrate it again."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    unit_path = tmp_path / "caddy.service"
    unit_path.write_text(
        f"[Service]\nExecStart=/bin/caddy run --config {paths.caddyfile}\n", encoding="utf-8"
    )
    assert detect_legacy(platform, paths, unit_dir=tmp_path, home=tmp_path) is None


def test_detect_legacy_returns_unconfirmed_state_when_config_unparseable(tmp_path):
    """A unit exists but --config cannot be parsed: do not treat it as a "clean machine".

    Returning None would make setup rewrite the user's unit directly -- with no
    backup, all manual configuration is lost. Must return a state with an error so
    migration refuses to touch anything.
    """
    paths = _paths(tmp_path)
    platform = FakePlatform()
    (tmp_path / "caddy.service").write_text("[Service]\nExecStart=/usr/bin/foo\n")
    state = detect_legacy(platform, paths, unit_dir=tmp_path, home=tmp_path)
    assert state is not None
    assert state.config_path is None
    assert state.error and "--config" in state.error


def test_detect_legacy_reads_unreadable_unit_through_elevation(tmp_path):
    """The unit is unreadable by a normal user -- it must be read through an elevated session.

    macOS system-scope units live in /Library/LaunchDaemons; with wrong permissions
    or ownership a normal user cannot read them. Treating an unreadable unit as "no
    legacy environment" silently takes over the user's hand-maintained Caddyfile and
    environment variables -- the worst mistake migration can make.
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
    assert state is not None, "an unreadable unit must not be treated as no legacy environment"
    assert state.env == {"MINIKUBE_IP": "1.2.3.4"}


# -- Migration ----------------------------------------------------------------


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
    assert backup.entries, "migration must produce a backup"
    assert (backup.directory / "MANIFEST.txt").exists()
    assert (backup.directory / "manifest.json").exists()
    # The original files are still in the backup
    assert any(e.original == unit_path and e.existed for e in backup.entries)


def test_migrate_backup_covers_every_file_to_be_touched(tmp_path):
    """Every existing file that will be touched must be in the backup -- otherwise rollback leaves residue."""
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
    assert state.unit_path in originals, "the unit will be rewritten"
    assert paths.sites_caddy in originals, "personal sites will be written"
    assert paths.caddyfile in originals, "the main Caddyfile will be generated"
    assert dnsmasq_conf in originals, "the dnsmasq config will be merged and rewritten"
    assert tmp_path / "resolved.conf" in originals, "the DNS route file will be rewritten"


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
    """The tool only imports sites.caddy -- not a single character of the user's own content may be touched."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    paths.sites_caddy.write_text('mine.example { respond "hi" }\n', encoding="utf-8")
    platform = FakePlatform()
    state = _legacy_state(tmp_path, caddyfile_text="localhost {\n\ttls internal\n}\n")
    outcome = _run_migration(platform, paths, Config(), state, tmp_path)
    assert paths.sites_caddy.read_text() == 'mine.example { respond "hi" }\n'
    assert any("sites.caddy" in n for n in outcome.notes), (
        "must tell the user personal sites were not migrated"
    )


def test_migrate_states_that_personal_sites_are_no_longer_served(tmp_path):
    """After migration the old Caddyfile is no longer imported -- the sites inside it are no longer served.

    This must be stated explicitly (naming the file and addresses): a bare "not
    covered" makes people think their personal sites are fine when they are already 404ing.
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
    assert "will no longer be served" in joined, (
        f"must state that sites are no longer served: {outcome.notes}"
    )
    assert "localhost" in joined and "api.example.com" in joined, "must name the affected addresses"
    assert str(state.config_path) in joined, "must name the file to merge from"
    assert str(paths.sites_caddy) in joined, "must name the file to merge into"


def test_migrate_unit_falls_back_to_legacy_binary_when_config_binary_missing(tmp_path):
    """When the binary has not been downloaded yet, the unit must not point at a nonexistent file.

    Migration happens before the component flow (download) -- copying the path from
    config would break the running Caddy (the unit points at a nonexistent binary),
    and nothing could bring it back up.
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
    assert any("keep using the legacy" in n for n in outcome.notes), (
        f"must state which binary was used: {outcome.notes}"
    )


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
    """When setup later reinstalls the unit it must also include MINIKUBE_IP -- personal sites depend on it."""
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
        [Action(description="install unit", kind="run", command=["install-service", "caddy"])]
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
    """The old Caddyfile is only backed up, never rewritten -- it is what rollback restores."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path, caddyfile_text="localhost {\n\ttls internal\n}\n")
    _migrate(platform, paths, Config(), state, tmp_path)
    assert state.config_path.read_text(encoding="utf-8") == "localhost {\n\ttls internal\n}\n"


def test_migrate_aborts_when_backup_target_is_not_a_regular_file(tmp_path):
    """Backup failure -> migration aborts without changing a single file."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    # Replace the old Caddyfile with a symlink -- the backupper must refuse, not record it as "did not exist"
    state.config_path.unlink()
    state.config_path.symlink_to(tmp_path / "target.txt")
    with pytest.raises(MigrationError):
        _migrate(platform, paths, Config(), state, tmp_path)
    assert platform.services == {}, "the unit must not be rewritten when backup fails"
    assert paths.sites_caddy.read_text(encoding="utf-8") == ""


def test_migrate_restores_backup_when_rewrite_fails(tmp_path, monkeypatch):
    """A failure in the middle of rewriting -> restore from backup; never leave the machine half-migrated."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    original_unit = state.unit_path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("failed to write unit")

    monkeypatch.setattr(platform, "install_service", boom)
    with pytest.raises(MigrationError):
        _migrate(platform, paths, Config(), state, tmp_path)
    assert state.unit_path.read_text(encoding="utf-8") == original_unit


def test_migrate_restores_backup_on_keyboard_interrupt(tmp_path, monkeypatch):
    """Ctrl-C is also a failure path: must restore, and the error must include the backup location.

    KeyboardInterrupt is a BaseException -- catching only Exception would leave the
    machine in a half-migrated state ("sites.caddy written, unit not swapped yet"),
    and the user would not know where the backup is.
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
    assert "interrupted" in message
    assert "devproxy setup --rollback" in message
    assert state.unit_path.read_text(encoding="utf-8") == original_unit
    assert paths.sites_caddy.read_text(encoding="utf-8").strip() == ""
    assert any("discarded" in n for n in ei.value.notes), (
        "notes emitted before the failure must also reach the caller"
    )


def test_migrate_refuses_unconfirmed_legacy_state(tmp_path):
    """Unconfirmable legacy unit: refuse migration without touching a file or creating the backup directory."""
    paths = _paths(tmp_path)
    platform = FakePlatform()
    unit_path = tmp_path / "caddy.service"
    unit_path.write_text("ORIGINAL\n", encoding="utf-8")
    state = LegacyState(
        unit_path=unit_path, config_path=None, error="cannot confirm legacy caddy unit"
    )
    with pytest.raises(MigrationError) as ei:
        _migrate(platform, paths, Config(), state, tmp_path)
    assert "cannot confirm" in str(ei.value)
    assert unit_path.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert platform.services == {}
    assert not paths.backup_dir.exists()


def test_run_migration_returns_notes_in_outcome(tmp_path):
    """notes are part of the return value, no longer passed in by the caller as a mutable list."""
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
    assert any("discarded" in n for n in outcome.notes)
    assert outcome.notes is not None


# -- Rollback -----------------------------------------------------------------


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
    """Did not exist before -> delete on restore; tool-created files must not be left on the machine."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    backup = _migrate(platform, paths, Config(), state, tmp_path)
    # The main Caddyfile did not exist before migration; it was generated afterwards
    assert paths.caddyfile.exists()
    rollback(backup)
    assert not paths.caddyfile.exists(), "the tool-generated Caddyfile must be deleted"


def test_rollback_restores_sites_caddy_to_empty(tmp_path):
    """sites.caddy was empty before migration (created by the tool) -> restore must not leave personal site residue."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path, caddyfile_text="localhost {\n\ttls internal\n}\n")
    backup = _migrate(platform, paths, Config(), state, tmp_path)
    assert "localhost" in paths.sites_caddy.read_text(encoding="utf-8")
    rollback(backup)
    assert paths.sites_caddy.read_text(encoding="utf-8").strip() == ""


def test_rollback_from_loaded_backup(tmp_path):
    """The rollback directory can be reloaded later -- manifest.json is machine-readable."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    backup = _migrate(platform, paths, Config(), state, tmp_path)
    loaded = load_backup(backup.directory)
    state.unit_path.write_text("CLOBBERED\n")
    rollback(loaded)
    assert "ExecStart" in state.unit_path.read_text(encoding="utf-8")


# -- DNS backend consistency (the backup must be the file that will actually be changed) --


def test_default_dns_route_follows_explicit_backend(tmp_path):
    """When the user explicitly selects a backend, the backed-up drop-in must follow it.

    The component writes /etc/dnsmasq.d/devproxy.conf, but the resolved file was
    backed up -- the file actually changed is not in the backup, so rollback cannot
    delete it.
    """
    paths = _paths(tmp_path)
    platform = FakePlatform()
    config = Config()
    system = default_dns_route(platform, paths, config, "system")
    resolved = default_dns_route(platform, paths, config, "resolved")
    assert system.as_posix().endswith("/etc/dnsmasq.d/devproxy.conf")
    assert resolved.as_posix().endswith("/etc/systemd/resolved.conf.d/devproxy.conf")
    assert system != resolved


# -- Config validation: caddy itself must validate before swapping the unit ----


def test_migrate_aborts_when_caddy_validate_fails(tmp_path, monkeypatch):
    """A syntax error in personal sites must abort migration -- not one character of the unit may change."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    original_unit = state.unit_path.read_text(encoding="utf-8")

    # Build a caddy binary that exists but fails validation
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
    assert "config validation failed" in str(ei.value)
    assert state.unit_path.read_text(encoding="utf-8") == original_unit
    assert not paths.caddyfile.exists(), (
        "the main Caddyfile must not be written when validation fails"
    )
    assert paths.sites_caddy.read_text(encoding="utf-8").strip() == "", (
        "sites.caddy must be restored"
    )


def test_migrate_skips_validation_when_binary_missing(tmp_path):
    """A missing binary must not be treated as migration failure -- setup validates after downloading."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    config = Config(caddy_bin=str(tmp_path / "absent"))
    outcome = _run_migration(platform, paths, config, state, tmp_path)
    assert any("skipping config validation" in n for n in outcome.notes)
    assert paths.caddyfile.exists()


def test_validate_falls_back_to_legacy_unit_binary(tmp_path, monkeypatch):
    """When the caddy in config.toml does not exist, validate with the binary from the legacy unit.

    "Always validate before swapping the unit" must not become empty words just
    because the configured path has not been downloaded yet -- the legacy unit's
    ExecStart points at the caddy actually running on this machine.
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
    assert validate_calls, "caddy validate must actually run once"
    assert validate_calls[0][0] == str(legacy_bin)
    assert any("from the legacy unit" in n for n in outcome.notes)
    assert not any("skipping config validation" in n for n in outcome.notes)


def test_validate_still_skips_when_no_binary_anywhere(tmp_path):
    """There is no binary in either config or the legacy unit -- can only skip, but must leave a hint."""
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
    assert any("skipping config validation" in n for n in outcome.notes)


def test_unit_exec_start_parsed():
    from devproxy.lifecycle.migrate import unit_exec_start

    assert unit_exec_start("[Service]\nExecStart=/opt/caddy run --config /etc/Caddyfile\n") == [
        "/opt/caddy",
        "run",
        "--config",
        "/etc/Caddyfile",
    ]
    assert unit_exec_start("[Service]\nEnvironment=X=1\n") is None


# -- storage continuity: the CA must not move --------------------------------


def test_legacy_storage_path_parsed():
    legacy = "{\n\tstorage file_system /home/u/.local/share/caddy\n}\n\nlocalhost {\n}\n"
    assert legacy_storage_path(legacy) == Path("/home/u/.local/share/caddy")


def test_legacy_storage_path_none_without_global_block():
    assert legacy_storage_path("localhost {\n}\n") is None


def test_legacy_storage_path_strips_inline_comment():
    """For `storage file_system /data # old ca` the path is /data, not the whole string including the comment."""
    assert legacy_storage_path("{\n\tstorage file_system /data # old ca\n}\n") == Path("/data")


def test_legacy_storage_path_keeps_quoted_path_with_spaces():
    text = '{\n\tstorage file_system "/home/u/my caddy" # old CA\n}\n'
    assert legacy_storage_path(text) == Path("/home/u/my caddy")


def test_legacy_storage_path_keeps_hash_inside_quotes():
    text = '{\n\tstorage file_system "/home/u/my # caddy"\n}\n'
    assert legacy_storage_path(text) == Path("/home/u/my # caddy")


def test_migrate_keeps_legacy_storage_so_ca_stays(tmp_path):
    """When legacy storage differs from config, the legacy one must be kept -- changing the CA makes browsers stop trusting it."""
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
    assert any("storage" in n and str(legacy_storage) in n for n in outcome.notes), (
        "the difference must be stated"
    )
    # The effective config must be carried back: setup uses it to continue the component flow and must not write back the old storage
    assert outcome.config.storage_path == legacy_storage
    # Archive: later setup must use the same place, otherwise the CA is regenerated
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


# -- Failure paths must give the backup location (I5) -------------------------


def test_failure_message_points_at_backup_dir(tmp_path, monkeypatch):
    """When restore fails, what the user most needs to know is: where the backup is and how to restore manually."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("failed to write unit")

    monkeypatch.setattr(platform, "install_service", boom)
    with pytest.raises(MigrationError) as ei:
        _migrate(platform, paths, Config(), state, tmp_path)
    message = str(ei.value)
    assert "backups" in message, message
    assert "devproxy setup --rollback" in message, message


# -- Backup targets and discard notes -----------------------------------------


def test_backup_covers_caddy_env_and_storage(tmp_path):
    """Archives left by migration (environment variables, storage) must also be backed up -- otherwise they survive rollback."""
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    state = _legacy_state(tmp_path)
    backup = _migrate(platform, paths, Config(), state, tmp_path)
    originals = {e.original for e in backup.entries}
    assert paths.caddy_env in originals
    assert paths.caddy_storage in originals


def test_dropped_blocks_note_lists_addresses(tmp_path):
    """Discarded blocks must be named -- the user needs to see whether any hand-written *.test block is among them."""
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
    dropped_notes = [n for n in outcome.notes if "discarded" in n]
    assert dropped_notes, outcome.notes
    assert "*.test" in dropped_notes[0]
    assert "http://*.test" in dropped_notes[0]
    assert "backup" in dropped_notes[0]


def test_migrate_creates_import_targets_before_validation(tmp_path, monkeypatch):
    """Import targets must already exist before validation -- caddy rejects a missing import outright.

    Migration is an API; it must not assume the caller has already created
    sites.caddy / routes.caddy: the main Caddyfile used for validation imports both,
    and if either is missing caddy necessarily reports "file not found", so migration
    would be blocked by its own validation.
    """
    paths = _paths(tmp_path)  # deliberately not ensure_files()
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
            return (1, "import target does not exist") if seen["missing"] else (0, "")
        return 0, ""

    monkeypatch.setattr(platform, "run", fake_run)
    _migrate(platform, paths, config, state, tmp_path)
    assert seen.get("missing") == [], "import targets must already exist during validation"


# -- One-shot storage resolution ----------------------------------------------


def test_resolve_storage_details_returns_legacy_path(tmp_path):
    """One parse gives both the path and the note -- the caller need not read the legacy Caddyfile again."""
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


# -- Archives: atomic writes and corruption warnings --------------------------


def test_load_extra_env_fails_loudly_on_corrupt_archive(tmp_path):
    """The archive exists but cannot be parsed -> raise, never silently treat it as empty (that would lose MINIKUBE_IP)."""
    from devproxy.core.caddy_env import CaddyEnvArchiveError
    from devproxy.core.caddy_env import load_extra_env

    paths = _paths(tmp_path)
    paths.ensure_files()
    paths.caddy_env.write_text("{ truncated", encoding="utf-8")
    with pytest.raises(CaddyEnvArchiveError) as ei:
        load_extra_env(paths)
    message = str(ei.value)
    assert str(paths.caddy_env) in message, "must name which file"
    assert "fix" in message and "delete" in message, "must give a way to fix it"
    assert "MINIKUBE_IP" in message, "must explain the consequence"


def test_load_extra_env_rejects_non_object_json(tmp_path):
    """Valid JSON that is not an object (e.g. an array) is equally a bad archive."""
    from devproxy.core.caddy_env import CaddyEnvArchiveError
    from devproxy.core.caddy_env import load_extra_env

    paths = _paths(tmp_path)
    paths.ensure_files()
    paths.caddy_env.write_text('["MINIKUBE_IP", "1.2.3.4"]', encoding="utf-8")
    with pytest.raises(CaddyEnvArchiveError, match="not a JSON object"):
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
    paths.caddy_storage.write_text("{ truncated", encoding="utf-8")
    with pytest.warns(UserWarning, match="storage archive"):
        assert load_legacy_storage(paths) is None


def test_save_extra_env_is_atomic_when_replace_fails(tmp_path, monkeypatch):
    """When the write fails the target file keeps its old content and no temp file is left behind."""
    import devproxy.core.paths as paths_mod
    from devproxy.core.caddy_env import save_extra_env

    paths = _paths(tmp_path)
    save_extra_env(paths, {"A": "1"})
    before = paths.caddy_env.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(paths_mod.os, "replace", boom)
    with pytest.raises(OSError):
        save_extra_env(paths, {"A": "2"})
    assert paths.caddy_env.read_text(encoding="utf-8") == before
    leftovers = [p.name for p in paths.caddy_env.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == [], f"temporary files must be cleaned up: {leftovers}"


def test_save_legacy_storage_is_atomic_when_replace_fails(tmp_path, monkeypatch):
    import devproxy.core.paths as paths_mod
    from devproxy.lifecycle.migrate import save_legacy_storage

    paths = _paths(tmp_path)
    paths.ensure_files()
    save_legacy_storage(paths, Path("/old"))
    before = paths.caddy_storage.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(paths_mod.os, "replace", boom)
    with pytest.raises(OSError):
        save_legacy_storage(paths, Path("/new"))
    assert paths.caddy_storage.read_text(encoding="utf-8") == before


# -- Annotations must resolve (get_type_hints must not raise NameError) -------


def test_public_type_hints_resolve():
    from typing import get_type_hints

    assert get_type_hints(resolve_storage_details)
    assert get_type_hints(run_migration)
    assert get_type_hints(MigrationRequest)
