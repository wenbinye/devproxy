"""目录约定。所有路径集中在此，其他模块不得自行拼路径。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """devproxy 使用的三棵目录树。"""

    config_dir: Path
    state_dir: Path
    data_dir: Path

    @property
    def config_toml(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def caddyfile(self) -> Path:
        return self.config_dir / "Caddyfile"

    @property
    def sites_caddy(self) -> Path:
        return self.config_dir / "sites.caddy"

    @property
    def routes_json(self) -> Path:
        return self.state_dir / "routes.json"

    @property
    def routes_caddy(self) -> Path:
        return self.state_dir / "routes.caddy"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "routes.lock"

    @property
    def backup_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def caddy_env(self) -> Path:
        """迁移来的 caddy 单元环境变量（如 MINIKUBE_IP）。

        用户站点里的 {$MINIKUBE_IP:…} 依赖它 —— 重装单元时必须带上。
        """
        return self.state_dir / "caddy-env.json"

    @property
    def caddy_storage(self) -> Path:
        """迁移时生效的 Caddy storage 路径（CA 所在目录）。

        沿用遗留环境的 storage 之后必须留档：之后的 setup 若换回配置里的
        路径，Caddy 会重新生成 CA，浏览器信任与已有证书全部失效。
        """
        return self.state_dir / "caddy-storage.json"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    def ensure(self) -> None:
        """创建配置与状态目录。data_dir 交给 Caddy 自行创建。"""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def ensure_files(self) -> None:
        """创建 Caddyfile import 目标必须存在的空文件。"""
        self.ensure()
        for f in (self.sites_caddy, self.routes_caddy):
            f.touch(exist_ok=True)


def default_state_dir(home: Path | None = None) -> Path:
    """状态目录（默认 ~/.local/state/devproxy）。

    状态目录只有一个来源 —— 平台层（macOS 的日志目录等）也走这里，
    不许自己拼路径：拼两份就迟早对不上，日志写一半、状态存一半。

    环境变量 DEVPROXY_STATE_DIR 优先（测试与多实例场景用）。
    """
    env = os.environ.get("DEVPROXY_STATE_DIR")
    if env:
        return Path(env).expanduser()
    base = home or Path.home()
    return base / ".local" / "state" / "devproxy"


def default_paths() -> Paths:
    """默认路径；环境变量可整体覆盖（测试与多实例场景用）。"""
    home = Path.home()
    config_dir = Path(
        os.environ.get("DEVPROXY_CONFIG_DIR", home / ".config" / "devproxy")
    )
    state_dir = default_state_dir(home)
    data_dir = Path(
        os.environ.get("DEVPROXY_DATA_DIR", home / ".local" / "share" / "caddy")
    )
    return Paths(config_dir=config_dir, state_dir=state_dir, data_dir=data_dir)
