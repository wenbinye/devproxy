"""遗留环境检测与迁移。

迁移的铁律：先备份，再动手。任何一步失败都能按备份还原。

被迁移的是用户**手工维护**的环境：自己写的 caddy.service、自己写的 Caddyfile、
自己写的 dnsmasq 配置。工具接管它们之前必须先把原件完整备份下来 ——
备份失败就一步都不动。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from devproxy.backup import Backup, BackupManager, BackupRestoreError
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Platform

# .test 相关的通配块由工具接管（精确域名由 routes.caddy 动态生成）——
# 迁移时必须丢掉，否则与工具生成的站点重复定义，Caddy 直接起不来。
TEST_BLOCK_MARKERS = ("*.test", "http://*.test", "http://*.local.test")


class MigrationError(RuntimeError):
    """迁移失败。

    备份失败时迁移根本没开始；改写失败时已按备份还原，机器不会停在半迁移状态。
    """


@dataclass
class LegacyState:
    """检测到的遗留环境。"""

    unit_path: Path
    config_path: Path
    env: dict[str, str] = field(default_factory=dict)


# -- Caddyfile 拆分 ------------------------------------------------------------


def _block_head(block: str) -> str:
    """块的首个有效行（跳过注释与空行）。"""
    for line in block.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        return s
    return ""


def _block_address(block: str) -> str:
    """块声明的站点地址；全局块（以 "{" 开头）返回空串。"""
    head = _block_head(block)
    if head.startswith("{"):
        return ""
    return head.split("{")[0].strip()


def _split_blocks(text: str) -> tuple[list[str], list[str]]:
    """按大括号深度切分顶层块。返回 (全局块列表, 站点块列表)。

    站点块前面的注释归入该块 —— 用户 Caddyfile 里每个站点都带一段说明，
    拆开丢掉等于丢掉用户的文档。注释里的花括号不参与深度计算。
    """
    global_blocks: list[str] = []
    site_blocks: list[str] = []
    current: list[str] = []
    depth = 0
    for line in text.splitlines():
        current.append(line)
        stripped = line.strip()
        if not stripped.startswith("#"):
            depth += line.count("{") - line.count("}")
        if depth <= 0 and current:
            block = "\n".join(current).rstrip() + "\n"
            current = []
            depth = 0
            if _block_address(block) == "" and _block_head(block).startswith("{"):
                global_blocks.append(block)
            else:
                site_blocks.append(block)
    if current and any(line.strip() for line in current):
        # 结尾没有闭合的残留内容：原样保留，不吞掉用户的配置
        site_blocks.append("\n".join(current).rstrip() + "\n")
    return global_blocks, site_blocks


def split_caddyfile_blocks(
    text: str, suffix: str = "test"
) -> tuple[list[str], list[str]]:
    """拆出个人站点块。返回 (个人站点块列表, 被丢弃的 .test 相关块列表)。

    只做块级切分，不解析 Caddyfile 语法 —— 站点块原样保留。
    """
    _, sites = _split_blocks(text)
    markers = TEST_BLOCK_MARKERS
    if suffix and suffix != "test":
        markers = (
            f"*.{suffix}",
            f"http://*.{suffix}",
            f"http://*.local.{suffix}",
        )
    personal: list[str] = []
    dropped: list[str] = []
    for block in sites:
        address = _block_address(block)
        if address and any(address.startswith(m) for m in markers):
            dropped.append(block)
        else:
            personal.append(block)
    return personal, dropped


def split_caddyfile(text: str, suffix: str = "test") -> tuple[str, list[str]]:
    """拆出个人站点。返回 (个人站点文本, 被丢弃的 .test 相关块)。"""
    personal, dropped = split_caddyfile_blocks(text, suffix)
    return "\n".join(personal), dropped


def block_addresses(blocks: list[str]) -> list[str]:
    """一组站点块声明的地址（全局块与解析不出地址的块略过）。"""
    addresses = [_block_address(b) for b in blocks]
    return [a for a in addresses if a]


def wildcard_addresses(text: str, suffix: str = "test") -> list[str]:
    """文本里仍存在的通配站点块地址。

    通配块（如 *.test）与工具生成的精确站点块同后缀时，Caddy 的 TLS
    握手会整体失败 —— 所以 doctor 必须能指出"现役配置里还有通配块"。
    """
    _, dropped = split_caddyfile_blocks(text, suffix)
    return block_addresses(dropped)


def legacy_storage_path(text: str) -> Path | None:
    """遗留 Caddyfile 全局块里的 storage 路径。

    例：`storage file_system /home/u/.local/share/caddy`。
    迁移时必须沿用它 —— storage 换了位置，Caddy 会重新生成 CA，
    浏览器里的信任与已有站点的 tls internal 证书全部失效。
    """
    global_blocks, _ = _split_blocks(text)
    for block in global_blocks:
        for line in block.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or not s.startswith("storage"):
                continue
            parts = s.split(None, 2)
            if len(parts) >= 3 and parts[1] == "file_system":
                return Path(parts[2].strip().strip('"')).expanduser()
    return None


# -- 单元解析 -----------------------------------------------------------------


def unit_config_path(unit_text: str, home: Path) -> Path | None:
    """从 ExecStart= 里解析 --config 指向的路径（展开 %h）。"""
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line.startswith("ExecStart="):
            continue
        args = line[len("ExecStart="):].split()
        if "--config" in args:
            i = args.index("--config")
            if i + 1 < len(args):
                return Path(args[i + 1].replace("%h", str(home))).expanduser()
    return None


def unit_env(unit_text: str) -> dict[str, str]:
    """提取单元里的 Environment= 键值。"""
    env: dict[str, str] = {}
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        for item in line[len("Environment="):].split():
            if "=" in item:
                k, v = item.split("=", 1)
                env[k] = v
    return env


def unit_exec_start(unit_text: str) -> list[str] | None:
    """从 ExecStart= 里解析出命令行参数列表（没有则返回 None）。"""
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line.startswith("ExecStart="):
            continue
        args = line[len("ExecStart="):].split()
        if args:
            return args
    return None


# -- 遗留检测 -----------------------------------------------------------------


def _home(platform: Platform, home: Path | None = None) -> Path:
    if home is not None:
        return home
    return getattr(platform, "user_home", None) or Path.home()


def _read_unit_text(platform: Platform, path: Path) -> str | None:
    """读单元内容；普通读不到时（权限不足）改走提权会话。

    macOS 的 system scope 单元在 /Library/LaunchDaemons —— 权限或归属不对时
    普通用户读不了。读不到就当"没有遗留环境"，等于把用户手工维护的
    Caddyfile 与环境变量悄悄接管掉 —— 那是迁移最不能犯的错。
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass
    try:
        return platform.privilege().read(path)
    except (OSError, RuntimeError):
        # 提权也读不到（sudo 不可用等）—— 当作没有单元。
        return None


def detect_legacy(
    platform: Platform,
    paths: Paths,
    unit_dir: Path | None = None,
    *,
    home: Path | None = None,
) -> LegacyState | None:
    """检测是否存在本工具未管理的 caddy 服务。"""
    unit_path = (
        unit_dir / "caddy.service" if unit_dir else platform.service_unit_path("caddy")
    )
    if not unit_path.exists():
        return None
    unit_text = _read_unit_text(platform, unit_path)
    if unit_text is None:
        return None
    config_path = unit_config_path(unit_text, _home(platform, home))
    if config_path is None:
        return None
    try:
        if config_path.resolve() == paths.caddyfile.resolve():
            return None  # 已经是本工具管理的
    except OSError:
        pass
    return LegacyState(
        unit_path=unit_path, config_path=config_path, env=unit_env(unit_text)
    )


# -- storage 连续性：CA 不能换地方 -------------------------------------------


def _legacy_storage(state: LegacyState) -> Path | None:
    """旧 Caddyfile 全局块里声明的 storage 路径（读不到时返回 None）。"""
    if not state.config_path.exists():
        return None
    try:
        text = state.config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return legacy_storage_path(text)


def resolve_storage(
    config: Config, state: LegacyState
) -> tuple[Config, str | None]:
    """沿用遗留 Caddyfile 的 storage 路径。

    返回 (生效的配置, 提示语)。没有差异时配置原样返回、提示为 None。
    差异必须说出来 —— 悄悄换掉 storage 等于让 Caddy 重新生成 CA，
    浏览器里的信任与个人站点已有的 tls internal 证书全部失效。
    """
    legacy = _legacy_storage(state)
    if legacy is None or legacy == config.storage_path:
        return config, None
    note = (
        f"已沿用遗留 Caddyfile 的 storage 路径 {legacy}"
        f"（当前配置是 {config.storage_path}）：换了位置 Caddy 会重新生成 CA，"
        f"浏览器信任与已有站点证书都会失效"
    )
    return replace(config, storage=str(legacy)), note


def save_legacy_storage(paths: Paths, storage: Path | None) -> None:
    """留档生效的 storage 路径 —— 之后的 setup 必须用同一个地方。"""
    path = paths.caddy_storage
    if storage is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"storage": str(storage)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_legacy_storage(paths: Paths) -> Path | None:
    """读回留档的 storage 路径（读不到或损坏时返回 None）。"""
    try:
        data = json.loads(paths.caddy_storage.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("storage")
    if not isinstance(value, str) or not value:
        return None
    return Path(value).expanduser()


# -- 备份辅助：/etc 下的文件普通用户读不到 -------------------------------------


def _make_copier(platform: Platform):
    """备份用的复制函数：普通文件直接复制，读不到的（/etc 下的）走提权会话。"""

    def copy(src: Path, dest: Path) -> None:
        try:
            shutil.copy2(src, dest)
        except PermissionError:
            # /etc 下的 drop-in 是 root 拥有的：用提权会话读出内容，
            # 再以用户身份写进备份目录（否则备份副本也归 root，用户删不掉）
            text = platform.privilege().read(src)
            dest.write_text(text, encoding="utf-8")

    return copy


def _make_restorer(platform: Platform):
    """还原用的文件操作：src 为 None 表示删除 dest。

    /etc 下的文件普通用户写不了也删不了 —— 权限不够时走提权会话。
    """

    def restore(src: Path | None, dest: Path) -> None:
        if src is None:
            try:
                dest.unlink(missing_ok=True)
                return
            except PermissionError:
                code, msg = platform.privilege().run(["rm", "-f", str(dest)])
                if code != 0:
                    raise OSError(f"删除 {dest} 失败: {msg}")
                return
        try:
            shutil.copy2(src, dest)
        except PermissionError:
            platform.privilege().write(dest, src.read_text(encoding="utf-8"))

    return restore


# -- 迁移 ---------------------------------------------------------------------


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


def _has_content(text: str) -> bool:
    """文本里有没有实质内容（注释与空行不算）。"""
    return any(
        line.strip() and not line.strip().startswith("#") for line in text.splitlines()
    )


def _write_sites_caddy(
    paths: Paths, state: LegacyState, config: Config, notes: list[str]
) -> None:
    """把个人站点写进 sites.caddy。

    工具只 import sites.caddy，绝不覆盖已有内容 —— 用户自己写的站点不能被
    迁移冲掉（重复的站点块还会让 Caddy 直接起不来）。

    但"不覆盖"不等于"没影响"：迁移会把单元改指向工具的 Caddyfile，而它
    只 import sites.caddy。旧 Caddyfile 从此不再被读取 —— 留在那里的个人
    站点**就此不再被服务**。这件事必须明说（点名文件与地址），否则用户
    只会看到"站点突然 404 了"。
    """
    if not state.config_path.exists():
        notes.append(f"遗留 Caddyfile 不存在，跳过站点拆分：{state.config_path}")
        return
    legacy = state.config_path.read_text(encoding="utf-8")
    personal_blocks, dropped = split_caddyfile_blocks(
        legacy, suffix=config.domain_suffix
    )
    if dropped:
        addresses = block_addresses(dropped)
        notes.append(
            f"已丢弃 {len(dropped)} 个 .{config.domain_suffix} 站点块"
            f"（由工具接管）：{', '.join(addresses)}；"
            f"旧 Caddyfile 未被改动且已完整备份，需要可从备份里取回"
        )
    if not personal_blocks:
        return
    existing = ""
    if paths.sites_caddy.exists():
        existing = paths.sites_caddy.read_text(encoding="utf-8")
    if _has_content(existing):
        addresses = block_addresses(personal_blocks)
        notes.append(
            f"⚠ 迁移后这些个人站点将不再被服务：{state.config_path} 已不再被"
            f"主 Caddyfile import，工具只 import {paths.sites_caddy}。"
            f"涉及地址：{', '.join(addresses) or '（无法从文件解析）'}。"
            f"请手工把这些站点块从 {state.config_path} 合并到 {paths.sites_caddy}"
            f"（备份目录里也有原件）。"
        )
        return
    paths.sites_caddy.parent.mkdir(parents=True, exist_ok=True)
    paths.sites_caddy.write_text("\n".join(personal_blocks), encoding="utf-8")


def _main_caddyfile_content(paths: Paths, config: Config) -> str:
    """工具自己的主 Caddyfile（全局块 + 两个 import）。"""
    from devproxy.proxy.caddy import build_main_caddyfile

    return build_main_caddyfile(
        admin_addr=config.admin_addr,
        storage=config.storage_path,
        routes_caddy=paths.routes_caddy,
        sites_caddy=paths.sites_caddy,
    )


def _write_main_caddyfile(paths: Paths, config: Config) -> None:
    paths.caddyfile.parent.mkdir(parents=True, exist_ok=True)
    paths.caddyfile.write_text(_main_caddyfile_content(paths, config), encoding="utf-8")


def _legacy_caddy_binary(
    state: LegacyState | None, home: Path | None = None
) -> Path | None:
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
    notes: list[str],
    state: LegacyState | None = None,
) -> None:
    """换单元之前先让 caddy 验一遍配置。

    个人站点块是从用户自己的 Caddyfile 原样搬过来的 —— 语法冲突、重复站点
    都可能让 Caddy 起不来。校验不通过就不能换单元：否则服务起不来，
    用户连"是哪一行的错"都看不到。

    配置里的 caddy 二进制还没下载时，退回遗留单元里的那个 —— 否则
    "换单元前必校验"会因为一个路径不存在而变成空话。
    """
    binary = config.caddy_bin_path
    if not binary.exists():
        fallback = _legacy_caddy_binary(state)
        if fallback is not None and fallback.exists():
            notes.append(
                f"配置里的 caddy 二进制不存在，改用遗留单元里的 {fallback} 校验配置"
            )
            binary = fallback
        else:
            notes.append("caddy 二进制不存在，跳过配置校验（setup 下载后仍会校验）")
            return
    fd, tmp_name = tempfile.mkstemp(prefix="devproxy-validate-", suffix=".Caddyfile")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_main_caddyfile_content(paths, config))
        code, msg = platform.run([str(binary), "validate", "--config", str(tmp)])
    finally:
        tmp.unlink(missing_ok=True)
    if code != 0:
        raise MigrationError(
            f"caddy 配置校验未通过，迁移已中止（单元未改动）：{msg or '校验失败'}"
        )


def _save_extra_env(paths: Paths, env: dict[str, str]) -> None:
    """把遗留单元的环境变量留档。

    用户站点里的 {$MINIKUBE_IP:…} 依赖它 —— 后续 setup 重装单元时必须带上，
    否则个人站点会指向错误的网关。
    """
    path = paths.caddy_env
    if not env:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(env, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_extra_env(paths: Paths) -> dict[str, str]:
    """读回遗留单元的环境变量（读不到或损坏时返回空）。"""
    try:
        data = json.loads(paths.caddy_env.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _install_caddy_unit(
    platform: Platform,
    paths: Paths,
    config: Config,
    state: LegacyState,
    notes: list[str],
) -> None:
    """改写单元：指向工具的 Caddyfile，保留原有 Environment=。

    二进制路径必须能落到一个**真实存在**的文件上：迁移发生在组件流程
    之前，caddy 二进制可能还没下载（首次 setup、无网络）。那时若照抄
    config 里的路径，单元就指向一个不存在的二进制 —— 正在跑的 Caddy
    会被这一改弄坏，而且没有任何东西能把它拉起来。

    所以退回遗留单元里的 ExecStart 二进制：那个文件正在这台机器上跑，
    一定可用。setup 后续的组件流程会把单元换成正式路径。
    """
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
                f"caddy 二进制不存在（{binary}），单元将指向它 —— "
                f"setup 下载完成后才会可用"
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
    _save_extra_env(paths, state.env)


def _restart_caddy(platform: Platform, notes: list[str]) -> None:
    """重启 caddy，让新配置真正生效。

    单元换了但进程没重启的话，跑的还是旧配置 —— 这种"看起来迁移完了"的
    状态比迁移失败更难查。启动失败不阻断迁移：setup 随后的组件流程会
    把服务拉起来。
    """
    try:
        platform.enable_service("caddy")
        platform.restart_service("caddy")
    except Exception as exc:  # noqa: BLE001 —— 服务起不来不该回滚文件迁移
        notes.append(f"caddy 服务未能重启（{exc}）；devproxy setup 会继续处理")


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


def _create_backup(
    platform: Platform, paths: Paths, targets: list[Path]
) -> Backup:
    """备份全部待改动的文件。失败即中止迁移。"""
    try:
        return BackupManager(paths.backup_dir, copier=_make_copier(platform)).create(
            targets
        )
    except ValueError as e:
        raise MigrationError(
            f"备份目标不是普通文件（可能是符号链接或目录），迁移已中止：{e}"
        ) from e
    except OSError as e:
        raise MigrationError(
            f"备份失败，迁移已中止（未改动任何文件）：{e}"
        ) from e


def _restore_after_failure(
    platform: Platform, backup: Backup, exc: Exception
) -> None:
    """改写失败 → 按备份还原，绝不把机器留在半迁移状态。"""
    hint = (
        f"备份目录：{backup.directory}"
        f"（可手动还原：devproxy setup --rollback {backup.directory}）"
    )
    try:
        backup.restorer = _make_restorer(platform)
        backup.restore()
    except BackupRestoreError as restore_exc:
        raise MigrationError(
            f"迁移失败且还原不完整：{exc}；还原失败：{restore_exc}；{hint}"
        ) from exc
    except OSError as restore_exc:
        raise MigrationError(
            f"迁移失败且还原失败：{exc}；还原失败：{restore_exc}；{hint}"
        ) from exc
    raise MigrationError(f"迁移失败，已按备份还原：{exc}；{hint}") from exc


def rollback(backup: Backup, *, platform: Platform | None = None) -> list[Path]:
    """按备份还原。返回被还原（或被删除）的原始路径。

    部分条目失败时抛 BackupRestoreError —— 调用方必须捕获它，
    否则"已还原了哪些"会随异常一起丢掉。
    """
    if platform is not None:
        backup.restorer = _make_restorer(platform)
    return backup.restore()
