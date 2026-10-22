"""配置加载。优先级：CLI 参数 > config.toml > 内置默认值。"""

from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """配置文件非法。"""


# 单个 DNS 标签：小写字母/数字开头结尾，中间可有连字符，不含点。
# domain_suffix 会直通 Caddyfile 的站点地址 —— 大括号、换行这类元字符
# 能伪造出额外站点块，必须在配置入口就拦下
DNS_LABEL_RE = re.compile(r"\A[a-z0-9]([a-z0-9-]*[a-z0-9])?\Z")
MAX_LABEL_LEN = 63
DNS_BACKENDS = ("auto", "resolved", "system")
PORT_MIN, PORT_MAX = 1, 65535
READY_TIMEOUT_MIN, READY_TIMEOUT_MAX = 1, 3600


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
    cfg = Config(**{k: v for k, v in data.items() if k in known})
    _validate_config(cfg)
    return cfg


def _bad(key: str, msg: str) -> ConfigError:
    return ConfigError(f"配置项 {key} 非法：{msg}")


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _valid_admin_addr(addr: object) -> bool:
    """admin_addr 必须是 host:port（host 可为 IPv4/主机名/[IPv6]）。"""
    if not isinstance(addr, str) or ":" not in addr or any(c.isspace() for c in addr):
        return False
    host, _, port = addr.rpartition(":")
    return bool(host) and port.isdigit() and PORT_MIN <= int(port) <= PORT_MAX


def _validate_config(cfg: Config) -> None:
    """值级校验。默认值必然合法，因此只需对整个生效配置跑一遍。"""
    if (
        not isinstance(cfg.domain_suffix, str)
        or len(cfg.domain_suffix) > MAX_LABEL_LEN
        or not DNS_LABEL_RE.match(cfg.domain_suffix)
    ):
        raise _bad(
            "domain_suffix",
            f"必须是单个 DNS 标签（小写字母或数字开头结尾、中间可含连字符、"
            f"不含点、≤{MAX_LABEL_LEN} 字符），当前值 {cfg.domain_suffix!r}",
        )
    if cfg.dns_backend not in DNS_BACKENDS:
        raise _bad(
            "dns_backend", f"必须是 {' | '.join(DNS_BACKENDS)}，当前值 {cfg.dns_backend!r}"
        )
    for key in ("http_port", "https_port"):
        value = getattr(cfg, key)
        if not _is_int(value) or not PORT_MIN <= value <= PORT_MAX:
            raise _bad(key, f"必须是 {PORT_MIN}..{PORT_MAX} 的整数，当前值 {value!r}")
    if not _is_int(cfg.ready_timeout) or not (
        READY_TIMEOUT_MIN <= cfg.ready_timeout <= READY_TIMEOUT_MAX
    ):
        raise _bad(
            "ready_timeout",
            f"必须是 {READY_TIMEOUT_MIN}..{READY_TIMEOUT_MAX} 的整数（秒），"
            f"当前值 {cfg.ready_timeout!r}",
        )
    if not _valid_admin_addr(cfg.admin_addr):
        raise _bad(
            "admin_addr", f"必须是 host:port（如 127.0.0.1:2019），当前值 {cfg.admin_addr!r}"
        )
    for key in ("storage", "caddy_bin"):
        if not isinstance(getattr(cfg, key), str):
            raise _bad(key, f"必须是字符串路径，当前值 {getattr(cfg, key)!r}")
