"""The background up worker: it supervises the child process when `devproxy up` starts an app.

Why this process exists: `devproxy up` is a **foreground** command -- it stays with the
app, unregisters the route when the app exits, and re-adds the route when a Caddy
heartbeat shows it is gone. sync has to start several routes at once and still return,
so it cannot block on a foreground command; the same "stay with the app" logic therefore
lives in this separate process:

- sync starts it (in its own session); it starts the app, registers the route, and stays;
- app exits -> the route is unregistered automatically (same semantics as foreground `devproxy up`);
- sync exiting does not affect it -- it lives in its own session and keeps the app company.

The payload comes from the `DEVPROXY_UP_PAYLOAD` environment variable (JSON); the command
line stays for `python -m` itself, so the app's own arguments never mix with devproxy's.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer

from devproxy.cli._context import _require_caddyfile
from devproxy.cli._context import build_context
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.runner import PreconditionError
from devproxy.gateway.runner import UpError
from devproxy.gateway.runner import UpRequest
from devproxy.gateway.runner import UpRunner
from devproxy.gateway.store import RouteStoreError

PAYLOAD_ENV = "DEVPROXY_UP_PAYLOAD"


def _load_payload() -> dict:
    raw = os.environ.get(PAYLOAD_ENV)
    if not raw:
        raise ValueError(f"missing environment variable {PAYLOAD_ENV}")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("the payload must be a JSON object")
    return payload


class _LogProxy:
    """Append the worker's output to the log file (reopened on every write)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, data: str) -> int:
        if not data:
            return 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(data)
        except OSError:
            pass  # a failed log write must not take the worker down
        return len(data)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def main(argv: list[str] | None = None) -> int:
    """Run one up and stay with the app. Returns the process exit code."""
    try:
        payload = _load_payload()
    except (ValueError, json.JSONDecodeError) as e:
        print(f"devproxy: {e}", file=sys.stderr)
        return 2
    try:
        name = str(payload["name"])
        command = [str(part) for part in payload["command"]]
    except (KeyError, TypeError) as e:
        print(f"devproxy: the payload is missing a field: {e}", file=sys.stderr)
        return 2

    cwd = payload.get("cwd")
    log_path = payload.get("log_path")
    if log_path:
        # The worker's own output goes into the log file too: reopened on every write, so
        # after a rotation the next message lands in the **new** file instead of following
        # the old inode into .log.1
        proxy = _LogProxy(Path(log_path))
        sys.stdout = proxy
        sys.stderr = proxy
    request = UpRequest(
        name=name,
        cmd=command,
        port=payload.get("port"),
        ready_timeout=payload.get("ready_timeout"),
        cwd=Path(cwd) if cwd else None,
        source=payload.get("source"),
        log_path=Path(log_path) if log_path else None,
        extra_env={str(k): str(v) for k, v in (payload.get("extra_env") or {}).items()},
    )
    try:
        paths, config, store, admin = build_context()
        _require_caddyfile(paths)
        runner = UpRunner(store, config, admin)
        return runner.run_request(request)
    except typer.Exit as e:
        # Unmet precondition (missing Caddyfile, ...) -- this is what fail() raises
        return int(e.exit_code)
    except PreconditionError as e:
        print(f"devproxy: {e}", file=sys.stderr)
        return 3
    except (UpError, RouteStoreError, RouteTableError) as e:
        print(f"devproxy: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
