"""doctor：体检报告。只读，不改任何东西。"""

from __future__ import annotations

from devproxy.components.base import Component
from devproxy.setup import ComponentReport, check_component


def build_report(components: list[Component]) -> list[ComponentReport]:
    """逐组件 check()，复用 setup 的异常隔离 —— 一个组件炸了也要列完其余。"""
    return [check_component(c)[0] for c in components]


def report_is_healthy(reports: list[ComponentReport]) -> bool:
    return all(r.status.ok for r in reports)
