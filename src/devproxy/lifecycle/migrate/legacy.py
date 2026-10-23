"""遗留环境的探测：单元解析、Caddyfile 定位、storage 留档。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from devproxy.core.paths import Paths
from devproxy.platform.base import Platform

from devproxy.lifecycle.migrate.caddyfile import legacy_storage_path


class MigrationError(RuntimeError):
    """迁移失败。"""

@dataclass
class LegacyState:
    """检测到的遗留环境。"""

    unit_path: Path
    config_path: Path
    env: dict[str, str] = field(default_factory=dict)

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

def _home(platform: Platform, home: Path | None = None) -> Path:
    if home is not None:
        return home
    return getattr(platform, "user_home", None) or Path.home()

def _read_unit_text(
    platform: Platform, path: Path, *, interactive: bool = True
) -> str | None:
    """读单元内容；普通读不到时（权限不足）改走提权会话。

    macOS 的 system scope 单元在 /Library/LaunchDaemons —— 权限或归属不对时
    普通用户读不了。读不到就当"没有遗留环境"，等于把用户手工维护的
    Caddyfile 与环境变量悄悄接管掉 —— 那是迁移最不能犯的错。

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
        # 提权也读不到（sudo 不可用等）—— 当作没有单元。
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

    interactive=False 用于只读场景（doctor）：读不到单元就当没有，
    绝不弹密码框。
    """
    unit_path = (
        unit_dir / "caddy.service" if unit_dir else platform.service_unit_path("caddy")
    )
    if not unit_path.exists():
        return None
    unit_text = _read_unit_text(platform, unit_path, interactive=interactive)
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
