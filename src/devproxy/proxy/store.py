"""路由表的事务性写入口。

事务顺序固定为：加锁 → 改表 → 渲染 → 校验 → 落盘 → reload。
校验在落盘之前，reload 失败则回滚落盘内容 —— 保证 Caddy 永远不因本工具而挂掉。
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.proxy.caddy import CaddyConfig, build_main_caddyfile
from devproxy.proxy.routes import (
    Route,
    RouteTable,
    atomic_write,
    load_table,
    render_caddyfile,
    save_table,
)


class RouteStoreError(RuntimeError):
    """事务失败。"""


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """flock 互斥。持锁范围必须覆盖整个事务。"""
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
        return load_table(self.paths.routes_json).routes

    def get(self, name: str) -> Route | None:
        return next((r for r in self.list() if r.name == name), None)

    def mutate(self, fn: Callable[[RouteTable], None]) -> list[Route]:
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

            table = load_table(paths.routes_json)
            fn(table)
            rendered = render_caddyfile(table)

            ok, msg = self._validate(rendered)
            if not ok:
                raise RouteStoreError(f"Caddy 配置校验失败，未做任何修改：\n{msg}")

            save_table(paths.routes_json, table)
            atomic_write(paths.routes_caddy, rendered)

            ok, msg = self.caddy.reload()
            if not ok:
                self._restore(old_json, old_caddy)
                raise RouteStoreError(f"Caddy reload 失败，已回滚路由表：\n{msg}")
            return table.routes

    def _validate(self, rendered: str) -> tuple[bool, str]:
        """用临时主 Caddyfile 校验新内容，不动线上文件。"""
        tmp_routes = self.paths.state_dir / "routes.caddy.new"
        tmp_main = self.paths.state_dir / "Caddyfile.new"
        atomic_write(tmp_routes, rendered)
        atomic_write(
            tmp_main,
            build_main_caddyfile(
                admin_addr=self.config.admin_addr,
                storage=self.config.storage_path,
                routes_caddy=tmp_routes,
                sites_caddy=self.paths.sites_caddy,
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
