"""up 的子进程生命周期：端口分配、启动、就绪等待、信号转发、按组清理。

沿用 devp 的成熟做法：子进程以 start_new_session=True 启动（独立进程组），
信号按组转发，退出时按组 SIGTERM→SIGKILL，确保孙进程不残留。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from devproxy.core.config import Config
from devproxy.gateway.caddy import CaddyAdmin
from devproxy.gateway.names import domain_for, validate_name
from devproxy.gateway.routes import Route, RouteTable
from devproxy.gateway.store import RouteStore

HEARTBEAT_INTERVAL = 30.0


class _RouteGone(Exception):
    """路由表里已经没有这条路由（用户已 down）—— 心跳不得复活它。"""


class UpError(RuntimeError):
    """up 失败。"""


class PreconditionError(UpError):
    """前置条件不满足（如 Caddy 未运行）——CLI 层应映射为退出码 3。"""


def alloc_port() -> int:
    """绑定 :0 取一个随机空闲端口后立即释放。存在微小时隙竞争。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def reachable(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _route_live(admin: CaddyAdmin, domain: str) -> bool:
    """路由是否真的在 Caddy 当前配置里。

    整个 /config/ 序列化后做子串匹配 —— 免去猜测 Caddy 内部的 server 编号
    （srv0/srv1 取决于 listen 地址的字典序）。

    匹配带引号的域名（"codeup.test"）：裸域名匹配会让 "x.test" 命中
    "ax.test" —— 前缀不同的另一条路由会把丢失的路由伪装成存活。
    """
    code, body = admin.api("GET", "/config/")
    if code != 200 or body is None:
        return False
    return f'"{domain}"' in json.dumps(body)


def stop_process_group(
    proc: subprocess.Popen | None, pgid: int | None = None, sig: int = signal.SIGTERM
) -> None:
    """向子进程组发信号 → 有限等待 → SIGKILL 进程组，确保孙进程也不残留。

    pgid 在启动时捕获：组长先退出时（launcher 场景）进程组里仍留着孙进程，
    依然要按组发信号 —— 不能因为 proc.poll() 已非 None 就跳过。
    """
    if proc is None:
        return
    if pgid is None:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = proc.pid
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # 组长可能已退出但组内仍有孙进程：再按组补一刀 SIGKILL
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class UpRunner:
    heartbeat_interval = HEARTBEAT_INTERVAL

    def __init__(self, store: RouteStore, config: Config, admin: CaddyAdmin):
        self.store = store
        self.config = config
        self.admin = admin

    def run(
        self,
        name: str,
        cmd: list[str],
        port: int | None = None,
        ready_timeout: int | None = None,
        on_ready: Callable[[str, int], None] | None = None,
    ) -> int:
        try:
            validate_name(name)
        except ValueError as e:
            raise UpError(str(e)) from e

        timeout = ready_timeout if ready_timeout is not None else self.config.ready_timeout
        if port is not None and not (1 <= port <= 65535):
            raise UpError(f"非法端口 {port}：必须在 1..65535 之间")

        if not self.admin.is_alive():
            raise PreconditionError(
                "Caddy 未运行（Admin API 不可达），请先执行 devproxy setup 或启动 Caddy"
            )

        # 同名路由先清理（旧子进程一并收掉）
        existing = self.store.get(name)
        if existing is not None:
            self.store.mutate(lambda t: t.remove(name))
            if existing.pid:
                kill_pid_group(existing.pid, existing.proc_start)

        if port is None:
            port = alloc_port()
        elif reachable(port, timeout=0.2):
            raise UpError(f"端口 {port} 已被占用")

        env = dict(os.environ, PORT=str(port))
        stop = threading.Event()
        proc: subprocess.Popen | None = None
        pgid: int | None = None
        watcher: threading.Thread | None = None
        registered = False

        def on_signal(signum, frame):
            # 清理期间收到第二个信号时直接忽略，避免中断 finally 留下残留
            if stop.is_set():
                return
            stop.set()
            if pgid is not None:
                child_sig = signal.SIGINT if signum == signal.SIGINT else signal.SIGTERM
                try:
                    os.killpg(pgid, child_sig)
                except (ProcessLookupError, PermissionError):
                    pass
            raise SystemExit(128 + signum)

        old_handlers = {
            s: signal.signal(s, on_signal)
            for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }
        try:
            try:
                proc = subprocess.Popen(cmd, env=env, start_new_session=True)
                pgid = proc.pid  # start_new_session=True → 子进程是进程组 leader
            except FileNotFoundError:
                raise UpError(f"命令不存在: {cmd[0]}") from None

            deadline = time.time() + timeout
            while time.time() < deadline:
                if proc.poll() is not None:
                    raise UpError(f"子进程先退出（code={proc.returncode}），未注册")
                if reachable(port, timeout=1):
                    break
                time.sleep(0.3)
            else:
                raise UpError(
                    f"就绪超时（{timeout}s）——应用可能未使用 $PORT，可用 --port 显式指定"
                )

            domain = domain_for(name, self.config.domain_suffix)
            route = Route(
                name=name,
                domain=domain,
                port=port,
                mode="up",
                upstream=f"127.0.0.1:{port}",
                command=list(cmd),
                pid=proc.pid,
                proc_start=proc_start_time(proc.pid),
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            # 先置位再注册：信号可能在 mutate（validate+reload，秒级窗口）内部
            # 落下并抛 SystemExit，此时 finally 仍要尝试注销，不能留下指向死
            # 端口的路由；对从未写入的路由做 remove 是无害的 no-op
            registered = True
            self.store.mutate(lambda t: t.upsert(route))
            watcher = threading.Thread(
                target=self._watch, args=(route, stop), daemon=True
            )
            watcher.start()
            if on_ready is not None:
                on_ready(domain, port)
            rc = proc.wait()
        finally:
            stop.set()
            if watcher is not None:
                watcher.join(timeout=10)
            if registered:
                try:
                    self.store.mutate(lambda t: t.remove(name))
                except Exception:
                    # 静默吞掉会让残留路由指向已被杀死的子进程 —— 必须提示用户
                    print(
                        f"⚠️  devproxy: 注销失败，残留路由请执行 devproxy down {name}",
                        file=sys.stderr,
                    )
            stop_process_group(proc, pgid)
            for s, handler in old_handlers.items():
                signal.signal(s, handler)
        # 被信号杀死时 wait() 返回 -n；映射成 shell 约定的 128+n
        return 128 - rc if rc < 0 else rc

    def _watch(self, route: Route, stop: threading.Event) -> None:
        """每 heartbeat_interval 秒确认路由还在 Caddy 里。

        路由是文件化的，正常情况下 Caddy 重启会自动加载；这个自检兜住
        "Caddy 是用别的配置启动的" 与 "有人手改了渲染产物" 两种情况。
        「路由表里还有没有它」的检查必须放在 mutate 回调里、与写回同一把
        flock 之下 —— 检查在锁外的话，down 可以恰好插在检查与写回之间，
        把刚注销的路由复活成指向死端口的条目。
        """
        while not stop.wait(self.heartbeat_interval):
            try:
                if stop.is_set():
                    return
                if _route_live(self.admin, route.domain):
                    continue

                def apply(t: RouteTable) -> None:
                    if t.get(route.name) is None:
                        raise _RouteGone  # 用户已注销 —— 不复活
                    t.upsert(route)

                # 异常从回调里抛出会先于渲染/落盘/reload 穿出 mutate，
                # 事务什么都不写就结束 —— 既不复活路由，也不空转 reload
                try:
                    self.store.mutate(apply)
                except _RouteGone:
                    return  # 路由已不存在，线程结束
            except Exception:
                # Admin 返回畸形响应等异常不能杀死守护线程
                pass


def kill_pid_group(pid: int, proc_start: str | None = None) -> bool:
    """按进程组杀掉残留的上一次 up 子进程（pid == pgid）。

    routes.json 会跨重启存活，pid 会被 OS 回收复用 —— 只有当前进程的
    启动时刻与注册时记录的一致才发信号；身份对不上或无法核实时宁可
    留下残留进程也不误杀无辜进程组（up 自己的 finally 仍按直接句柄
    清理自己的孩子）。返回是否真的发出了信号。
    """
    current = proc_start_time(pid)
    if current is None:
        return False  # 进程已不在（或读不到身份），无从下手
    if proc_start is None or proc_start != current:
        print(
            f"⚠️  进程 {pid} 已不是当初那个（pid 被复用），跳过清理",
            file=sys.stderr,
        )
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    return True


def proc_start_time(pid: int) -> str | None:
    """读取进程「身份」：/proc/<pid>/stat 第 22 字段（启动时刻，clock ticks）。

    pid 会被 OS 回收复用，单看 pid 无法断定还是不是当初那个进程 ——
    启动时刻是检测 pid 复用的标准做法。没有 /proc 的平台（macOS）
    退回 `ps -o lstart=`。两者都拿不到时返回 None（= 无法核实）。
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        try:
            proc = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout.strip() or None
    # 第 2 字段 comm 带括号且可能含空格 —— 只能在最后一个 ')' 之后切分；
    # 切完后第一个字段是第 3 字段 state，第 22 字段 starttime 位于索引 19
    fields = stat.rsplit(")", 1)[1].split()
    if len(fields) <= 19:
        return None
    return fields[19]
