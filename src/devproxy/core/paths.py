"""Directory conventions. All paths live here; other modules must not compose paths themselves."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """The three directory trees used by devproxy."""

    config_dir: Path
    state_dir: Path
    data_dir: Path

    @property
    def config_toml(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def caddyfile(self) -> Path:
        return self.config_dir / "Caddyfile"

    @property
    def sites_caddy(self) -> Path:
        return self.config_dir / "sites.caddy"

    @property
    def routes_json(self) -> Path:
        return self.state_dir / "routes.json"

    @property
    def routes_caddy(self) -> Path:
        return self.state_dir / "routes.caddy"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "routes.lock"

    @property
    def backup_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def caddy_env(self) -> Path:
        """Caddy unit environment variables migrated over (such as MINIKUBE_IP).

        `{$MINIKUBE_IP:…}` in user sites depends on it -- it must be carried
        over when the unit is reinstalled.
        """
        return self.state_dir / "caddy-env.json"

    @property
    def caddy_storage(self) -> Path:
        """Caddy storage path in effect during migration (the directory holding the CA).

        After reusing the legacy environment's storage it must be archived: if
        a later setup switches back to the path from the config, Caddy
        regenerates the CA and all browser trust and existing certificates
        become invalid.
        """
        return self.state_dir / "caddy-storage.json"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    def ensure(self) -> None:
        """Create the config and state directories. data_dir is left for Caddy to create itself."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def ensure_files(self) -> None:
        """Create the empty files that Caddyfile import targets must have in order to exist."""
        self.ensure()
        for f in (self.sites_caddy, self.routes_caddy):
            f.touch(exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """Same-directory temp file + os.replace for an atomic write.

    Archives (caddy-env.json / caddy-storage.json) being read half-written
    has serious consequences: a truncated JSON file is treated as "no
    archive", and a storage fallback makes Caddy rebuild the CA. A direct
    write_text leaves a truncated file behind when the process is killed or
    the disk fills up; this guarantees the target is either the old content
    or the complete new content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def default_state_dir(home: Path | None = None) -> Path:
    """State directory (defaults to ~/.local/state/devproxy).

    There is only one source for the state directory -- the platform layer
    (such as the macOS log directory) uses this too and must not compose the
    path itself: two copies will eventually diverge, with logs and state
    written to different places.

    The DEVPROXY_STATE_DIR environment variable takes precedence (for tests
    and multi-instance scenarios).
    """
    env = os.environ.get("DEVPROXY_STATE_DIR")
    if env:
        return Path(env).expanduser()
    base = home or Path.home()
    return base / ".local" / "state" / "devproxy"


def default_paths() -> Paths:
    """Default paths; an environment variable can override the whole set (for tests and multi-instance scenarios)."""
    home = Path.home()
    config_dir = Path(os.environ.get("DEVPROXY_CONFIG_DIR", home / ".config" / "devproxy"))
    state_dir = default_state_dir(home)
    data_dir = Path(os.environ.get("DEVPROXY_DATA_DIR", home / ".local" / "share" / "caddy"))
    return Paths(config_dir=config_dir, state_dir=state_dir, data_dir=data_dir)
