"""Foreground supervision of Caddy: start the process, take over signals, and
clean up.

Division of labor:
- `setup` installs a **background service** (systemd / launchd, starts on boot);
- `selftest` starts an **isolated instance** (temporary ports, temporary
  storage, removed when done);
- this module runs the **live config** (the main Caddyfile), and does so
  **in the foreground** -- the user wants live logs and "Ctrl-C removes it,
  leaving no trace".

The point of foreground mode is stdio passed straight to the terminal: no
redirection, so Caddy's logs appear live in the terminal (the
redirect-to-file approach used by `selftest` is counterproductive here).

Signal model: the child starts with `start_new_session=True` -- it forms its
own session and process group, so Ctrl-C in the terminal does **not** reach
Caddy, only devproxy. The shutdown pace is therefore ours to decide: SIGTERM
lets Caddy exit gracefully (finishing its autosave), and only on timeout do
we escalate to SIGKILL.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Exit codes match cli/_runtime.py; the gateway layer must not depend on the
# CLI layer.
EXIT_OK = 0
EXIT_FAIL = 1

# Time (seconds) given to Caddy to exit gracefully after SIGTERM; only on
# timeout do we escalate to SIGKILL.
GRACEFUL_TIMEOUT = 10.0

# Final deadline when it still does not exit after SIGKILL (process stuck in
# D state and similar extremes).
KILL_GRACE = 2.0

# Poll interval: frequent polling is needed to notice "Caddy exited" and
# "graceful exit timed out" promptly.
POLL_INTERVAL = 0.2


class _Managed:
    """The supervised Caddy process and its signal state."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.signum: int | None = None
        self._term_sent = False

    def attach(self, proc: subprocess.Popen) -> None:
        """Attach the child process. A signal may arrive in the window between
        "handlers installed" and "process started" -- forward it now so Caddy
        does not miss this shutdown."""
        self.proc = proc
        if self.signum is not None:
            self.request_stop()

    def request_stop(self) -> None:
        """Ask Caddy to exit gracefully: send one SIGTERM to the **process
        group** (only once)."""
        if self._term_sent or self.proc is None:
            return
        self._term_sent = True
        self._signal_group(signal.SIGTERM)

    def force_stop(self) -> None:
        """Graceful exit timed out -> SIGKILL the whole process group (leaving
        no grandchildren either)."""
        self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig: int) -> None:
        # start_new_session=True -> the child is the session and process group
        # leader, so pgid == pid. We only get here while the child has not
        # been reaped (after reaping the pid may be reused), so signaling the
        # group cannot hit an unrelated process group.
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _handler(managed: _Managed):
    """Signal handler: record the signal and ask Caddy to exit gracefully.

    Deliberately does **not** raise -- raising would interrupt the wait loop
    below and leave no time to clean up.
    """

    def on_signal(signum, frame):
        managed.signum = signum
        managed.request_stop()

    return on_signal


def _install_handlers(managed: _Managed) -> dict[int, object]:
    """Take over SIGINT / SIGTERM / SIGHUP.

    A terminal Ctrl-C is sent only to the foreground process group (devproxy
    itself), and Caddy in its own session does not receive it -- so the
    shutdown pace is entirely under our control.
    """
    old: dict[int, object] = {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            old[sig] = signal.signal(sig, _handler(managed))
        except ValueError:
            # Signal handlers cannot be installed from a non-main thread (the
            # CLI won't hit this; embedding/tests might)
            pass
    return old


def _restore_handlers(old: dict[int, object]) -> None:
    for sig, handler in old.items():
        try:
            signal.signal(sig, handler)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            pass


def _supervise(managed: _Managed, graceful_timeout: float) -> int | None:
    """Wait for Caddy to exit; on a signal, shut down gracefully and escalate
    to SIGKILL on timeout.

    Return Caddy's exit code; None means "even SIGKILL could not make it exit".
    """
    proc = managed.proc
    if proc is None:  # pragma: no cover -- only called after attach
        return None
    deadline: float | None = None
    killed = False
    while True:
        try:
            return proc.wait(timeout=POLL_INTERVAL)
        except subprocess.TimeoutExpired:
            pass
        if managed.signum is None:
            continue
        now = time.monotonic()
        if deadline is None:
            # Signal just received: give Caddy time for a graceful exit (it
            # needs to write autosave)
            deadline = now + graceful_timeout
            continue
        if now < deadline:
            continue
        if killed:
            # Still not exiting after SIGKILL (stuck in D state and similar
            # extremes): waiting longer will not help, and the terminal must
            # not be stuck forever.
            return None
        killed = True
        managed.force_stop()
        deadline = now + KILL_GRACE


def _ensure_gone(managed: _Managed) -> None:
    """Fallback: Caddy must never be left running in the background.

    On the normal path it has already exited; this only guards the
    exceptional paths (an exception was raised, or we did not wait for exit)
    -- foreground mode promises "Ctrl-C removes it, leaving no trace", and a
    stray process would break that promise.
    """
    proc = managed.proc
    if proc is None or proc.poll() is not None:
        return
    managed.force_stop()
    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass


def _describe_exit(rc: int) -> str:
    """Describe the child's exit status in plain words."""
    if rc < 0:
        return f"terminated by signal {-rc}"
    return f"exit code {rc}"


def run_foreground(
    caddy_bin: Path,
    caddyfile: Path,
    graceful_timeout: float = GRACEFUL_TIMEOUT,
) -> int:
    """Run `caddy run --config <caddyfile>` in the foreground and return
    devproxy's exit code.

    - 0: Caddy was shut down by Ctrl-C (graceful exit, state written)
    - 1: Caddy exited on its own (its exit code was reported), or failed to
      start

    Nothing on the system is written: no services installed, no config
    changed, no route table touched.
    """
    managed = _Managed()
    old = _install_handlers(managed)
    try:
        argv = [str(caddy_bin), "run", "--config", str(caddyfile)]
        try:
            # Do not redirect stdio: inherit the terminal -- that is the point
            # of foreground mode. start_new_session=True: Caddy forms its own
            # process group, so Ctrl-C does not reach it directly.
            proc = subprocess.Popen(argv, start_new_session=True)
        except OSError as e:
            # Missing file / not executable / bad format (Exec format error)
            # all land here
            print(f"devproxy: cannot start caddy: {e}", file=sys.stderr)
            return EXIT_FAIL
        managed.attach(proc)
        rc = _supervise(managed, graceful_timeout)
    finally:
        _ensure_gone(managed)
        _restore_handlers(old)

    if rc is None:
        # Even SIGKILL could not make it exit -- must not be treated as
        # "shut down normally"
        print("devproxy: caddy did not exit, gave up waiting", file=sys.stderr)
        return EXIT_FAIL
    if managed.signum is not None:
        # Stopped by the user: Caddy has exited (autosave finished during
        # graceful exit)
        return EXIT_OK
    print(f"devproxy: caddy exited ({_describe_exit(rc)})", file=sys.stderr)
    return EXIT_FAIL
