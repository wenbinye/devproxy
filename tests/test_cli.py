import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.cli._shared import DevProxyGroup
from devproxy.core.caddy_env import CaddyEnvArchiveError
from devproxy.core.config import Config
from devproxy.core.config import ConfigError
from devproxy.core.paths import Paths
from devproxy.gateway.routes import Route
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.store import RouteStore
from devproxy.gateway.store import RouteStoreError


class FakeCaddy:
    """Fake Caddy: validate/reload always succeeds, so tests never call the real caddy binary."""

    def validate(self, caddyfile=None):
        return True, "ok"

    def reload(self, caddyfile=None):
        return True, "ok"


class FakeAdmin:
    """Fake Admin API client: alive is scriptable, for precondition tests."""

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
    """Point devproxy's three directory trees at tmp_path."""
    monkeypatch.setenv("DEVPROXY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DEVPROXY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DEVPROXY_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def _patch_ctx(monkeypatch, tmp_path, admin_alive=True):
    """Replace build_context to inject a fake Caddy and fake Admin API."""
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    # up/expose require the main Caddyfile to exist (when missing, run devproxy setup first)
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
    """Arguments after -- go to the subcommand untouched, not swallowed by devproxy's option parser."""
    _patch_ctx(monkeypatch, env)
    captured = {}

    def fake_run(self, name, cmd, port=None, ready_timeout=None, on_ready=None, extra_env=None):
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
    # Wrong command syntax -> usage error (2), not a runtime failure
    assert res.exit_code == 2, res.output
    assert "--" in res.output, (
        f"env COLUMNS={os.environ.get('COLUMNS')!r} NO_COLOR={os.environ.get('NO_COLOR')!r} "
        f"FORCE_COLOR={os.environ.get('FORCE_COLOR')!r}; output lines: {res.output.splitlines()!r}"
    )


def test_up_without_command_is_rejected(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "codeup", "--"])
    assert res.exit_code == 2, res.output


def test_expose_registers_route(env, monkeypatch):
    """expose is one-shot: it returns right after registering, and the route persists."""
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
    """down must also kill leftover process groups of mode=up routes (using the identity recorded at registration)."""
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


def test_ls_sorts_routes_by_name(env, monkeypatch):
    """ls sorts by app name ascending -- independent of registration order, stable and predictable."""
    _patch_ctx(monkeypatch, env)
    state = env / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "routes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "domain_suffix": "test",
                "routes": [
                    {
                        "name": name,
                        "domain": f"{name}.test",
                        "port": 42100 + i,
                        "mode": "up",
                        "upstream": f"127.0.0.1:{42100 + i}",
                    }
                    for i, name in enumerate(["zebra", "alpha", "mike"])
                ],
            }
        ),
        encoding="utf-8",
    )
    res = CliRunner().invoke(app, ["ls"])
    assert res.exit_code == 0, res.output
    positions = [res.output.index(f"{name}.test") for name in ("alpha", "mike", "zebra")]
    assert positions == sorted(positions), f"should be sorted by name:\n{res.output}"


def test_config_shows_effective_config(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 0, res.output
    assert "domain_suffix" in res.output


def test_config_marks_effective_storage_and_labels_default_dir(env, monkeypatch):
    """config should only make clear "which storage is actually in effect".

    Paths.data_dir (affected only by DEVPROXY_DATA_DIR) and the storage in config.toml
    may not be the same -- calling both "storage" makes users think Caddy's data lives
    in the wrong place.
    """
    paths, cfg, _ = _patch_ctx(monkeypatch, env)
    # Make the two genuinely disagree: config.toml points elsewhere
    other = env / "other-storage"
    cfg = replace(cfg, storage=str(other))
    store = RouteStore(paths, cfg, FakeCaddy())
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context", lambda *a, **k: (paths, cfg, store, FakeAdmin())
    )
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 0, res.output
    assert str(other) in res.output, "the storage in effect must be printed"
    assert "data_dir_default" in res.output, "the default directory must get a non-misleading name"
    assert "is the Caddy storage path in effect" in res.output


# ---------------------------------------------- Trust hint after registering a route


def _write_ca(storage: Path) -> Path:
    """Put a CA root certificate under Caddy storage (simulating "the first route generated a CA")."""
    ca = storage / "pki" / "authorities" / "local" / "root.crt"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text(
        "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", encoding="utf-8"
    )
    return ca


def test_expose_prints_trust_hint_when_ca_not_trusted(env, monkeypatch):
    """The CA is generated when the first route is registered -- at that moment the user must be told to trust it.

    Without that, the user only sees "the browser says the certificate is insecure" and
    has no idea there is still one step left.
    """
    paths, cfg, _ = _patch_ctx(monkeypatch, env)
    _write_ca(Path(cfg.storage))
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 0, res.output
    assert "devproxy trust" in res.output


def test_expose_does_not_print_trust_hint_when_ca_trusted(env, monkeypatch):
    """When the CA is already trusted, do not repeat the hint -- popping up every time is noise."""
    paths, cfg, _ = _patch_ctx(monkeypatch, env)
    ca = _write_ca(Path(cfg.storage))
    platform = monkeypatch.undo  # noqa: F841 -- only to make the following read clearly
    from devproxy.platform.fake import FakePlatform

    fake = FakePlatform()
    fake.trusted.add(ca)
    fake.nss.add(ca)
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: fake)
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 0, res.output
    assert "devproxy trust" not in res.output


def test_expose_does_not_print_trust_hint_without_ca(env, monkeypatch):
    """With no CA yet (not generated), there is nothing to trust -- do not hint."""
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 0, res.output
    assert "devproxy trust" not in res.output


def test_up_prints_trust_hint_when_ca_not_trusted(env, monkeypatch):
    paths, cfg, _ = _patch_ctx(monkeypatch, env)
    _write_ca(Path(cfg.storage))

    def fake_run(self, name, cmd, port=None, ready_timeout=None, on_ready=None, extra_env=None):
        if on_ready is not None:
            on_ready("codeup.test", 8080)
        return 0

    monkeypatch.setattr("devproxy.cli._shared.UpRunner.run", fake_run)
    res = CliRunner().invoke(app, ["up", "codeup", "--", "sleep", "1"])
    assert res.exit_code == 0, res.output
    assert "devproxy trust" in res.output


# ---------------------------------------------------------------- I-1: exit code 3


def test_up_when_caddy_not_running_exits_3(env, monkeypatch):
    """Caddy not running is an unmet precondition -> exit code 3 (not 1)."""
    _patch_ctx(monkeypatch, env, admin_alive=False)
    res = CliRunner().invoke(app, ["up", "codeup", "--", "sleep", "5"])
    assert res.exit_code == 3, res.output
    assert "Caddy" in res.output


def test_expose_when_caddy_not_running_exits_3(env, monkeypatch):
    """expose registers a live route, so it also requires Caddy to be running -> exit code 3."""
    _patch_ctx(monkeypatch, env, admin_alive=False)
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 3, res.output
    assert "Caddy" in res.output
    assert not (env / "state" / "routes.json").exists(), (
        "must not write the route table when a precondition is unmet"
    )


def test_up_without_main_caddyfile_exits_3_and_points_at_setup(env, monkeypatch):
    """Not set up yet (main Caddyfile missing) is a precondition problem, not a reload runtime failure."""
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


# ---------------------------------------------------- I-2: corrupt route table must fail cleanly


def _write_routes_file(state_dir: Path, text: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "routes.json").write_text(text, encoding="utf-8")


def test_ls_with_invalid_json_routes_file_is_clean_error(env, monkeypatch):
    """routes.json is not valid JSON -> one-line error + exit code 1, not a Rich traceback."""
    _patch_ctx(monkeypatch, env)
    _write_routes_file(env / "state", "{ not json")
    res = CliRunner().invoke(app, ["ls"])
    assert res.exit_code == 1, res.output
    assert "devproxy" in res.output and "route table" in res.output
    assert "Traceback" not in res.output


def test_ls_with_routes_missing_required_key_is_clean_error(env, monkeypatch):
    """routes.json is missing a required key -> likewise a clean one-line error."""
    _patch_ctx(monkeypatch, env)
    _write_routes_file(env / "state", '{"version": 1, "routes": [{"name": "x"}]}')
    res = CliRunner().invoke(app, ["ls"])
    assert res.exit_code == 1, res.output
    assert "route table" in res.output
    assert "Traceback" not in res.output


def test_down_with_corrupt_routes_file_is_clean_error(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    _write_routes_file(env / "state", "[]")  # top level is a list rather than an object
    res = CliRunner().invoke(app, ["down", "codeup"])
    assert res.exit_code == 1, res.output
    assert "route table" in res.output
    assert "Traceback" not in res.output


def test_config_with_corrupt_routes_file_is_clean_error(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    _write_routes_file(env / "state", "{ oops")
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 1, res.output
    assert "route table" in res.output
    assert "Traceback" not in res.output


@pytest.mark.parametrize(
    "exc",
    [
        RouteStoreError("boom"),
        RouteTableError("corrupt"),
        ConfigError("bad"),
        pytest.param(
            CaddyEnvArchiveError("caddy env archive corrupt"),
            id="caddy-env-archive",
        ),
    ],
)
def test_main_backstop_turns_domain_errors_into_exit_1(monkeypatch, exc):
    """main() backstop: domain exceptions must never leak to the terminal as a traceback."""
    from devproxy import cli

    def fake_app():
        raise exc

    monkeypatch.setattr(cli, "app", fake_app)
    with pytest.raises(SystemExit) as cm:
        cli.main()
    assert cm.value.code == 1


# ------------------------------------- Migration and rollback (setup)


def test_cli_never_builds_the_real_platform(env, monkeypatch, tmp_path):
    """Regression guard: the CLI path must never construct the real platform.

    The real platform looks at the real units under ~/.config/systemd/user/, and once
    migration runs it rewrites the user's caddy.service (this really overwrote one
    once). conftest's autouse fixture provides the safety net; this turns "the real
    platform must not be constructed" into an explicit assertion so the net cannot be
    removed unnoticed.
    """
    import devproxy.platform.linux as linux_mod

    def boom(*args, **kwargs):
        raise AssertionError("constructing the real platform is not allowed in tests")

    monkeypatch.setattr(linux_mod, "LinuxPlatform", boom)
    monkeypatch.setattr("devproxy.cli._shared._components", lambda *a, **k: [])
    res = CliRunner().invoke(app, ["setup", "--yes"])
    assert res.exit_code == 0, res.output


def _fake_setup_platform(monkeypatch, tmp_path, patch_dns_route=True):
    """Replace everything setup depends on with fakes under tmp_path -- never touch the real system."""
    from devproxy.platform.fake import FakePlatform

    platform = FakePlatform()
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    # Do not assemble any components: migration itself is unrelated to the component
    # flow, and these tests only watch migration and rollback
    monkeypatch.setattr("devproxy.cli._shared._components", lambda *a, **k: [])
    monkeypatch.setattr("devproxy.cli._shared.DNSMASQ_CONF", tmp_path / "dnsmasq.conf")
    if patch_dns_route:
        monkeypatch.setattr(
            "devproxy.cli._shared.default_dns_route", lambda *a, **k: tmp_path / "resolved.conf"
        )
    return platform


def _seed_legacy_unit(platform, tmp_path, caddyfile_text=None):
    """Build a legacy environment: unit + old Caddyfile."""
    old_cfg = tmp_path / "old" / "Caddyfile"
    old_cfg.parent.mkdir(parents=True, exist_ok=True)
    old_cfg.write_text(
        caddyfile_text or "localhost {\n\ttls internal\n}\n\n*.test {\n\trespond 404\n}\n",
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


class _CountingPath(Path):
    """A path that counts read_text calls: proves setup did not pre-read before the migration."""

    if hasattr(type(Path()), "_flavour"):  # Python 3.11's path subclass needs it
        _flavour = type(Path())._flavour

    reads: list[str] = []

    def read_text(self, *args, **kwargs):
        type(self).reads.append(str(self))
        return super().read_text(*args, **kwargs)


def test_setup_does_not_pre_read_legacy_caddyfile(env, monkeypatch, tmp_path):
    """The legacy Caddyfile is read only inside the migration; setup no longer pre-reads it for storage."""
    from devproxy.cli import setup as setup_mod
    from devproxy.lifecycle.migrate import LegacyState
    from devproxy.platform.fake import FakePlatform

    _CountingPath.reads = []
    platform = FakePlatform()
    monkeypatch.setattr("devproxy.cli._shared.DNSMASQ_CONF", tmp_path / "dnsmasq.conf")
    monkeypatch.setattr(
        "devproxy.cli._shared.default_dns_route",
        lambda *a, **k: tmp_path / "resolved.conf",
    )
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    old_cfg = tmp_path / "old" / "Caddyfile"
    old_cfg.parent.mkdir(parents=True)
    legacy_storage = tmp_path / "legacy-data"
    old_cfg.write_text(
        f"{{\n\tstorage file_system {legacy_storage}\n}}\n\nlocalhost {{\n\ttls internal\n}}\n",
        encoding="utf-8",
    )
    unit = platform.service_unit_path("caddy")
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("ExecStart=/bin/caddy run --config /old/Caddyfile\n", encoding="utf-8")
    state = LegacyState(
        unit_path=unit,
        config_path=_CountingPath(str(old_cfg)),
        env={"MINIKUBE_IP": "1.2.3.4"},
    )
    monkeypatch.setattr(setup_mod, "detect_legacy", lambda *a, **k: state)

    real_migrate = setup_mod._migrate_legacy
    reads_before_migration: list[str] = []

    def wrapped(req):
        reads_before_migration.extend(_CountingPath.reads)
        return real_migrate(req)

    monkeypatch.setattr(setup_mod, "_migrate_legacy", wrapped)
    config = setup_mod._maybe_migrate(
        platform, paths, Config(storage=str(tmp_path / "config-data")), "auto", True
    )

    assert reads_before_migration == [], (
        "setup must not read the legacy Caddyfile before the migration"
    )
    assert _CountingPath.reads, (
        "the migration itself must still read it (once for storage, once for the site split)"
    )
    # The effective config must be carried back from MigrationOutcome, so the component flow does not write back the old storage
    assert config.storage_path == legacy_storage


def test_setup_migrates_legacy_caddy(env, monkeypatch, tmp_path):
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    unit, _old = _seed_legacy_unit(platform, tmp_path)

    res = CliRunner().invoke(app, ["setup", "--yes"])
    assert res.exit_code == 0, res.output
    assert "legacy" in res.output
    sites = (env / "config" / "sites.caddy").read_text(encoding="utf-8")
    assert "localhost" in sites
    assert "*.test" not in sites
    assert "MINIKUBE_IP=1.2.3.4" in platform.services["caddy"]
    # The backup directory must really have been created
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
    assert "Rolled back" in res.output
    assert "MINIKUBE_IP=1.2.3.4" in unit.read_text(encoding="utf-8")


def test_setup_rollback_reports_partial_failure(env, monkeypatch, tmp_path):
    """When some entries fail to restore, the list of restored entries must be reported honestly (not lost with the exception)."""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    unit, _old = _seed_legacy_unit(platform, tmp_path)
    assert CliRunner().invoke(app, ["setup", "--yes"]).exit_code == 0
    backup_dir = next((env / "state" / "backups").iterdir())

    # Delete the unit's backup copy -> restoring that entry must fail (independent of uid)
    (backup_dir / "caddy.service.bak").unlink()
    res = CliRunner().invoke(app, ["setup", "--rollback", str(backup_dir)])
    assert res.exit_code == 1, res.output
    assert "restored" in res.output
    assert "Traceback" not in res.output


def test_setup_rollback_with_bad_directory_is_clean_error(env, monkeypatch, tmp_path):
    _fake_setup_platform(monkeypatch, tmp_path)
    res = CliRunner().invoke(app, ["setup", "--rollback", str(tmp_path / "nope")])
    assert res.exit_code == 1, res.output
    assert "Traceback" not in res.output


# ------------------------------------- Rich markup must not swallow the charset in error messages


def test_invalid_name_error_keeps_charset_in_output(env, monkeypatch):
    """[a-z0-9-] would be swallowed by Rich as markup -- error output must keep the literal charset."""
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["expose", "Bad_Name", "--port", "3000"])
    # An illegal route name is a usage error (convention 2), not a runtime failure
    assert res.exit_code == 2
    assert "[a-z0-9-]" in res.output


def test_up_help_text_keeps_charset(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "--help"])
    assert res.exit_code == 0
    assert "[a-z0-9-]" in res.output


def test_setup_backup_targets_follow_dns_backend(env, monkeypatch, tmp_path):
    """With --dns-backend system, the file backed up must be the one under /etc/dnsmasq.d.

    The component writes dnsmasq.d, but the backup would take resolved -- the file that
    was actually changed would not be in the backup, and rollback could not delete it.
    """
    platform = _fake_setup_platform(monkeypatch, tmp_path, patch_dns_route=False)
    _seed_legacy_unit(platform, tmp_path)
    res = CliRunner().invoke(app, ["setup", "--yes", "--dns-backend", "system"])
    assert res.exit_code == 0, res.output
    manifest = json.loads(
        (next((env / "state" / "backups").iterdir()) / "manifest.json").read_text(encoding="utf-8")
    )
    originals = [e["original"] for e in manifest["entries"]]
    assert "/etc/dnsmasq.d/devproxy.conf" in originals
    assert "/etc/systemd/resolved.conf.d/devproxy.conf" not in originals


def test_setup_migration_failure_still_prints_notes(env, monkeypatch, tmp_path):
    """When migration fails, the hints already collected (which blocks were lost, etc.) must still be printed."""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    _seed_legacy_unit(platform, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("failed to write the unit")

    monkeypatch.setattr(platform, "install_service", boom)
    res = CliRunner().invoke(app, ["setup", "--yes"])
    assert res.exit_code == 1, res.output
    assert "discarded" in res.output
    assert "devproxy: " in res.output
    assert "Traceback" not in res.output


def test_setup_persists_legacy_storage_for_later_setups(env, monkeypatch, tmp_path):
    """After adopting the legacy storage it must be archived -- otherwise the next setup moves the CA.

    Migration changes the storage location, and Caddy regenerates its CA: every browser
    trust entry and every tls internal certificate already used by personal sites
    becomes void. Once archived, every setup must use the same location.
    """
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    legacy_storage = tmp_path / "legacy-caddy-data"
    _seed_legacy_unit(
        platform,
        tmp_path,
        caddyfile_text=(
            f"{{\n\tstorage file_system {legacy_storage}\n}}\n\nlocalhost {{\n\ttls internal\n}}\n"
        ),
    )
    res = CliRunner().invoke(app, ["setup", "--yes"])
    assert res.exit_code == 0, res.output
    saved = env / "state" / "caddy-storage.json"
    assert saved.exists(), "the adopted storage must be archived, otherwise the next setup moves it"
    assert str(legacy_storage) in saved.read_text(encoding="utf-8")
    # Rich wraps to the terminal width (long paths may even be split) -- strip all whitespace before comparing
    squashed = "".join(res.output.split())
    assert "".join(str(legacy_storage).split()) in squashed, (
        "the storage difference must be told to the user"
    )


# ------------------------------------- Archived storage overriding config.toml must be reported


def _write_storage_pair(env, config_storage: str, stored_storage: str):
    from devproxy.core.paths import default_paths

    paths = default_paths()
    paths.config_toml.parent.mkdir(parents=True, exist_ok=True)
    paths.config_toml.write_text(f'storage = "{config_storage}"\n', encoding="utf-8")
    paths.caddy_storage.parent.mkdir(parents=True, exist_ok=True)
    paths.caddy_storage.write_text(json.dumps({"storage": stored_storage}), encoding="utf-8")
    return paths


def test_build_context_says_so_when_stored_storage_overrides_config(env, capsys):
    """Archived storage overrides config.toml -- must be reported, with a way out.

    A user edits config.toml and is silently ignored: that is the hardest class of
    problem to diagnose.
    """
    from devproxy.cli._shared import build_context

    paths = _write_storage_pair(env, "/tmp/from-config-toml", "/tmp/from-legacy")
    _paths, config, _store, _admin = build_context()
    assert config.storage == "/tmp/from-legacy", (
        "the archive must keep taking effect (the CA must not move)"
    )
    err = capsys.readouterr().err
    assert "/tmp/from-legacy" in err, "must state the path actually in effect"
    assert "/tmp/from-config-toml" in err, "must state the ignored config value"
    # Rich wraps to the terminal width (long paths may even be split) -- strip all whitespace before comparing
    squashed = "".join(err.split())
    assert "".join(str(paths.caddy_storage).split()) in squashed, (
        "must give the way out (the archive file)"
    )


def test_build_context_quiet_when_storage_agrees(env, capsys):
    """When both agree, say nothing extra."""
    from devproxy.cli._shared import build_context

    _write_storage_pair(env, "/tmp/same", "/tmp/same")
    _paths, config, _store, _admin = build_context()
    assert config.storage == "/tmp/same"
    assert "storage" not in capsys.readouterr().err


def test_cli_setup_reports_the_storage_override(env, monkeypatch, tmp_path):
    """It must also be reported on the CLI path -- users see terminal output, not source code."""
    from devproxy.cli._shared import build_context

    _write_storage_pair(env, "/tmp/from-config-toml", "/tmp/from-legacy")
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 0, res.output
    assert "/tmp/from-legacy" in res.output
    assert "/tmp/from-config-toml" in res.output


# ------------------------------------- Example command in the setup completion hint


def test_setup_example_command_actually_uses_port():
    """The example command at the end of setup must really use $PORT.

    `python3 -m http.server` does not read PORT (it always binds 8000): a user copying
    the example waits the full 60 seconds and then sees "ready timeout". The example
    must be a form that actually works.
    """
    from devproxy.cli._shared import UP_EXAMPLE

    assert "sh -c" in UP_EXAMPLE, "the example must use sh -c to defer $PORT expansion"
    assert "$PORT" in UP_EXAMPLE, "the example must actually pass PORT to the app"


def test_setup_prints_the_working_example(env, monkeypatch):
    """The example printed on the terminal must be that same working form."""
    from devproxy.cli._shared import UP_EXAMPLE
    from devproxy.lifecycle.setup import ComponentReport
    from devproxy.lifecycle.setup import SetupPlan
    from devproxy.platform.base import Action
    from devproxy.platform.base import Status

    report = ComponentReport(
        name="caddy",
        status=Status(ok=True, detail="ready"),
        actions=[Action(description="noop", kind="run", command=["noop"])],
    )
    monkeypatch.setattr("devproxy.cli._shared._components", lambda *a, **k: [])
    monkeypatch.setattr(
        "devproxy.cli._shared.build_plan", lambda comps: SetupPlan(reports=[report])
    )
    monkeypatch.setattr(
        "devproxy.cli._shared.apply_plan",
        lambda plan: [("caddy", Status(ok=True, detail="ready"))],
    )
    res = CliRunner().invoke(app, ["setup", "--yes"])
    assert res.exit_code == 0, res.output
    assert UP_EXAMPLE in res.output
    assert "$PORT" in res.output


# ------------------------------------- I7: usage errors always exit 2


def test_up_invalid_name_exits_2(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "Bad_Name", "--", "sleep", "1"])
    assert res.exit_code == 2, res.output
    assert "[a-z0-9-]" in res.output


def test_up_invalid_port_exits_2(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "codeup", "--port", "0", "--", "sleep", "1"])
    assert res.exit_code == 2, res.output
    assert "must be between 1 and 65535" in res.output


def test_up_invalid_ready_timeout_exits_2(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "codeup", "--ready-timeout", "0", "--", "sleep", "1"])
    assert res.exit_code == 2, res.output
    assert "invalid ready timeout" in res.output


def test_expose_invalid_port_exits_2(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "65536"])
    assert res.exit_code == 2, res.output


def test_down_invalid_name_exits_2(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["down", "Bad_Name"])
    assert res.exit_code == 2, res.output


# ------------------------------------- I1: read-only commands produce no write side effects


@pytest.mark.parametrize("command", ["ls", "config", "doctor"])
def test_readonly_commands_do_not_create_config_dir(env, monkeypatch, command):
    """Query commands must not mkdir/touch -- and must not explode when the config dir's parent is a regular file."""
    blocker = env / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    config_dir = blocker / "devproxy"
    monkeypatch.setenv("DEVPROXY_CONFIG_DIR", str(config_dir))
    if command == "doctor":
        # doctor does real component probing (reads the host's /etc, etc.) -- here we only care about build_context
        monkeypatch.setattr("devproxy.cli._shared._components", lambda *a, **k: [])
    res = CliRunner().invoke(app, [command])
    assert "Traceback" not in res.output, res.output
    assert res.exit_code in (0, 3), res.output
    assert not config_dir.exists(), "read-only commands must not create the config directory"


# ------------------------------------- I2: a corrupt storage archive must not fall back silently


def test_corrupt_storage_archive_is_precondition_error(env):
    """The archive exists but cannot be read -> exit 3, tell the user to delete it, never switch storage silently."""
    from devproxy.core.paths import default_paths

    paths = default_paths()
    paths.caddy_storage.parent.mkdir(parents=True, exist_ok=True)
    paths.caddy_storage.write_text("{ truncated", encoding="utf-8")
    with pytest.warns(UserWarning, match="storage archive is unreadable"):
        res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 3, res.output
    assert "migration archive is corrupt" in res.output
    assert "Traceback" not in res.output


def test_missing_storage_archive_is_not_an_error(env):
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 0, res.output


def test_resolve_storage_reports_warnings_without_side_effects(env, capsys):
    """M2: the query function only returns warnings; it does not write to stderr directly."""
    from devproxy.cli._shared import _resolve_storage

    paths = _write_storage_pair(env, "/tmp/from-config-toml", "/tmp/from-legacy")
    config, warnings = _resolve_storage(Config(), paths)
    assert config.storage == "/tmp/from-legacy"
    assert warnings and "/tmp/from-legacy" in warnings[0]
    assert capsys.readouterr().err == "", (
        "the resolution function should have no stderr side effects"
    )


# ------------------------------------- C1: a failed restart after rollback must not be silent


def test_setup_rollback_restart_failure_is_reported(env, monkeypatch, tmp_path):
    """Files are restored but the caddy restart fails: must be reported, and must not be described as a failed rollback."""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    unit, _old = _seed_legacy_unit(platform, tmp_path)
    assert CliRunner().invoke(app, ["setup", "--yes"]).exit_code == 0
    backup_dir = next((env / "state" / "backups").iterdir())

    def boom(name):
        raise RuntimeError("systemctl restart caddy failed: unit is broken")

    monkeypatch.setattr(platform, "restart_service", boom)
    res = CliRunner().invoke(app, ["setup", "--rollback", str(backup_dir)])
    assert res.exit_code == 1, res.output
    assert "Rolled back" in res.output, (
        "the files really were restored; that must be stated honestly"
    )
    assert "Caddy may still be running the post-migration config" in res.output
    assert "Traceback" not in res.output


def test_setup_rollback_restart_permission_error_exits_4(env, monkeypatch, tmp_path):
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    _seed_legacy_unit(platform, tmp_path)
    assert CliRunner().invoke(app, ["setup", "--yes"]).exit_code == 0
    backup_dir = next((env / "state" / "backups").iterdir())

    def boom(name):
        raise PermissionError("elevation failed: sudo is unavailable")

    monkeypatch.setattr(platform, "restart_service", boom)
    res = CliRunner().invoke(app, ["setup", "--rollback", str(backup_dir)])
    assert res.exit_code == 4, res.output
    assert "restored" in res.output
    assert "Traceback" not in res.output


def test_setup_rollback_skips_restart_when_service_was_absent(env, monkeypatch, tmp_path):
    """No service unit before the migration -> rollback needs no restart, and must not error."""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    _seed_legacy_unit(platform, tmp_path)
    assert CliRunner().invoke(app, ["setup", "--yes"]).exit_code == 0
    backup_dir = next((env / "state" / "backups").iterdir())

    monkeypatch.setattr(
        platform, "service_unit_path", lambda name: tmp_path / "no-such-unit.service"
    )
    monkeypatch.setattr(
        platform, "restart_service", lambda name: pytest.fail("must not attempt a restart")
    )
    res = CliRunner().invoke(app, ["setup", "--rollback", str(backup_dir)])
    assert res.exit_code == 0, res.output
    assert "skipping the restart" in res.output


# ------------------------------------- I5 / I6: rollback argument boundaries and the recovery path


def test_setup_rejects_empty_rollback_dir(env, monkeypatch, tmp_path):
    """--rollback "" is a usage error and must never fall into the full deploy branch."""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    res = CliRunner().invoke(app, ["setup", "--rollback", ""])
    assert res.exit_code == 2, res.output
    assert "empty string" in res.output
    # No migration and no deploy started
    assert platform.services == {}


def test_setup_rejects_empty_domain_suffix(env, monkeypatch, tmp_path):
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    res = CliRunner().invoke(app, ["setup", "--domain-suffix", ""])
    assert res.exit_code == 2, res.output
    assert platform.services == {}


def test_setup_rollback_works_with_corrupt_config_toml(env, monkeypatch, tmp_path):
    """A corrupt config is exactly when a rollback is most needed -- the recovery path must not load the config first."""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    unit, _old = _seed_legacy_unit(platform, tmp_path)
    assert CliRunner().invoke(app, ["setup", "--yes"]).exit_code == 0
    backup_dir = next((env / "state" / "backups").iterdir())

    (env / "config" / "config.toml").write_text("storage = [", encoding="utf-8")
    unit.write_text("CLOBBERED\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["setup", "--rollback", str(backup_dir)])
    assert res.exit_code == 0, res.output
    assert "MINIKUBE_IP=1.2.3.4" in unit.read_text(encoding="utf-8")


# ------------------------------------- M7: interaction decisions all go through the _shared seam


def test_setup_migration_uses_interactive_seam(env, monkeypatch, tmp_path):
    """Stubbing `_shared._interactive` must be able to drive migration's interactive confirmation branch."""
    platform = _fake_setup_platform(monkeypatch, tmp_path)
    _seed_legacy_unit(platform, tmp_path)
    monkeypatch.setattr("devproxy.cli._shared._interactive", lambda: True)
    monkeypatch.setattr("devproxy.cli._shared.typer.confirm", lambda *a, **k: True)
    res = CliRunner().invoke(app, ["setup"])
    assert res.exit_code == 0, res.output
    assert "MINIKUBE_IP=1.2.3.4" in platform.services["caddy"]


# ------------------------------------- Platform construction failures (e.g. sudo/root)


def test_up_does_not_mask_programming_errors(env, monkeypatch):
    """M1: UpRunner should only turn known runtime errors into exit codes; bugs must not be disguised as runtime failures."""
    _patch_ctx(monkeypatch, env)

    def boom(self, *a, **k):
        raise AttributeError("bug inside runner")

    monkeypatch.setattr("devproxy.cli._shared.UpRunner.run", boom)
    res = CliRunner().invoke(app, ["up", "codeup", "--", "sleep", "1"])
    assert isinstance(res.exception, AttributeError), res.output


def test_trust_lets_programming_errors_escape(env, monkeypatch):
    """M6: trust should only catch PermissionError / RuntimeError from its contract."""
    from devproxy.platform.base import Action
    from devproxy.platform.fake import FakePlatform

    _patch_ctx(monkeypatch, env)
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: FakePlatform())

    class BoomCert:
        def __init__(self, *a, **k):
            pass

        def plan(self):
            return [Action(description="install CA")]

        def apply(self, actions):
            raise AttributeError("bug inside component")

    monkeypatch.setattr("devproxy.cli._shared.CertComponent", BoomCert)
    with pytest.raises(AttributeError):
        CliRunner().invoke(app, ["trust"], catch_exceptions=False)


def test_platform_construction_failure_is_clean_precondition(env, monkeypatch):
    """sudo devproxy ... is rejected while constructing the platform -> one readable line + exit 3."""
    from devproxy.cli import _context

    _patch_ctx(monkeypatch, env)
    monkeypatch.setattr("devproxy.cli._shared._platform", _context._platform)

    def refuse():
        raise RuntimeError(
            "run devproxy as a normal user: running as root would install into the wrong home directory"
        )

    monkeypatch.setattr("devproxy.platform.get_platform", refuse)
    res = CliRunner().invoke(app, ["trust"])
    assert res.exit_code == 3, res.output
    assert "normal user" in res.output
    assert "Traceback" not in res.output


# ------------------------------------- Exit-code semantics of the top-level domain allowlist


def test_setup_rejects_public_tld_from_cli_with_exit_2(env, monkeypatch):
    """--domain-suffix given a public TLD -> usage error (2).

    Unlike the same value written in config.toml: that is a configuration error (1).
    """
    platform = _fake_setup_platform(monkeypatch, env)
    res = CliRunner().invoke(app, ["setup", "--domain-suffix", "com"])
    assert res.exit_code == 2, res.output
    assert "com" in res.output
    assert "test" in res.output, "the error must make clear what to use instead"
    assert platform.services == {}, "an invalid argument must have no side effects"


def test_setup_accepts_tld_explicitly_allowed_in_config(env, monkeypatch):
    """Once explicitly allowed in config.toml, the same suffix on the command line must be accepted."""
    config_dir = env / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text('allowed_domain_suffixes = ["dev"]\n', encoding="utf-8")
    platform = _fake_setup_platform(monkeypatch, env)
    res = CliRunner().invoke(app, ["setup", "--domain-suffix", "dev"])
    assert res.exit_code == 0, res.output
    assert platform.services == {}  # the fake platform installed nothing, but the flow completed


def test_config_toml_with_public_tld_exits_1(env, monkeypatch):
    """Written in config.toml it is a **configuration error** (1) -- goes through main()'s backstop."""
    import sys

    from devproxy import cli

    config_dir = env / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text('domain_suffix = "com"\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["devproxy", "setup"])
    with pytest.raises(SystemExit) as cm:
        cli.main()
    assert cm.value.code == 1


# ------------------------------------- Firefox certificate trust reminder


class _FakeTrustCert:
    """Fake certificate component: both the CA install action and the Firefox note are scriptable."""

    name = "cert"

    def __init__(self, *a, **k):
        self.ca_path = Path("/tmp/fake-root.crt")

    def plan(self):
        from devproxy.platform.base import Action

        return [Action(description="install CA", command=["certutil", "-A", "-n", "x"])]

    def apply(self, actions):
        return None

    def firefox_note(self):
        return "Firefox has its own certificate store (cert9.db); the CA must be imported into it separately"


def test_trust_reminds_about_firefox(env, monkeypatch):
    """Firefox reads neither the NSS store nor the system trust store -- after installing, the user must be reminded to trust it separately."""
    monkeypatch.setattr("devproxy.cli._shared.CertComponent", _FakeTrustCert)
    res = CliRunner().invoke(app, ["trust", "--yes"])
    assert res.exit_code == 0, res.output
    assert "Firefox" in res.output
    assert "cert9.db" in res.output


def test_trust_stays_silent_without_firefox(env, monkeypatch):
    """Do not mention Firefox when it is not installed on this machine -- do not create noise."""

    class NoFirefoxCert(_FakeTrustCert):
        def firefox_note(self):
            return None

    monkeypatch.setattr("devproxy.cli._shared.CertComponent", NoFirefoxCert)
    res = CliRunner().invoke(app, ["trust", "--yes"])
    assert res.exit_code == 0, res.output
    assert "Firefox" not in res.output


def test_trust_tolerates_component_without_firefox_note(env, monkeypatch):
    """When the component lacks the firefox_note capability, trust still completes normally."""

    class PlainCert(_FakeTrustCert):
        firefox_note = None

    monkeypatch.setattr("devproxy.cli._shared.CertComponent", PlainCert)
    res = CliRunner().invoke(app, ["trust", "--yes"])
    assert res.exit_code == 0, res.output


def test_up_injects_sibling_route_ports(env, monkeypatch):
    """Foreground `devproxy up` also hands the app the ports of the routes that
    are already registered -- that is how a frontend finds its API when the
    two are started separately."""
    _patch_ctx(monkeypatch, env)
    _seed_route(env / "state", name="api", port=41001)
    captured = {}

    def fake_run(self, name, cmd, port=None, ready_timeout=None, on_ready=None, extra_env=None):
        captured["extra_env"] = extra_env
        return 0

    monkeypatch.setattr("devproxy.cli._shared.UpRunner.run", fake_run)
    res = CliRunner().invoke(app, ["up", "web", "--", "npm", "run", "dev"])
    assert res.exit_code == 0, res.output
    assert captured["extra_env"]["DEVPROXY_API_PORT"] == "41001"
    assert "DEVPROXY_WEB_PORT" not in captured["extra_env"], (
        "the route being started does not get its own name in the table"
    )
