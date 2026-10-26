"""setup orchestration: check -> plan -> apply -> verify.

The component layer contains no orchestration logic; this layer contains no
component-specific knowledge.
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
    """Call check() for a single component, isolating exceptions. Returns (report, whether check ran successfully).

    doctor only needs the report; setup also needs to know whether check threw
    (if it did, plan is skipped).
    """
    name = getattr(component, "name", "?")
    try:
        status = component.check()
    except Exception as e:  # noqa: BLE001 -- one component failing must not block the others
        return (
            ComponentReport(
                name=name,
                status=Status(ok=False, detail=f"check failed: {e}", hint="see the error above"),
                actions=[],
                component=component,
            ),
            False,
        )
    return ComponentReport(name=name, status=status, component=component), True


def build_plan(components: list[Component]) -> SetupPlan:
    """Get check() and plan() for each component. An empty plan means the component needs no changes.

    An exception from a single component's check()/plan() must not take down the
    whole setup -- record it as a failure for that component and keep collecting
    the others.
    """
    reports = []
    for c in components:
        report, checked = check_component(c)
        if not checked:
            reports.append(report)
            continue
        try:
            actions = c.plan()
        except Exception as e:  # noqa: BLE001 -- same as above
            actions = []
            report.status = Status(
                ok=False, detail=f"failed to compute changes: {e}", hint="see the error above"
            )
        report.actions = actions
        reports.append(report)
    return SetupPlan(reports=reports)


def _verify(component: Component, fallback: Status) -> Status:
    """Get the component's verify(); components without that method fall back to check()."""
    verify = getattr(component, "verify", None)
    if callable(verify):
        return verify()
    try:
        return component.check()
    except Exception:  # noqa: BLE001 -- verification failure must not interrupt setup
        return fallback


def apply_plan(plan: SetupPlan) -> list[tuple[str, Status]]:
    """Execute component by component; a single component's failure does not block the others.

    When check/plan already failed and there are no actions, verify() must not
    overwrite the failure with success -- otherwise setup would print ✔ and exit
    0 even though the component did nothing.
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
                # Failed already at plan time (or unhealthy with nothing to do) -- keep the failure
                results.append((report.name, report.status))
                continue
            results.append((report.name, _verify(component, report.status)))
        except Exception as e:  # noqa: BLE001 -- component failure must not interrupt setup
            results.append(
                (report.name, Status(ok=False, detail=str(e), hint="see the error above"))
            )
    return results
