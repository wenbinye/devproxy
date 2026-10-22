"""doctor：体检报告。只读，不改任何东西。"""

from __future__ import annotations

from devproxy.components.base import Component
from devproxy.setup import ComponentReport


def build_report(components: list[Component]) -> list[ComponentReport]:
    reports = []
    for c in components:
        reports.append(ComponentReport(name=c.name, status=c.check(), component=c))
    return reports


def report_is_healthy(reports: list[ComponentReport]) -> bool:
    return all(r.status.ok for r in reports)
