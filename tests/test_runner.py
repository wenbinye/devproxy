import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import devproxy
from devproxy.config import Config
from devproxy.proxy.caddy import CaddyAdmin
from devproxy.proxy.routes import Route
from devproxy.proxy.runner import (
    UpError,
    UpRunner,
    alloc_port,
    kill_pid_group,
    reachable,
    stop_process_group,
)
from devproxy.proxy.store import RouteStore

# 信号测试要在独立 python 子进程里 import devproxy —— 告诉它源码在哪
SRC_DIR = str(Path(devproxy.__file__).resolve().parent.parent)


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


def test_run_kills_process_on_ready_timeout(paths, tmp_path):
    """就绪超时 → 杀整个子进程组（含孙进程）→ 不注册路由。"""
    paths.ensure_files()
    runner = _runner(paths)
    marker = tmp_path / "timeout.pids"
    child_code = (
        "import os,subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"open({str(marker)!r},'w').write(f'{{os.getpid()}} {{p.pid}}');"
        "time.sleep(60)"
    )
    with pytest.raises(UpError) as cm:
        runner.run(
            "slow",
            [sys.executable, "-c", child_code],
            ready_timeout=1,
        )
    assert "就绪超时" in str(cm.value)
    assert not paths.routes_json.exists(), "超时不得留下路由"
    deadline = time.time() + 5
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    child_pid, grandchild_pid = (int(x) for x in marker.read_text().split())
    time.sleep(0.3)
    assert not _process_running(child_pid), "超时后子进程必须被杀"
    assert not _process_running(grandchild_pid), "超时后孙进程必须被按组清理"


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


def test_watch_re_registers_when_only_prefixed_domain_remains(paths):
    """Caddy 里只剩 ax.test 时，路由 x.test 不得被裸子串匹配误判为存活。"""
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

    # 只含另一条路由 ax.test —— 裸子串 "x.test" in '"ax.test"' 会命中
    admin.config_payload = {
        "apps": {
            "http": {
                "servers": {
                    "srv0": {"routes": [{"match": [{"host": ["ax.test"]}]}]}
                }
            }
        }
    }
    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    time.sleep(0.5)
    stop.set()
    t.join(timeout=2)
    assert caddy.reloads > before, "前缀域名（ax.test）不得让 x.test 伪装成存活"


def test_run_deregisters_when_signal_lands_during_registration(paths):
    """SIGTERM 恰好落在注册事务（validate+reload 窗口）内 → 路由也必须被注销。

    第一次 reload 即注册事务：在其中向本进程发 SIGTERM，信号处理程序
    raise SystemExit 从 mutate 内部抛出（无回滚），由 run() 的 finally 兜底注销。
    第二次 reload 落在 finally 的注销事务里 —— 此时的第二个信号必须被忽略
    （stop 已置位），否则注销事务被打断、路由残留指向已死的子进程。
    """
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))

    class SignalingCaddy(FakeCaddy):
        """每次 reload 都向本进程发 SIGTERM —— 分别命中注册与注销两个窗口。"""

        def reload(self, caddyfile=None):
            ok = super().reload(caddyfile)
            os.kill(os.getpid(), signal.SIGTERM)
            return ok

    caddy = SignalingCaddy()
    store = RouteStore(paths, cfg, caddy)
    runner = UpRunner(store, cfg, FakeAdmin())
    with pytest.raises(SystemExit) as cm:
        runner.run("codeup", [sys.executable, "-c", _LISTEN_AND_EXIT])
    assert cm.value.code == 143
    table = json.loads(paths.routes_json.read_text())
    assert table["routes"] == [], "信号中断注册后不得留下指向死端口的路由"
    assert caddy.reloads == 2, "注册一次 + 注销一次；第二个信号不得打断注销"


def test_run_signal_forwards_to_group_and_deregisters(paths, tmp_path):
    """SIGTERM → 按组转发子进程、注销路由、runner 以 128+15=143 退出。

    在独立 python 子进程里跑 UpRunner.run（否则信号会打到测试进程本身），
    路由表通过 DEVPROXY_*_DIR 指向 tmp_path 供本测试检查。
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    grandchild_pid_file = tmp_path / "signal.grandchild.pid"
    child_code = tmp_path / "signal_child.py"
    child_code.write_text(
        "import os, socket, subprocess, sys, time\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', int(os.environ['PORT'])))\n"
        "s.listen(8)\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "open(os.environ['GRANDCHILD_PID_FILE'], 'w').write(str(g.pid))\n"
        "time.sleep(300)\n"
    )

    runner_main = tmp_path / "signal_runner.py"
    runner_main.write_text(
        "import os, sys\n"
        "sys.path.insert(0, os.environ['DEVPROXY_SRC'])\n"
        "from devproxy.config import Config\n"
        "from devproxy.paths import default_paths\n"
        "from devproxy.proxy.runner import UpRunner\n"
        "from devproxy.proxy.store import RouteStore\n"
        "\n"
        "class FakeCaddy:\n"
        "    def validate(self, caddyfile=None):\n"
        "        return True, 'ok'\n"
        "\n"
        "    def reload(self, caddyfile=None):\n"
        "        return True, 'ok'\n"
        "\n"
        "class FakeAdmin:\n"
        "    def is_alive(self):\n"
        "        return True\n"
        "\n"
        "    def api(self, method, path, body=None):\n"
        "        return 200, {}\n"
        "\n"
        "paths = default_paths()\n"
        "paths.ensure_files()\n"
        "cfg = Config(storage=str(paths.data_dir))\n"
        "store = RouteStore(paths, cfg, FakeCaddy())\n"
        "runner = UpRunner(store, cfg, FakeAdmin())\n"
        "rc = runner.run(\n"
        "    'codeup',\n"
        "    [sys.executable, os.environ['CHILD_CODE']],\n"
        "    port=int(os.environ['CHILD_PORT']),\n"
        ")\n"
        "sys.exit(rc)\n"
    )

    env = dict(
        os.environ,
        DEVPROXY_CONFIG_DIR=str(paths.config_dir),
        DEVPROXY_STATE_DIR=str(paths.state_dir),
        DEVPROXY_DATA_DIR=str(paths.data_dir),
        DEVPROXY_SRC=SRC_DIR,
        CHILD_CODE=str(child_code),
        CHILD_PORT=str(port),
        GRANDCHILD_PID_FILE=str(grandchild_pid_file),
    )
    proc = subprocess.Popen(
        [sys.executable, str(runner_main)],
        env=env,
        start_new_session=True,  # 独立会话：SIGTERM 只打到 runner 本身
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_pid = None
    try:
        # 等注册完成（拿到子进程 pid）且孙进程 pid 已写出
        deadline = time.time() + 20
        table = None
        while time.time() < deadline and (
            table is None or not grandchild_pid_file.exists()
        ):
            assert proc.poll() is None, f"runner 提前退出 rc={proc.returncode}"
            if paths.routes_json.exists():
                loaded = json.loads(paths.routes_json.read_text())
                if loaded.get("routes"):
                    table = loaded
            time.sleep(0.1)
        assert table is not None, "20s 内路由未注册"
        child_pid = table["routes"][0]["pid"]
        grandchild_pid = int(grandchild_pid_file.read_text())

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=20)
        assert rc == 143, f"被 SIGTERM 杀死应以 128+15 退出，实际 {rc}"

        table = json.loads(paths.routes_json.read_text())
        assert table["routes"] == [], "信号退出后路由必须注销"
        time.sleep(0.3)
        assert not _process_running(child_pid), "子进程应被按组清理"
        assert not _process_running(grandchild_pid), "孙进程应被按组清理"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if child_pid is not None and _process_running(child_pid):
            kill_pid_group(child_pid)
