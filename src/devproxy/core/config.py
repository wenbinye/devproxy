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

# 保留给本地使用的顶级域白名单（RFC 2606 / RFC 6761）。
#
# 为什么是白名单而不是黑名单：
# - domain_suffix 会被写进**整机范围**的 DNS 路由
#   （Linux: systemd-resolved 的 `Domains=~.<后缀>`；macOS: /etc/resolver/<后缀>）。
#   写成 `Domains=~.com` 会让**这台机器全部 .com 域名**的解析都先问本地
#   dnsmasq —— 真实公共 TLD 会被劫持全机解析；
# - 用真实域名做本地站点，浏览器的 cookie 会串味，撞上 HSTS 时直接拒绝访问；
# - 白名单天然完备：将来新增的公共 TLD 不会漏网（黑名单会）。
#
# 用别的后缀不是不行，但那是**用户明确担责**的选择 —— 走 config.toml 的
# `allowed_domain_suffixes` 显式放行，默认不放行任何公共 TLD。
RESERVED_LOCAL_TLDS = ("test", "example", "localhost", "invalid")


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
    # 额外放行的域名后缀（默认空 —— 不放行任何公共 TLD）。
    # 写在 config.toml 里是**用户明确担责**的选择，见 RESERVED_LOCAL_TLDS。
    allowed_domain_suffixes: tuple[str, ...] = ()

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
    if "allowed_domain_suffixes" in data:
        # TOML 里的数组读出来是 list —— 归一成 tuple，Config 才是不可变的
        raw = data["allowed_domain_suffixes"]
        if isinstance(raw, list):
            data["allowed_domain_suffixes"] = tuple(raw)
    cfg = Config(**{k: v for k, v in data.items() if k in known})
    validate_config(cfg)
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


def _valid_label(value: object) -> bool:
    """是否是单个合法的 DNS 标签（与 domain_suffix 同一套判据）。"""
    return (
        isinstance(value, str) and len(value) <= MAX_LABEL_LEN and bool(DNS_LABEL_RE.match(value))
    )


def _check_allowed_domain_suffixes(cfg: Config) -> tuple[str, ...]:
    """校验放行名单，返回归一化后的元组。

    放行名单是**用户明确担责**的通道，因此只校验格式（必须是单个 DNS 标签），
    不校验它是不是公共 TLD —— 拦下它就等于拒绝了用户的选择。
    """
    raw = cfg.allowed_domain_suffixes
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise _bad(
            "allowed_domain_suffixes",
            f'必须是字符串数组（TOML 写成 ["dev"]），当前值 {raw!r}',
        )
    allowed: list[str] = []
    for item in raw:
        if not _valid_label(item):
            raise _bad(
                "allowed_domain_suffixes",
                f"每一项都必须是单个 DNS 标签（小写字母或数字开头结尾、"
                f"中间可含连字符、不含点、≤{MAX_LABEL_LEN} 字符），"
                f"当前值 {item!r}",
            )
        if item not in allowed:
            allowed.append(item)
    return tuple(allowed)


def _domain_suffix_rejection(value: str) -> str:
    """域名后缀不在白名单里时给用户的说明（为什么不行 + 怎么办）。"""
    return (
        f"当前值 {value!r} 不是保留给本地使用的顶级域。"
        f"devproxy 会把后缀写进整机范围的 DNS 路由"
        f"（Linux: systemd-resolved 的 `Domains=~.{value}`；"
        f"macOS: /etc/resolver/{value}），"
        f"这台机器该后缀的全部解析都会先问本地 dnsmasq —— "
        f"真实公共顶级域（如 com）会因此被劫持全机解析；"
        f"用真实域名做本地站点还会让浏览器的 cookie 串味，"
        f"撞上 HSTS 时直接拒绝访问。"
        f"请改用 {' / '.join(RESERVED_LOCAL_TLDS)}（推荐 test）。"
        f"确有正当理由时，可在 config.toml 里显式放行："
        f'allowed_domain_suffixes = ["{value}"]'
    )


def validate_config(cfg: Config) -> None:
    """配置校验的公开入口。

    load_config 与 CLI 的参数覆盖（--domain-suffix 等）都要过这一关：
    domain_suffix 会直通 Caddyfile 的站点地址，也会被写进 /etc 下的
    DNS 配置 —— 元字符（换行、花括号）能伪造出额外的配置块；
    它还会决定整机范围的解析路由，所以顶级域必须在白名单里
    （RESERVED_LOCAL_TLDS），除非用户自己在 config.toml 里显式放行。

    值级校验：默认值必然合法，因此只需对整个生效配置跑一遍。
    """
    if not _valid_label(cfg.domain_suffix):
        raise _bad(
            "domain_suffix",
            f"必须是单个 DNS 标签（小写字母或数字开头结尾、中间可含连字符、"
            f"不含点、≤{MAX_LABEL_LEN} 字符），当前值 {cfg.domain_suffix!r}",
        )
    allowed = _check_allowed_domain_suffixes(cfg)
    if cfg.domain_suffix not in RESERVED_LOCAL_TLDS and cfg.domain_suffix not in allowed:
        raise _bad("domain_suffix", _domain_suffix_rejection(cfg.domain_suffix))
    if cfg.dns_backend not in DNS_BACKENDS:
        raise _bad("dns_backend", f"必须是 {' | '.join(DNS_BACKENDS)}，当前值 {cfg.dns_backend!r}")
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
