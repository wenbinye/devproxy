import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from devproxy.gateway.routes import Route, RouteTable, render_caddyfile

CADDY = Path(os.environ.get("DEVPROXY_TEST_CADDY", Path.home() / ".local" / "bin" / "caddy"))


def _test_domain_resolves() -> bool:
    """*.test 必须解析到本机 —— 自检要靠真实主机名做 TLS 主机名校验。"""
    try:
        infos = socket.getaddrinfo(
            "devproxy-selftest-probe.test", 443, type=socket.SOCK_STREAM
        )
    except OSError:
        return False
    return any(info[4][0] in ("127.0.0.1", "::1") for info in infos)


pytestmark = [
    pytest.mark.skipif(
        not CADDY.exists(), reason=f"需要 caddy 二进制：{CADDY}"
    ),
    pytest.mark.skipif(
        not _test_domain_resolves(),
        reason="需要 *.test 解析到 127.0.0.1/::1（如 dnsmasq address=/.test/127.0.0.1）",
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
    """精确块：已注册域名可用；未注册域名拿不到证书。"""
    from devproxy.gateway.selftest import run_selftest

    result = run_selftest(CADDY)
    for desc, ok in result.checks:
        assert ok, f"检查失败：{desc}"
    assert result.failed == []
