"""doctor：体检报告。只读，不改任何东西。"""

from __future__ import annotations

from pathlib import Path

from devproxy.components.base import Component
from devproxy.lifecycle.migrate import wildcard_addresses
from devproxy.lifecycle.setup import ComponentReport
from devproxy.lifecycle.setup import check_component


def build_report(components: list[Component]) -> list[ComponentReport]:
    """逐组件 check()，复用 setup 的异常隔离 —— 一个组件炸了也要列完其余。"""
    return [check_component(c)[0] for c in components]


def report_is_healthy(reports: list[ComponentReport]) -> bool:
    return all(r.status.ok for r in reports)


def find_wildcard_sites(
    paths, config, legacy_config: Path | None = None
) -> list[tuple[Path, list[str]]]:
    """仍在生效的 Caddy 配置里的通配站点块。返回 [(文件, 地址列表)]。

    工具的设计前提是"精确站点块、没有通配"：通配块（如 *.test）与精确
    块同后缀时，Caddy 的 TLS 握手会整体失败（实测过）。所以现役配置里
    还有通配块就必须报出来 —— 迁移正是移除它的动作。

    扫描范围：工具自己的主 Caddyfile 与它 import 的两个文件，以及迁移
    之前仍然生效的遗留 Caddyfile。
    """
    targets: list[Path] = []
    if legacy_config is not None:
        targets.append(Path(legacy_config))
    targets.extend([paths.caddyfile, paths.routes_caddy, paths.sites_caddy])
    found: list[tuple[Path, list[str]]] = []
    seen: set[Path] = set()
    for path in targets:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        addresses = wildcard_addresses(text, config.domain_suffix)
        if addresses:
            found.append((path, addresses))
    return found
