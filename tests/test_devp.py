"""devp 单元测试。devp 是无扩展名脚本，用 SourceFileLoader 加载。"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

DEV = Path(__file__).resolve().parent.parent / "devp"


def load_devp():
    loader = importlib.machinery.SourceFileLoader("devp", str(DEV))
    spec = importlib.util.spec_from_loader("devp", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


devp = load_devp()


class TestValidateName(unittest.TestCase):
    def test_valid_names(self):
        # 含 63 字符（规格允许的最大长度）——锁住 len(name) > 63 的边界
        for name in ("codeup", "codeup-web", "a1-b2-c3", "x", "x" * 63):
            with self.subTest(name=name):
                devp.validate_name(name)  # 不抛异常即通过

    def test_invalid_names_exit(self):
        # "codeup\n" 必被拒：$ 会匹配末尾换行之前，name 拼进 host 会得到
        # "codeup\n.test"（永不匹配），且注册/注销的 URL 不一致
        for name in ("Codeup", "a.b", "-x", "", "a_b", "a b", "x" * 64, "codeup\n"):
            with self.subTest(name=name):
                with self.assertRaises(SystemExit) as cm:
                    devp.validate_name(name)
                code = cm.exception.code
                self.assertIsInstance(code, str, "sys.exit 应带字符串错误信息")
                self.assertTrue(code.strip(), "错误信息不应为空")
                self.assertIn(repr(name), code, "错误信息应包含被拒的 name")


class TestAllocPort(unittest.TestCase):
    def test_returns_bindable_ephemeral_port(self):
        port = devp.alloc_port()
        self.assertIsInstance(port, int)
        self.assertTrue(1024 < port < 65536)
        # 返回的端口确实可绑定（bind 后立即释放）
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        s.close()


class TestHeartbeatEnv(unittest.TestCase):
    """DEVP_HEARTBEAT 非法值不能让整个 CLI 起不来（模块级常量）。"""

    def test_numeric_heartbeat_used(self):
        with mock.patch.dict(os.environ, {"DEVP_HEARTBEAT": "45"}):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                mod = load_devp()
        self.assertEqual(mod.HEARTBEAT, 45.0)
        self.assertEqual(stderr.getvalue(), "")

    def test_non_numeric_heartbeat_falls_back_to_30(self):
        with mock.patch.dict(os.environ, {"DEVP_HEARTBEAT": "abc"}):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                mod = load_devp()
        self.assertEqual(mod.HEARTBEAT, 30.0)
        self.assertIn("DEVP_HEARTBEAT", stderr.getvalue())


class StubAdmin:
    """最小 Admin API 桩：内存维护 srv0.routes，支持 GET/PATCH(/routes)、GET/DELETE(/id/x)。"""

    def __init__(self):
        self.routes = []
        self.ops = []  # 记录 (method, path) 序列
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code, body=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.ops.append(("GET", self.path))
                if self.path == devp.ROUTES_PATH:
                    self._send(200, outer.routes)
                elif self.path == "/config/":
                    # Caddy 存活预检
                    self._send(200, {})
                elif self.path.startswith("/id/"):
                    rid = self.path[4:]
                    hit = next((r for r in outer.routes if r.get("@id") == rid), None)
                    if hit is not None:
                        self._send(200, hit)
                    else:
                        self._send(404, {"error": "unknown id"})
                else:
                    self._send(404, None)

            def do_PATCH(self):
                outer.ops.append(("PATCH", self.path))
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"null")
                if self.path == devp.ROUTES_PATH:
                    outer.routes = body
                    self._send(200, None)
                else:
                    self._send(404, None)

            def do_DELETE(self):
                outer.ops.append(("DELETE", self.path))
                if self.path.startswith("/id/"):
                    rid = self.path[4:]
                    before = len(outer.routes)
                    outer.routes = [r for r in outer.routes if r.get("@id") != rid]
                    if len(outer.routes) < before:
                        self._send(200, None)
                    else:
                        self._send(404, None)
                else:
                    self._send(404, None)

            def log_message(self, *args):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()


class TestApi(unittest.TestCase):
    def setUp(self):
        self.admin = StubAdmin()

    def tearDown(self):
        self.admin.close()

    def test_get_routes(self):
        self.admin.routes = [{"@id": "devp_x"}]
        st, body = devp.api("GET", devp.ROUTES_PATH, base=self.admin.base)
        self.assertEqual((st, body), (200, [{"@id": "devp_x"}]))

    def test_delete_unknown_id_returns_404(self):
        st, body = devp.api("DELETE", "/id/devp_nothing", base=self.admin.base)
        self.assertEqual(st, 404)
        self.assertIsNone(body)

    def test_connection_refused_returns_zero(self):
        # 端口 1 上无服务，连接必失败，应返回 (0, None) 而非抛异常
        st, body = devp.api("GET", "/config/", base="http://127.0.0.1:1")
        self.assertEqual((st, body), (0, None))


class TestBuildRoute(unittest.TestCase):
    def test_exact_shape(self):
        self.assertEqual(devp.build_route("codeup", 49831), {
            "@id": "devp_codeup",
            "match": [{"host": ["codeup.test"]}],
            "handle": [{"handler": "reverse_proxy",
                        "upstreams": [{"dial": "127.0.0.1:49831"}]}],
            "terminal": True,
        })

    def test_rejects_invalid_name(self):
        # 入口自校验：换行/大写等非法 name 必须在构造前被拒
        with self.assertRaises(SystemExit):
            devp.build_route("codeup\n", 49831)


class TestInsertRoute(unittest.TestCase):
    def setUp(self):
        self.wild = {"match": [{"host": ["*.test"]}], "handle": [], "terminal": True}

    def test_inserted_before_wildcard(self):
        got = devp.insert_route([self.wild], {"@id": "devp_a"})
        self.assertEqual([r.get("@id") for r in got], ["devp_a", None])

    def test_inserted_between_exact_and_wildcard(self):
        exact = {"@id": "static", "match": [{"host": ["localhost"]}]}
        got = devp.insert_route([exact, self.wild], {"@id": "devp_a"})
        self.assertEqual([r.get("@id") for r in got], ["static", "devp_a", None])

    def test_no_wildcard_appends(self):
        got = devp.insert_route([{"@id": "static"}], {"@id": "devp_a"})
        self.assertEqual([r.get("@id") for r in got], ["static", "devp_a"])

    def test_empty_list(self):
        got = devp.insert_route([], {"@id": "devp_a"})
        self.assertEqual(len(got), 1)


class TestLocked(unittest.TestCase):
    def test_creates_lock_file_and_releases(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "sub" / "lock")  # 目录不存在也要能建
            with devp.locked(p):
                self.assertTrue(Path(p).exists())
            # 释放后可再次加锁
            with devp.locked(p):
                pass
