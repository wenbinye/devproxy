import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from devproxy.gateway.routes import Route
from devproxy.gateway.routes import RouteTable
from devproxy.gateway.routes import render_caddyfile

CADDY = Path(os.environ.get("DEVPROXY_TEST_CADDY", Path.home() / ".local" / "bin" / "caddy"))


def _test_domain_resolves() -> bool:
    """*.test must resolve to this machine -- the self-check uses the real hostname for TLS verification."""
    try:
        infos = socket.getaddrinfo("devproxy-selftest-probe.test", 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return any(info[4][0] in ("127.0.0.1", "::1") for info in infos)


pytestmark = [
    pytest.mark.skipif(not CADDY.exists(), reason=f"needs the caddy binary: {CADDY}"),
    pytest.mark.skipif(
        not _test_domain_resolves(),
        reason="needs *.test to resolve to 127.0.0.1/::1 (e.g. dnsmasq address=/.test/127.0.0.1)",
    ),
]


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello-from-upstream"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _fetch(host, port, ctx):
    conn = HTTPSConnection(host, port, context=ctx, timeout=5)
    conn.request("GET", "/")
    r = conn.getresponse()
    body = r.read().decode()
    conn.close()
    return r.status, body


def test_exact_block_serves_route_and_withholds_cert_for_unknown():
    """Exact blocks: a registered domain works; an unregistered one gets no certificate."""
    from devproxy.gateway.selftest import run_selftest

    result = run_selftest(CADDY)
    for desc, ok in result.checks:
        assert ok, f"check failed: {desc}"
    assert result.failed == []
