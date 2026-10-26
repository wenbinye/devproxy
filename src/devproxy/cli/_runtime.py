"""CLI runtime: the Rich consoles, exit codes, and the single error exit.

These are process-level facilities shared by **every** command module. They know
nothing about configuration, components, or rendering.
"""

from __future__ import annotations

import sys
from typing import NoReturn

import typer
from rich.console import Console
from rich.markup import escape

console = Console()

err_console = Console(stderr=True)

# Exit-code semantics (the same everywhere in this project):
#   0 success / 1 runtime failure / 2 usage error / 3 precondition unmet
#   4 needs a human (interactive prompt or elevation)
EXIT_OK = 0

EXIT_FAIL = 1

EXIT_USAGE = 2

EXIT_PRECONDITION = 3

EXIT_SUDO = 4

EXIT_DECLINED = 4


def fail(msg: str, code: int = EXIT_FAIL) -> NoReturn:
    """Print an error in the unified shape and exit (one Rich line, no traceback).

    Annotated NoReturn rather than None: it always raises typer.Exit, and type
    checkers use that to learn "nothing after this line runs" — otherwise every
    line following an `s.fail(...)` would be reported as a possibly-unbound
    variable.
    """
    # escape: error messages may contain character classes like [a-z0-9-] or user
    # input; without escaping, Rich would swallow them as markup
    err_console.print(f"devproxy: {escape(msg)}")
    raise typer.Exit(code)


def _interactive() -> bool:
    """Whether there is an interactive terminal.

    Command modules must look this up at call time through
    `_shared._interactive()`, so that patching `_shared._interactive` in tests
    covers every branch.
    """
    return sys.stdin.isatty()
