"""遗留环境检测与迁移。

迁移的铁律：先备份，再动手。任何一步失败都能按备份还原。

被迁移的是用户**手工维护**的环境：自己写的 caddy.service、自己写的 Caddyfile、
自己写的 dnsmasq 配置。工具接管它们之前必须先把原件完整备份下来 ——
备份失败就一步都不动。

实现分散在同包的几个模块里（caddyfile / legacy / install / rollback），
这里只做编排，并把公共名字转出，供包外按 `devproxy.lifecycle.migrate.<名字>` 使用。
"""
from __future__ import annotations

from pathlib import Path

from devproxy.core.caddy_env import load_extra_env, save_extra_env
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.platform.base import Platform

from devproxy.lifecycle.migrate.caddyfile import (
    block_addresses,
    legacy_storage_path,
    split_caddyfile,
    split_caddyfile_blocks,
    wildcard_addresses,
)
from devproxy.lifecycle.migrate.install import (
    _install_caddy_unit,
    _restart_caddy,
    _validate_caddyfile,
    _write_main_caddyfile,
    _write_sites_caddy,
)
from devproxy.lifecycle.migrate.legacy import (
    LegacyState,
    MigrationError,
    _legacy_storage,
    detect_legacy,
    load_legacy_storage,
    resolve_storage,
    save_legacy_storage,
    unit_config_path,
    unit_env,
    unit_exec_start,
)
from devproxy.lifecycle.migrate.rollback import (
    _create_backup,
    _restore_after_failure,
    rollback,
)


def default_dns_route(
    platform: Platform, paths: Paths, config: Config, backend: str | None = None
) -> Path:
    """本次迁移要备份的 DNS 路由文件（/etc 下的 drop-in）。

    与 DnsComponent 写的是同一个文件 —— 迁移后的 setup 会改写它，
    所以必须在迁移前备份。

    backend 必须与组件流程实际使用的后端一致：用户显式指定
    `--dns-backend system` 时，组件写的是 /etc/dnsmasq.d/devproxy.conf，
    备份的却是 resolved 那个文件 —— 被改动的文件不在备份里，回滚删不掉。
    """
    from devproxy.components.dns import DnsComponent

    comp = DnsComponent(
        platform, paths, config, backend=backend or config.dns_backend
    )
    return comp.target_path


def _default_dnsmasq_conf() -> Path:
    from devproxy.components.dnsmasq import DNSMASQ_CONF

    return DNSMASQ_CONF.expanduser()


def migrate(
    platform: Platform,
    paths: Paths,
    config: Config,
    state: LegacyState,
    *,
    dnsmasq_conf: Path | None = None,
    dns_route: Path | None = None,
    backend: str | None = None,
    notes: list[str] | None = None,
) -> Backup:
    """执行迁移。返回备份对象，调用方可据此回滚。

    顺序是铁律：先备份，再动手。备份失败 → 一个文件都不改；改写失败 →
    按备份还原后再抛。
    """
    notes = notes if notes is not None else []
    conf = dnsmasq_conf if dnsmasq_conf is not None else _default_dnsmasq_conf()
    route = (
        dns_route
        if dns_route is not None
        else default_dns_route(platform, paths, config, backend)
    )
    # storage 连续性：沿用遗留 Caddyfile 的 storage —— CA 不能换地方
    legacy = _legacy_storage(state)
    config, storage_note = resolve_storage(config, state)
    if storage_note:
        notes.append(storage_note)
    targets = [
        state.unit_path,  # 会被改写
        state.config_path,  # 旧 Caddyfile —— 留存副本，回滚要用
        paths.caddyfile,  # 工具的主 Caddyfile
        paths.sites_caddy,  # 个人站点
        conf,  # 用户的 dnsmasq 配置（后续 setup 会合并改写）
        platform.service_unit_path("dnsmasq"),  # 后续 setup 会重装这个单元
        route,  # /etc 下的 DNS 路由 drop-in
        paths.caddy_env,  # 迁移留下的单元环境变量留档
        paths.caddy_storage,  # 迁移留下的 storage 留档
    ]
    backup = _create_backup(platform, paths, targets)
    try:
        # 主 Caddyfile 会 import 这两个文件 —— 校验之前必须先在，
        # 否则 caddy 只会报 "file not found"，迁移被自己的校验拦死。
        paths.ensure_files()
        _write_sites_caddy(paths, state, config, notes)
        _validate_caddyfile(platform, paths, config, notes, state)
        _write_main_caddyfile(paths, config)
        _install_caddy_unit(platform, paths, config, state, notes)
        # 生效的 storage 就是遗留的 storage 时必须留档：之后的 setup
        # 换了位置，Caddy 会重新生成 CA。
        if legacy is not None and legacy == config.storage_path:
            save_legacy_storage(paths, legacy)
    except Exception as exc:
        _restore_after_failure(platform, backup, exc)
    _restart_caddy(platform, notes)
    return backup
