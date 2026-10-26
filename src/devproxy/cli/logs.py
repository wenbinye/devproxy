from __future__ import annotations

import os
from pathlib import Path
from shutil import which

import typer
from rich.markup import escape
from rich.text import Text

from devproxy.cli import _completion
from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_SUDO
from devproxy.cli._shared import EXIT_USAGE
from devproxy.core.logs import follow_log
from devproxy.core.logs import follow_many
from devproxy.gateway.project import ProjectFileError
from devproxy.gateway.project import find_project_file
from devproxy.gateway.project import load_project_file
from devproxy.gateway.sync import route_log_path

_SERVICE_LOGS = ("caddy", "dnsmasq")


def _route_log_files() -> list[Path]:
    """The route log files that exist (sorted by name)."""
    try:
        paths, _, _, _ = s.build_context()
    except Exception:  # noqa: BLE001 -- unreadable means none; this must not raise
        return []
    log_dir = paths.log_dir / "routes"
    if not log_dir.is_dir():
        return []
    return sorted(log_dir.glob("*.log"))


def _known_route_logs() -> list[str]:
    """The route logs that can be viewed (names only)."""
    return [path.stem for path in _route_log_files()]


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size // 1024} KB"
    return f"{size // (1024 * 1024)} MB"


def _declared_route_names() -> tuple[set[str] | None, Path | None]:
    """Which routes this project (devproxy.toml) declares.

    Returns (name set, project file path). Outside a project it returns (None, None);
    a broken project file returns (None, path) -- the caller shows a note and then
    behaves as if there were no project.
    """
    found = find_project_file()
    if found is None:
        return None, None
    try:
        project = load_project_file(found)
    except ProjectFileError as e:
        s.err_console.print(f"⚠️  {escape(str(e))} -- listing every route instead")
        return None, found
    return {route.name for route in project.routes}, found


def _log_line(path: Path) -> str:
    try:
        size = _format_size(path.stat().st_size)
    except OSError:
        return f"{path.stem:<20} (no log yet)"
    return f"{path.stem:<20} {size:>8}"


_PALETTE = (
    "cyan",
    "magenta",
    "yellow",
    "green",
    "blue",
    "bright_red",
    "bright_cyan",
    "bright_magenta",
    "bright_yellow",
    "bright_green",
)


def _follow_project(project_path: Path, names: list[str]) -> None:
    """Follow the logs of every app in this project. One is tail -f; several get coloured tags."""
    paths, _, _, _ = s.build_context()
    targets: list[tuple[str, Path]] = []
    missing: list[str] = []
    for name in names:
        path = route_log_path(paths, name)
        if path.exists():
            targets.append((name, path))  # the tag is the app name
        else:
            missing.append(name)

    if not targets:
        s.err_console.print(
            f"devproxy: this project declares {', '.join(names)}, but there are no logs yet "
            "-- run devproxy up"
        )
        raise typer.Exit(EXIT_USAGE)
    if missing:
        s.err_console.print(f"   declared but no logs yet (skipped): {', '.join(missing)}")

    try:
        if len(targets) == 1:
            follow_log(targets[0][1])
        else:
            colors = {tag: _PALETTE[i % len(_PALETTE)] for i, (tag, _) in enumerate(targets)}
            width = max(len(tag) for tag, _ in targets)
            s.console.print("Following (Ctrl-C to exit):")
            for tag, _ in targets:
                # The name must be escaped: rich parses anything in brackets as markup.
                # No path -- long paths wrap, and the name is the identity
                s.console.print(f"  {escape(f'[{tag}]')}", style=colors[tag])
            for tag, line in follow_many(targets):
                text = Text()
                # Pad the name to one width so consecutive lines line up vertically
                text.append(f"[{tag}]".ljust(width + 2) + " ", style=colors[tag])
                text.append(line.rstrip("\n"))
                s.console.print(text)
    except KeyboardInterrupt:
        # Ctrl-C is a normal way to exit, not an error
        raise typer.Exit(0) from None


def _print_one(service: str) -> None:
    """`logs <name> --list`: show the information for this one target."""
    if service in _SERVICE_LOGS:
        s.console.print(f"Service log: {service} (follow it with devproxy logs {service})")
        return
    known = _known_route_logs()
    paths, _, _, _ = s.build_context()
    path = route_log_path(paths, service)
    if service in known:
        s.console.print(f"Route log: {_log_line(path)}")
        s.console.print(f"  Full path: {path}")
        s.console.print(f"  Follow: devproxy logs {service}")
        return
    s.err_console.print(
        f"devproxy: there is no log named {service} "
        f"(choose from: {' / '.join(_SERVICE_LOGS)}, or a route name)"
    )
    if known:
        s.err_console.print(f"   available route logs: {', '.join(known)}")
    raise typer.Exit(EXIT_USAGE)


# Candidates when EDITOR is unset: probe PATH in order and use the first one present
_EDITOR_CANDIDATES = ("vim", "vi", "nano")


def _resolve_editor() -> str:
    """Which editor to use: $EDITOR first, then $VISUAL, then look for vim / vi / nano.

    The candidates are **probed** (which), never forced -- forcing an editor the user's
    machine does not have just earns a "command not found". Only when all three are
    missing does this error out with exit 2.
    """
    editor = (os.environ.get("EDITOR") or os.environ.get("VISUAL") or "").strip()
    if editor:
        return editor
    for candidate in _EDITOR_CANDIDATES:
        if which(candidate):
            return candidate
    s.err_console.print(
        "devproxy: do not know which editor to open the log with -- neither EDITOR nor "
        "VISUAL is set and vim / vi / nano are not on PATH.\n"
        "   e.g.: export EDITOR=vim   then rerun devproxy logs <name> --edit"
    )
    raise typer.Exit(EXIT_USAGE)


def _edit_log(service: str) -> None:
    """`logs <name> --edit`: open the log file with $EDITOR."""
    if service in _SERVICE_LOGS:
        s.err_console.print(
            f"devproxy: {service} is a service log rather than a file, so an editor cannot "
            f"open it (use devproxy logs {service} to follow it)"
        )
        raise typer.Exit(EXIT_USAGE)

    known = _known_route_logs()
    if service not in known:
        s.err_console.print(
            f"devproxy: there is no log named {service} "
            f"(choose from: {' / '.join(_SERVICE_LOGS)}, or a route name)"
        )
        if known:
            s.err_console.print(f"   available route logs: {', '.join(known)}")
        raise typer.Exit(EXIT_USAGE)

    editor = _resolve_editor()
    paths, _, _, _ = s.build_context()
    path = route_log_path(paths, service)
    try:
        code = s._platform().open_in_editor(path, editor)
    except FileNotFoundError as e:
        s.err_console.print(f"devproxy: cannot open the editor {editor}: {escape(str(e))}")
        raise typer.Exit(EXIT_FAIL) from e
    except OSError as e:
        s.err_console.print(f"devproxy: failed to open the log: {escape(str(e))}")
        raise typer.Exit(EXIT_FAIL) from e
    if code != 0:
        s.err_console.print(f"devproxy: the editor {editor} exited with code {code}")
        raise typer.Exit(EXIT_FAIL)


def _print_list(project_names: set[str] | None, project_path: Path | None) -> None:
    """List the log targets that can be followed.

    Inside a project (a devproxy.toml exists) only the routes **this project declares**
    are listed -- the machine's route table is global, and other projects' logs only
    get in the way.
    """
    in_project = project_names is not None
    s.console.print("Service logs:")
    for name in _SERVICE_LOGS:
        s.console.print(f"  {name}")

    files = _route_log_files()
    if project_names is not None:
        files = [f for f in files if f.stem in project_names]

    if not files:
        if in_project:
            s.console.print(
                "Route logs: (this project declares "
                f"{', '.join(sorted(project_names or ()))}, but there are no logs yet "
                "-- run devproxy up)"
            )
        else:
            s.console.print("Route logs: (none -- run devproxy up and they will appear)")
        s.console.print("")
        s.console.print("Follow one with devproxy logs <name>.")
        return

    # The heading carries no long path (Rich wraps at 80 columns and the names would no
    # longer line up with the sizes); each log's full path goes on its own line -- a wrap
    # there does not disturb the alignment above, and users who want to copy the path
    # (into an editor, tail, grep) need not assemble it themselves
    if in_project:
        s.console.print(f"This project (devproxy.toml: {project_path}):", soft_wrap=True)
    else:
        s.console.print("Route logs:")
    for path in files:
        try:
            size = _format_size(path.stat().st_size)
        except OSError:
            size = "?"
        s.console.print(f"  {path.stem:<20} {size:>8}")
        # soft_wrap: do not wrap. A path split across two lines cannot be copied (the
        # terminal soft-wraps on its own, but the stream still holds one line)
        s.console.print(f"    {path}", soft_wrap=True)

    # Also mention routes this project declares that have never run (no log file yet)
    if in_project:
        have = {f.stem for f in files}
        missing = sorted((project_names or set()) - have)
        if missing:
            s.console.print(f"  declared but no logs yet: {', '.join(missing)} (run devproxy up)")

    s.console.print("")
    s.console.print("Follow one with devproxy logs <name>.")


@app.command()
def logs(
    service: str = typer.Argument(
        None,
        help="caddy|dnsmasq|<route name> (caddy by default)",
        autocompletion=_completion.log_targets,
    ),
    list_all: bool = typer.Option(
        False,
        "--list",
        help="List the log targets that can be followed (only this project's when inside one); do not follow",
    ),
    edit: bool = typer.Option(
        False,
        "--edit",
        "-e",
        help="Open the log file with $EDITOR (route logs only; falls back to vim / vi / nano "
        "when EDITOR is unset)",
    ),
) -> None:
    """Follow logs: a service (caddy / dnsmasq) or a route (an app started by up)."""
    if list_all and edit:
        s.err_console.print("devproxy: --list and --edit cannot be used together")
        raise typer.Exit(EXIT_USAGE)

    if list_all:
        if service:
            _print_one(service)
            raise typer.Exit(0)
        project_names, project_path = _declared_route_names()
        _print_list(project_names, project_path)
        raise typer.Exit(0)

    if edit:
        if not service:
            s.err_console.print(
                "devproxy: --edit needs to know which log to open (devproxy logs <name> --edit)"
            )
            raise typer.Exit(EXIT_USAGE)
        _edit_log(service)
        raise typer.Exit(0)

    if service is None:
        # Bare `devproxy logs`: inside a project follow **every app** in it; outside one,
        # follow caddy (the old behaviour)
        found = find_project_file()
        if found is not None:
            try:
                project = load_project_file(found)
            except ProjectFileError as e:
                s.err_console.print(f"⚠️  {escape(str(e))} -- following caddy instead")
            else:
                names = [route.name for route in project.routes]
                _follow_project(found, names)
                raise typer.Exit(0)
        service = "caddy"

    if service in _SERVICE_LOGS:
        try:
            # Read-only command: skip build_context (it loads the config and resolves the
            # storage archive)
            s._platform().tail_service_log(service)
        except PermissionError as e:
            s.err_console.print(f"devproxy: {escape(str(e))}")
            raise typer.Exit(EXIT_SUDO) from e
        except OSError as e:
            # journalctl missing, etc.: fail cleanly, never leak a FileNotFoundError as a
            # traceback
            s.err_console.print(f"devproxy: cannot read the {service} log: {escape(str(e))}")
            raise typer.Exit(EXIT_FAIL) from e
        return

    # Route logs: ~/.local/state/devproxy/logs/routes/<name>.log
    known = _known_route_logs()
    if service not in known:
        # A usage error exits 2 by global convention, not the runtime-failure 1
        s.err_console.print(
            f"devproxy: unknown service {service} "
            f"(choose from: {' / '.join(_SERVICE_LOGS)}, or a route name)"
        )
        if known:
            s.err_console.print(f"   available route logs: {', '.join(known)}")
        else:
            s.err_console.print(
                "   (no route logs right now -- run devproxy up and they will appear)"
            )
        raise typer.Exit(EXIT_USAGE)

    paths, _, _, _ = s.build_context()
    path = route_log_path(paths, service)
    try:
        follow_log(path)
    except KeyboardInterrupt:
        raise typer.Exit(0) from None
    except FileNotFoundError:
        s.err_console.print(f"devproxy: log file not found: {path}")
        raise typer.Exit(EXIT_FAIL) from None
