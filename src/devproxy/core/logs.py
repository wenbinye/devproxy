"""Logs for background apps: rotating writes and follow mode.

Apps started by `sync` are background processes (separate session); their
output cannot go to the terminal -- devproxy captures it and writes it to
`~/.local/state/devproxy/logs/routes/<name>.log`.

**Why rotate**: during development, apps such as Spring Boot write INFO logs
at MB/hour (measured at ~4.4 MB/hour). Left unmanaged, a log file can eat
hundreds of MB after a few days, and `uninstall` does not clean it up either
(it deletes services and config, not the user's logs).

Rotation happens on the **write side** (not via logrotate) -- these logs are
written by processes devproxy started itself, and only the writer can
guarantee "the file never grows without bound".
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

# Upper bound for a single log file. Rotate once exceeded: rename the current
# file to .1 (overwriting the old .1) and start the new file from scratch --
# total size is therefore bounded (limit × (1 + number of retained files)).
MAX_LOG_BYTES = 10 * 1024 * 1024

# How many history files to keep. 1 = current file up to 10 MB + one previous
# file of 10 MB = at most 20 MB per route.
KEEP_HISTORY = 1


def history_path(path: Path, index: int) -> Path:
    """Name of the index-th history file: <name>.log.1 / <name>.log.2 …"""
    return path.with_name(f"{path.name}.{index}")


def rotate_at_start(path: Path, max_bytes: int = MAX_LOG_BYTES) -> None:
    """Move an oversized old log to .1 before startup so the current file starts from scratch.

    Only acts when the limit is **exceeded** -- keep small logs (frequent
    restarts will not lose the useful tail), while a large log means the
    previous run was long or chatty and it is time to split.

    Only done at **startup**: a running file is held by the child process's
    fd and cannot be replaced.
    """
    if not path.exists():
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    for index in range(KEEP_HISTORY, 0, -1):
        src = path if index == 1 else history_path(path, index - 1)
        dst = history_path(path, index)
        if not src.exists():
            continue
        try:
            if dst.exists():
                dst.unlink()
            src.replace(dst)
        except OSError:
            continue
        break


class RotatingLogWriter:
    """Write bytes to a log file, rotating once the limit is exceeded.

    It must hold the file handle **itself** (not rely on an inherited fd) --
    that is the only way to swap files at runtime: close the current handle,
    rename, then open a new one.
    """

    def __init__(self, path: Path, max_bytes: int = MAX_LOG_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._handle: BinaryIO | None = None
        self._open()

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(
            self.path, "ab"
        )  # Append mode: concurrent writers do not truncate each other

    def write(self, data: bytes) -> None:
        if self._handle is None or not data:
            return
        try:
            self._handle.write(data)
            self._handle.flush()
            if self._handle.tell() >= self.max_bytes:
                self._rotate()
        except (OSError, ValueError):
            # Cannot write (disk full, handle closed) -- drop this chunk of
            # output, but do not let the child process block on the write pipe
            # because of it (see the note in pump).
            pass

    def _rotate(self) -> None:
        try:
            if self._handle is not None:
                self._handle.close()
        except OSError:
            pass
        self._handle = None
        for index in range(KEEP_HISTORY, 0, -1):
            src = self.path if index == 1 else history_path(self.path, index - 1)
            dst = history_path(self.path, index)
            try:
                if dst.exists():
                    dst.unlink()
                src.replace(dst)
            except OSError:
                continue
            break
        try:
            self._open()
        except OSError:
            self._handle = None

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


def follow_log(path: Path) -> None:
    """Follow a log file (like tail -f). Exit with Ctrl-C.

    Read it ourselves instead of spawning a `tail -f` subprocess: when the
    file is rotated, `tail -f` follows the old inode and stops at the end of
    that file, whereas reading ourselves can notice "the file was swapped"
    and reopen it.
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)  # Start from the end: only new content is printed
        inode = os.fstat(handle.fileno()).st_ino
        while True:
            chunk = handle.readline()
            if chunk:
                print(chunk.decode("utf-8", "replace"), end="", flush=True)
                continue
            time.sleep(0.3)
            # File was rotated away (inode changed) -> reopen and keep following the new one
            try:
                current = os.stat(path).st_ino
            except OSError:
                current = None
            if current is not None and current != inode:
                handle.close()
                handle = path.open("rb")
                inode = os.fstat(handle.fileno()).st_ino


def follow_many(targets: list[tuple[str, Path]], poll: float = 0.3) -> Iterator[tuple[str, str]]:
    """Follow multiple log files at once, yielding (tag, one line of text).

    - Existing files are followed from the **end** (no history replay);
    - Files that do not exist yet are **waited for** and read from the start
      once they appear (that is the output from the app just starting,
      exactly what you want to see); rotation (inode changed) automatically
      reopens and continues following.
    - Yield order is arrival order; line order across files is not guaranteed.

    Never ends on its own -- following is interrupted with Ctrl-C
    (KeyboardInterrupt emerges from get()).
    """
    import queue
    import threading

    queue_: queue.Queue[tuple[str, str]] = queue.Queue()

    def watch(tag: str, path: Path) -> None:
        handle = None
        inode = None
        carry = b""  # partial line still waiting for a newline
        # Wait for the file to appear (it does not exist until the app has written a log)
        while handle is None:
            try:
                handle = path.open("rb")
            except OSError:
                time.sleep(poll)
        # **Existing files are followed from the end** -- no history replay.
        # Logs such as xxl-job-admin's can reach 9 MB; reading from the start
        # makes users think "the log keeps scrolling" when it is actually
        # replaying all history.
        handle.seek(0, os.SEEK_END)
        inode = os.fstat(handle.fileno()).st_ino
        try:
            while True:
                data = handle.read(65536)
                if data:
                    carry += data
                    # Yield only **complete lines**: keep a partial line until
                    # the next newline, otherwise one log record would be split
                    # into several chunks, each with its own prefix.
                    *complete, carry = carry.split(b"\n")
                    for line in complete:
                        queue_.put((tag, line.decode("utf-8", "replace") + "\n"))
                    continue
                time.sleep(poll)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if stat.st_ino != inode:
                    # Rotated: reopen the new file and read from the start (the new file is the new session's output)
                    handle.close()
                    handle = path.open("rb")
                    inode = os.fstat(handle.fileno()).st_ino
                    carry = b""
                elif stat.st_size < handle.tell():
                    # Truncated (`: > file`): go back to the start
                    handle.seek(0)
                    carry = b""
        finally:
            if handle is not None:
                handle.close()

    for tag, path in targets:
        threading.Thread(
            target=watch, args=(tag, path), name=f"devproxy-follow-{tag}", daemon=True
        ).start()
    while True:
        yield queue_.get()
