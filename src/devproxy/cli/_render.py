"""Terminal rendering: status tables, hint texts, and result summaries.

Rendering only consumes structured data (Status / ComponentReport). It parses no
configuration and decides no exit codes.
"""

from __future__ import annotations

import typer
from rich.markup import escape
from rich.table import Table

from devproxy.cli._runtime import EXIT_FAIL
from devproxy.cli._runtime import EXIT_SUDO
from devproxy.cli._runtime import console
from devproxy.cli._runtime import err_console

TRUST_HINT_ROUTE = (
    "Hint: the CA root certificate has been generated but is not trusted yet -- curl and "
    "Chrome will report certificate errors right now. Run devproxy trust so they accept it."
)

UP_EXAMPLE = "devproxy up demo -- sh -c 'python3 -m http.server $PORT'"


def _print_trust_hint_if_needed(paths, config) -> None:
    """Show the hint once, when the CA exists but is not trusted yet.

    The CA is only created when the **first route is registered** -- right after the
    first up/expose, curl and Chrome will not trust the certificate. Without saying so,
    the user just sees "the browser says this is unsafe" and has no idea that
    `devproxy trust` is still missing.

    When the CA does not exist yet (nothing to trust) or is already trusted, stay
    quiet -- otherwise the hint shows up on every run and turns into noise.

    Components and the platform are looked up through `_shared` at call time so test
    doubles take effect.
    """
    from devproxy.cli import _shared as s

    comp = s.CertComponent(s._platform(), paths, config)
    try:
        if comp.fully_trusted():
            return
    except Exception:  # noqa: BLE001 -- a failed hint must not disturb the main flow
        return
    if not comp.ca_path.exists():
        return
    console.print(TRUST_HINT_ROUTE)


def _has_notice(report) -> bool:
    """True when the component is ok but its detail is a "not there yet" notice.

    The criterion comes from the component's own structured state (whether the CA root
    certificate exists), never from the detail text -- prose changes, and one reworded
    sentence would silently change control flow.
    """
    status = report.status
    if not status.detail:
        return False
    component = getattr(report, "component", None)
    ca_path = getattr(component, "ca_path", None)
    if ca_path is None:
        return False
    try:
        return not ca_path.exists()
    except OSError:
        return False


def _status_cell(report) -> str:
    """The "current state" column of the table.

    When the component is ok but the detail is a notice (such as "no CA root certificate
    yet") it must be shown verbatim -- printing only "✔ ready" would hide the crucial
    "you still need devproxy trust" part.
    """
    status = report.status
    if not status.ok:
        return "✗ " + escape(status.detail)
    if _has_notice(report):
        return "✔ " + escape(status.detail)
    return "✔ ready"


def render_plan(plan) -> None:
    """Print the change-plan table."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("Component")
    table.add_column("Current")
    table.add_column("Will do")
    for r in plan.reports:
        table.add_row(
            r.name,
            _status_cell(r),
            "\n".join(escape(a.description) for a in r.actions) or "—",
        )
    console.print(table)


def print_scope_notes(plan) -> None:
    """Print the machine-wide scope disclosure (it does not fit in the table)."""
    for note in _global_scope_notes(plan):
        console.print(f"[yellow]{escape(note)}[/yellow]")


def _global_scope_notes(plan) -> list[str]:
    """Disclosure texts for machine-wide scope.

    The change-plan table cannot hold sentences like these (Rich truncates them), yet
    they are exactly what the user most needs to see: `DNS=` changes the resolution
    path of the **whole machine**. Components supply the text through the optional
    `global_scope_note()` -- no method means the component has no machine-wide effect.

    Disclose whenever the machine-wide effect really holds -- at that moment the machine
    is in a state where all resolution goes through the user-scope dnsmasq, and the user
    is entitled to know. Two situations count: this run writes it (r.actions is
    non-empty), or it was already written (the component's status is fine).

    A probe failure in one component must not take setup down (build_plan promises the
    same isolation): log one warning and continue. Better a missing disclosure than a
    traceback before the user has confirmed anything.
    """
    notes = []
    for r in plan.reports:
        fn = getattr(r.component, "global_scope_note", None)
        if not callable(fn):
            continue
        if not r.actions and not r.status.ok:
            # Neither changing it nor installed -- cannot claim resolution goes through dnsmasq.
            continue
        try:
            note = fn()
        except Exception as e:  # noqa: BLE001 -- a failed disclosure does not block setup
            err_console.print(
                f"devproxy: cannot get the machine-wide scope note for "
                f"{escape(r.name)}: {escape(str(e))}"
            )
            continue
        if note:
            notes.append(note)
    return notes


def component_note(component, method: str) -> str | None:
    """A note a component provides through an optional method; absent or raising means none.

    Capabilities are probed with getattr and stay out of the platform protocol -- the
    same pattern as `global_scope_note`. A note is only a hint, so a failed probe must
    never take down doctor or trust.
    """
    fn = getattr(component, method, None)
    if not callable(fn):
        return None
    try:
        note = fn()
    except Exception:  # noqa: BLE001 -- a failed hint must not disturb the main flow
        return None
    return note or None


def firefox_notes(reports) -> list[str]:
    """Browser-trust notes supplied by components (Firefox keeps its own certificate store).

    Only the certificate component provides one -- other components simply lack the
    method, which means they have no note.
    """
    notes = []
    for r in reports:
        note = component_note(getattr(r, "component", None), "firefox_note")
        if note:
            notes.append(note)
    return notes


def print_noop_unhealthy(reports) -> None:
    """No actions to take, but components are unhealthy: report each reason and its fix."""
    for r in reports:
        err_console.print(f"devproxy: {r.name}: {escape(r.status.detail)}")
        if r.status.hint:
            err_console.print(f"    Fix: {escape(r.status.hint)}")


def print_apply_results(results) -> bool:
    """Print the per-component results; return whether everything succeeded."""
    ok = True
    for name, status in results:
        mark = "✔" if status.ok else "✗"
        console.print(f"{mark} {name}: {escape(status.detail)}")
        if not status.ok:
            ok = False
            if status.hint:
                console.print(f"    Fix: {escape(status.hint)}")
    return ok


def fail_for_apply_errors(errors: list[tuple[str, PermissionError]]) -> None:
    """Pick the exit code when the apply phase failed.

    `apply_plan` collapses exception types into Status (one component may fail without
    blocking the rest), so an elevation failure can only be recorded by the caller
    around apply. A PermissionError means "elevation needed" (exit 4); anything else is
    an ordinary runtime failure (exit 1).
    """
    if not errors:
        raise typer.Exit(EXIT_FAIL)
    err_console.print("devproxy: elevation failed, the changes did not complete:")
    for name, exc in errors:
        err_console.print(f"  · {name}: {escape(str(exc))}")
    err_console.print(
        "Fix: make sure the current user can run sudo (and do not run devproxy with "
        "sudo/root), then run devproxy setup again."
    )
    raise typer.Exit(EXIT_SUDO)


def _print_notes(notes: list[str]) -> None:
    """Print each note collected during the migration."""
    for note in notes:
        console.print(f"  · {note}")


def render_uninstall_plan(plan, *, dry_run: bool) -> None:
    """Print the uninstall plan. In dry-run mode say plainly that it is a preview."""
    if dry_run:
        console.print(
            "[bold]The following uninstall steps would run (preview; nothing is changed):[/bold]"
        )
    else:
        console.print("[bold]The following uninstall steps will run:[/bold]")
    if plan.actions:
        for index, action in enumerate(plan.actions, 1):
            sudo = " (needs sudo)" if action.privileged else ""
            console.print(f"  {index}. {escape(action.description)}{sudo}")
    else:
        console.print("  (no steps to run)")
    for note in plan.notes:
        console.print(f"  [yellow]⚠ {escape(note)}[/yellow]")


def print_uninstall_results(results) -> bool:
    """Print ✔/✗ per step; return whether everything succeeded."""
    ok = True
    for result in results:
        mark = "✔" if result.ok else "✗"
        line = f"{mark} {escape(result.description)}"
        if result.detail:
            line += f": {escape(result.detail)}"
        console.print(line)
        if not result.ok:
            ok = False
    return ok


def print_uninstall_kept(plan, *, dry_run: bool = False) -> None:
    """Print the "left untouched by default" list and the migration rollback hint."""
    if plan.kept:
        heading = (
            "The following is kept (nothing is deleted):"
            if dry_run
            else "The following was kept (not deleted):"
        )
        console.print(heading)
        for item in plan.kept:
            console.print(f"  · {escape(item)}")
    if plan.migration_backup is not None:
        path = escape(str(plan.migration_backup))
        console.print(
            f"The original caddy.service and Caddyfile are in {path}; "
            f"restore them with devproxy setup --rollback {path}"
        )
