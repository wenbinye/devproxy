"""Transactional write entry point for the route table.

The transaction order is fixed: lock -> mutate table -> render -> validate ->
write to disk -> reload. Validation happens before writing to disk, and a
failed reload rolls back what was written -- Caddy must never go down because
of this tool.
"""

from __future__ import annotations

import builtins
import fcntl
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.gateway.caddy import CaddyConfig
from devproxy.gateway.caddy import MainCaddyfileSpec
from devproxy.gateway.caddy import build_main_caddyfile
from devproxy.gateway.routes import Route
from devproxy.gateway.routes import RouteTable
from devproxy.gateway.routes import atomic_write
from devproxy.gateway.routes import load_table
from devproxy.gateway.routes import render_caddyfile
from devproxy.gateway.routes import save_table


class RouteStoreError(RuntimeError):
    """Transaction failed."""


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """flock mutual exclusion. The lock scope must cover the whole transaction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fh.close()


class RouteStore:
    def __init__(self, paths: Paths, config: Config, caddy: CaddyConfig):
        self.paths = paths
        self.config = config
        self.caddy = caddy

    def list(self) -> list[Route]:
        """All registered routes, sorted by name.

        The route table is a **set**: the order in routes.json is only write
        order and means nothing (register then unregister, and the order
        changes). Sort on read so output is stable and predictable --
        `devproxy ls` looks the same every time instead of wobbling with
        registration history.
        """
        return sorted(load_table(self.paths.routes_json).routes, key=lambda r: r.name)

    def get(self, name: str) -> Route | None:
        return next((r for r in self.list() if r.name == name), None)

    def mutate(self, fn: Callable[[RouteTable], None]) -> None:
        """Run the whole "mutate table -> render -> validate -> write ->
        reload" transaction under flock.

        Once validation passes, a failure at any later step (including the
        second atomic_write and reload) must restore both files to their
        state at the start of the transaction -- otherwise routes.json and
        routes.caddy stay permanently inconsistent and Caddy reverse-proxies
        to a dead port using the old file.
        """
        paths = self.paths
        paths.ensure_files()
        with file_lock(paths.lock_path):
            old_json = (
                paths.routes_json.read_text(encoding="utf-8")
                if paths.routes_json.exists()
                else None
            )
            old_caddy = (
                paths.routes_caddy.read_text(encoding="utf-8")
                if paths.routes_caddy.exists()
                else None
            )

            try:
                table = load_table(paths.routes_json)
                fn(table)
                rendered = render_caddyfile(table, self.config.https_port)

                ok, msg = self._validate(rendered)
                if not ok:
                    raise RouteStoreError(
                        f"Caddy config validation failed, no changes made:\n{msg}"
                    )

                try:
                    save_table(paths.routes_json, table)
                    atomic_write(paths.routes_caddy, rendered)

                    ok, msg = self.caddy.reload()
                    if not ok:
                        raise RouteStoreError(
                            f"Caddy reload failed, route table rolled back:\n{msg}"
                        )
                except Exception as e:
                    self._rollback(old_json, old_caddy, e)
            finally:
                # Temp files used for validation must be removed -- leaving
                # them is just noise and makes people think two configs are
                # live.
                self._cleanup_temp()

    def _rollback(self, old_json: str | None, old_caddy: str | None, cause: Exception) -> NoReturn:
        """Write transaction failed: restore both files and wrap the cause
        uniformly in RouteStoreError."""
        try:
            self._restore(old_json, old_caddy)
        except Exception as restore_error:
            raise RouteStoreError(
                f"failed to write route table, and rollback failed too: {cause}; rollback error: {restore_error}"
            ) from cause
        if isinstance(cause, RouteStoreError):
            raise cause
        raise RouteStoreError(f"failed to write route table, rolled back: {cause}") from cause

    def _cleanup_temp(self) -> None:
        for path in self._temp_paths():
            path.unlink(missing_ok=True)

    def _temp_paths(self) -> builtins.list[Path]:
        return [
            self.paths.state_dir / "routes.caddy.new",
            self.paths.state_dir / "Caddyfile.new",
        ]

    def _validate(self, rendered: str) -> tuple[bool, str]:
        """Validate new content with a temporary main Caddyfile, without
        touching live files."""
        tmp_routes, tmp_main = self._temp_paths()
        atomic_write(tmp_routes, rendered)
        atomic_write(
            tmp_main,
            build_main_caddyfile(
                MainCaddyfileSpec(
                    admin_addr=self.config.admin_addr,
                    storage=self.config.storage_path,
                    routes_caddy=tmp_routes,
                    sites_caddy=self.paths.sites_caddy,
                    http_port=self.config.http_port,
                    https_port=self.config.https_port,
                )
            ),
        )
        return self.caddy.validate(tmp_main)

    def _restore(self, old_json: str | None, old_caddy: str | None) -> None:
        if old_json is None:
            self.paths.routes_json.unlink(missing_ok=True)
        else:
            atomic_write(self.paths.routes_json, old_json)
        if old_caddy is None:
            self.paths.routes_caddy.unlink(missing_ok=True)
        else:
            atomic_write(self.paths.routes_caddy, old_caddy)
