"""setup 编排：check → plan → apply → verify。

组件层不含任何编排逻辑；这里也不含任何具体组件的知识。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from devproxy.components.base import Component
from devproxy.platform.base import Action
from devproxy.platform.base import Status


@dataclass
class ComponentReport:
    name: str
    status: Status
    actions: list[Action] = field(default_factory=list)
    component: Component | None = None


@dataclass
class SetupPlan:
    reports: list[ComponentReport] = field(default_factory=list)

    @property
    def needs_sudo(self) -> list[Action]:
        return [a for r in self.reports for a in r.actions if a.privileged]

    @property
    def is_noop(self) -> bool:
        return not any(r.actions for r in self.reports)


def check_component(component: Component) -> tuple[ComponentReport, bool]:
    """对单个组件调 check()，隔离异常。返回 (报告, check 是否成功执行)。

    doctor 只需要报告；setup 还要知道 check 是否抛过异常（抛过就不再 plan）。
    """
    name = getattr(component, "name", "?")
    try:
        status = component.check()
    except Exception as e:  # noqa: BLE001 —— 单组件失败不阻断其他组件
        return (
            ComponentReport(
                name=name,
                status=Status(ok=False, detail=f"检查失败: {e}", hint="查看上面的报错"),
                actions=[],
                component=component,
            ),
            False,
        )
    return ComponentReport(name=name, status=status, component=component), True


def build_plan(components: list[Component]) -> SetupPlan:
    """对每个组件取 check() 与 plan()。plan 为空表示该组件无需变更。

    单个组件的 check()/plan() 抛异常不应带崩整个 setup —— 记为该组件失败，
    其余组件照常收集。
    """
    reports = []
    for c in components:
        report, checked = check_component(c)
        if not checked:
            reports.append(report)
            continue
        try:
            actions = c.plan()
        except Exception as e:  # noqa: BLE001 —— 同上
            actions = []
            report.status = Status(ok=False, detail=f"计算变更失败: {e}", hint="查看上面的报错")
        report.actions = actions
        reports.append(report)
    return SetupPlan(reports=reports)


def _verify(component: Component, fallback: Status) -> Status:
    """取组件的 verify()；没有该方法的组件退回 check()。"""
    verify = getattr(component, "verify", None)
    if callable(verify):
        return verify()
    try:
        return component.check()
    except Exception:  # noqa: BLE001 —— 校验失败不应中断 setup
        return fallback


def apply_plan(plan: SetupPlan) -> list[tuple[str, Status]]:
    """逐组件执行；单个组件失败不阻断其他组件。

    check/plan 阶段已失败且没有任何动作时，verify() 不能把失败覆盖成成功 ——
    否则组件什么都没做，setup 也会打印 ✔ 并退出 0。
    """
    results: list[tuple[str, Status]] = []
    for report in plan.reports:
        component = report.component
        if component is None:
            results.append((report.name, report.status))
            continue
        try:
            if report.actions:
                component.apply(report.actions)
            if not report.actions and not report.status.ok:
                # 计划阶段就失败了（或组件不健康却无计可施）—— 保留失败
                results.append((report.name, report.status))
                continue
            results.append((report.name, _verify(component, report.status)))
        except Exception as e:  # noqa: BLE001 —— 组件失败不应中断 setup
            results.append((report.name, Status(ok=False, detail=str(e), hint="查看上面的报错")))
    return results
