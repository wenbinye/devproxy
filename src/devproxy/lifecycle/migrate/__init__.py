"""Legacy environment detection and migration.

The iron rule of migration: back up first, then act. Any failed step can be
restored from the backup.

What gets migrated is the user's **hand-maintained** environment: their own
caddy.service, their own Caddyfile, their own dnsmasq config. Before taking
them over, the tool must back up the originals in full -- if the backup fails,
nothing is touched.

The implementation is spread across several modules in this package
(caddyfile / legacy / install / rollback); this module only orchestrates and
re-exports the public names so code outside the package can use them via
`devproxy.lifecycle.migrate.<name>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from devproxy.core.backup import Backup
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.lifecycle.migrate.caddyfile import block_addresses
from devproxy.lifecycle.migrate.caddyfile import legacy_storage_path
from devproxy.lifecycle.migrate.caddyfile import split_caddyfile
from devproxy.lifecycle.migrate.caddyfile import split_caddyfile_blocks
from devproxy.lifecycle.migrate.caddyfile import wildcard_addresses
from devproxy.lifecycle.migrate.install import _install_caddy_unit
from devproxy.lifecycle.migrate.install import _restart_caddy
from devproxy.lifecycle.migrate.install import _validate_caddyfile
from devproxy.lifecycle.migrate.install import _write_main_caddyfile
from devproxy.lifecycle.migrate.install import _write_sites_caddy
from devproxy.lifecycle.migrate.legacy import LegacyState
from devproxy.lifecycle.migrate.legacy import MigrationError
from devproxy.lifecycle.migrate.legacy import detect_legacy
from devproxy.lifecycle.migrate.legacy import load_legacy_storage
from devproxy.lifecycle.migrate.legacy import resolve_storage_details
from devproxy.lifecycle.migrate.legacy import save_legacy_storage
from devproxy.lifecycle.migrate.legacy import unit_config_path
from devproxy.lifecycle.migrate.legacy import unit_env
from devproxy.lifecycle.migrate.legacy import unit_environment_files
from devproxy.lifecycle.migrate.legacy import unit_exec_start
from devproxy.lifecycle.migrate.rollback import _create_backup
from devproxy.lifecycle.migrate.rollback import _restore_after_failure
from devproxy.lifecycle.migrate.rollback import rollback
from devproxy.platform.base import Platform

__all__ = [
    "LegacyState",
    "MigrationError",
    "MigrationOutcome",
    "MigrationRequest",
    "block_addresses",
    "default_dns_route",
    "detect_legacy",
    "legacy_storage_path",
    "load_legacy_storage",
    "resolve_storage_details",
    "rollback",
    "run_migration",
    "save_legacy_storage",
    "split_caddyfile",
    "split_caddyfile_blocks",
    "unit_config_path",
    "unit_env",
    "unit_environment_files",
    "unit_exec_start",
    "wildcard_addresses",
]


@dataclass(frozen=True)
class MigrationRequest:
    """Complete input for one legacy environment migration.

    Parameters are gathered into a single object so call sites can no longer swap
    the two same-typed optional paths `dnsmasq_conf` / `dns_route`; `config`
    already folds in the effective DNS backend.
    """

    platform: Platform
    paths: Paths
    config: Config
    state: LegacyState
    dnsmasq_conf: Path | None = None
    dns_route: Path | None = None
    backend: str | None = None


@dataclass
class MigrationOutcome:
    """Result of a migration: backup object + effective config + notes for the user.

    `notes` is now part of the return value, no longer a mutable output parameter
    that the caller passes in and that gets modified in place (CQS).

    `config` is the **effective config** after storage continuity handling: the
    caller (setup) must continue the component flow with it, otherwise it would
    write the old storage back into the main Caddyfile and make Caddy regenerate
    the CA. Returning it means the caller no longer has to read the legacy
    Caddyfile before migrating (that would be a duplicate read).
    """

    backup: Backup
    config: Config
    notes: list[str] = field(default_factory=list)


def default_dns_route(
    platform: Platform, paths: Paths, config: Config, backend: str | None = None
) -> Path:
    """DNS route file to back up for this migration (the drop-in under /etc).

    It is the same file DnsComponent writes -- the setup after migration will
    rewrite it, so it must be backed up before migration.

    backend must match the backend the component flow actually uses: when the
    user explicitly passes `--dns-backend system`, the component writes
    /etc/dnsmasq.d/devproxy.conf, but the backup would be of the resolved file --
    the modified file would not be in the backup and rollback could not delete it.
    """
    from devproxy.components.dns import DnsComponent

    comp = DnsComponent(platform, paths, config, backend=backend or config.dns_backend)
    return comp.target_path


def _default_dnsmasq_conf() -> Path:
    from devproxy.components.dnsmasq import DNSMASQ_CONF

    return DNSMASQ_CONF.expanduser()


def run_migration(request: MigrationRequest) -> MigrationOutcome:
    """Run the migration, returning the backup and notes. The caller can roll back with them.

    The order is an iron rule: back up first, then act. Backup fails -> not a
    single file is changed; rewrite fails or is interrupted by Ctrl-C -> restore
    from the backup, then re-raise.
    """
    platform = request.platform
    paths = request.paths
    state = request.state
    config = request.config
    if state.error:
        raise MigrationError(
            f"{state.error}; to avoid overwriting a hand-maintained unit without a backup, "
            f"migration was aborted (no files changed)"
        )
    if state.config_path is None:
        raise MigrationError(
            "legacy Caddyfile path is unknown; migration aborted (no files changed)"
        )
    notes: list[str] = []
    conf = request.dnsmasq_conf if request.dnsmasq_conf is not None else _default_dnsmasq_conf()
    route = (
        request.dns_route
        if request.dns_route is not None
        else default_dns_route(platform, paths, config, request.backend)
    )
    # Storage continuity: keep the legacy Caddyfile's storage -- the CA must not move.
    # A single parse yields both the path and the note: no repeated file reads and
    # no always-true comparison.
    config, legacy_storage, storage_note = resolve_storage_details(config, state)
    if storage_note:
        notes.append(storage_note)
    targets = [
        state.unit_path,  # gets rewritten
        state.config_path,  # old Caddyfile -- keep a copy, rollback needs it
        paths.caddyfile,  # the tool's main Caddyfile
        paths.sites_caddy,  # personal sites
        conf,  # the user's dnsmasq config (later setup merges and rewrites it)
        platform.service_unit_path("dnsmasq"),  # later setup reinstalls this unit
        route,  # DNS route drop-in under /etc
        paths.caddy_env,  # archive of unit environment variables left by migration
        paths.caddy_storage,  # archive of the storage left by migration
    ]
    backup = _create_backup(platform, paths, targets)
    try:
        # The main Caddyfile imports these two files -- they must exist before
        # validation, otherwise caddy only reports "file not found" and migration
        # gets blocked by its own validation.
        paths.ensure_files()
        notes.extend(_write_sites_caddy(paths, state, config))
        notes.extend(_validate_caddyfile(platform, paths, config, state))
        _write_main_caddyfile(paths, config)
        notes.extend(_install_caddy_unit(platform, paths, config, state))
        # When the effective storage is the legacy storage it must be archived:
        # later setup moves it, and Caddy would regenerate the CA.
        if legacy_storage is not None:
            save_legacy_storage(paths, legacy_storage)
        # Restart is on the same failure path too: ordinary failures are
        # downgraded to a note by _restart_caddy itself; Ctrl-C still restores
        # everything as an "interruption" and never leaves the user with just a
        # traceback.
        notes.extend(_restart_caddy(platform))
    except BaseException as exc:  # noqa: BLE001 -- includes KeyboardInterrupt, must restore
        _restore_after_failure(platform, backup, exc, notes=notes)
    return MigrationOutcome(backup=backup, config=config, notes=notes)
