# devp 本地开发动态域名网关 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一条命令 `devp up <name> -- CMD...` 启动本地项目：自动分配空闲端口、注册 `https://<name>.test` 到 Caddy，退出/崩溃自动注销。

**Architecture:** devp 是 Python stdlib 单文件 CLI，通过 Caddy Admin API（localhost:2019）管理 `*.test` 路由。Caddyfile 静态增加 `*.test` 通配站点（通配证书 + 404 兜底 + 80 跳转），动态路由用「GET 路由数组 → 插到通配路由之前 → PATCH 回写」注册（POST 追加会被通配路由遮蔽，已实测），按 `@id` DELETE 注销，30s 心跳应对 caddy reload。

**Tech Stack:** Python 3 stdlib only（argparse/subprocess/socket/threading/urllib/fcntl/json）、Caddy v2.11（现有 systemd 用户级服务）、just、unittest（无 pytest）。

**Spec:** `docs/superpowers/specs/2026-10-21-devp-dynamic-domain-gateway-design.md`（本计划与 spec 同仓：`~/work/tasks/local-https/`）

## Global Constraints

- **零三方依赖**：devp 只用 Python 3 标准库；测试只用 unittest。
- 主脚本为**无扩展名文件 `devp`**（shebang `#!/usr/bin/env python3`），测试用 `importlib.machinery.SourceFileLoader` 加载。
- 域名规范：`<name>.test`；name 校验 `^[a-z0-9][a-z0-9-]*$`，长度 ≤ 63，不得含点。
- 路由 `@id` 前缀固定 `devp_`；动态路由 `terminal: true`，Host 头不改写。
- **注册必须 GET→插入通配路由之前→PATCH**；禁止 POST 追加（会被 `*.test` 通配兜底遮蔽）。
- 可覆盖默认值（环境变量，测试与 selftest 依赖）：`DEVP_ADMIN`（默认 `http://127.0.0.1:2019`）、`DEVP_LOCK`（默认 `~/.local/state/devp/lock`）、`DEVP_HEARTBEAT`（默认 `30` 秒）。
- `--ready-timeout` 默认 60s；注销重试 3 次（1s/2s/4s 退避）。
- git 提交信息用中文 + gitmoji，结尾加 `Co-Authored-By: Claude Code <noreply@anthropic.com>`；只 `git add` 本任务触碰的文件（`site/` 等历史文件保持未跟踪）。
- 所有命令在 `~/work/tasks/local-https/` 下执行。

---

### Task 1: devp 骨架 — 常量 + name 校验 + 端口分配

**Files:**
- Create: `devp`
- Create: `tests/__init__.py`（空文件）
- Test: `tests/test_devp.py`

**Interfaces:**
- Consumes: 无（首任务）
- Produces: 模块级常量 `ADMIN_BASE`/`ROUTES_PATH`/`LOCK_PATH`/`HEARTBEAT`（str/str/str/float）；`validate_name(name: str) -> None`（非法时 `sys.exit` 带中文错误）；`alloc_port() -> int`（返回可绑定的随机空闲端口）。后续任务直接引用这些名字。

- [ ] **Step 1: 写失败测试**

`tests/test_devp.py`：

```python
"""devp 单元测试。devp 是无扩展名脚本，用 SourceFileLoader 加载。"""
import importlib.machinery
import importlib.util
import socket
import unittest
from pathlib import Path

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
        for name in ("codeup", "codeup-web", "a1-b2-c3", "x"):
            devp.validate_name(name)  # 不抛异常即通过

    def test_invalid_names_exit(self):
        for name in ("Codeup", "a.b", "-x", "", "a_b", "a b", "x" * 64):
            with self.assertRaises(SystemExit):
                devp.validate_name(name)


class TestAllocPort(unittest.TestCase):
    def test_returns_bindable_ephemeral_port(self):
        port = devp.alloc_port()
        self.assertIsInstance(port, int)
        self.assertTrue(1024 < port < 65536)
        # 返回的端口确实可绑定（bind 后立即释放）
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        s.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest discover -s tests -v`
Expected: FAIL/ERROR（`devp` 文件不存在，加载即报 FileNotFoundError）

- [ ] **Step 3: 最小实现**

创建 `devp`（本任务只写头部 + 三个函数，后续任务追加）：

```python
#!/usr/bin/env python3
"""devp — 本地开发动态域名网关（*.test → 127.0.0.1:动态端口）

用法:
  devp up <name> [--port N] [--ready-timeout S] -- CMD...
  devp down <name>
  devp ls
  devp selftest

注册协议与设计决策见 docs/superpowers/specs/2026-10-21-devp-dynamic-domain-gateway-design.md
"""
import os
import re
import socket
import sys

ADMIN_BASE = os.environ.get("DEVP_ADMIN", "http://127.0.0.1:2019")
ROUTES_PATH = "/config/apps/http/servers/srv0/routes"
LOCK_PATH = os.environ.get("DEVP_LOCK", os.path.expanduser("~/.local/state/devp/lock"))
HEARTBEAT = float(os.environ.get("DEVP_HEARTBEAT", "30"))

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_name(name):
    """非法 name 直接退出：通配证书只覆盖一级子域，name 不得含点。"""
    if not NAME_RE.match(name) or len(name) > 63:
        sys.exit(f"devp: 非法 name {name!r}: 限单标签 [a-z0-9-]（不得含点）")


def alloc_port():
    """绑定 :0 取一个随机空闲端口后立即释放。存在微小时隙竞争，
    子进程 bind 失败时会以子进程自身错误呈现，重跑即可。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
```

`chmod +x devp`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS（3 个用例：valid_names / invalid_names / alloc_port）

- [ ] **Step 5: Commit**

```bash
git add devp tests/__init__.py tests/test_devp.py
git commit -m "✨ feat(devp): 骨架——常量/name 校验/空闲端口分配

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: Admin API 层 — api()/locked()/build_route/insert_route

**Files:**
- Modify: `devp`（追加函数）
- Modify: `tests/test_devp.py`（追加测试与 StubAdmin）

**Interfaces:**
- Consumes: Task 1 的 `ADMIN_BASE`/`ROUTES_PATH`/`LOCK_PATH`。
- Produces:
  - `api(method: str, path: str, body=None, base: str|None=None) -> tuple[int, any]`（HTTPError 映射为 `(code, None)`，连接失败映射为 `(0, None)`）
  - `locked(path: str|None=None)`（上下文管理器，flock 互斥）
  - `build_route(name: str, port: int) -> dict`（含 `@id: devp_<name>`）
  - `insert_route(routes: list[dict], route: dict) -> list[dict]`（插到首个通配 host 路由之前）
  - `is_wildcard(route: dict) -> bool`
  - 测试侧 `StubAdmin`（内存版 Admin API，`base`/`routes`/`ops` 属性），Task 3/4/5 复用。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_devp.py`（文件顶部 import 补充 `json`、`threading`、`from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer`、`from contextlib import contextmanager` 不需要；补 `import tempfile`）：

```python
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "sub" / "lock")  # 目录不存在也要能建
            with devp.locked(p):
                self.assertTrue(Path(p).exists())
            # 释放后可再次加锁
            with devp.locked(p):
                pass
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest discover -s tests -v`
Expected: 新增用例 FAIL（`devp.api`/`build_route` 等不存在 → AttributeError）

- [ ] **Step 3: 实现**

`devp` 顶部 import 区追加：

```python
import fcntl
import json
import urllib.error
import urllib.request
from contextlib import contextmanager
```

追加函数：

```python
def api(method, path, body=None, base=None):
    """调用 Caddy Admin API。HTTP 错误返回 (code, None)，连接失败返回 (0, None)。"""
    url = (base or ADMIN_BASE).rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError):
        return 0, None


@contextmanager
def locked(path=None):
    """flock 互斥，包住 GET+PATCH 读改写临界区。"""
    path = path or LOCK_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fh.close()


def build_route(name, port):
    return {
        "@id": f"devp_{name}",
        "match": [{"host": [f"{name}.test"]}],
        "handle": [{"handler": "reverse_proxy",
                    "upstreams": [{"dial": f"127.0.0.1:{port}"}]}],
        "terminal": True,
    }


def is_wildcard(route):
    return any("*" in h for m in route.get("match", []) for h in m.get("host", []))


def insert_route(routes, route):
    """插到首个通配 host 路由之前（POST 追加会被 *.test 兜底遮蔽，实测结论）。"""
    idx = next((i for i, r in enumerate(routes) if is_wildcard(r)), len(routes))
    routes.insert(idx, route)
    return routes
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS 全绿

- [ ] **Step 5: Commit**

```bash
git add devp tests/test_devp.py
git commit -m "✨ feat(devp): Admin API 封装 + 路由构造/插前逻辑

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: 注册/注销/查询原语 — register/deregister/route_exists/list_routes

**Files:**
- Modify: `devp`
- Modify: `tests/test_devp.py`

**Interfaces:**
- Consumes: Task 2 的 `api`/`locked`/`build_route`/`insert_route`，Task 1 的 `ROUTES_PATH`。
- Produces:
  - `register(name: str, port: int, base: str|None=None) -> bool`
  - `deregister(name: str, base: str|None=None) -> bool`
  - `route_exists(name: str, base: str|None=None) -> bool`
  - `register_retry(name: str, port: int, base: str|None=None, attempts: int=3) -> bool`（1s/2s/4s 退避，测试传 `attempts=1` 避免等待）
  - `list_routes(base: str|None=None) -> list[tuple[str, str, str, int]]`（(name, host, dial, port)）
  - `reachable(port: int, timeout: float=0.5) -> bool`（TCP 探测）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_devp.py`：

```python
class TestRegisterLifecycle(unittest.TestCase):
    def setUp(self):
        self.admin = StubAdmin()
        self.admin.routes = [{"match": [{"host": ["*.test"]}], "handle": [], "terminal": True}]

    def tearDown(self):
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

    def test_list_routes(self):
        devp.register("a", 1111, base=self.admin.base)
        devp.register("b", 2222, base=self.admin.base)
        self.assertEqual(devp.list_routes(base=self.admin.base), [
            ("a", "a.test", "127.0.0.1:1111", 1111),
            ("b", "b.test", "127.0.0.1:2222", 2222),
        ])

    def test_register_retry_eventually_succeeds(self):
        # 第一次 GET 前 stub 崩掉 503？——简化：直接验证 attempts=1 失败路径返回 False
        self.assertFalse(devp.register_retry("x", 1, base="http://127.0.0.1:1", attempts=1))


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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest discover -s tests -v`
Expected: 新增用例 FAIL（`register` 等不存在）

- [ ] **Step 3: 实现**

`devp` 顶部 import 区追加 `import time`。追加函数：

```python
def register(name, port, base=None):
    """GET 路由数组 → 插到通配路由之前 → PATCH 回写。flock 防并发。"""
    with locked():
        st, routes = api("GET", ROUTES_PATH, base=base)
        if st != 200:
            return False
        insert_route(routes, build_route(name, port))
        st, _ = api("PATCH", ROUTES_PATH, body=routes, base=base)
        return st == 200


def deregister(name, base=None):
    st, _ = api("DELETE", f"/id/devp_{name}", base=base)
    return st == 200


def route_exists(name, base=None):
    st, _ = api("GET", f"/id/devp_{name}", base=base)
    return st == 200


def register_retry(name, port, base=None, attempts=3):
    """注册重试：1s/2s/4s 退避。"""
    for i in range(attempts):
        if register(name, port, base=base):
            return True
        if i < attempts - 1:
            time.sleep(2 ** i)
    return False


def list_routes(base=None):
    st, routes = api("GET", ROUTES_PATH, base=base)
    out = []
    for r in routes or []:
        rid = r.get("@id", "")
        if not rid.startswith("devp_"):
            continue
        name = rid[len("devp_"):]
        host = r["match"][0]["host"][0]
        dial = r["handle"][0]["upstreams"][0]["dial"]
        out.append((name, host, dial, int(dial.rsplit(":", 1)[1])))
    return out


def reachable(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS 全绿

- [ ] **Step 5: Commit**

```bash
git add devp tests/test_devp.py
git commit -m "✨ feat(devp): 注册/注销/查询原语 + 退避重试

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: `up` 生命周期 + 心跳 + CLI 入口

**Files:**
- Modify: `devp`
- Modify: `tests/test_devp.py`

**Interfaces:**
- Consumes: Task 1-3 全部原语。
- Produces:
  - `cmd_up(a)`：完整生命周期；成功路径打印 `✅ https://<name>.test`；子进程退出码透传（`sys.exit(rc)`）
  - `heartbeat(name: str, port: int, stop: threading.Event, interval: float|None=None)`：daemon 线程体
  - `main(argv: list|None=None)`：argparse 入口，`up`/`down`/`ls`/`selftest` 四个子命令（`down`/`ls`/`selftest` 的 handler 在 Task 5/6 落地前先 `pass` 占位由对应任务替换——**本任务先只注册 up 分支，后续任务扩展 main**）
  - `if __name__ == "__main__": main()` 入口

- [ ] **Step 1: 写失败测试（子进程集成测试）**

追加到 `tests/test_devp.py`（顶部 import 补 `import os`、`import subprocess`、`import sys`）：

```python
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
        # 注册发生过，且插到了通配路由之前
        self.assertEqual(self.admin.routes[0].get("@id"), "devp_testapp")
        # 子进程退出后已注销
        self.assertFalse(any(r_.get("@id") == "devp_testapp" for r_ in self.admin.routes))
        self.assertIn(("DELETE", "/id/devp_testapp"), self.admin.ops)

    def test_up_heartbeat_reregisters_after_route_loss(self):
        # 起一个后台线程在注册后 ~1.2s 删掉路由，心跳(0.5s)应重注册
        def sabotage():
            time.sleep(1.2)
            self.admin.routes = [r for r in self.admin.routes if r.get("@id") != "devp_testapp"]
        threading.Thread(target=sabotage, daemon=True).start()
        before = len(self._ops("PATCH", devp.ROUTES_PATH))
        r = run_devp(["up", "testapp", "--", sys.executable, "-c", FAKE_APP], self.env)
        self.assertEqual(r.returncode, 7, msg=r.stderr)
        patches = self._ops("PATCH", devp.ROUTES_PATH)
        self.assertGreaterEqual(len(patches), before + 2, msg=f"心跳未重注册: {self.admin.ops}")

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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest discover -s tests -v`
Expected: 新增用例 FAIL（`main` 不存在 → devp 作为脚本运行无输出/退出码错）

- [ ] **Step 3: 实现**

`devp` 顶部 import 区追加：

```python
import argparse
import signal
import subprocess
import threading
```

（`signal` 本任务暂不强依赖，up 用 KeyboardInterrupt 路径；为避免未用 import，**不要**加 `signal`，Task 5 亦不需要。最终 import 区：`argparse/subprocess/threading`。）

追加：

```python
def cmd_up(a):
    validate_name(a.name)
    st, _ = api("GET", "/config/")
    if st != 200:
        sys.exit("devp: Caddy Admin API 不可达（caddy 是否在运行？）")
    if route_exists(a.name):
        sys.exit(f"devp: {a.name} 已注册，先 devp down {a.name} 或换名")
    port = a.port or alloc_port()
    env = dict(os.environ, PORT=str(port))
    print(f"PORT={port} 已分配，启动: {' '.join(a.cmd)}")
    proc = subprocess.Popen(a.cmd, env=env)
    print(f"等待 127.0.0.1:{port} 就绪（超时 {a.ready_timeout}s）…")
    deadline = time.time() + a.ready_timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit(f"devp: 子进程先退出（code={proc.returncode}），未注册")
        if reachable(port, timeout=1):
            break
        time.sleep(0.3)
    else:
        proc.terminate()
        sys.exit("devp: 就绪超时——应用可能未使用 $PORT，可用 --port 显式指定")
    if not register_retry(a.name, port):
        proc.terminate()
        sys.exit("devp: 注册失败（Admin API PATCH 重试 3 次未成功）")
    print(f"✅ https://{a.name}.test")
    stop = threading.Event()
    threading.Thread(target=heartbeat, args=(a.name, port, stop), daemon=True).start()
    rc = 0
    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        # 同进程组：子进程也已收到 SIGINT，给 5s 宽限后强杀
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
    finally:
        stop.set()
        if not deregister(a.name):
            print(f"⚠️ devp: 注销失败，残留路由请执行 devp down {a.name}", file=sys.stderr)
    sys.exit(rc)


def heartbeat(name, port, stop, interval=None):
    """每 interval 校验路由存在，丢失（caddy reload/重启）则重注册。"""
    interval = interval or HEARTBEAT
    while not stop.wait(interval):
        try:
            if not route_exists(name):
                register_retry(name, port)
        except OSError:
            pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="devp",
                                description="本地开发动态域名网关（*.test → 动态端口）")
    sub = p.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="分配端口并启动命令，注册 <name>.test")
    up.add_argument("name", help="路由名（单标签 [a-z0-9-]）")
    up.add_argument("--port", type=int, default=None, help="显式端口（默认自动分配）")
    up.add_argument("--ready-timeout", type=int, default=60,
                    help="就绪超时秒数（默认 60）")
    up.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- CMD...",
                    help="启动命令（会获得 PORT 环境变量）")

    a = p.parse_args(argv)
    if a.command == "up":
        if a.cmd and a.cmd[0] == "--":
            a.cmd = a.cmd[1:]
        if not a.cmd:
            p.error("缺少启动命令：devp up <name> -- CMD...")
        cmd_up(a)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS 全绿（4 个集成用例各约 3-5s）

- [ ] **Step 5: Commit**

```bash
git add devp tests/test_devp.py
git commit -m "✨ feat(devp): up 生命周期——PORT 注入/就绪轮询/注册/心跳/注销

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: `down` / `ls` 命令

**Files:**
- Modify: `devp`
- Modify: `tests/test_devp.py`

**Interfaces:**
- Consumes: Task 3 的 `deregister`/`list_routes`/`reachable`，Task 1 的 `validate_name`，Task 4 的 `main`。
- Produces: `cmd_down(a)`、`cmd_ls(a)`；`main` 中新增 `down`/`ls` 分支。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_devp.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest discover -s tests -v`
Expected: 新增用例 FAIL（invalid choice: 'down'）

- [ ] **Step 3: 实现**

`devp` 追加：

```python
def cmd_down(a):
    validate_name(a.name)
    if deregister(a.name):
        print(f"已注销 {a.name}.test")
    else:
        sys.exit(f"devp: {a.name} 未注册（或注销失败）")


def cmd_ls(a):
    rows = list_routes()
    if not rows:
        print("（无 devp 路由）")
        return
    for name, host, dial, port in rows:
        mark = "✅" if reachable(port) else "⛔ 不可达"
        print(f"{host:40s} {dial:22s} {mark}")
```

`main()` 中 `up = sub.add_parser(...)` 块之后、`a = p.parse_args(argv)` 之前追加：

```python
    down = sub.add_parser("down", help="注销 <name>.test")
    down.add_argument("name")

    sub.add_parser("ls", help="列出 devp 路由")
```

`main()` 分支区追加：

```python
    elif a.command == "down":
        cmd_down(a)
    elif a.command == "ls":
        cmd_ls(a)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS 全绿

- [ ] **Step 5: Commit**

```bash
git add devp tests/test_devp.py
git commit -m "✨ feat(devp): down/ls 命令——注销与路由清单

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: `selftest` + Caddyfile 通配站点上线

**Files:**
- Modify: `devp`
- Modify: `Caddyfile`（文件当前未跟踪，本任务 `git add Caddyfile` 首次纳管）
- Modify: `tests/test_devp.py`（无新增单测；selftest 本身就是端到端测试，由 Step 4 实跑验证）

**Interfaces:**
- Consumes: Task 3/4/5 的 `register`/`deregister`/`route_exists`/`api`，Task 4 的 `main`。
- Produces: `cmd_selftest(a)`；`main` 中新增 `selftest` 分支；生产 Caddyfile 的 `*.test` 通配站点 + 80 跳转。

- [ ] **Step 1: 实现 selftest（无单测——它就是测试）**

`devp` 追加：

```python
def cmd_selftest(a):
    """隔离 Caddy 实例（独立端口/存储）端到端自检，不碰生产 443/2019。"""
    import http.client
    import http.server
    import shutil
    import ssl
    import tempfile

    caddy = shutil.which("caddy") or os.path.expanduser("~/.local/bin/caddy")
    if not os.path.exists(caddy):
        sys.exit(f"devp: 找不到 caddy（{caddy}）")
    tmp = tempfile.mkdtemp(prefix="devp-selftest-")
    admin_port, https_port, http_port = 20191, 14443, 14080
    cfg = (f"{{\n"
           f"\tadmin localhost:{admin_port}\n"
           f"\tstorage file_system {tmp}/data\n"
           f"\thttp_port {http_port}\n"
           f"\thttps_port {https_port}\n"
           f"}}\n\n"
           f"*.test {{\n"
           f"\ttls internal\n"
           f"\trespond \"fallback\" 404\n"
           f"}}\n")
    cfg_path = os.path.join(tmp, "Caddyfile")
    with open(cfg_path, "w") as f:
        f.write(cfg)
    base = f"http://127.0.0.1:{admin_port}"

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hello-from-upstream")

        def log_message(self, *args):
            pass

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    uport = upstream.server_address[1]

    caddy_proc = subprocess.Popen([caddy, "run", "--config", cfg_path],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failures = []

    def check(desc, ok):
        print(("  ✅ " if ok else "  ❌ ") + desc)
        if not ok:
            failures.append(desc)

    def fetch(host):
        # foo.test 经 dnsmasq 解析到 127.0.0.1/::1，直连沙箱 caddy 的 https_port
        ctx = ssl._create_unverified_context()
        conn = http.client.HTTPSConnection(host, https_port, context=ctx, timeout=5)
        conn.request("GET", "/")
        r = conn.getresponse()
        body = r.read().decode()
        conn.close()
        return r.status, body

    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            st, _ = api("GET", "/config/", base=base)
            if st == 200:
                break
            time.sleep(0.3)
        else:
            sys.exit("devp: selftest 沙箱 caddy 启动超时")

        check("通配站点可服务（未注册域名兜底 404）", fetch("anything-unregistered.test")[0] == 404)
        check("注册前 foo.test 命中兜底 404", fetch("foo.test")[0] == 404)
        check("register(foo) 成功", register("foo", uport, base=base))
        check("注册后 foo.test 命中上游", fetch("foo.test") == (200, "hello-from-upstream"))
        check("route_exists(foo)", route_exists("foo", base=base))
        check("deregister(foo) 成功", deregister("foo", base=base))
        check("注销后 foo.test 回落兜底 404", fetch("foo.test")[0] == 404)
    finally:
        caddy_proc.terminate()
        upstream.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        sys.exit(f"devp: selftest 失败 {len(failures)} 项：{'；'.join(failures)}")
    print("selftest 全部通过 ✅")
```

`main()` 子命令区追加：

```python
    sub.add_parser("selftest", help="隔离 Caddy 实例端到端自检")
```

分支区追加：

```python
    elif a.command == "selftest":
        cmd_selftest(a)
```

- [ ] **Step 2: 修改生产 Caddyfile**

`Caddyfile` 末尾追加（保持现有 `localhost` / `nginx.local.yikuaigao.com` 块不动）：

```caddyfile

# devp 动态注册的本地项目网关
# 未注册的 *.test 域名返回 404；devp up 通过 Admin API 把精确域名路由插到本块之前
*.test {
	tls internal
	respond "devp: no route registered for {http.request.host}" 404
}

http://*.test {
	redir https://{host}{uri} permanent
}
```

- [ ] **Step 3: 校验并热加载生产 caddy**

Run: `just validate && just reload`
Expected: validate 输出 `Valid configuration`；reload 无报错。

- [ ] **Step 4: 实跑 selftest 与生产兜底验证**

Run: `python3 devp selftest`
Expected: 7 项全 ✅ + `selftest 全部通过 ✅`

Run: `curl -sk -o /dev/null -w '%{http_code}\n' https://never-registered-xyz.test`
Expected: `404`（生产 caddy 兜底生效；通配证书已签发）

Run: `curl -s -o /dev/null -w '%{http_code} -> %{redirect_url}\n' http://never-registered-xyz.test`
Expected: `308 -> https://never-registered-xyz.test/`（80 跳 443）

- [ ] **Step 5: 回归全量单测**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS 全绿

- [ ] **Step 6: Commit**

```bash
git add devp Caddyfile
git commit -m "✨ feat(devp): selftest 端到端自检 + Caddyfile *.test 通配网关上线

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: 安装配方 + README + 真实验收

**Files:**
- Modify: `Justfile`
- Modify: `README.md`（当前未跟踪，本任务 `git add README.md` 首次纳管）

**Interfaces:**
- Consumes: Task 1-6 全部产出。
- Produces: `just install-devp` 配方；README devp 章节。

- [ ] **Step 1: Justfile 追加配方**

`Justfile` 末尾追加：

```make

# 安装 devp CLI 到 ~/.local/bin（修改 devp 脚本后需重跑）
install-devp:
    install -m 755 devp {{home_dir()}}/.local/bin/devp
    {{home_dir()}}/.local/bin/devp --help > /dev/null && echo "devp 已安装到 ~/.local/bin/devp"
```

- [ ] **Step 2: README 追加 devp 章节**

`README.md` 末尾追加：

```markdown
## devp — 本地开发动态域名网关

`*.test` 域名下的本地项目反向代理：一条命令启动，自动分配空闲端口并注册
`https://<name>.test`，退出/崩溃自动注销。`.test`（本机进程）与
`.local.yikuaigao.com`（minikube/APISIX）两条链路互不影响。

```bash
just install-devp                      # 安装 CLI（~/.local/bin/devp）

devp up codeup-web -- npm run dev -- --port $PORT   # → https://codeup-web.test
devp up codeup-api -- uvicorn app:app --port $PORT  # → https://codeup-api.test

devp ls                                # 查看已注册路由（含可达性）
devp down codeup-web                   # 手动清理残留路由
devp selftest                          # 隔离实例端到端自检
```

要点：

- **应用需使用 `$PORT`**：Next.js/CRA/gunicorn 原生读 `PORT` 环境变量；Vite/uvicorn
  在命令里显式传 `--port $PORT`。不配合的应用用 `devp up <name> --port 3000 -- CMD...`。
- **Host 校验放行**：请求的 Host 头是 `<name>.test`，Vite 需 `server.allowedHosts`、
  Django 需 `ALLOWED_HOSTS` 加上该域名。
- **caddy 重启后**最长 30s 内已注册域名 404（心跳周期内自动恢复）。
- name 限单标签 `[a-z0-9-]`（通配证书 `*.test` 只覆盖一级子域），分支信息编码进名字
  （如 `codeup-featx`）。
- 注册协议：Admin API GET 路由数组 → 插到 `*.test` 通配兜底之前 → PATCH 回写
  （POST 追加会被通配路由遮蔽）；注销按 `@id` DELETE。详见
  `docs/superpowers/specs/2026-10-21-devp-dynamic-domain-gateway-design.md`。
```

- [ ] **Step 3: 安装并验收**

Run: `just install-devp && devp --help`
Expected: 输出 usage 帮助。

真实验收（codeup-manager 双域名并行，两个终端）：

```bash
# 终端 1（codeup-manager 仓库）
devp up codeup-api -- uv run uvicorn codeup_manager.web.app:app --host 127.0.0.1 --port $PORT
# 终端 2（codeup-manager 仓库，frontend）
devp up codeup-web -- npx vite --port $PORT --host 127.0.0.1
```

验证清单：
- [ ] 浏览器打开 `https://codeup-api.test` 返回 API（如 `/api/...` 端点或文档页）
- [ ] 浏览器打开 `https://codeup-web.test` 返回前端页面（Vite 若报 host 校验错，`vite.config.ts` 加 `server: { allowedHosts: true }` 后重启 devp）
- [ ] 两个终端各按一次 Ctrl+C，`devp ls` 显示「（无 devp 路由）」，浏览器再访问两域名得 404 兜底

（若 uvicorn 入口路径与上面示例不符，以 `just dev` 实际命令为准替换 CMD 部分。）

- [ ] **Step 4: Commit**

```bash
git add Justfile README.md
git commit -m "✨ feat(devp): install-devp 配方 + README 使用手册

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Self-Review 记录

- **Spec 覆盖**：§4 Caddyfile→Task 6；§5 注册协议/心跳/flock→Task 2/3/4；§6 四命令→Task 4/5/6，name 校验→Task 1，预检/重复拒绝/退出码透传→Task 4；§7 错误处理（Admin 不可达预检、注册重试、注销警告、心跳静默）→Task 3/4；§8 取舍已在 README（Task 7）；§9 selftest + 真实验收→Task 6/7。无缺口。
- **占位符**：Task 4 Interfaces 段提到「down/ls/selftest handler 先占位」已改为「本任务只注册 up 分支，后续任务扩展 main」，各任务代码完整。
- **类型一致性**：`register(name, port, base)`、`route_exists(name, base)`、`api(method, path, body, base)`、`list_routes() -> [(name, host, dial, port)]` 在 Task 2-6 间签名一致；`main` 的 subparsers dest 用 `command` 避免与 REMAINDER 位置参数 `cmd` 冲突。
