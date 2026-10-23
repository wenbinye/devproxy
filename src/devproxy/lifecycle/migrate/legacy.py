"""遗留环境的探测：单元解析、Caddyfile 定位、storage 留档。"""
from __future__ import annotations

import json
import shlex
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path

from devproxy.core.config import Config
from devproxy.core.paths import Paths, atomic_write_text
from devproxy.platform.base import Platform

from devproxy.lifecycle.migrate.caddyfile import legacy_storage_path


class MigrationError(RuntimeError):
    """迁移失败。

    `notes` 保存失败前已经收集到的用户提示（丢了哪些块、用了哪个二进制
    等）—— 失败路径同样要把它们打出来，否则用户只看到“失败”，不知道
    sites.caddy 被改成了什么样。
    """

    def __init__(self, message: str, notes: list[str] | None = None):
        super().__init__(message)
        self.notes = list(notes or [])

@dataclass
class LegacyState:
    """检测到的遗留环境。

    `error` 非空表示“单元存在，但无法确认它的配置/内容”。这种状态必须
    让迁移停下：把“解析失败”折叠成“没有遗留环境”，就等于按干净机器
    身份无备份覆盖用户手工维护的单元 —— 迁移最不能犯的错。
    """

    unit_path: Path
    config_path: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    error: str | None = None

def _exec_start_args(unit_text: str) -> list[str] | None:
    """ExecStart= 的命令行参数（systemd 的 "-" 前缀与引号都要正确处理）。"""
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line.startswith("ExecStart="):
            continue
        value = line[len("ExecStart="):]
        if value.startswith("-"):
            # systemd：ExecStart=-cmd 表示启动失败不记错
            value = value[1:].lstrip()
        try:
            args = shlex.split(value)
        except ValueError:
            # 引号不配对：退回朴素切分，至少别把整行丢掉
            args = value.split()
        if args:
            return args
    return None

def _config_arg(args: list[str]) -> str | None:
    """从参数列表里取出 --config 的值（同时支持空格与 = 两种写法）。"""
    for i, arg in enumerate(args):
        if arg == "--config":
            value = args[i + 1] if i + 1 < len(args) else ""
        elif arg.startswith("--config="):
            value = arg[len("--config="):]
        else:
            continue
        if value:
            return value
    return None

def unit_config_path(unit_text: str, home: Path) -> Path | None:
    """从 ExecStart= 里解析 --config 指向的路径（展开 %h）。

    Caddy/pflag 同时接受 `--config <path>` 与 `--config=<path>`；
    systemd 还允许给二进制与路径加引号（路径里可以有空格）。
    用 shlex 按真实词法解析，不能用裸 split()。
    """
    args = _exec_start_args(unit_text)
    if args is None:
        return None
    value = _config_arg(args)
    if value is None:
        return None
    return Path(value.replace("%h", str(home))).expanduser()

def unit_env(unit_text: str) -> dict[str, str]:
    """提取单元里的 Environment= 键值。

    systemd 的 Environment= 是按 shell 风格分词的（支持引号与转义），
    一行里也可以写多个 K=V。裸 split() 会把带空格的值拆坏，重写单元后
    systemd 直接拒绝加载。
    """
    env: dict[str, str] = {}
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        try:
            items = shlex.split(line[len("Environment="):])
        except ValueError:
            items = line[len("Environment="):].split()
        for item in items:
            if "=" in item:
                k, v = item.split("=", 1)
                env[k] = v
    return env

def unit_environment_files(unit_text: str) -> list[str]:
    """单元引用的 EnvironmentFile= 路径列表（去掉 systemd 的 "-" 前缀）。"""
    files: list[str] = []
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line.startswith("EnvironmentFile="):
            continue
        try:
            items = shlex.split(line[len("EnvironmentFile="):])
        except ValueError:
            items = line[len("EnvironmentFile="):].split()
        for item in items:
            path = item.lstrip("-")
            if path:
                files.append(path)
    return files

def unit_exec_start(unit_text: str) -> list[str] | None:
    """从 ExecStart= 里解析出命令行参数列表（没有则返回 None）。"""
    return _exec_start_args(unit_text)

def _home(platform: Platform, home: Path | None = None) -> Path:
    if home is not None:
        return home
    return getattr(platform, "user_home", None) or Path.home()

def _read_unit_text(
    platform: Platform, path: Path, *, interactive: bool = True
) -> str | None:
    """读单元内容；普通读不到时（权限不足）改走提权会话。

    macOS 的 system scope 单元在 /Library/LaunchDaemons —— 权限或归属不对时
    普通用户读不了。读不到时调用方必须按“无法确认的遗留单元”处理，
    绝不能当成“没有遗留环境” —— 那等于把用户手工维护的 Caddyfile 与
    环境变量悄悄接管掉。

    interactive=False 时走**非交互**的特权读（sudo -n）：只读命令
    （doctor）绝不能把密码框弹到用户脸上。
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass
    if not interactive:
        reader = getattr(platform, "privilege_read", None)
        if callable(reader):
            try:
                return reader(path)
            except (OSError, RuntimeError):
                return None
        return None
    try:
        return platform.privilege().read(path)
    except (OSError, RuntimeError):
        # 提权也读不到（sudo 不可用等）—— 调用方按“无法确认”处理
        return None

def detect_legacy(
    platform: Platform,
    paths: Paths,
    unit_dir: Path | None = None,
    *,
    home: Path | None = None,
    interactive: bool = True,
) -> LegacyState | None:
    """检测是否存在本工具未管理的 caddy 服务。

    返回 None 只表示“没有遗留单元”或“单元已经指向工具的 Caddyfile”。
    单元存在却读不到/解析不出配置时，返回带 `error` 的 LegacyState ——
    调用方（迁移）必须据此拒绝动手，不能当成干净机器。

    interactive=False 用于只读场景（doctor）：读不到单元时不弹密码框，
    但同样返回“无法确认”的状态，让报告层有机会如实呈现。
    """
    unit_path = (
        unit_dir / "caddy.service" if unit_dir else platform.service_unit_path("caddy")
    )
    if not unit_path.exists():
        return None
    unit_text = _read_unit_text(platform, unit_path, interactive=interactive)
    if unit_text is None:
        return LegacyState(
            unit_path=unit_path,
            error=f"无法读取遗留 caddy 单元 {unit_path}，无法确认它的配置与来源",
        )
    config_path = unit_config_path(unit_text, _home(platform, home))
    if config_path is None:
        return LegacyState(
            unit_path=unit_path,
            env=unit_env(unit_text),
            error=(
                f"遗留 caddy 单元 {unit_path} 存在，但 ExecStart= 里没有"
                f"可解析的 --config，无法确认它使用的 Caddyfile"
            ),
        )
    try:
        if config_path.resolve() == paths.caddyfile.resolve():
            return None  # 已经是本工具管理的
    except OSError:
        pass
    env_files = unit_environment_files(unit_text)
    if env_files:
        return LegacyState(
            unit_path=unit_path,
            config_path=config_path,
            env=unit_env(unit_text),
            error=(
                f"遗留 caddy 单元 {unit_path} 使用 EnvironmentFile="
                f"{', '.join(env_files)}；迁移无法安全保留该文件里的变量。"
                f"请把变量内联到 Environment= 或移除 EnvironmentFile 后重试"
            ),
        )
    return LegacyState(
        unit_path=unit_path,
        config_path=config_path,
        env=unit_env(unit_text),
    )

def _legacy_storage(state: LegacyState) -> Path | None:
    """旧 Caddyfile 全局块里声明的 storage 路径（读不到时返回 None）。"""
    if state.config_path is None or not state.config_path.exists():
        return None
    try:
        text = state.config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return legacy_storage_path(text)

def resolve_storage_details(
    config: Config, state: LegacyState
) -> tuple[Config, Path | None, str | None]:
    """沿用遗留 Caddyfile 的 storage 路径。

    返回 (生效的配置, 遗留 storage 路径, 提示语)。没有遗留 storage 时
    路径为 None；没有差异时配置原样返回、提示为 None。

    一次返回路径，调用方就不必再读一遍遗留 Caddyfile，也不会把
    “等值判断”写成一个恒真条件。差异必须说出来 —— 悄悄换掉 storage
    等于让 Caddy 重新生成 CA，浏览器里的信任与个人站点已有的
    tls internal 证书全部失效。
    """
    legacy = _legacy_storage(state)
    if legacy is None or legacy == config.storage_path:
        return config, legacy, None
    note = (
        f"已沿用遗留 Caddyfile 的 storage 路径 {legacy}"
        f"（当前配置是 {config.storage_path}）：换了位置 Caddy 会重新生成 CA，"
        f"浏览器信任与已有站点证书都会失效"
    )
    return replace(config, storage=str(legacy)), legacy, note

def save_legacy_storage(paths: Paths, storage: Path | None) -> None:
    """留档生效的 storage 路径 —— 之后的 setup 必须用同一个地方。"""
    path = paths.caddy_storage
    if storage is None:
        path.unlink(missing_ok=True)
        return
    atomic_write_text(
        path,
        json.dumps({"storage": str(storage)}, ensure_ascii=False, indent=2) + "\n",
    )

def _warn_archive_unusable(path: Path, exc: Exception) -> None:
    warnings.warn(
        f"storage 留档不可读：{path}（{exc}）。"
        f"为避免 Caddy 重建 CA，请确认后删除或修复该文件。",
        UserWarning,
        stacklevel=3,
    )

def load_legacy_storage(paths: Paths) -> Path | None:
    """读回留档的 storage 路径。

    文件不存在 → None（正常情况）。文件存在但损坏/无权限 → 发出显式
    警告后返回 None：调用方必须先判断文件是否存在，才能区分“没有留档”
    与“留档坏了”（`cli/_context.py` 正是这么做的）。
    """
    path = paths.caddy_storage
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _warn_archive_unusable(path, exc)
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:  # UnicodeDecodeError 也是 ValueError 子类
        _warn_archive_unusable(path, exc)
        return None
    if not isinstance(data, dict):
        _warn_archive_unusable(path, ValueError("顶层不是 JSON 对象"))
        return None
    value = data.get("storage")
    if not isinstance(value, str) or not value:
        _warn_archive_unusable(path, ValueError("缺少有效的 storage 字段"))
        return None
    return Path(value).expanduser()
