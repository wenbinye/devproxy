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
from devproxy.core.config import Config
from devproxy.gateway.caddy import CaddyAdmin
from devproxy.gateway.routes import Route
from devproxy.gateway.runner import (
    PreconditionError,
    UpError,
    UpRunner,
    alloc_port,
    kill_pid_group,
    proc_start_time,
    reachable,
    stop_process_group,
)
from devproxy.gateway.store import RouteStore

from .support import wait_until

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
    """僵尸进程不算活着。/proc 不存在时（macOS）退回 kill(pid,0) 探测。

    两个分支必须合在一条 `except OSError` 里：`FileNotFoundError` 是
    `OSError` 的子类，单列一条并 `return False` 会让 **macOS 上恒返回 False**
    —— 因为 macOS 根本没有 /proc。后果不只是两条用例变红：
    `wait_until(lambda: not _process_running(...))` 这类断言会立刻成立，
    变成永远为真的空断言，清理逻辑回归时也发现不了。
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except OSError:
        # 没有 /proc（macOS）、或读不了 —— 用 ps 探活。
        # 不能用 os.kill(pid, 0)：它只回答"这个 pid 还在不在"，
        # 而**僵尸进程**也还在（只是没人回收），Linux 靠 /proc 的 Z 状态
        # 识别僵尸，macOS 必须靠 ps 的 stat 首字母。
        try:
            out = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        state = out.stdout.strip()
        return bool(state) and not state.startswith("Z")


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
    assert wait_until(
        lambda: marker.exists() and marker.read_text().strip()
    ), "孙进程 pid 未写出"
    gpid = int(marker.read_text().strip())
    stop_process_group(proc, proc.pid)
    assert wait_until(lambda: not _process_running(gpid)), "孙进程应已被按组清理"


def test_run_requires_caddy_alive(paths):
    paths.ensure_files()
    runner = _runner(paths, alive=False)
    with pytest.raises(UpError) as cm:
        runner.run("codeup", [sys.executable, "-c", "import time; time.sleep(1)"])
    assert "Admin API" in str(cm.value)


def test_run_caddy_not_running_raises_precondition(paths):
    """Caddy 未运行是前置条件不满足 —— 必须是 PreconditionError（CLI 映射为退出码 3）。"""
    paths.ensure_files()
    runner = _runner(paths, alive=False)
    with pytest.raises(PreconditionError) as cm:
        runner.run("codeup", [sys.executable, "-c", "import time; time.sleep(1)"])
    assert "Caddy 未运行" in str(cm.value)
    assert "devproxy setup" in str(cm.value), "报错要告诉用户下一步做什么"


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
    assert wait_until(
        lambda: marker.exists() and marker.read_text().strip()
    ), "子进程没写出 pid 标记"
    child_pid, grandchild_pid = (int(x) for x in marker.read_text().split())
    assert wait_until(
        lambda: not _process_running(child_pid)
        and not _process_running(grandchild_pid)
    ), "超时后子进程与孙进程必须被杀（子进程按组清理）"


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
    # 事件驱动：等到自检真的重新注册了为止，而不是「睡 0.5 秒就当它发生了」
    assert wait_until(lambda: caddy.reloads > before), "路由丢失时应触发重新注册"
    stop.set()
    t.join(timeout=10)


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
    # 给自检至少跑一轮的机会（heartbeat_interval=0.1）—— 这里必须留够余量：
    # 负载高时守护线程的第一轮可能晚到，睡太短会误判成「没复活」
    time.sleep(1.0)
    stop.set()
    t.join(timeout=10)
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
    assert wait_until(lambda: caddy.reloads > before), (
        "前缀域名（ax.test）不得让 x.test 伪装成存活"
    )
    stop.set()
    t.join(timeout=10)


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
        "from devproxy.core.config import Config\n"
        "from devproxy.core.paths import default_paths\n"
        "from devproxy.gateway.runner import UpRunner\n"
        "from devproxy.gateway.store import RouteStore\n"
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
        # 超时给到 60s：负载高的机器上 python 子进程启动本身就要好几秒
        deadline = time.time() + 60
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
        assert table is not None, "60s 内路由未注册"
        child_pid = table["routes"][0]["pid"]
        grandchild_pid = int(grandchild_pid_file.read_text())

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=60)
        assert rc == 143, f"被 SIGTERM 杀死应以 128+15 退出，实际 {rc}"

        table = json.loads(paths.routes_json.read_text())
        assert table["routes"] == [], "信号退出后路由必须注销"
        assert wait_until(
            lambda: not _process_running(child_pid)
            and not _process_running(grandchild_pid)
        ), "子进程与孙进程应被按组清理"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if child_pid is not None and _process_running(child_pid):
            # 测试自己的兜底清理：此刻读到的身份必然匹配，可直接杀
            kill_pid_group(child_pid, proc_start_time(child_pid))


# ------------------------------------------- I-3: pid 复用防护（进程身份校验）


def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )


def test_proc_start_time_is_stable_and_missing_for_dead_pid():
    """同一进程两次读取身份一致；不存在的 pid 返回 None。"""
    a = proc_start_time(os.getpid())
    b = proc_start_time(os.getpid())
    assert a is not None and a == b
    assert proc_start_time(999_999_999) is None


def test_kill_pid_group_kills_when_identity_matches():
    proc = _spawn_sleeper()
    try:
        assert kill_pid_group(proc.pid, proc_start_time(proc.pid)) is True
        assert wait_until(lambda: not _process_running(proc.pid)), "进程组应已被杀"
    finally:
        stop_process_group(proc, proc.pid)


def test_kill_pid_group_skips_reused_pid():
    """记录的启动时刻与当前进程对不上（pid 被复用）→ 绝不误杀。"""
    proc = _spawn_sleeper()
    try:
        forged = (proc_start_time(proc.pid) or "0") + "-forged"
        assert kill_pid_group(proc.pid, forged) is False
        time.sleep(0.3)
        assert _process_running(proc.pid), "身份不符时不得杀掉无辜进程"
    finally:
        stop_process_group(proc, proc.pid)


def test_kill_pid_group_skips_when_identity_unknown():
    """老版本路由表没有 proc_start —— 无法核实身份时宁可留下残留进程。"""
    proc = _spawn_sleeper()
    try:
        assert kill_pid_group(proc.pid, None) is False
        time.sleep(0.3)
        assert _process_running(proc.pid), "无法核实身份时不得杀掉进程"
    finally:
        stop_process_group(proc, proc.pid)


def test_kill_pid_group_on_dead_pid_is_noop():
    proc = _spawn_sleeper()
    pid, start = proc.pid, proc_start_time(proc.pid)
    stop_process_group(proc, proc.pid)
    time.sleep(0.3)
    assert kill_pid_group(pid, start) is False


def test_run_persists_proc_start_with_pid(paths):
    """注册路由时把进程身份一并固化，供 down 时防 pid 复用。"""
    paths.ensure_files()
    runner = _runner(paths)
    snapshots = []

    def on_ready(domain, port):
        snapshots.append(json.loads(paths.routes_json.read_text()))

    runner.run("codeup", [sys.executable, "-c", _LISTEN_AND_EXIT], on_ready=on_ready)
    entry = snapshots[0]["routes"][0]
    assert entry["pid"], "pid 必须记录"
    assert entry.get("proc_start"), "proc_start 必须记录"


# --------------------------------------------- I-4: 心跳不得复活已注销的路由


def test_watch_does_not_resurrect_route_removed_between_check_and_write(
    paths, monkeypatch
):
    """down 恰好落在心跳的「查表」与「写回」之间 → 不得复活（检查必须持锁）。"""
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.05

    route = Route(name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1")
    store.mutate(lambda t: t.upsert(route))
    admin.config_payload = {}  # Caddy 当前配置里没有它

    real_mutate = store.mutate
    armed = {"hit": False}

    def racing_mutate(fn, *args, **kwargs):
        if armed["hit"]:
            return real_mutate(fn, *args, **kwargs)
        armed["hit"] = True
        # 用户恰在此刻完成了一次 down（落在无锁检查与写回之间）
        real_mutate(lambda t: t.remove("x"))
        return real_mutate(fn, *args, **kwargs)

    monkeypatch.setattr(store, "mutate", racing_mutate)

    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    # 守护线程必须自己结束（路由没了就该退出）—— 给足超时，负载高时
    # 第一轮心跳可能晚到
    t.join(timeout=30)
    stop.set()
    assert not t.is_alive(), "路由已不存在，守护线程应结束"
    table = json.loads(paths.routes_json.read_text())
    assert table["routes"] == [], "被 down 的路由不得被心跳复活"


# --------------------------------------- C1: 组长被回收后不得盲杀旧 pgid


def test_stop_process_group_skips_unverifiable_reaped_leader():
    """组长已被回收 + 身份无法核实 → 绝不对该 pgid 发信号（可能已被复用）。"""
    victim = _spawn_sleeper()
    reaped = subprocess.Popen(
        [sys.executable, "-c", "pass"], start_new_session=True
    )
    try:
        reaped.wait()  # 组长已被回收，pid/pgid 理论上可被复用
        stop_process_group(reaped, victim.pid, expected_start="forged")
        time.sleep(0.3)
        assert _process_running(victim.pid), "身份不符时不得杀掉无关进程组"

        stop_process_group(reaped, victim.pid)  # 未提供身份 → 同样跳过
        time.sleep(0.2)
        assert _process_running(victim.pid), "无法核实身份时不得发信号"
    finally:
        stop_process_group(victim, victim.pid)


def test_run_kills_grandchildren_after_leader_already_exited(paths, tmp_path):
    """正常退出路径：组长先退出，孙进程仍必须被按组清理。

    旧实现先 proc.wait() 回收组长、再 killpg —— 存在 pid 复用窗口；
    新实现用 waitid(WNOWAIT) 把组长保持为僵尸直到补刀完成。
    """
    paths.ensure_files()
    runner = _runner(paths)
    marker = tmp_path / "grandchild-left.pid"
    child_code = (
        "import os,socket,subprocess,sys,time;"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        "s.bind(('127.0.0.1',int(os.environ['PORT'])));s.listen(8);"
        "g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"open({str(marker)!r},'w').write(str(g.pid));"
        "time.sleep(0.5)"
    )
    rc = runner.run("codeup", [sys.executable, "-c", child_code])
    assert rc == 0
    assert wait_until(lambda: marker.exists() and marker.read_text().strip())
    gpid = int(marker.read_text())
    assert wait_until(
        lambda: not _process_running(gpid)
    ), "组长退出后孙进程仍必须被清理"


# ------------------------------- I2: 同名路由的注销 / 心跳必须校验归属


def test_deregister_does_not_remove_route_replaced_by_new_up(paths):
    import threading  # noqa: F401

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    runner = UpRunner(store, cfg, FakeAdmin())

    old = Route(
        name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1", pid=111
    )
    new = Route(
        name="x", domain="x.test", port=2, mode="up", upstream="127.0.0.1:2", pid=222
    )
    store.mutate(lambda t: t.upsert(new))

    runner._deregister(old)
    assert store.get("x") == new, "旧 up 的清理不得删掉同名的新路由"


def test_watch_stops_without_overwriting_route_replaced_by_new_up(paths):
    """同名路由已被新 up 接管（pid/port 不同）→ 旧心跳既不复写也不空转。"""
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.05

    old = Route(
        name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1", pid=111
    )
    new = Route(
        name="x", domain="x.test", port=2, mode="up", upstream="127.0.0.1:2", pid=222
    )
    store.mutate(lambda t: t.upsert(new))
    admin.config_payload = {}  # 旧路由确实不在 Caddy 里
    before = caddy.reloads

    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(old, stop), daemon=True)
    t.start()
    t.join(timeout=10)
    stop.set()
    assert not t.is_alive(), "路由已被接管，旧心跳应结束"
    assert caddy.reloads == before, "旧心跳不得覆盖新路由"
    assert store.get("x") == new


# --------------------------- I3: kill_pid_group 等待退出并升级 SIGKILL


def test_kill_pid_group_escalates_to_sigkill(tmp_path):
    marker = tmp_path / "sigterm-ignored.ready"
    code = (
        "import signal,time,pathlib;"
        "signal.signal(signal.SIGTERM, lambda *a: None);"
        f"pathlib.Path({str(marker)!r}).write_text('1');"
        "time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code], start_new_session=True
    )
    try:
        assert wait_until(marker.exists), "子进程未完成 SIGTERM handler 安装"
        start = proc_start_time(proc.pid)
        assert kill_pid_group(proc.pid, start, timeout=0.3) is True
        assert wait_until(
            lambda: not _process_running(proc.pid)
        ), "忽略 SIGTERM 的进程必须被 SIGKILL 收掉"
    finally:
        stop_process_group(proc, proc.pid)


def test_run_reclaims_existing_route_and_reuses_its_port(paths):
    """同名旧进程占着显式端口：先等它死透，再启动新进程 —— 不得误报端口占用。"""
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    port = alloc_port()
    old_code = (
        "import os,socket,time;"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        "s.bind(('127.0.0.1',int(os.environ['PORT'])));s.listen(8);"
        "time.sleep(60)"
    )
    old = subprocess.Popen(
        [sys.executable, "-c", old_code],
        env=dict(os.environ, PORT=str(port)),
        start_new_session=True,
    )
    try:
        assert wait_until(lambda: reachable(port, timeout=0.2))
        store.mutate(
            lambda t: t.upsert(
                Route(
                    name="codeup",
                    domain="codeup.test",
                    port=port,
                    mode="up",
                    upstream=f"127.0.0.1:{port}",
                    pid=old.pid,
                    proc_start=proc_start_time(old.pid),
                )
            )
        )
        runner = UpRunner(store, cfg, FakeAdmin())
        rc = runner.run(
            "codeup", [sys.executable, "-c", _LISTEN_AND_EXIT], port=port
        )
        assert rc == 0
        assert wait_until(lambda: not _process_running(old.pid)), "旧进程必须被收掉"
        assert wait_until(lambda: json.loads(paths.routes_json.read_text())["routes"] == [])
    finally:
        stop_process_group(old, old.pid)


# ------------------------------------- I4: 不可执行命令映射成 UpError


def test_run_maps_non_executable_command_to_up_error(paths, tmp_path):
    paths.ensure_files()
    script = tmp_path / "no-exec.sh"
    script.write_text("#!/bin/sh\nexit 0\n")  # 故意不 chmod +x
    with pytest.raises(UpError) as cm:
        _runner(paths).run("codeup", [str(script)])
    assert "无法启动命令" in str(cm.value)


def test_run_maps_missing_command_to_up_error(paths, tmp_path):
    paths.ensure_files()
    with pytest.raises(UpError) as cm:
        _runner(paths).run("codeup", [str(tmp_path / "absent-cmd")])
    assert "无法启动命令" in str(cm.value)


# ------------------------- I5: 心跳不再静默吞异常（可重试报一次，未知则停）


def test_watch_retries_store_errors_and_reports_once(paths, monkeypatch, capsys):
    import threading

    from devproxy.gateway.store import RouteStoreError

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.05

    route = Route(
        name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1"
    )
    store.mutate(lambda t: t.upsert(route))
    admin.config_payload = {}

    calls = {"n": 0}

    def failing_mutate(fn, *args, **kwargs):
        calls["n"] += 1
        raise RouteStoreError("reload 持续失败（测试）")

    monkeypatch.setattr(store, "mutate", failing_mutate)
    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    assert wait_until(lambda: calls["n"] >= 3), "可重试错误不得杀死守护线程"
    stop.set()
    t.join(timeout=10)
    assert not t.is_alive()

    err = capsys.readouterr().err
    assert err.count("心跳失败") == 1, "相同错误只报一次，避免每轮刷屏"


def test_watch_stops_on_unknown_exception(paths, monkeypatch, capsys):
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    store = RouteStore(paths, cfg, FakeCaddy())
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.05

    route = Route(
        name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1"
    )
    store.mutate(lambda t: t.upsert(route))
    admin.config_payload = {}

    def broken_mutate(fn, *args, **kwargs):
        raise RuntimeError("未知错误（测试）")

    monkeypatch.setattr(store, "mutate", broken_mutate)
    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    t.join(timeout=10)
    stop.set()
    assert not t.is_alive(), "未知异常应记录后结束线程"
    assert "已停止" in capsys.readouterr().err


# --------------------------- M5: 区分「身份缺失」与「pid 被复用」的诊断


def test_kill_pid_group_messages_distinguish_unknown_identity_from_reuse(capsys):
    proc = _spawn_sleeper()
    try:
        assert kill_pid_group(proc.pid, None) is False
        err = capsys.readouterr().err
        assert "缺少 proc_start" in err
        assert "pid 被复用" not in err

        forged = (proc_start_time(proc.pid) or "0") + "-forged"
        assert kill_pid_group(proc.pid, forged) is False
        assert "已被复用" in capsys.readouterr().err
    finally:
        stop_process_group(proc, proc.pid)


# ----------------------- M7: 路由存活判断必须结构化，不能做全局子串匹配


def test_route_live_ignores_domain_string_in_unrelated_fields():
    from devproxy.gateway.runner import _route_live

    admin = FakeAdmin()
    admin.config_payload = {
        "apps": {
            "http": {
                "servers": {
                    "srv0": {
                        "routes": [
                            {"headers": {"X-Dev": ["x.test"]}},
                            {"match": [{"host": ["ax.test"]}]},
                        ]
                    }
                }
            }
        },
        "logging": {"note": "x.test"},
    }
    assert _route_live(admin, "x.test") is False


def test_route_live_sees_structured_host():
    from devproxy.gateway.runner import _route_live

    admin = FakeAdmin()
    admin.config_payload = {
        "apps": {
            "http": {
                "servers": {
                    "srv0": {
                        "routes": [{"match": [{"host": ["x.test", "y.test"]}]}]
                    }
                }
            }
        }
    }
    assert _route_live(admin, "x.test") is True
    assert _route_live(admin, "y.test") is True
    assert _route_live(admin, "z.test") is False


def test_route_live_handles_malformed_payload_without_raising():
    from devproxy.gateway.runner import _route_live

    admin = FakeAdmin()
    admin.config_payload = ["not", "a", "dict"]
    assert _route_live(admin, "x.test") is False
    admin.config_payload = {"apps": {"http": {"servers": "broken"}}}
    assert _route_live(admin, "x.test") is False
