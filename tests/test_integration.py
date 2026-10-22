import os
import shutil
import ssl
import subprocess
import tempfile
import time
import unittest
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from devproxy.proxy.routes import Route, RouteTable, render_caddyfile

CADDY = Path(os.environ.get("DEVPROXY_TEST_CADDY", Path.home() / ".local" / "bin" / "caddy"))

pytestmark = pytest.mark.skipif(
    not CADDY.exists(), reason=f"需要 caddy 二进制：{CADDY}"
)


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
    """精确块：已注册域名可用；未注册域名拿不到证书。"""
    from devproxy.proxy.selftest import run_selftest

    result = run_selftest(CADDY)
    for desc, ok in result.checks:
        assert ok, f"检查失败：{desc}"
    assert result.failed == []
