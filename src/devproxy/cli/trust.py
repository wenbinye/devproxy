from __future__ import annotations

import typer
from rich.markup import escape

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_SUDO


def _print_firefox_note(comp) -> None:
    """Remind the user that Firefox needs the CA trusted separately."""
    note = s.component_note(comp, "firefox_note")
    if note:
        s.console.print(f"[yellow]⚠ {escape(note)}[/yellow]")


@app.command()
def trust(
    yes: bool = typer.Option(False, "--yes", help="Non-interactive mode; do not ask"),
) -> None:
    """Install the CA root certificate into the system trust store and the NSS DB."""
    paths, config, _, _ = s.build_context()
    comp = s.CertComponent(s._platform(), paths, config)
    actions = comp.plan()
    if not actions:
        if not comp.ca_path.exists():
            s.console.print(
                "No CA root certificate yet (Caddy creates it when the first route is "
                "registered); nothing to do."
            )
        else:
            s.console.print("The CA root certificate is already trusted; nothing to do.")
        # Even when it is already trusted, Firefox alone may still reject it -- remind anyway
        _print_firefox_note(comp)
        raise typer.Exit(EXIT_OK)
    # Always go through _shared._interactive() so patching it in tests covers every command
    if any(a.privileged for a in actions) and not yes and not s._interactive():
        s.err_console.print(
            "Non-interactive environment without --yes; refusing to run steps that need elevation."
        )
        raise typer.Exit(EXIT_SUDO)
    try:
        comp.apply(actions)
    except PermissionError as e:
        s.err_console.print(f"devproxy: {escape(str(e))}")
        raise typer.Exit(EXIT_SUDO) from e
    except RuntimeError as e:
        # The component's documented contract is PermissionError / RuntimeError only;
        # anything else is a bug in this program and must not be dressed up as an ops error
        s.fail(str(e))
    # Cleanup-only runs must not say "installed" -- that is a different thing, and the
    # user would think the CA had been reinstalled
    commands = {a.command[0] for a in actions if a.command}
    if commands <= {"remove-stale-nss-ca"}:
        s.console.print(
            "Removed the stale CA root certificate from the NSS DB "
            "(Chrome picks it up only after a full restart)"
        )
    else:
        s.console.print(
            "Installed the CA root certificate (Chrome picks it up only after a full restart)"
        )
    # Firefox keeps its own certificate store -- trusting it in the system store and the
    # NSS DB has **no effect** on it. Only mention this on machines that have Firefox, so
    # people without it get no noise.
    _print_firefox_note(comp)
