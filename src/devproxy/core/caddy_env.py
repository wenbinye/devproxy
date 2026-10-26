"""Archive of unit environment variables.

During migration, copy the `Environment=` entries from the legacy unit so that
later setup runs can reinstall the unit with them -- otherwise `{$MINIKUBE_IP:…}`
in user sites points at the wrong gateway.
"""

from __future__ import annotations

import json

from devproxy.core.paths import Paths
from devproxy.core.paths import atomic_write_text


class CaddyEnvArchiveError(RuntimeError):
    """The environment variable archive exists but cannot be parsed.

    This is not "no archive": silently treating it as empty would drop variables
    such as `MINIKUBE_IP` when the unit is reinstalled, causing user sites to
    point at the wrong gateway with no warning. Construction/planning must
    abort so the user can fix or explicitly delete the file first.
    """


def save_extra_env(paths: Paths, env: dict[str, str]) -> None:
    """Archive the environment variables of the legacy unit.

    `{$MINIKUBE_IP:…}` in user sites depends on it -- later setup runs must
    reinstall the unit with it, otherwise personal sites point at the wrong
    gateway. Write atomically: a half-written JSON file is more dangerous than
    none at all.
    """
    path = paths.caddy_env
    if not env:
        path.unlink(missing_ok=True)
        return
    atomic_write_text(path, json.dumps(env, ensure_ascii=False, indent=2) + "\n")


def _archive_error(path, exc: Exception) -> CaddyEnvArchiveError:
    """Unified "archive is broken" exception: path, consequences and fix."""
    return CaddyEnvArchiveError(
        f"cannot parse the environment variable archive: {path} ({exc}); "
        f"losing migrated variables such as MINIKUBE_IP would point user sites at the wrong gateway; "
        f"fix the file (a valid JSON object) or delete it once it is no longer needed, then run again"
    )


def load_extra_env(paths: Paths) -> dict[str, str]:
    """Read back the environment variables of the legacy unit.

    Missing file -> empty (the normal case, meaning there is no migration
    archive). File present but corrupt/unreadable -> raise
    `CaddyEnvArchiveError`: silently treating it as empty would lose
    environment variables without the user noticing.
    """
    path = paths.caddy_env
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise _archive_error(path, exc) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:  # UnicodeDecodeError is also a ValueError subclass
        raise _archive_error(path, exc) from exc
    if not isinstance(data, dict):
        raise _archive_error(path, ValueError("top level is not a JSON object"))
    return {str(k): str(v) for k, v in data.items()}
