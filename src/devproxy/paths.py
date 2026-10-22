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


def default_paths() -> Paths:
    """默认路径；环境变量可整体覆盖（测试与多实例场景用）。"""
    home = Path.home()
    config_dir = Path(
        os.environ.get("DEVPROXY_CONFIG_DIR", home / ".config" / "devproxy")
    )
    state_dir = Path(
        os.environ.get("DEVPROXY_STATE_DIR", home / ".local" / "state" / "devproxy")
    )
    data_dir = Path(
        os.environ.get("DEVPROXY_DATA_DIR", home / ".local" / "share" / "caddy")
    )
    return Paths(config_dir=config_dir, state_dir=state_dir, data_dir=data_dir)
