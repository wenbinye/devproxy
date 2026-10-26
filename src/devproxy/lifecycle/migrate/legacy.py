"""Legacy environment detection: unit parsing, Caddyfile location, storage archive."""

from __future__ import annotations

import json
import shlex
import warnings
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from pathlib import Path

from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.core.paths import atomic_write_text
from devproxy.lifecycle.migrate.caddyfile import legacy_storage_path
from devproxy.platform.base import Platform


class MigrationError(RuntimeError):
    """Migration failure.

    `notes` holds user-facing notes already collected before the failure (which
    blocks were discarded, which binary was used, etc.) -- the failure path must
    print them too, otherwise the user only sees "failed" and does not know what
    sites.caddy was changed into.
    """

    def __init__(self, message: str, notes: list[str] | None = None):
        super().__init__(message)
        self.notes = list(notes or [])


@dataclass
class LegacyState:
    """Detected legacy environment.

    A non-empty `error` means "the unit exists, but its config/content cannot be
    confirmed". This state must stop migration: folding "parse failed" into "no
    legacy environment" amounts to treating it as a clean machine and overwriting
    the user's hand-maintained unit without a backup -- the worst mistake
    migration can make.
    """

    unit_path: Path
    config_path: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    error: str | None = None


def _exec_start_args(unit_text: str) -> list[str] | None:
    """Command-line arguments of ExecStart= (systemd's "-" prefix and quotes must be handled correctly)."""
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line.startswith("ExecStart="):
            continue
        value = line[len("ExecStart=") :]
        if value.startswith("-"):
            # systemd: ExecStart=-cmd means a start failure is not logged as an error
            value = value[1:].lstrip()
        try:
            args = shlex.split(value)
        except ValueError:
            # Unbalanced quotes: fall back to naive splitting; at least do not drop the whole line
            args = value.split()
        if args:
            return args
    return None


def _config_arg(args: list[str]) -> str | None:
    """Extract the value of --config from the argument list (supports both space and = forms)."""
    for i, arg in enumerate(args):
        if arg == "--config":
            value = args[i + 1] if i + 1 < len(args) else ""
        elif arg.startswith("--config="):
            value = arg[len("--config=") :]
        else:
            continue
        if value:
            return value
    return None


def unit_config_path(unit_text: str, home: Path) -> Path | None:
    """Parse the path --config points to from ExecStart= (expanding %h).

    Caddy/pflag accepts both `--config <path>` and `--config=<path>`; systemd
    also allows quoting the binary and path (paths may contain spaces). Use
    shlex to parse real lexical rules, not a bare split().
    """
    args = _exec_start_args(unit_text)
    if args is None:
        return None
    value = _config_arg(args)
    if value is None:
        return None
    return Path(value.replace("%h", str(home))).expanduser()


def unit_env(unit_text: str) -> dict[str, str]:
    """Extract Environment= key-value pairs from the unit.

    systemd's Environment= is tokenized shell-style (quotes and escapes
    supported), and multiple K=V pairs can appear on one line. A bare split()
    would break values containing spaces, and systemd would refuse to load the
    rewritten unit.
    """
    env: dict[str, str] = {}
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        try:
            items = shlex.split(line[len("Environment=") :])
        except ValueError:
            items = line[len("Environment=") :].split()
        for item in items:
            if "=" in item:
                k, v = item.split("=", 1)
                env[k] = v
    return env


def unit_environment_files(unit_text: str) -> list[str]:
    """List of EnvironmentFile= paths referenced by the unit (with systemd's "-" prefix stripped)."""
    files: list[str] = []
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line.startswith("EnvironmentFile="):
            continue
        try:
            items = shlex.split(line[len("EnvironmentFile=") :])
        except ValueError:
            items = line[len("EnvironmentFile=") :].split()
        for item in items:
            path = item.lstrip("-")
            if path:
                files.append(path)
    return files


def unit_exec_start(unit_text: str) -> list[str] | None:
    """Parse the command-line argument list from ExecStart= (None if absent)."""
    return _exec_start_args(unit_text)


def _home(platform: Platform, home: Path | None = None) -> Path:
    if home is not None:
        return home
    return getattr(platform, "user_home", None) or Path.home()


def _read_unit_text(platform: Platform, path: Path, *, interactive: bool = True) -> str | None:
    """Read unit content; when an ordinary read fails (insufficient permission), go through an elevated session.

    macOS system-scope units live in /Library/LaunchDaemons -- with wrong
    permissions or ownership an ordinary user cannot read them. When unreadable,
    the caller must treat it as an "unconfirmable legacy unit", never as "no
    legacy environment" -- that would silently take over the user's
    hand-maintained Caddyfile and environment variables.

    With interactive=False, use a **non-interactive** privileged read (sudo -n):
    read-only commands (doctor) must never pop a password prompt in the user's
    face.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass
    if not interactive:
        reader = getattr(platform, "privilege_read", None)
        if callable(reader):
            try:
                return reader(path)
            except (OSError, RuntimeError):
                return None
        return None
    try:
        return platform.privilege().read(path)
    except (OSError, RuntimeError):
        # An elevated read also failed (sudo unavailable, etc.) -- caller treats it as "unconfirmable"
        return None


def detect_legacy(
    platform: Platform,
    paths: Paths,
    unit_dir: Path | None = None,
    *,
    home: Path | None = None,
    interactive: bool = True,
) -> LegacyState | None:
    """Detect whether a caddy service not managed by this tool exists.

    Returning None only means "no legacy unit" or "the unit already points at
    the tool's Caddyfile". When the unit exists but cannot be read / its config
    cannot be parsed, return a LegacyState with `error` -- the caller (migration)
    must refuse to act based on it, not treat it as a clean machine.

    interactive=False is for read-only scenarios (doctor): do not pop a password
    prompt when the unit is unreadable, but still return an "unconfirmable"
    state so the report layer can present it truthfully.
    """
    unit_path = unit_dir / "caddy.service" if unit_dir else platform.service_unit_path("caddy")
    if not unit_path.exists():
        return None
    unit_text = _read_unit_text(platform, unit_path, interactive=interactive)
    if unit_text is None:
        return LegacyState(
            unit_path=unit_path,
            error=f"cannot read legacy caddy unit {unit_path}; its config and origin cannot be confirmed",
        )
    config_path = unit_config_path(unit_text, _home(platform, home))
    if config_path is None:
        return LegacyState(
            unit_path=unit_path,
            env=unit_env(unit_text),
            error=(
                f"legacy caddy unit {unit_path} exists, but ExecStart= has no "
                f"parsable --config; the Caddyfile it uses cannot be confirmed"
            ),
        )
    try:
        if config_path.resolve() == paths.caddyfile.resolve():
            return None  # already managed by this tool
    except OSError:
        pass
    env_files = unit_environment_files(unit_text)
    if env_files:
        return LegacyState(
            unit_path=unit_path,
            config_path=config_path,
            env=unit_env(unit_text),
            error=(
                f"legacy caddy unit {unit_path} uses EnvironmentFile="
                f"{', '.join(env_files)}; migration cannot safely preserve the "
                f"variables in that file. Inline the variables into Environment= "
                f"or remove EnvironmentFile and retry"
            ),
        )
    return LegacyState(
        unit_path=unit_path,
        config_path=config_path,
        env=unit_env(unit_text),
    )


def _legacy_storage(state: LegacyState) -> Path | None:
    """storage path declared in the legacy Caddyfile's global block (None when unreadable)."""
    if state.config_path is None or not state.config_path.exists():
        return None
    try:
        text = state.config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return legacy_storage_path(text)


def resolve_storage_details(
    config: Config, state: LegacyState
) -> tuple[Config, Path | None, str | None]:
    """Reuse the legacy Caddyfile's storage path.

    Returns (effective config, legacy storage path, note). When there is no
    legacy storage the path is None; when there is no difference the config is
    returned unchanged and the note is None.

    Returning the path once means the caller no longer needs to read the legacy
    Caddyfile again, and cannot turn an "equality check" into an always-true
    condition. The difference must be stated -- silently switching storage means
    letting Caddy regenerate the CA, invalidating browser trust and the tls
    internal certificates already held by personal sites.
    """
    legacy = _legacy_storage(state)
    if legacy is None or legacy == config.storage_path:
        return config, legacy, None
    note = (
        f"reusing the storage path {legacy} from the legacy Caddyfile "
        f"(current config is {config.storage_path}): moving it makes Caddy "
        f"regenerate the CA, invalidating browser trust and existing site certificates"
    )
    return replace(config, storage=str(legacy)), legacy, note


def save_legacy_storage(paths: Paths, storage: Path | None) -> None:
    """Archive the effective storage path -- later setup must use the same location."""
    path = paths.caddy_storage
    if storage is None:
        path.unlink(missing_ok=True)
        return
    atomic_write_text(
        path,
        json.dumps({"storage": str(storage)}, ensure_ascii=False, indent=2) + "\n",
    )


def _warn_archive_unusable(path: Path, exc: Exception) -> None:
    warnings.warn(
        f"storage archive is unreadable: {path} ({exc}). To avoid Caddy regenerating "
        f"the CA, verify and then delete or repair this file",
        UserWarning,
        stacklevel=3,
    )


def load_legacy_storage(paths: Paths) -> Path | None:
    """Read back the archived storage path.

    File missing -> None (normal). File exists but is corrupt/unreadable -> emit
    an explicit warning and return None: the caller must first check whether the
    file exists to distinguish "no archive" from "broken archive"
    (`cli/_context.py` does exactly that).
    """
    path = paths.caddy_storage
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _warn_archive_unusable(path, exc)
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:  # UnicodeDecodeError is also a ValueError subclass
        _warn_archive_unusable(path, exc)
        return None
    if not isinstance(data, dict):
        _warn_archive_unusable(path, ValueError("top level is not a JSON object"))
        return None
    value = data.get("storage")
    if not isinstance(value, str) or not value:
        _warn_archive_unusable(path, ValueError("missing a valid storage field"))
        return None
    return Path(value).expanduser()
