"""Dependency assembly: config loading, storage-archive resolution, platform and components.

This module only answers "where do the objects a command needs come from". It does no
rendering and prints no user-facing tables. Command modules look these functions up at
call time through `_shared` (tests patch `_shared`).
"""

from __future__ import annotations

from dataclasses import replace

from devproxy.cli._runtime import EXIT_PRECONDITION
from devproxy.cli._runtime import EXIT_USAGE
from devproxy.cli._runtime import err_console
from devproxy.cli._runtime import fail
from devproxy.core.config import DNS_BACKENDS
from devproxy.core.config import Config
from devproxy.core.config import load_config
from devproxy.core.paths import Paths
from devproxy.core.paths import default_paths
from devproxy.gateway.caddy import CaddyAdmin
from devproxy.gateway.caddy import CaddyConfig
from devproxy.gateway.store import RouteStore
from devproxy.lifecycle.migrate import load_legacy_storage


class StorageArchiveError(RuntimeError):
    """The migrated storage archive exists but cannot be read -- never fall back silently."""


def _resolve_storage(config: Config, paths: Paths) -> tuple[Config, list[str]]:
    """Keep the storage path from the migration archive and return the warnings to show.

    The archive (state/caddy-storage.json) records a decision made during migration:
    changing the storage location makes Caddy regenerate its CA, and every browser
    trust entry and existing certificate becomes void. So it wins over config.toml --
    but it must **never** win silently: a user who edits config.toml and is ignored
    without a word is the hardest kind of problem to diagnose.

    "Exists but unreadable" is not the same as "does not exist": the former means a
    full disk or an interrupted process left a truncated file, and falling back to
    config.toml could make Caddy switch storage and regenerate the CA. That must abort
    and be resolved by the user. Query and output are kept separate -- the caller
    prints the warnings (CQS).
    """
    archive = paths.caddy_storage
    if not archive.exists():
        return config, []
    storage = load_legacy_storage(paths)
    if storage is None:
        raise StorageArchiveError(
            f"the migration archive is corrupt, so Caddy's storage path cannot be "
            f"confirmed: {archive}. "
            "Aborted to keep Caddy from rebuilding its CA (existing certificates and "
            "browser trust would become void). "
            "Delete this file once you are sure the archive is no longer needed, then run again."
        )
    if storage == config.storage_path:
        return config, []
    warning = (
        f"devproxy: keeping the storage path {storage} from the migration archive "
        f"(config.toml says {config.storage_path}): the archive wins, "
        f"so the storage setting in config.toml is not in effect. "
        f"Delete {archive} to restore the config.toml setting."
    )
    return replace(config, storage=str(storage)), [warning]


def build_context(
    paths: Paths | None = None, config: Config | None = None
) -> tuple[Paths, Config, RouteStore, CaddyAdmin]:
    """Assemble the dependencies. Tests may pass their own paths/config.

    **Creates no directories and touches no files**: read-only commands such as ls /
    config / doctor call this too, and a query must not have write side effects (even
    less so to blow up on a read-only mount or an unwritable HOME). Write paths ensure
    their own directories (RouteStore.mutate, migrate); setup calls
    `paths.ensure_files()` explicitly on the deploy branch.
    """
    paths = paths or default_paths()
    config = config or load_config(paths.config_toml)
    try:
        config, warnings = _resolve_storage(config, paths)
    except StorageArchiveError as e:
        # Unmet precondition: the conflict between archive and config must be resolved
        # by the user
        fail(str(e), EXIT_PRECONDITION)
    for warning in warnings:
        err_console.print(warning)
    # The migration kept the legacy Caddyfile's storage (the CA still lives there) --
    # do not switch back: Caddy would regenerate its CA and every browser trust entry
    # and certificate would become void.
    caddy = CaddyConfig(
        caddy_bin=config.caddy_bin_path,
        caddyfile=paths.caddyfile,
        admin_addr=config.admin_addr,
    )
    store = RouteStore(paths, config, caddy)
    admin = CaddyAdmin(base=config.admin_base)
    return paths, config, store, admin


def _require_caddyfile(paths: Paths) -> None:
    """The precondition for up/expose: setup must already have generated the main Caddyfile.

    Without it, RouteStore.mutate -> CaddyConfig.reload() fails with exit 1, which
    misreports "not set up yet" as a runtime fault. Per the exit-code table this is an
    unmet precondition (exit 3).
    """
    if not paths.caddyfile.exists():
        fail(
            f"main Caddyfile not found: {paths.caddyfile}; run devproxy setup first",
            EXIT_PRECONDITION,
        )


def _platform():
    """Pick the platform implementation for the current system (Linux / macOS).

    Constructing the platform rejects unsuitable identities (for example
    `sudo devproxy ...` running as root) -- that is an unmet precondition, translated
    into a readable message plus exit 3 rather than letting a RuntimeError escape as
    a traceback.
    """
    from devproxy.platform import get_platform

    try:
        return get_platform()
    except RuntimeError as e:
        fail(str(e), EXIT_PRECONDITION)


def _components(
    paths,
    config,
    dns_backend=None,
    platform=None,
    *,
    include_cert: bool = True,
):
    """Assemble the components per the config (or an explicit override).

    dns_backend defaults to config.dns_backend -- setup and doctor must use the same
    backend, otherwise setup installs one and doctor keeps reporting "route missing".

    Component classes and the platform are looked up through `_shared` at call time:
    replacing `_shared.CertComponent` / `_shared._platform` in tests must affect this.
    """
    from devproxy.cli import _shared as s

    platform = platform or s._platform()
    backend = config.dns_backend if dns_backend is None else dns_backend
    components = [
        s.CaddyComponent(platform, paths, config),
        s.DnsmasqComponent(platform, paths, config),
        s.DnsComponent(platform, paths, config, backend=backend),
    ]
    if include_cert:
        components.append(s.CertComponent(platform, paths, config))
    return components


def _dns_backend(value: str | None, config: Config) -> str:
    """Resolve and validate the DNS backend. Reject invalid values outright, never treat
    them as auto.

    Passing an explicit empty string (--dns-backend "") is a usage error and must not
    quietly fall back to the configured value through the falsy semantics of `"" or ...`.
    """
    backend = config.dns_backend if value is None else value
    if backend not in DNS_BACKENDS:
        fail(
            f"dns_backend must be one of {', '.join(DNS_BACKENDS)}, got {backend!r}",
            EXIT_USAGE,
        )
    return backend


class _ElevationAware:
    """Record PermissionError raised by a component's apply; pass everything else through.

    `lifecycle.setup.apply_plan` collapses every exception into a Status so that one
    failing component does not block the rest, which loses the exception type; the CLI
    needs it to tell "elevation failed (exit 4)" from an ordinary failure (exit 1).
    This records and **re-raises**, so apply_plan's existing semantics are unchanged.
    """

    def __init__(self, component, sink: list[tuple[str, PermissionError]]):
        self._component = component
        self._sink = sink

    def __getattr__(self, name):
        return getattr(self._component, name)

    def apply(self, actions) -> None:
        try:
            self._component.apply(actions)
        except PermissionError as e:
            self._sink.append((getattr(self._component, "name", "?"), e))
            raise


def track_elevation_errors(
    components: list,
) -> tuple[list, list[tuple[str, PermissionError]]]:
    """Wrap the components in an elevation-failure recorder; return (components, sink)."""
    errors: list[tuple[str, PermissionError]] = []
    return [_ElevationAware(c, errors) for c in components], errors


def ensure_backend_supported(backend: str, platform) -> None:
    """Reject a backend/platform mismatch as a **usage error**.

    macOS has no systemd-resolved: DnsComponent.plan() would raise RuntimeError. Catching
    it here in the CLI means the user sees a clear usage error (exit 2) instead of
    "the component failed to compute changes" or a traceback.
    """
    if backend == "resolved" and getattr(platform, "name", "") == "mac":
        fail(
            "macOS does not support the resolved backend (there is no systemd-resolved); "
            "use auto or system",
            EXIT_USAGE,
        )
