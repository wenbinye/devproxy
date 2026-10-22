"""配置加载。优先级：CLI 参数 > config.toml > 内置默认值。"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """配置文件非法。"""


@dataclass(frozen=True)
class Config:
    domain_suffix: str = "test"
    dns_backend: str = "auto"
    admin_addr: str = "127.0.0.1:2019"
    http_port: int = 80
    https_port: int = 443
    storage: str = "~/.local/share/caddy"
    ready_timeout: int = 60
    caddy_bin: str = "~/.local/bin/caddy"

    @property
    def admin_base(self) -> str:
        return f"http://{self.admin_addr}"

    @property
    def storage_path(self) -> Path:
        return Path(self.storage).expanduser()

    @property
    def caddy_bin_path(self) -> Path:
        return Path(self.caddy_bin).expanduser()


def load_config(path: Path | None = None) -> Config:
    """读取 TOML 配置；文件不存在时返回全默认值。未知键直接报错。

    storage 的单一权威：config.toml 显式设置 > 环境变量 DEVPROXY_DATA_DIR >
    内置默认值 —— 保证 Caddy 的 storage 与 Paths.data_dir 始终指向同一棵树。
    """
    if path is None or not path.exists():
        data: dict = {}
    else:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"配置文件语法错误: {e}") from e
    known = {f.name for f in dataclasses.fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"未知配置项: {', '.join(unknown)}")
    if "storage" not in data:
        env_storage = os.environ.get("DEVPROXY_DATA_DIR", "")
        if env_storage:
            data["storage"] = os.path.expanduser(env_storage)
    return Config(**{k: v for k, v in data.items() if k in known})
