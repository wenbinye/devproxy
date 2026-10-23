"""遗留环境检测与迁移。

迁移的铁律：先备份，再动手。任何一步失败都能按备份还原。

被迁移的是用户**手工维护**的环境：自己写的 caddy.service、自己写的 Caddyfile、
自己写的 dnsmasq 配置。工具接管它们之前必须先把原件完整备份下来 ——
备份失败就一步都不动。

实现分散在同包的几个模块里（caddyfile / legacy / install / rollback），
这里只做编排，并把公共名字转出，供包外按 `devproxy.lifecycle.migrate.<名字>` 使用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from devproxy.core.backup import Backup
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
    detect_legacy,
    load_legacy_storage,
    resolve_storage,
    resolve_storage_details,
    save_legacy_storage,
    unit_config_path,
    unit_env,
    unit_environment_files,
    unit_exec_start,
)
from devproxy.lifecycle.migrate.rollback import (
    _create_backup,
    _restore_after_failure,
    rollback,
)

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
    "migrate",
    "resolve_storage",
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
    """一次遗留环境迁移的完整输入。

    参数收敛在一个对象里，调用点不会再把 `dnsmasq_conf` / `dns_route`
    两个同类型的可选路径传反；`config` 里已经折进生效的 DNS 后端。
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
    """迁移的结果：备份对象 + 需要告诉用户的提示。

    `notes` 是返回值的一部分，不再是调用方传进来、被就地修改的
    可变输出参数（CQS）。
    """

    backup: Backup
    notes: list[str] = field(default_factory=list)


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


def run_migration(request: MigrationRequest) -> MigrationOutcome:
    """执行迁移，返回备份与提示。调用方可据此回滚。

    顺序是铁律：先备份，再动手。备份失败 → 一个文件都不改；改写失败或
    被 Ctrl-C 中断 → 按备份还原后再抛。
    """
    platform = request.platform
    paths = request.paths
    state = request.state
    config = request.config
    if state.error:
        raise MigrationError(
            f"{state.error}。为避免在没有备份的情况下覆盖用户手工维护的单元，"
            f"迁移已中止（未改动任何文件）"
        )
    if state.config_path is None:
        raise MigrationError("遗留 Caddyfile 路径未知，迁移已中止（未改动任何文件）")
    notes: list[str] = []
    conf = (
        request.dnsmasq_conf
        if request.dnsmasq_conf is not None
        else _default_dnsmasq_conf()
    )
    route = (
        request.dns_route
        if request.dns_route is not None
        else default_dns_route(platform, paths, config, request.backend)
    )
    # storage 连续性：沿用遗留 Caddyfile 的 storage —— CA 不能换地方。
    # 一次解析同时拿到路径与提示，不再重复读文件、也不再做恒真比较。
    config, legacy_storage, storage_note = resolve_storage_details(config, state)
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
        notes.extend(_write_sites_caddy(paths, state, config))
        notes.extend(_validate_caddyfile(platform, paths, config, state))
        _write_main_caddyfile(paths, config)
        notes.extend(_install_caddy_unit(platform, paths, config, state))
        # 生效的 storage 就是遗留的 storage 时必须留档：之后的 setup
        # 换了位置，Caddy 会重新生成 CA。
        if legacy_storage is not None:
            save_legacy_storage(paths, legacy_storage)
        # 重启也在同一条失败路径里：普通失败由 _restart_caddy 自己降级成
        # 提示；Ctrl-C 仍按“中断”整体还原，绝不会让用户只看到 traceback。
        notes.extend(_restart_caddy(platform))
    except BaseException as exc:  # noqa: BLE001 —— 含 KeyboardInterrupt，必须还原
        _restore_after_failure(platform, backup, exc, notes=notes)
    return MigrationOutcome(backup=backup, notes=notes)


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
    """兼容入口：返回备份对象，并把提示追加进 `notes`（若提供）。

    `cli/_migrate.py` 仍按这个形状调用（CLI 不在本批修改范围）。新代码
    请直接用 `MigrationRequest` + `run_migration()`，提示从
    `MigrationOutcome.notes` 取，不要再依赖可变输出参数。
    """
    try:
        outcome = run_migration(
            MigrationRequest(
                platform=platform,
                paths=paths,
                config=config,
                state=state,
                dnsmasq_conf=dnsmasq_conf,
                dns_route=dns_route,
                backend=backend,
            )
        )
    except MigrationError as exc:
        # 失败路径也要把已收集的提示交给调用方（CLI 会在报错前打印）
        if notes is not None:
            notes.extend(exc.notes)
        raise
    if notes is not None:
        notes.extend(outcome.notes)
    return outcome.backup
