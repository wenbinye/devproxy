"""Config loading. Precedence: CLI arguments > config.toml > built-in defaults."""

from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Invalid configuration file."""


# A single DNS label: starts and ends with a lowercase letter/digit, may
# contain hyphens in the middle, and contains no dots. domain_suffix goes
# straight into the Caddyfile site address -- metacharacters such as braces
# and newlines could forge extra site blocks, so they must be rejected at the
# configuration entry point.
DNS_LABEL_RE = re.compile(r"\A[a-z0-9]([a-z0-9-]*[a-z0-9])?\Z")
MAX_LABEL_LEN = 63
DNS_BACKENDS = ("auto", "resolved", "system")
PORT_MIN, PORT_MAX = 1, 65535
READY_TIMEOUT_MIN, READY_TIMEOUT_MAX = 1, 3600

# Whitelist of top-level domains reserved for local use (RFC 2606 / RFC 6761).
#
# Why a whitelist rather than a blacklist:
# - domain_suffix is written into **machine-wide** DNS routing
#   (Linux: systemd-resolved `Domains=~.<suffix>`; macOS: /etc/resolver/<suffix>).
#   Writing `Domains=~.com` would make resolution of **every .com domain on
#   this machine** ask the local dnsmasq first -- a real public TLD would
#   hijack resolution machine-wide;
# - using a real domain for local sites makes browser cookies bleed across
#   sites and gets rejected outright when HSTS is involved;
# - a whitelist is inherently complete: public TLDs added in the future cannot
#   slip through (a blacklist could).
#
# Using another suffix is not forbidden, but it is a choice **the user
# explicitly owns** -- opt in via `allowed_domain_suffixes` in config.toml;
# no public TLD is allowed by default.
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
    # Additionally allowed domain suffixes (empty by default -- no public TLD
    # is allowed). Setting this in config.toml is a choice **the user
    # explicitly owns**; see RESERVED_LOCAL_TLDS.
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
    """Read the TOML config; return all defaults when the file does not exist. Unknown keys raise an error.

    Single source of truth for storage: explicit config.toml value >
    DEVPROXY_DATA_DIR environment variable > built-in default -- so Caddy's
    storage and Paths.data_dir always point at the same tree.
    """
    if path is None or not path.exists():
        data: dict = {}
    else:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"config file syntax error: {e}") from e
    known = {f.name for f in dataclasses.fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown config option(s): {', '.join(unknown)}")
    if "storage" not in data:
        env_storage = os.environ.get("DEVPROXY_DATA_DIR", "")
        if env_storage:
            data["storage"] = os.path.expanduser(env_storage)
    if "allowed_domain_suffixes" in data:
        # Arrays read from TOML are lists -- normalize to a tuple so Config stays immutable
        raw = data["allowed_domain_suffixes"]
        if isinstance(raw, list):
            data["allowed_domain_suffixes"] = tuple(raw)
    cfg = Config(**{k: v for k, v in data.items() if k in known})
    validate_config(cfg)
    return cfg


def _bad(key: str, msg: str) -> ConfigError:
    return ConfigError(f"invalid config option {key}: {msg}")


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _valid_admin_addr(addr: object) -> bool:
    """admin_addr must be host:port (host may be IPv4/hostname/[IPv6])."""
    if not isinstance(addr, str) or ":" not in addr or any(c.isspace() for c in addr):
        return False
    host, _, port = addr.rpartition(":")
    return bool(host) and port.isdigit() and PORT_MIN <= int(port) <= PORT_MAX


def _valid_label(value: object) -> bool:
    """Whether this is a single valid DNS label (same criterion as domain_suffix)."""
    return (
        isinstance(value, str) and len(value) <= MAX_LABEL_LEN and bool(DNS_LABEL_RE.match(value))
    )


def _check_allowed_domain_suffixes(cfg: Config) -> tuple[str, ...]:
    """Validate the allow list and return the normalized tuple.

    The allow list is a channel **the user explicitly owns**, so only its
    format is validated (must be a single DNS label); whether it is a public
    TLD is not checked -- rejecting that would amount to rejecting the user's
    choice.
    """
    raw = cfg.allowed_domain_suffixes
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise _bad(
            "allowed_domain_suffixes",
            f'must be an array of strings (write ["dev"] in TOML), current value {raw!r}',
        )
    allowed: list[str] = []
    for item in raw:
        if not _valid_label(item):
            raise _bad(
                "allowed_domain_suffixes",
                f"each entry must be a single DNS label (starts and ends with a "
                f"lowercase letter or digit, may contain hyphens in the middle, "
                f"no dots, ≤{MAX_LABEL_LEN} characters), "
                f"current value {item!r}",
            )
        if item not in allowed:
            allowed.append(item)
    return tuple(allowed)


def _domain_suffix_rejection(value: str) -> str:
    """Explanation shown to the user when the domain suffix is not on the whitelist (why not + what to do)."""
    return (
        f"the current value {value!r} is not a top-level domain reserved for local use; "
        f"devproxy writes the suffix into machine-wide DNS routing "
        f"(Linux: systemd-resolved `Domains=~.{value}`; "
        f"macOS: /etc/resolver/{value}), so all resolution of that suffix on "
        f"this machine asks the local dnsmasq first -- a real public TLD "
        f"(such as com) would therefore hijack resolution machine-wide; "
        f"using a real domain for local sites also makes browser cookies bleed "
        f"across sites and gets rejected outright when HSTS is involved; "
        f"use {' / '.join(RESERVED_LOCAL_TLDS)} instead (test is recommended); "
        f"if you really have a valid reason, explicitly allow it in config.toml: "
        f'allowed_domain_suffixes = ["{value}"]'
    )


def validate_config(cfg: Config) -> None:
    """Public entry point for config validation.

    Both load_config and CLI argument overrides (--domain-suffix etc.) must
    pass through it: domain_suffix goes straight into the Caddyfile site
    address and is also written into DNS config under /etc -- metacharacters
    (newlines, braces) could forge extra config blocks; it also determines
    machine-wide resolution routing, so the TLD must be on the whitelist
    (RESERVED_LOCAL_TLDS) unless the user explicitly allows it in config.toml.

    Value-level validation: the defaults are always valid, so it is enough to
    run it once over the entire effective config.
    """
    if not _valid_label(cfg.domain_suffix):
        raise _bad(
            "domain_suffix",
            f"must be a single DNS label (starts and ends with a lowercase "
            f"letter or digit, may contain hyphens in the middle, no dots, "
            f"≤{MAX_LABEL_LEN} characters), current value {cfg.domain_suffix!r}",
        )
    allowed = _check_allowed_domain_suffixes(cfg)
    if cfg.domain_suffix not in RESERVED_LOCAL_TLDS and cfg.domain_suffix not in allowed:
        raise _bad("domain_suffix", _domain_suffix_rejection(cfg.domain_suffix))
    if cfg.dns_backend not in DNS_BACKENDS:
        raise _bad(
            "dns_backend",
            f"must be one of {' | '.join(DNS_BACKENDS)}, current value {cfg.dns_backend!r}",
        )
    for key in ("http_port", "https_port"):
        value = getattr(cfg, key)
        if not _is_int(value) or not PORT_MIN <= value <= PORT_MAX:
            raise _bad(
                key, f"must be an integer in {PORT_MIN}..{PORT_MAX}, current value {value!r}"
            )
    if not _is_int(cfg.ready_timeout) or not (
        READY_TIMEOUT_MIN <= cfg.ready_timeout <= READY_TIMEOUT_MAX
    ):
        raise _bad(
            "ready_timeout",
            f"must be an integer in {READY_TIMEOUT_MIN}..{READY_TIMEOUT_MAX} (seconds), "
            f"current value {cfg.ready_timeout!r}",
        )
    if not _valid_admin_addr(cfg.admin_addr):
        raise _bad(
            "admin_addr",
            f"must be host:port (e.g. 127.0.0.1:2019), current value {cfg.admin_addr!r}",
        )
    for key in ("storage", "caddy_bin"):
        if not isinstance(getattr(cfg, key), str):
            raise _bad(key, f"must be a string path, current value {getattr(cfg, key)!r}")
