"""把工具自己的配置与单元落到盘上：主 Caddyfile、sites.caddy、systemd/launchd 单元。"""

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
    """把个人站点写进 sites.caddy，返回要告诉用户的提示。

    工具只 import sites.caddy，绝不覆盖已有内容 —— 用户自己写的站点不能被
    迁移冲掉（重复的站点块还会让 Caddy 直接起不来）。

    但"不覆盖"不等于"没影响"：迁移会把单元改指向工具的 Caddyfile，而它
    只 import sites.caddy。旧 Caddyfile 从此不再被读取 —— 留在那里的个人
    站点**就此不再被服务**。这件事必须明说（点名文件与地址），否则用户
    只会看到"站点突然 404 了"。
    """
    config_path = state.config_path
    if config_path is None:
        raise MigrationError("遗留 Caddyfile 路径未知，迁移已中止")
    notes: list[str] = []
    if not config_path.exists():
        return [f"遗留 Caddyfile 不存在，跳过站点拆分：{config_path}"]
    legacy = config_path.read_text(encoding="utf-8")
    personal_blocks, dropped = split_caddyfile_blocks(legacy, suffix=config.domain_suffix)
    if dropped:
        addresses = block_addresses(dropped)
        notes.append(
            f"已丢弃 {len(dropped)} 个 .{config.domain_suffix} 站点块"
            f"（由工具接管）：{', '.join(addresses)}；"
            f"旧 Caddyfile 未被改动且已完整备份，需要可从备份里取回"
        )
    if not personal_blocks:
        return notes
    existing = ""
    if paths.sites_caddy.exists():
        existing = paths.sites_caddy.read_text(encoding="utf-8")
    if _has_content(existing):
        addresses = block_addresses(personal_blocks)
        notes.append(
            f"⚠ 迁移后这些个人站点将不再被服务：{config_path} 已不再被"
            f"主 Caddyfile import，工具只 import {paths.sites_caddy}。"
            f"涉及地址：{', '.join(addresses) or '（无法从文件解析）'}。"
            f"请手工把这些站点块从 {config_path} 合并到 {paths.sites_caddy}"
            f"（备份目录里也有原件）。"
        )
        return notes
    paths.sites_caddy.parent.mkdir(parents=True, exist_ok=True)
    paths.sites_caddy.write_text("\n".join(personal_blocks), encoding="utf-8")
    return notes


def _main_caddyfile_content(paths: Paths, config: Config) -> str:
    """工具自己的主 Caddyfile（全局块 + 两个 import）。"""
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
    """遗留单元的 ExecStart 指向的 caddy 二进制。

    配置里的 caddy_bin 可能还没下载（首次 setup），但遗留单元里的那个
    正在这台机器上跑 —— 用它做配置校验才有意义。
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
    """换单元之前先让 caddy 验一遍配置，返回提示。

    个人站点块是从用户自己的 Caddyfile 原样搬过来的 —— 语法冲突、重复站点
    都可能让 Caddy 起不来。校验不通过就不能换单元：否则服务起不来，
    用户连"是哪一行的错"都看不到。

    配置里的 caddy 二进制还没下载时，退回遗留单元里的那个 —— 否则
    "换单元前必校验"会因为一个路径不存在而变成空话。
    """
    notes: list[str] = []
    binary = config.caddy_bin_path
    if not binary.exists():
        fallback = _legacy_caddy_binary(state)
        if fallback is not None and fallback.exists():
            notes.append(f"配置里的 caddy 二进制不存在，改用遗留单元里的 {fallback} 校验配置")
            binary = fallback
        else:
            notes.append("caddy 二进制不存在，跳过配置校验（setup 下载后仍会校验）")
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
        raise MigrationError(f"caddy 配置校验未通过，迁移已中止（单元未改动）：{msg or '校验失败'}")
    return notes


def _install_caddy_unit(
    platform: Platform,
    paths: Paths,
    config: Config,
    state: LegacyState,
) -> list[str]:
    """改写单元：指向工具的 Caddyfile，保留原有 Environment=，返回提示。

    二进制路径必须能落到一个**真实存在**的文件上：迁移发生在组件流程
    之前，caddy 二进制可能还没下载（首次 setup、无网络）。那时若照抄
    config 里的路径，单元就指向一个不存在的二进制 —— 正在跑的 Caddy
    会被这一改弄坏，而且没有任何东西能把它拉起来。

    所以退回遗留单元里的 ExecStart 二进制：那个文件正在这台机器上跑，
    一定可用。setup 后续的组件流程会把单元换成正式路径。
    """
    notes: list[str] = []
    binary = config.caddy_bin_path
    if not binary.exists():
        fallback = _legacy_caddy_binary(state)
        if fallback is not None and fallback.exists():
            notes.append(
                f"配置里的 caddy 二进制不存在，单元先沿用遗留的 {fallback}"
                f"（setup 下载完成后会自动换成 {binary}）"
            )
            binary = fallback
        else:
            notes.append(
                f"caddy 二进制不存在（{binary}），单元将指向它 —— setup 下载完成后才会可用"
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
    """重启 caddy，让新配置真正生效，返回提示。

    单元换了但进程没重启的话，跑的还是旧配置 —— 这种"看起来迁移完了"的
    状态比迁移失败更难查。启动失败不阻断迁移：setup 随后的组件流程会
    把服务拉起来。
    """
    try:
        platform.enable_service("caddy")
        platform.restart_service("caddy")
    except Exception as exc:  # noqa: BLE001 —— 服务起不来不该回滚文件迁移
        return [f"caddy 服务未能重启（{exc}）；devproxy setup 会继续处理"]
    return []
