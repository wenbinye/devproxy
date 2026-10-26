"""Interact with Caddy: Admin API client, config validation, hot reload, and
main Caddyfile generation."""

from __future__ import annotations

import http.client
import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class CaddyError(RuntimeError):
    """Caddy interaction failed: unreachable, invalid config, or reload failure."""


@dataclass
class CaddyAdmin:
    """Caddy Admin API client."""

    base: str = "http://127.0.0.1:2019"
    timeout: float = 5.0

    def api(self, method: str, path: str, body: object | None = None) -> tuple[int, object]:
        """HTTP errors return (code, None); connection failures return (0, None)."""
        url = self.base.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            return e.code, None
        except (OSError, http.client.HTTPException):
            # URLError is a subclass of OSError; HTTPException (BadStatusLine /
            # IncompleteRead, etc.) is not -- when the admin port is occupied
            # by a non-HTTP service we must likewise return (0, None) and not
            # violate api()'s "never raises" contract
            return 0, None
        try:
            return status, (json.loads(raw) if raw else None)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 200 but the response body is not JSON -- the contract is
            # (code, body), so we cannot raise raw
            return status, None

    def is_alive(self) -> bool:
        code, _ = self.api("GET", "/config/")
        return code == 200


@dataclass
class CaddyConfig:
    """Validation and hot reload for the main Caddyfile."""

    caddy_bin: Path
    caddyfile: Path
    admin_addr: str = "127.0.0.1:2019"

    def validate(self, caddyfile: Path | None = None, timeout: float = 30.0) -> tuple[bool, str]:
        """Validate the config; a missing/non-executable binary or a timeout
        also returns (False, message) instead of raising."""
        return self._run("validate", caddyfile or self.caddyfile, False, timeout)

    def reload(self, timeout: float = 30.0) -> tuple[bool, str]:
        """Hot reload the config; a missing/non-executable binary or a timeout
        also returns (False, message) instead of raising."""
        return self._run("reload", self.caddyfile, True, timeout)

    def _run(
        self, subcommand: str, path: Path, with_address: bool, timeout: float
    ) -> tuple[bool, str]:
        """Shared implementation for validate / reload: build argv, run, and
        map failures to (False, message)."""
        argv = [str(self.caddy_bin), subcommand, "--config", str(path)]
        if with_address:
            argv += ["--address", self.admin_addr]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except OSError as e:
            # Missing file / not executable / bad format (Exec format error)
            # all land here
            return False, f"caddy binary is missing or not executable: {self.caddy_bin} ({e})"
        except subprocess.TimeoutExpired:
            return False, f"caddy {subcommand} timed out ({timeout}s)"
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def _caddy_token(value: str) -> str:
    """Render a value as a single Caddyfile token.

    Paths/addresses go straight into the Caddyfile configuration language: if
    whitespace-containing values are not quoted they are split into multiple
    tokens (`/Volumes/My Disk`); with newlines they can even inject extra
    config lines. Simple tokens are emitted as-is (keeping existing output
    byte-for-byte identical); everything else is quoted as a JSON string --
    JSON escapes quotes and newlines, and Caddy's lexer only treats `\\"` as an
    escape, leaving unknown escapes inside the token, so a newline can never
    become a real line separator.
    """
    safe = value and all(
        not ch.isspace() and ch not in '"`{}#' and 0x20 <= ord(ch) < 0x7F for ch in value
    )
    return value if safe else json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class MainCaddyfileSpec:
    """All inputs needed to generate the main Caddyfile.

    The parameters are consolidated into one value object: call sites can no
    longer swap two paths of the same type, and new fields only need
    changing here.
    """

    admin_addr: str
    storage: Path
    routes_caddy: Path
    sites_caddy: Path
    http_port: int = 80
    https_port: int = 443


def build_main_caddyfile(spec: MainCaddyfileSpec) -> str:
    """Generate the main Caddyfile: a single global block plus two imports.

    Imported files must not contain a global block -- only one global block
    is allowed.

    http_port / https_port are Caddy global options and only take effect when
    written here; otherwise Caddy always uses its default 80/443 and those
    two config keys are effectively dead. The defaults are 80/443, so the
    generated output matches the previous behavior.
    """
    return (
        "{\n"
        f"\tadmin {_caddy_token(str(spec.admin_addr))}\n"
        f"\tstorage file_system {_caddy_token(str(spec.storage))}\n"
        f"\thttp_port {spec.http_port}\n"
        f"\thttps_port {spec.https_port}\n"
        "}\n"
        "\n"
        f"import {_caddy_token(str(spec.routes_caddy))}\n"
        f"import {_caddy_token(str(spec.sites_caddy))}\n"
    )
