"""路由表的事务性写入口。

事务顺序固定为：加锁 → 改表 → 渲染 → 校验 → 落盘 → reload。
校验在落盘之前，reload 失败则回滚落盘内容 —— 保证 Caddy 永远不因本工具而挂掉。
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, NoReturn

from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.gateway.caddy import CaddyConfig, MainCaddyfileSpec, build_main_caddyfile
from devproxy.gateway.routes import (
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

    def mutate(self, fn: Callable[[RouteTable], None]) -> None:
        """在 flock 内完成「改表 → 渲染 → 校验 → 落盘 → reload」的整个事务。

        校验通过后，任何一步失败（包括第二次 atomic_write 与 reload）都必须
        把两份文件恢复到事务开始前的状态 —— 否则 routes.json 与 routes.caddy
        会永久不一致，Caddy 按旧文件反代到已经死掉的端口。
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
                rendered = render_caddyfile(table)

                ok, msg = self._validate(rendered)
                if not ok:
                    raise RouteStoreError(f"Caddy 配置校验失败，未做任何修改：\n{msg}")

                try:
                    save_table(paths.routes_json, table)
                    atomic_write(paths.routes_caddy, rendered)

                    ok, msg = self.caddy.reload()
                    if not ok:
                        raise RouteStoreError(
                            f"Caddy reload 失败，已回滚路由表：\n{msg}"
                        )
                except Exception as e:
                    self._rollback(old_json, old_caddy, e)
            finally:
                # 校验用的临时文件必须清掉 —— 留着它们只是噪音，
                # 还会让人以为线上有两份配置。
                self._cleanup_temp()

    def _rollback(
        self, old_json: str | None, old_caddy: str | None, cause: Exception
    ) -> NoReturn:
        """落盘事务失败：恢复两份文件，并把原因统一包装成 RouteStoreError。"""
        try:
            self._restore(old_json, old_caddy)
        except Exception as restore_error:
            raise RouteStoreError(
                f"写入路由表失败，且回滚也失败：{cause}；回滚错误：{restore_error}"
            ) from cause
        if isinstance(cause, RouteStoreError):
            raise cause
        raise RouteStoreError(f"写入路由表失败，已回滚：{cause}") from cause

    def _cleanup_temp(self) -> None:
        for path in self._temp_paths():
            path.unlink(missing_ok=True)

    def _temp_paths(self) -> list[Path]:
        return [
            self.paths.state_dir / "routes.caddy.new",
            self.paths.state_dir / "Caddyfile.new",
        ]

    def _validate(self, rendered: str) -> tuple[bool, str]:
        """用临时主 Caddyfile 校验新内容，不动线上文件。"""
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
