"""Write the tool's own config and units to disk: main Caddyfile, sites.caddy, systemd/launchd units."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from devproxy.core.caddy_env import save_extra_env
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.lifecycle.migrate.caddyfile import _has_content
from devproxy.lifecycle.migrate.caddyfile import block_addresses
from devproxy.lifecycle.migrate.caddyfile import split_caddyfile_blocks
from devproxy.lifecycle.migrate.legacy import LegacyState
from devproxy.lifecycle.migrate.legacy import MigrationError
from devproxy.lifecycle.migrate.legacy import unit_exec_start
from devproxy.platform.base import Platform


def _write_sites_caddy(paths: Paths, state: LegacyState, config: Config) -> list[str]:
    """Write personal sites into sites.caddy and return notes for the user.

    The tool only imports sites.caddy and never overwrites existing content --
    sites the user wrote must not be wiped by migration (duplicate site blocks
    would also prevent Caddy from starting at all).

    But "not overwriting" is not the same as "no impact": migration repoints the
    unit at the tool's Caddyfile, which only imports sites.caddy. The old
    Caddyfile is no longer read from then on -- personal sites left there are
    **no longer served**. This must be stated explicitly (naming the file and
    addresses), otherwise the user just sees "my site suddenly 404s".
    """
    config_path = state.config_path
    if config_path is None:
        raise MigrationError("legacy Caddyfile path is unknown; migration aborted")
    notes: list[str] = []
    if not config_path.exists():
        return [f"legacy Caddyfile does not exist; skipping site split: {config_path}"]
    legacy = config_path.read_text(encoding="utf-8")
    personal_blocks, dropped = split_caddyfile_blocks(legacy, suffix=config.domain_suffix)
    if dropped:
        addresses = block_addresses(dropped)
        notes.append(
            f"discarded {len(dropped)} .{config.domain_suffix} site block(s) "
            f"(taken over by the tool): {', '.join(addresses)}; "
            f"the old Caddyfile was left untouched and fully backed up, "
            f"so you can recover them from the backup"
        )
    if not personal_blocks:
        return notes
    existing = ""
    if paths.sites_caddy.exists():
        existing = paths.sites_caddy.read_text(encoding="utf-8")
    if _has_content(existing):
        addresses = block_addresses(personal_blocks)
        notes.append(
            f"⚠ these personal sites will no longer be served after migration: "
            f"{config_path} is no longer imported by the main Caddyfile; "
            f"the tool only imports {paths.sites_caddy}. "
            f"Affected addresses: {', '.join(addresses) or '(could not be parsed from the file)'}. "
            f"Please merge these site blocks manually from {config_path} into "
            f"{paths.sites_caddy} (the originals are also in the backup directory)"
        )
        return notes
    paths.sites_caddy.parent.mkdir(parents=True, exist_ok=True)
    paths.sites_caddy.write_text("\n".join(personal_blocks), encoding="utf-8")
    return notes


def _main_caddyfile_content(paths: Paths, config: Config) -> str:
    """The tool's own main Caddyfile (global block + two imports)."""
    from devproxy.gateway.caddy import MainCaddyfileSpec
    from devproxy.gateway.caddy import build_main_caddyfile

    return build_main_caddyfile(
        MainCaddyfileSpec(
            admin_addr=config.admin_addr,
            storage=config.storage_path,
            routes_caddy=paths.routes_caddy,
            sites_caddy=paths.sites_caddy,
            http_port=config.http_port,
            https_port=config.https_port,
        )
    )


def _write_main_caddyfile(paths: Paths, config: Config) -> None:
    paths.caddyfile.parent.mkdir(parents=True, exist_ok=True)
    paths.caddyfile.write_text(_main_caddyfile_content(paths, config), encoding="utf-8")


def _legacy_caddy_binary(state: LegacyState | None, home: Path | None = None) -> Path | None:
    """The caddy binary pointed to by the legacy unit's ExecStart.

    caddy_bin in the config may not be downloaded yet (first setup), but the one
    in the legacy unit is running on this machine -- validating config with it is
    what makes sense.
    """
    if state is None:
        return None
    try:
        text = state.unit_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    args = unit_exec_start(text)
    if not args:
        return None
    program = args[0].strip('"')
    if not program:
        return None
    base = home or Path.home()
    return Path(program.replace("%h", str(base))).expanduser()


def _validate_caddyfile(
    platform: Platform,
    paths: Paths,
    config: Config,
    state: LegacyState | None = None,
) -> list[str]:
    """Let caddy validate the config before swapping the unit, returning notes.

    Personal site blocks are copied verbatim from the user's own Caddyfile --
    syntax conflicts or duplicate sites may prevent Caddy from starting. If
    validation fails, the unit must not be swapped: otherwise the service would
    not start and the user would not even see which line is wrong.

    When the caddy binary in the config is not downloaded yet, fall back to the
    one in the legacy unit -- otherwise "must validate before swapping the unit"
    would become empty words because of a nonexistent path.
    """
    notes: list[str] = []
    binary = config.caddy_bin_path
    if not binary.exists():
        fallback = _legacy_caddy_binary(state)
        if fallback is not None and fallback.exists():
            notes.append(
                f"caddy binary from config does not exist; validating with the one "
                f"from the legacy unit: {fallback}"
            )
            binary = fallback
        else:
            notes.append(
                "caddy binary does not exist; skipping config validation "
                "(it will still be validated after setup downloads it)"
            )
            return notes
    fd, tmp_name = tempfile.mkstemp(prefix="devproxy-validate-", suffix=".Caddyfile")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_main_caddyfile_content(paths, config))
        code, msg = platform.run([str(binary), "validate", "--config", str(tmp)])
    finally:
        tmp.unlink(missing_ok=True)
    if code != 0:
        raise MigrationError(
            f"caddy config validation failed; migration aborted (unit unchanged): "
            f"{msg or 'validation failed'}"
        )
    return notes


def _install_caddy_unit(
    platform: Platform,
    paths: Paths,
    config: Config,
    state: LegacyState,
) -> list[str]:
    """Rewrite the unit: point it at the tool's Caddyfile, keep the existing Environment=, return notes.

    The binary path must land on a file that **actually exists**: migration
    happens before the component flow, so the caddy binary may not be downloaded
    yet (first setup, no network). Copying the path from config then would point
    the unit at a nonexistent binary -- the running Caddy would be broken by
    this change, and nothing could bring it back up.

    So fall back to the ExecStart binary from the legacy unit: that file is
    running on this machine and is definitely usable. setup's subsequent
    component flow will switch the unit to the official path.
    """
    notes: list[str] = []
    binary = config.caddy_bin_path
    if not binary.exists():
        fallback = _legacy_caddy_binary(state)
        if fallback is not None and fallback.exists():
            notes.append(
                f"caddy binary from config does not exist; the unit will keep using "
                f"the legacy {fallback} (setup will switch to {binary} automatically "
                f"once the download finishes)"
            )
            binary = fallback
        else:
            notes.append(
                f"caddy binary does not exist ({binary}); the unit will point at it -- "
                f"it becomes usable only after setup downloads it"
            )
    platform.install_service(
        "caddy",
        exec_start=[str(binary), "run", "--config", str(paths.caddyfile)],
        exec_reload=[
            str(binary),
            "reload",
            "--config",
            str(paths.caddyfile),
            "--address",
            config.admin_addr,
        ],
        exec_stop=[str(binary), "stop"],
        env=state.env,
        description="Caddy local HTTP/HTTPS server (managed by devproxy)",
    )
    save_extra_env(paths, state.env)
    return notes


def _restart_caddy(platform: Platform) -> list[str]:
    """Restart caddy so the new config actually takes effect, returning notes.

    If the unit changed but the process was not restarted, the old config is
    still running -- this "looks migrated" state is harder to diagnose than a
    failed migration. A start failure does not block migration: setup's
    subsequent component flow will bring the service up.
    """
    try:
        platform.enable_service("caddy")
        platform.restart_service("caddy")
    except Exception as exc:  # noqa: BLE001 -- a failing service must not roll back the file migration
        return [f"caddy service could not be restarted ({exc}); devproxy setup will continue"]
    return []
