"""End-to-end self-test on an isolated Caddy instance -- never touches the
production 443/2019.

Following devp selftest's proven approach: separate admin port, separate
https/http ports, separate storage, and HTTPS requests with real hostname
verification using the sandbox's own CA.

Difference from devp: the sandbox uses **exact site blocks** (one block per
route + tls internal), with no wildcard blocks and no on-demand -- so
unregistered domains get no certificate at all and the handshake fails
outright.
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from devproxy.gateway.caddy import MainCaddyfileSpec
from devproxy.gateway.caddy import build_main_caddyfile
from devproxy.gateway.routes import Route
from devproxy.gateway.routes import RouteTable
from devproxy.gateway.routes import render_caddyfile

UPSTREAM_BODY = b"hello-from-upstream"

# The pki app (and its install_trust switch) only exists since Caddy 2.7.
# Older caddy silently ignores the install_trust field in JSON -- in that
# case the sandbox CA gets installed into the host's system/NSS/Java trust
# stores, exactly what this task is meant to avoid.
MIN_CADDY_VERSION = (2, 7, 0)

# Caddy logs this line when install_trust=false (modules/caddypki/pki.go).
# It is the only externally visible evidence that "host trust store
# installation is really disabled".
TRUST_DISABLED_LOG = "root certificate trust store installation disabled"

# External commands are always bounded: a hung adapt/reload would drag down
# the sandbox cleanup in finally too.
CADDY_COMMAND_TIMEOUT = 30.0


@dataclass
class SelftestResult:
    checks: list[tuple[str, bool]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def record(self, desc: str, ok: bool) -> None:
        self.checks.append((desc, ok))
        if not ok:
            self.failed.append(desc)


class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
    """Upstream for the self-test: every GET returns a fixed piece of text."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(UPSTREAM_BODY)))
        self.end_headers()
        self.wfile.write(UPSTREAM_BODY)

    def log_message(self, *args):
        pass


def _fetch(host: str, port: int, ctx: ssl.SSLContext, timeout: float = 5.0):
    """HTTPS GET with real hostname verification using the given CA; returns
    (status code, response body)."""
    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)
    try:
        conn.request("GET", "/")
        r = conn.getresponse()
        body = r.read().decode()
        return r.status, body
    finally:
        conn.close()


def _probe_context() -> ssl.SSLContext:
    """Probe context that verifies no certificates -- used only to answer
    "does the server hand out a certificate at all?".

    Trying an unregistered domain with a CA-verifying context does not work:
    a wildcard block *.test would also make the handshake succeed, only the
    hostname would not match, and the client raises a verification error
    rather than "no certificate". The probe context verifies nothing, so
    "handshake succeeds" = the server really issued a certificate, and
    "handshake fails" = it has no usable certificate on hand.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _free_ports(count: int) -> list[int]:
    """Get count distinct free ports at once.

    Hold all sockets at the same time, then release them together -- binding
    and closing one at a time can hand out the same port twice. The sandbox
    uses dynamic ports throughout: fixed ports would let a concurrent
    self-test connect to someone else's Caddy and reload their config,
    making the self-test's conclusions completely untrustworthy.
    """
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            sockets.append(s)
        return [s.getsockname()[1] for s in sockets]
    finally:
        for s in sockets:
            s.close()


def _storage_root(config: object) -> str | None:
    """Read storage.file_system's root from the /config/ JSON."""
    if not isinstance(config, dict):
        return None
    storage = config.get("storage")
    if not isinstance(storage, dict):
        return None
    root = storage.get("root")
    return root if isinstance(root, str) else None


def _wait_admin(base: str, expected_storage: str, timeout: float = 15.0) -> bool:
    """Wait for the Admin API to be ready and confirm it really is **this
    sandbox's** instance.

    Checking only for HTTP 200 is not enough: if the sandbox caddy failed to
    start due to a port conflict, the port may be held by another self-test
    (or the user's own Caddy), and a subsequent reload would change someone
    else's config. storage is unique to the sandbox, so use it for identity
    verification.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/config/", timeout=2) as r:
                if r.status == 200:
                    body = json.loads(r.read())
                    if _storage_root(body) == expected_storage:
                        return True
        except (
            urllib.error.URLError,
            OSError,
            http.client.HTTPException,
            json.JSONDecodeError,
        ):
            pass
        time.sleep(0.3)
    return False


def _wait_file(path: Path, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.2)
    return False


def _caddy_version(caddy_bin: Path) -> tuple[int, int, int] | None:
    """Parse the output of `caddy version`, e.g. "v2.11.7 h1:..."."""
    try:
        proc = subprocess.run(
            [str(caddy_bin), "version"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", (proc.stdout or "") + (proc.stderr or ""))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _read_log_tail(path: Path, limit: int = 800) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(cannot read caddy log)"
    return text[-limit:].strip()


def _wait_trust_disabled_log(log_path: Path, timeout: float = 10.0) -> bool:
    """Wait for caddy to log the "host trust store installation disabled"
    line.

    This is the only externally visible evidence that install_trust=false
    actually took effect (older caddy silently ignores the field). If it does
    not appear, record a failure but do not abort the remaining checks -- the
    caller aggregates this failure into the final result.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if TRUST_DISABLED_LOG in _read_log_tail(log_path, limit=100000):
            return True
        time.sleep(0.2)
    return False


def _adapt(
    caddy_bin: Path,
    caddyfile: Path,
    config_json: Path,
    timeout: float = CADDY_COMMAND_TIMEOUT,
) -> tuple[bool, str]:
    """Caddyfile -> JSON, with the sandbox CA's "install into the system
    trust store" turned off.

    There are two reasons: the sandbox is a one-off CA, and installing it
    into the system trust store pollutes the host; also, installation goes
    through sudo (may prompt for a password), which would stall startup for
    tens of seconds. This only affects the trust store, not issuance or
    routing behavior -- what is under test is still tls internal.
    """
    try:
        proc = subprocess.run(
            [str(caddy_bin), "adapt", "--config", str(caddyfile)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"caddy adapt timed out ({timeout}s)"
    except OSError as e:
        return False, f"caddy adapt could not run: {e}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    try:
        cfg = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return False, f"adapt output is not valid JSON: {e}"
    cfg.setdefault("apps", {})["pki"] = {
        "certificate_authorities": {"local": {"install_trust": False}}
    }
    try:
        config_json.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return False, f"failed to write sandbox config: {e}"
    return True, ""


def _reload(
    caddy_bin: Path,
    config_json: Path,
    admin_port: int,
    timeout: float = CADDY_COMMAND_TIMEOUT,
) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [
                str(caddy_bin),
                "reload",
                "--config",
                str(config_json),
                "--address",
                f"127.0.0.1:{admin_port}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"caddy reload timed out ({timeout}s)"
    except OSError as e:
        return False, f"caddy reload could not run: {e}"
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def _write_sandbox_configs(
    result: SelftestResult,
    tmp: Path,
    admin_port: int,
    https_port: int,
    http_port: int,
) -> tuple[bool, Path, Path, Path]:
    """Create the sandbox's routes/sites/Caddyfile; on failure record it and
    return (False, ...).

    The main Caddyfile is generated by the production build_main_caddyfile --
    otherwise a change to the production global block would not be covered by
    the self-test, and passing the self-test would not prove the production
    path is correct.
    """
    routes_file = tmp / "routes.caddy"
    sites_file = tmp / "sites.caddy"
    caddyfile = tmp / "Caddyfile"
    try:
        sites_file.write_text("", encoding="utf-8")
        routes_file.write_text(render_caddyfile(RouteTable()), encoding="utf-8")
        caddyfile.write_text(
            build_main_caddyfile(
                MainCaddyfileSpec(
                    admin_addr=f"127.0.0.1:{admin_port}",
                    storage=tmp / "data",
                    routes_caddy=routes_file,
                    sites_caddy=sites_file,
                    http_port=http_port,
                    https_port=https_port,
                )
            ),
            encoding="utf-8",
        )
    except OSError as e:
        result.record(f"failed to create sandbox config: {e}", False)
        return False, routes_file, sites_file, caddyfile
    return True, routes_file, sites_file, caddyfile


def run_selftest(caddy_bin: Path) -> SelftestResult:
    """Start an isolated Caddy and run the full flow: register -> forward ->
    redirect -> unregister -> certificate policy."""
    result = SelftestResult()
    if not caddy_bin.exists():
        result.record(f"found caddy binary {caddy_bin}", False)
        return result

    tmp = Path(tempfile.mkdtemp(prefix="devproxy-selftest-"))
    upstream: http.server.ThreadingHTTPServer | None = None
    log_file = None
    proc: subprocess.Popen | None = None
    try:
        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        uport = upstream.server_address[1]

        # Version floor: caddy older than 2.7 has no pki app, install_trust is
        # silently ignored, and the sandbox CA gets installed into the host
        # trust store -- in that case it is better not to run at all.
        version = _caddy_version(caddy_bin)
        if version is None:
            result.record("read caddy version", False)
            return result
        ver_str = ".".join(str(x) for x in version)
        if version < MIN_CADDY_VERSION:
            floor = ".".join(str(x) for x in MIN_CADDY_VERSION)
            result.record(
                f"caddy version {ver_str} is too old (requires >= {floor}: older caddy has "
                "no pki app and the sandbox CA would be installed into the host trust store)",
                False,
            )
            return result
        result.record(
            f"caddy version {ver_str} (>= {'.'.join(str(x) for x in MIN_CADDY_VERSION)})",
            True,
        )

        # Dynamic ports: concurrent self-tests / the user's existing services
        # never claim each other's.
        admin_port, https_port, http_port = _free_ports(3)
        ok, routes_file, sites_file, caddyfile = _write_sandbox_configs(
            result, tmp, admin_port, https_port, http_port
        )
        if not ok:
            return result

        def write_routes(routes: list[Route]) -> bool:
            try:
                routes_file.write_text(
                    render_caddyfile(RouteTable(routes=routes)), encoding="utf-8"
                )
            except OSError as e:
                result.record(f"failed to write sandbox route table: {e}", False)
                return False
            return True

        config_json = tmp / "config.json"
        ok, detail = _adapt(caddy_bin, caddyfile, config_json)
        if not ok:
            result.record(f"sandbox config generation (caddy adapt): {detail}", False)
            return result

        # Isolation: point caddy's config/data directories at tmp too, so the
        # production autosave.json is not touched
        env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(tmp / "xdg-config"),
            "XDG_DATA_HOME": str(tmp / "xdg-data"),
        }
        # Do not discard caddy's output: whether install_trust really took
        # effect can only be seen from its own log
        log_path = tmp / "caddy.log"
        try:
            log_file = open(log_path, "wb")
            proc = subprocess.Popen(
                [str(caddy_bin), "run", "--config", str(config_json)],
                stdout=log_file,
                stderr=log_file,
                env=env,
            )
        except OSError as e:
            result.record(f"sandbox caddy start ({e})", False)
            return result

        base = f"http://127.0.0.1:{admin_port}"
        if not _wait_admin(base, str(tmp / "data")):
            result.record("sandbox caddy start", False)
            return result
        result.record("sandbox caddy start", True)

        # "Did not pollute the host" is one of this self-test's core promises
        # and needs evidence, not assumptions. Older caddy silently ignores
        # install_trust, and then this log line never appears.
        if _wait_trust_disabled_log(log_path):
            result.record(
                "confirmed host trust store installation is disabled (caddy log evidence)",
                True,
            )
        else:
            result.record(
                "cannot confirm host trust store installation is disabled (Caddy too old?)"
                " -- caddy output tail: " + _read_log_tail(log_path),
                False,
            )

        cafile = tmp / "data" / "pki" / "authorities" / "local" / "root.crt"
        ctx = None
        if _wait_file(cafile):
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    ctx = ssl.create_default_context(cafile=str(cafile))
                    break
                except ssl.SSLError:
                    time.sleep(0.2)
        result.record("sandbox CA root certificate generated and loadable", ctx is not None)
        if ctx is None:
            return result

        # Before registration the sandbox should not serve anything yet --
        # assert "connection refused" explicitly. This item asserts isolation
        # (there is no site block yet, so nothing listens on the https port);
        # the item after registration is what really verifies "unregistered
        # domains get no certificate".
        probe = _probe_context()
        try:
            _fetch("unregistered.test", https_port, probe)
            result.record(
                "sandbox is not serving before registration (connection should be refused)", False
            )
        except ConnectionRefusedError:
            result.record(
                "sandbox is not serving before registration (connection should be refused)", True
            )
        except (ssl.SSLError, OSError, http.client.HTTPException) as e:
            result.record(
                f"sandbox is not serving before registration (connection should be refused): {e}",
                False,
            )

        # Registered domain: exact block + tls internal
        if not write_routes(
            [
                Route(
                    name="foo",
                    domain="foo.test",
                    port=uport,
                    mode="expose",
                    upstream=f"127.0.0.1:{uport}",
                )
            ]
        ):
            return result
        ok, detail = _adapt(caddy_bin, caddyfile, config_json)
        if ok:
            ok, detail = _reload(caddy_bin, config_json, admin_port)
        result.record(f"reload took effect: {detail}" if not ok else "reload took effect", ok)

        # Certificates are issued asynchronously after reload; give it some
        # time
        hit = False
        deadline = time.time() + 15
        last_err = ""
        while time.time() < deadline and not hit:
            try:
                status, body = _fetch("foo.test", https_port, ctx)
                hit = status == 200 and body == UPSTREAM_BODY.decode()
                if not hit:
                    last_err = f"status {status}"
            except (ssl.SSLError, OSError, http.client.HTTPException) as e:
                last_err = str(e)
            if not hit:
                time.sleep(0.3)
        result.record(
            "registered domain reaches upstream"
            if hit
            else f"registered domain reaches upstream ({last_err})",
            hit,
        )

        # This time the TLS service is already up: an unregistered domain must
        # fail the handshake (it really has no certificate)
        try:
            _fetch("unregistered.test", https_port, probe)
            result.record("unregistered domain still gets no certificate after registration", False)
        except ssl.SSLError:
            result.record("unregistered domain still gets no certificate after registration", True)
        except (OSError, http.client.HTTPException) as e:
            # Cannot connect or the response is abnormal: the server either
            # did not start or actually handed out a certificate
            result.record(
                f"unregistered domain still gets no certificate after registration ({e})",
                False,
            )

        # 80 -> 443 redirect (must be tested before unregistering: afterwards
        # Caddy no longer manages the redirect for that domain)
        location = None
        deadline = time.time() + 10
        while time.time() < deadline and location is None:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", http_port, timeout=5)
                conn.request("GET", "/", headers={"Host": "foo.test"})
                r = conn.getresponse()
                location = r.getheader("Location")
                r.read()
                conn.close()
            except (OSError, http.client.HTTPException):
                time.sleep(0.3)
        result.record(
            "http redirects to https",
            location is not None and location.startswith("https://"),
        )

        # No certificate after unregistering
        if not write_routes([]):
            return result
        ok, detail = _adapt(caddy_bin, caddyfile, config_json)
        if ok:
            ok, detail = _reload(caddy_bin, config_json, admin_port)
        result.record(
            f"unregister reload took effect: {detail}"
            if not ok
            else "unregister reload took effect",
            ok,
        )
        try:
            status, body = _fetch("foo.test", https_port, ctx)
            result.record("no route after unregistering", body != UPSTREAM_BODY.decode())
        except (ssl.SSLError, OSError, http.client.HTTPException):
            result.record("no route after unregistering", True)
        return result
    finally:
        # Every resource from here back (tmp directory, upstream thread/
        # server, log, caddy) must be reclaimed in one finally: an exception
        # in any preceding step must not leave /tmp residue or leak threads/
        # sockets when called in-process.
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                # Must reap after killing too, otherwise a zombie process is
                # left
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if log_file is not None:
            try:
                log_file.close()
            except OSError:
                pass
        if upstream is not None:
            upstream.shutdown()
            upstream.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
