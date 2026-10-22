import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from devproxy.config import Config
from devproxy.proxy.caddy import CaddyAdmin
from devproxy.proxy.routes import Route
from devproxy.proxy.runner import (
    UpError,
    UpRunner,
    alloc_port,
    reachable,
    stop_process_group,
)
from devproxy.proxy.store import RouteStore


class FakeCaddy:
    """假 Caddy：validate/reload 恒成功，并记录 reload 次数。"""

    def __init__(self):
        self.reloads = 0

    def validate(self, caddyfile=None):
        return True, "ok"

    def reload(self, caddyfile=None):
        self.reloads += 1
        return True, "ok"


class FakeAdmin:
    """假 Admin API 客户端。config_payload 模拟 Caddy 当前配置。"""

    def __init__(self, alive=True):
        self.alive = alive
        self.config_payload = {}

    def is_alive(self):
        return self.alive

    def api(self, method, path, body=None):
        return 200, self.config_payload


def _runner(paths, alive=True):
    cfg = Config(storage=str(paths.data_dir))
    store = RouteStore(paths, cfg, FakeCaddy())
    return UpRunner(store, cfg, FakeAdmin(alive=alive))


# 起一个真正监听 $PORT 的子进程，并在 0.5s 后退出 —— 同时验证 PORT 注入
_LISTEN_AND_EXIT = (
    "import http.server,os,threading,time;"
    "s=http.server.HTTPServer(('127.0.0.1',int(os.environ['PORT'])),"
    "http.server.SimpleHTTPRequestHandler);"
    "threading.Thread(target=s.serve_forever,daemon=True).start();"
    "time.sleep(0.5)"
)


def _process_running(pid: int) -> bool:
    """僵尸进程不算活着。/proc 不存在时退回 kill(pid,0) 探测。"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False


def test_alloc_port_returns_bindable_port():
    port = alloc_port()
    assert 1024 < port < 65536
    s = socket.socket()
    s.bind(("127.0.0.1", port))
    s.close()


def test_reachable_true_and_false():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert reachable(port, timeout=1.0) is True
    finally:
        s.close()
    assert reachable(port, timeout=0.2) is False


def test_stop_process_group_kills_grandchildren(tmp_path):
    """子进程 fork 出孙进程后，stop_process_group 必须按组清理干净。"""
    marker = tmp_path / "grandchild.pid"
    script = tmp_path / "spawn.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"(sleep 60 & echo $! > {marker})\n"
        "sleep 60\n"
    )
    script.chmod(0o755)
    proc = subprocess.Popen(
        [str(script)], start_new_session=True, stdout=subprocess.DEVNULL
    )
    deadline = time.time() + 5
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), "孙进程 pid 未写出"
    gpid = int(marker.read_text().strip())
    stop_process_group(proc, proc.pid)
    time.sleep(0.5)
    assert not _process_running(gpid), "孙进程应已被按组清理"


def test_run_requires_caddy_alive(paths):
    paths.ensure_files()
    runner = _runner(paths, alive=False)
    with pytest.raises(UpError) as cm:
        runner.run("codeup", [sys.executable, "-c", "import time; time.sleep(1)"])
    assert "Admin API" in str(cm.value)


def test_run_rejects_occupied_explicit_port(paths):
    paths.ensure_files()
    runner = _runner(paths)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        with pytest.raises(UpError) as cm:
            runner.run("codeup", [sys.executable, "-c", "pass"], port=port)
        assert "已被占用" in str(cm.value)
    finally:
        s.close()


def test_run_rejects_invalid_name(paths):
    paths.ensure_files()
    runner = _runner(paths)
    with pytest.raises(UpError):
        runner.run("bad.name", [sys.executable, "-c", "pass"])


def test_run_rejects_invalid_port(paths):
    paths.ensure_files()
    runner = _runner(paths)
    with pytest.raises(UpError):
        runner.run("codeup", [sys.executable, "-c", "pass"], port=0)


def test_run_injects_port_and_registers_route(paths):
    """子进程应拿到 PORT 环境变量；就绪后注册路由。"""
    paths.ensure_files()
    runner = _runner(paths)
    ready = []
    # 子进程 0.5s 后退出，run() 返回前路由会被注销；
    # 因此在 on_ready（注册刚落盘）时刻抓路由表快照来断言注册结果
    snapshots = []

    def on_ready(domain, port):
        ready.append((domain, port))
        snapshots.append(json.loads(paths.routes_json.read_text()))

    rc = runner.run(
        "codeup",
        [sys.executable, "-c", _LISTEN_AND_EXIT],
        on_ready=on_ready,
    )
    assert rc == 0
    assert ready and ready[0][0] == "codeup.test"
    table = snapshots[0]
    assert table["routes"][0]["name"] == "codeup"
    assert table["routes"][0]["mode"] == "up"
    assert table["routes"][0]["port"] == ready[0][1]
    assert table["routes"][0]["command"][0] == sys.executable


def test_run_cleans_up_route_when_child_exits(paths):
    """子进程退出后路由必须被注销。"""
    paths.ensure_files()
    runner = _runner(paths)
    runner.run("codeup", [sys.executable, "-c", _LISTEN_AND_EXIT])
    table = json.loads(paths.routes_json.read_text())
    assert table["routes"] == [], "子进程退出后路由必须注销"


def test_run_kills_process_on_ready_timeout(paths):
    paths.ensure_files()
    runner = _runner(paths)
    with pytest.raises(UpError) as cm:
        runner.run(
            "slow",
            [sys.executable, "-c", "import time; time.sleep(30)"],
            ready_timeout=1,
        )
    assert "就绪超时" in str(cm.value)
    assert not paths.routes_json.exists(), "超时不得留下路由"


def test_watch_re_registers_when_route_lost_from_caddy(paths):
    """Caddy 里路由丢了、但路由表里还在 → 自检应重新注册。"""
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.1

    route = Route(
        name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1"
    )
    store.mutate(lambda t: t.upsert(route))
    before = caddy.reloads

    admin.config_payload = {}  # Caddy 当前配置里没有它
    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    time.sleep(0.5)
    stop.set()
    t.join(timeout=2)
    assert caddy.reloads > before, "路由丢失时应触发重新注册"


def test_watch_does_not_revive_removed_route(paths):
    """路由表里已经没有它（用户主动 down 过）→ 自检不得复活。"""
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.1

    route = Route(
        name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1"
    )
    store.mutate(lambda t: t.upsert(route))
    store.mutate(lambda t: t.remove("x"))
    before = caddy.reloads

    admin.config_payload = {}
    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    time.sleep(0.4)
    stop.set()
    t.join(timeout=2)
    assert caddy.reloads == before, "用户已注销的路由不应被复活"
