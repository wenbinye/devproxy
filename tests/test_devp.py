"""devp 单元测试。devp 是无扩展名脚本，用 SourceFileLoader 加载。"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
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
        self.patch_bodies = []  # PATCH 收到的 body（序列化路径的可观测点）
        self.patch_content_types = []  # PATCH 收到的 Content-Type
        self.get_status = 200  # 置为非 200 可模拟 Admin API 故障
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
                    self._send(outer.get_status, outer.routes if outer.get_status == 200 else None)
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
                outer.patch_bodies.append(body)
                outer.patch_content_types.append(self.headers.get("Content-Type"))
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
        self.srv.server_close()  # 释放监听 socket，避免后续任务积累 fd


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

    def test_patch_sends_json_body(self):
        # PATCH 带 body 是注册协议的核心调用，必须锁住序列化与 Content-Type
        routes = [{"@id": "devp_x", "match": [{"host": ["x.test"]}]}]
        st, _ = devp.api("PATCH", devp.ROUTES_PATH, body=routes, base=self.admin.base)
        self.assertEqual(st, 200)
        self.assertEqual(self.admin.patch_bodies, [routes])
        self.assertEqual(self.admin.patch_content_types, ["application/json"])
        self.assertEqual(self.admin.routes, routes)

    def test_default_base_falls_back_to_admin_base(self):
        # base=None 时应回落到模块级 ADMIN_BASE（此处临时改指向桩）
        old = devp.ADMIN_BASE
        devp.ADMIN_BASE = self.admin.base
        try:
            st, body = devp.api("GET", devp.ROUTES_PATH)
        finally:
            devp.ADMIN_BASE = old
        self.assertEqual(st, 200)
        self.assertEqual(body, [])


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


class TestIsWildcard(unittest.TestCase):
    def test_wildcard_host_is_wildcard(self):
        self.assertTrue(devp.is_wildcard({"match": [{"host": ["*.test"]}]}))

    def test_exact_host_is_not_wildcard(self):
        self.assertFalse(devp.is_wildcard({"match": [{"host": ["a.test"]}]}))

    def test_route_without_match_is_not_wildcard(self):
        self.assertFalse(devp.is_wildcard({"@id": "devp_a"}))


class TestLocked(unittest.TestCase):
    def test_creates_lock_file_and_releases(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "sub" / "lock")  # 目录不存在也要能建
            with devp.locked(p):
                self.assertTrue(Path(p).exists())
            # 释放后可再次加锁
            with devp.locked(p):
                pass

    def test_relative_path_without_dir_component(self):
        # DEVP_LOCK 可被设为相对路径（如 devp.lock），此时 dirname 为空串，
        # makedirs("") 会抛 FileNotFoundError
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                with devp.locked("devp.lock"):
                    self.assertTrue(Path(d, "devp.lock").exists())
            finally:
                os.chdir(cwd)


class TestRegisterLifecycle(unittest.TestCase):
    def setUp(self):
        self.admin = StubAdmin()
        self.admin.routes = [{"match": [{"host": ["*.test"]}], "handle": [], "terminal": True}]
        # 别碰真实 HOME 下的锁文件（用户开着 devp 时还会阻塞）
        self._lock_dir = tempfile.TemporaryDirectory()
        self._old_lock_path = devp.LOCK_PATH
        devp.LOCK_PATH = str(Path(self._lock_dir.name) / "lock")

    def tearDown(self):
        devp.LOCK_PATH = self._old_lock_path
        self._lock_dir.cleanup()
        self.admin.close()

    def test_register_inserts_before_wildcard(self):
        self.assertTrue(devp.register("codeup", 49831, base=self.admin.base))
        self.assertEqual(self.admin.routes[0]["@id"], "devp_codeup")
        self.assertEqual(self.admin.routes[1]["match"][0]["host"], ["*.test"])
        self.assertIn(("GET", devp.ROUTES_PATH), self.admin.ops)
        self.assertIn(("PATCH", devp.ROUTES_PATH), self.admin.ops)

    def test_route_exists_and_deregister(self):
        devp.register("codeup", 49831, base=self.admin.base)
        self.assertTrue(devp.route_exists("codeup", base=self.admin.base))
        self.assertFalse(devp.route_exists("other", base=self.admin.base))
        self.assertTrue(devp.deregister("codeup", base=self.admin.base))
        self.assertFalse(devp.route_exists("codeup", base=self.admin.base))
        # 注销后通配兜底仍在（只删 devp_ 元素，不动数组其他项）
        self.assertEqual(len(self.admin.routes), 1)

    def test_deregister_unknown_returns_false(self):
        self.assertFalse(devp.deregister("ghost", base=self.admin.base))

    def test_deregister_rejects_invalid_name(self):
        # name 会被拼进 /id/ 路径与 @id，含换行等非法字符必须在入口就被拒
        with self.assertRaises(SystemExit):
            devp.deregister("codeup\n", base=self.admin.base)

    def test_route_exists_rejects_invalid_name(self):
        with self.assertRaises(SystemExit):
            devp.route_exists("codeup\n", base=self.admin.base)

    def test_list_routes(self):
        devp.register("a", 1111, base=self.admin.base)
        devp.register("b", 2222, base=self.admin.base)
        self.assertEqual(devp.list_routes(base=self.admin.base), [
            ("a", "a.test", "127.0.0.1:1111", 1111),
            ("b", "b.test", "127.0.0.1:2222", 2222),
        ])

    def test_register_is_idempotent_for_same_name(self):
        # PATCH 已生效但响应丢失后重试：数组里已有 devp_x，不能插第二个同 @id 元素
        self.assertTrue(devp.register("x", 1111, base=self.admin.base))
        self.assertTrue(devp.register("x", 2222, base=self.admin.base))
        self.assertEqual([r.get("@id") for r in self.admin.routes].count("devp_x"), 1)
        hit = next(r for r in self.admin.routes if r.get("@id") == "devp_x")
        self.assertEqual(hit["handle"][0]["upstreams"][0]["dial"], "127.0.0.1:2222")

    def test_register_returns_false_and_skips_patch_when_get_fails(self):
        self.admin.get_status = 503
        self.assertFalse(devp.register("codeup", 49831, base=self.admin.base))
        self.assertEqual(self.admin.ops, [("GET", devp.ROUTES_PATH)])

    def test_register_rejects_invalid_name(self):
        # admin 挂掉时非法 name 也必须在入口退出，而不是静默返回 False
        with self.assertRaises(SystemExit):
            devp.register("codeup\n", 49831, base="http://127.0.0.1:1")

    def test_register_retry_fails_when_admin_unreachable(self):
        # 端口 1 上无服务：attempts=1 时只试一次，返回 False 且不等待
        self.assertFalse(devp.register_retry("x", 1, base="http://127.0.0.1:1", attempts=1))

    def test_register_retry_backoff_intervals(self):
        with mock.patch.object(devp.time, "sleep") as sleep:
            self.assertFalse(devp.register_retry(
                "x", 1, base="http://127.0.0.1:1", attempts=3))
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])


class TestReachable(unittest.TestCase):
    def test_closed_port_unreachable(self):
        # 找一个确定没监听的端口：bind 后关掉，立刻探测（TIME_WAIT 不影响 listen 探测）
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(devp.reachable(port))

    def test_listening_port_reachable(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertTrue(devp.reachable(srv.server_address[1]))
        finally:
            srv.shutdown()
            srv.server_close()


FAKE_APP = """
import os, socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(os.environ["PORT"])))
s.listen(1)
time.sleep(3)
raise SystemExit(7)
"""


def run_devp(args, env_extra=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run([sys.executable, str(DEV), *args],
                          env=env, capture_output=True, text=True, timeout=60)


class TestUpLifecycle(unittest.TestCase):
    """集成测试：真实 devp 子进程 + StubAdmin，覆盖注册→心跳重注册→注销→退出码。"""

    def setUp(self):
        self.admin = StubAdmin()
        self.admin.routes = [{"match": [{"host": ["*.test"]}], "handle": [], "terminal": True}]
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {
            "DEVP_ADMIN": self.admin.base,
            "DEVP_LOCK": str(Path(self.tmp.name) / "lock"),
            "DEVP_HEARTBEAT": "0.5",
        }

    def tearDown(self):
        self.admin.close()
        self.tmp.cleanup()

    def _ops(self, method, path):
        return [o for o in self.admin.ops if o == (method, path)]

    def test_up_registers_then_deregisters_and_exits_with_child_code(self):
        r = run_devp(["up", "testapp", "--", sys.executable, "-c", FAKE_APP], self.env)
        self.assertEqual(r.returncode, 7, msg=r.stderr)
        self.assertIn("✅ https://testapp.test", r.stdout)

        # 注册发生过：至少有一次 PATCH 的 body 里，devp_testapp 插在通配路由之前。
        # 不能查 live routes——此时子进程已退出、路由已被注销，只剩通配兜底。
        def index_of(routes, pred):
            return next((i for i, x in enumerate(routes) if pred(x)), None)

        self.assertTrue(self.admin.patch_bodies, "未发生 PATCH")
        registered_before_wildcard = any(
            (i := index_of(body, lambda x: x.get("@id") == "devp_testapp")) is not None
            and (w := index_of(body, devp.is_wildcard)) is not None
            and i < w
            for body in self.admin.patch_bodies
        )
        self.assertTrue(
            registered_before_wildcard,
            msg=f"没有一次 PATCH 把 devp_testapp 插到通配之前: {self.admin.patch_bodies}",
        )
        # 子进程退出后已注销
        self.assertFalse(any(x.get("@id") == "devp_testapp" for x in self.admin.routes))
        self.assertIn(("DELETE", "/id/devp_testapp"), self.admin.ops)

    def test_up_heartbeat_reregisters_after_route_loss(self):
        # 起一个后台线程在注册后 ~2.0s 删掉路由，心跳(0.5s)应重注册。
        # 2.0s 而非 1.2s：给首次注册留足余量，避免慢机上删在注册之前。
        def sabotage():
            time.sleep(2.0)
            self.admin.routes = [r for r in self.admin.routes if r.get("@id") != "devp_testapp"]
        t = threading.Thread(target=sabotage, daemon=True)
        t.start()
        before = len(self._ops("PATCH", devp.ROUTES_PATH))
        r = run_devp(["up", "testapp", "--", sys.executable, "-c", FAKE_APP], self.env)
        # 等 sabotage 确定结束后再断言，避免与 PATCH 处理器的 routes 重绑定交错
        t.join()
        self.assertEqual(r.returncode, 7, msg=r.stderr)
        patches = self._ops("PATCH", devp.ROUTES_PATH)
        self.assertGreaterEqual(len(patches), before + 2, msg=f"心跳未重注册: {self.admin.ops}")

    def test_up_ready_timeout_terminates_child(self):
        # 只 sleep 不监听：devp 应在 --ready-timeout 1 后终止子进程并退出
        marker = Path(self.tmp.name) / "slow_exited"
        slow_app = f"""
import signal, sys, time
def stop(signum, frame):
    open({str(marker)!r}, "w").write("terminated")
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
time.sleep(10)
"""
        r = run_devp(["up", "--ready-timeout", "1", "slowapp", "--",
                      sys.executable, "-c", slow_app], self.env)
        self.assertNotEqual(r.returncode, 0, msg=r.stdout)
        self.assertIn("就绪超时", r.stderr)
        self.assertTrue(marker.exists(), "就绪超时后子进程未被终止")
        self.assertEqual(self._ops("PATCH", devp.ROUTES_PATH), [])

    def test_up_exits_when_child_dies_before_ready(self):
        r = run_devp(["up", "dieapp", "--", sys.executable, "-c", "raise SystemExit(3)"],
                     self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("子进程先退出", r.stderr)
        self.assertIn("未注册", r.stderr)
        self.assertEqual(self._ops("PATCH", devp.ROUTES_PATH), [])

    def test_up_rejects_duplicate_name(self):
        self.admin.routes.insert(0, devp.build_route("testapp", 1))
        r = run_devp(["up", "testapp", "--", sys.executable, "-c", FAKE_APP], self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("已注册", r.stderr)

    def test_up_fails_when_admin_unreachable(self):
        r = run_devp(["up", "testapp", "--", sys.executable, "-c", FAKE_APP],
                     {"DEVP_ADMIN": "http://127.0.0.1:1", "DEVP_LOCK": str(Path(self.tmp.name) / "l")})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Admin API 不可达", r.stderr)


class TestUpCliParsing(unittest.TestCase):
    """D18：up 的参数解析不再用 REMAINDER，name 之后的选项必须生效。"""

    def setUp(self):
        self.admin = StubAdmin()
        self.admin.routes = [{"match": [{"host": ["*.test"]}], "handle": [], "terminal": True}]
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {
            "DEVP_ADMIN": self.admin.base,
            "DEVP_LOCK": str(Path(self.tmp.name) / "lock"),
            "DEVP_HEARTBEAT": "0.5",
        }

    def tearDown(self):
        self.admin.close()
        self.tmp.cleanup()

    def _dial(self):
        return next(
            route["handle"][0]["upstreams"][0]["dial"]
            for body in self.admin.patch_bodies
            for route in body
            if route.get("@id") == "devp_testapp"
        )

    def test_explicit_port_after_name_is_used(self):
        # devp up <name> --port N -- CMD...：--port 写在 name 之后必须被 devp 消费，
        # 而不是变成子进程参数（Task 4 的 REMAINDER 会吞掉它）
        port = devp.alloc_port()
        r = run_devp(
            ["up", "testapp", "--port", str(port), "--", sys.executable, "-c", FAKE_APP],
            self.env,
        )
        self.assertEqual(r.returncode, 7, msg=r.stderr)
        self.assertEqual(self._dial(), f"127.0.0.1:{port}")

    def test_options_after_name_are_accepted(self):
        r = run_devp(
            ["up", "testapp", "--ready-timeout", "5", "--", sys.executable, "-c", FAKE_APP],
            self.env,
        )
        self.assertEqual(r.returncode, 7, msg=r.stderr)
        self.assertIn("✅ https://testapp.test", r.stdout)

    def test_missing_separator_is_rejected(self):
        r = run_devp(["up", "testapp"], self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("缺少 '--'", r.stderr)

    def test_empty_command_is_rejected(self):
        r = run_devp(["up", "testapp", "--"], self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("缺少启动命令", r.stderr)


class TestDownAndLs(unittest.TestCase):
    def setUp(self):
        self.admin = StubAdmin()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"DEVP_ADMIN": self.admin.base,
                    "DEVP_LOCK": str(Path(self.tmp.name) / "lock")}

    def tearDown(self):
        self.admin.close()
        self.tmp.cleanup()

    def test_down_removes_route(self):
        self.admin.routes = [
            devp.build_route("testapp", 12345),
            {"match": [{"host": ["*.test"]}], "handle": [], "terminal": True},
        ]
        r = run_devp(["down", "testapp"], self.env)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("已注销 testapp.test", r.stdout)
        self.assertEqual(len(self.admin.routes), 1)  # 只剩通配兜底

    def test_down_unknown_fails(self):
        r = run_devp(["down", "ghost"], self.env)
        self.assertNotEqual(r.returncode, 0)

    def test_ls_lists_routes(self):
        self.admin.routes = [devp.build_route("testapp", 12345)]
        r = run_devp(["ls"], self.env)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("testapp.test", r.stdout)
        self.assertIn("127.0.0.1:12345", r.stdout)

    def test_ls_empty(self):
        r = run_devp(["ls"], self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("无 devp 路由", r.stdout)
