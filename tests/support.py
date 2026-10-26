"""Small helpers shared by the tests: simulate paths a regular user cannot read.

macOS's system-scope unit lives in /Library/LaunchDaemons -- when the permissions or
ownership are wrong, a regular user cannot read it. The tool must be able to read such
files itself (through the elevated session); otherwise "cannot read" is misread as
"no unit", which makes doctor report red forever and lets the migration silently take
over an environment the user maintains by hand.
"""

import time
from pathlib import Path


def wait_until(predicate, timeout=30.0, interval=0.02) -> bool:
    """Poll until the predicate holds; return False on timeout.

        "Sleep N seconds, then assert" flakes at random on a loaded machine: having slept
    long enough does not mean the other side really acted (the child process may not be up
    yet, the daemon thread may not have reached its first round). This waits **until it
    happens**, with an explicit timeout -- the normal path returns immediately, only the
    abnormal path waits it out (tests get slower, but never lie).
    """
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def wait_for_file(path, timeout=30.0) -> bool:
    """Wait for a file to appear (a child writing a marker file, and so on)."""
    return wait_until(lambda: Path(path).exists(), timeout=timeout)


class UnreadablePath(Path):
    """Behaves like a 0600 root file: read_text is refused outright for a regular user."""

    if hasattr(type(Path()), "_flavour"):  # path subclasses on Python 3.11 need it
        _flavour = type(Path())._flavour

    def read_text(self, *args, **kwargs):  # noqa: D102 -- overridden on purpose
        raise PermissionError(13, "Permission denied", str(self))


class ElevatedReader:
    """An elevated session: it can read files a regular user cannot (like sudo cat)."""

    def __init__(self, platform=None):
        self.platform = platform

    def run(self, argv):
        return 0, ""

    def read(self, path):
        return Path(str(path)).read_text(encoding="utf-8")

    def write(self, path, content):
        target = Path(str(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def copy(self, src, dest):
        target = Path(str(dest))
        target.write_text(Path(str(src)).read_text(encoding="utf-8"), encoding="utf-8")
