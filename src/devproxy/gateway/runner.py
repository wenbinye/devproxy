"""up 的子进程生命周期：端口分配、启动、就绪等待、信号转发、按组清理。

沿用 devp 的成熟做法：子进程以 start_new_session=True 启动（独立进程组），
信号按组转发，退出时按组 SIGTERM→SIGKILL，确保孙进程不残留。

pid 复用防护：组长在按组补刀完成之前一律保留为僵尸（waitid + WNOWAIT）——
僵尸存在期间内核不会复用它的 pid，killpg 打到的必然还是当初那个进程组。
只有组长已被别处回收、且记录的身份与当前不符时，才放弃按组清理。
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from devproxy.core.config import Config
from devproxy.gateway.caddy import CaddyAdmin, CaddyError
from devproxy.gateway.names import domain_for, validate_name
from devproxy.gateway.routes import Route, RouteTable, RouteTableError
from devproxy.gateway.store import RouteStore, RouteStoreError

HEARTBEAT_INTERVAL = 30.0

# os.waitid 的 WNOWAIT 只在部分 Unix 上可用。缺失时退回 Popen.wait()，
# 那会提前回收组长 —— stop_process_group 会因此放弃按组补刀（宁可留下
# 残留进程，也不对可能被复用的 pgid 发信号）。
_HAS_WAITID = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")


class _RouteGone(Exception):
    """路由表里已经没有这条路由（用户已 down 或已被新的 up 接管）。"""


class UpError(RuntimeError):
    """up 失败。"""


class PreconditionError(UpError):
    """前置条件不满足（如 Caddy 未运行）——CLI 层应映射为退出码 3。"""


@dataclass
class UpRequest:
    """一次 up 的输入。参数收敛在这里，避免编排函数继续膨胀。"""

    name: str
    cmd: list[str]
    port: int | None = None
    ready_timeout: int | None = None
    on_ready: Callable[[str, int], None] | None = None
    # 子进程的工作目录。None = 继承 devproxy 进程的当前目录（老行为）。
    # 项目级配置（devproxy.toml）在项目根目录，命令通常要在**那里**跑 ——
    # 用户在子目录里执行 sync 时，应用的 cwd 仍必须是项目根目录。
    cwd: Path | None = None
    # 声明来源（devproxy.toml 的绝对路径）。落到路由表里，
    # `devproxy sync --prune` 据此判断「这条路由是不是本文件声明的」。
    source: str | None = None


class _ChildHandle:
    """已启动子进程及其进程组身份，供信号处理与清理共享。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.pgid: int | None = None
        self.start: str | None = None


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


def _config_hosts_contains(config: object, domain: str) -> bool:
    """在 /config/ 的结构化 JSON 里找 host 匹配。

    不能对整份 JSON 做子串匹配：用户的 sites.caddy 里可能有
    `header X-Dev "foo.test"` 或含该域名的 TLS SAN，子串会把它当成
    路由存活，导致真正丢失的路由永远不被补写。
    """
    apps = config.get("apps") if isinstance(config, dict) else None
    http = apps.get("http") if isinstance(apps, dict) else None
    servers = http.get("servers") if isinstance(http, dict) else None
    if not isinstance(servers, dict):
        return False
    for server in servers.values():
        if not isinstance(server, dict):
            continue
        routes = server.get("routes")
        if not isinstance(routes, list):
            continue
        for route in routes:
            matches = route.get("match") if isinstance(route, dict) else None
            if not isinstance(matches, list):
                continue
            for match in matches:
                hosts = match.get("host") if isinstance(match, dict) else None
                if isinstance(hosts, list) and domain in hosts:
                    return True
    return False


def _route_live(admin: CaddyAdmin, domain: str) -> bool:
    """路由是否真的在 Caddy 当前配置里（按 host 列表结构化判断）。"""
    code, body = admin.api("GET", "/config/")
    if code != 200 or body is None:
        return False
    return _config_hosts_contains(body, domain)


def _pid_is_running(pid: int) -> bool:
    """进程是否存在且不是僵尸（僵尸等同于已退出）。

    没有 /proc 的平台（macOS）退回 `ps -o stat=`；两种方式都拿不到时
    按「已退出」处理 —— 清理流程等的是「不再运行」，不是「pid 还在」。
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        state = proc.stdout.strip()
        return bool(state) and not state.startswith("Z")
    fields = stat.rsplit(")", 1)[1].split()
    return bool(fields) and fields[0] != "Z"


def _leader_status(proc: subprocess.Popen) -> int | None:
    """非阻塞读取组长退出状态，但不回收（WNOWAIT）；None = 仍在运行。"""
    if proc.returncode is not None:
        return proc.returncode
    if _HAS_WAITID:
        try:
            info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except (ChildProcessError, OSError):
            return proc.poll()
        if info is None:
            return None
        if info.si_code == getattr(os, "CLD_EXITED", None):
            return info.si_status
        return -info.si_status
    return proc.poll()


def _wait_leader(proc: subprocess.Popen) -> int:
    """等待组长退出，但把回收留给清理阶段（僵尸期间 pid 不会被复用）。"""
    while True:
        status = _leader_status(proc)
        if status is not None:
            return status
        time.sleep(0.1)


def _wait_exit_unreaped(proc: subprocess.Popen, timeout: float) -> bool:
    """等组长退出但保留僵尸；返回是否仍「钉住」pgid（未被回收）。

    返回 False 表示平台不支持 WNOWAIT、或组长已被别处回收 —— 此时调用方
    必须放弃后续的按组补刀。
    """
    if not _HAS_WAITID:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except (ChildProcessError, OSError):
            return False
        if info is not None:
            return True
        time.sleep(0.05)
    # 超时仍未退出：组长还活着，pid 同样不会被复用
    return True


def stop_process_group(
    proc: subprocess.Popen | None,
    pgid: int | None = None,
    sig: int = signal.SIGTERM,
    expected_start: str | None = None,
) -> None:
    """向子进程组发信号 → 有限等待 → SIGKILL 进程组，确保孙进程也不残留。

    pgid 在启动时捕获：组长先退出时（launcher 场景）进程组里仍留着孙进程。

    安全前提：发信号时组长必须仍未被回收（僵尸也算），或者记录的启动时刻
    与 pgid 当前的身份一致。组长一旦被回收，pid/pgid 就可能被内核复用，
    盲目 killpg 会杀掉无关进程组。
    """
    if proc is None:
        return
    if pgid is None:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = proc.pid

    if proc.returncode is not None:
        # 组长已被别处回收：只有身份仍匹配才敢按组发信号。
        if expected_start is None or proc_start_time(pgid) != expected_start:
            print(
                f"⚠️  devproxy: 进程组 {pgid} 的组长已被回收、身份无法核实，"
                "跳过按组清理（孙进程可能残留）",
                file=sys.stderr,
            )
            return
        try:
            os.killpg(pgid, sig)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return

    # 组长未被回收（活着或僵尸）→ pid 不可能被复用，按组发信号是安全的。
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass

    if _wait_exit_unreaped(proc, 5):
        # 组长已退出但仍是僵尸，pgid 仍被钉住：补一刀清理组内孙进程。
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _wait_group_gone(pid: int, expected_start: str, timeout: float) -> bool:
    """轮询到 pid 不再是「运行中的进程」（含僵尸）或身份已变。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        if proc_start_time(pid) != expected_start:
            return True
        time.sleep(0.05)
    return False


def kill_pid_group(pid: int, proc_start: str | None = None, timeout: float = 5.0) -> bool:
    """按进程组杀掉残留的上一次 up 子进程（pid == pgid）。

    routes.json 会跨重启存活，pid 会被 OS 回收复用 —— 只有当前进程的
    启动时刻与注册时记录的一致才发信号；身份对不上或无法核实时宁可
    留下残留进程也不误杀无辜进程组（up 自己的 finally 仍按直接句柄
    清理自己的孩子）。

    先 SIGTERM，有界等待后仍未退出再 SIGKILL —— 与 stop_process_group
    的策略一致，也保证返回时监听端口已经释放。返回是否真的向已核实的
    进程组发出过信号。
    """
    current = proc_start_time(pid)
    if current is None:
        return False  # 进程已不在，无从下手
    if proc_start is None:
        print(
            f"⚠️  无法核实进程 {pid} 的身份（路由表缺少 proc_start），跳过清理",
            file=sys.stderr,
        )
        return False
    if proc_start != current:
        print(
            f"⚠️  进程 {pid} 已被复用（记录的启动时刻与当前不符），跳过清理",
            file=sys.stderr,
        )
        return False
    if not _pid_is_running(pid):
        return False  # 只剩僵尸，端口已释放
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    if _wait_group_gone(pid, proc_start, timeout):
        return True
    # SIGTERM 超时且身份仍然匹配 → 升级 SIGKILL
    if proc_start_time(pid) != proc_start or not _pid_is_running(pid):
        return True
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return False
    _wait_group_gone(pid, proc_start, 2.0)
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


def _same_owner(current: Route, expected: Route) -> bool:
    """同名路由的归属判断：name 相同不够，pid/port 也必须一致。"""
    return (
        current.name == expected.name
        and current.pid == expected.pid
        and current.port == expected.port
    )


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
        """兼容 CLI 的入口；真正的编排在 `_execute`。

        参数签名暂时保留（cli/up.py 直接按位置/关键字调用），内部统一
        收敛成 UpRequest，后续新增选项不必再扩散到编排逻辑里。
        """
        return self.run_request(
            UpRequest(
                name=name,
                cmd=list(cmd),
                port=port,
                ready_timeout=ready_timeout,
                on_ready=on_ready,
            )
        )

    def run_request(self, req: UpRequest) -> int:
        """按 UpRequest 执行一次 up —— 编排的唯一入口。

        `run()` 只是把位置参数收敛成 UpRequest 后的薄封装；需要 cwd /
        source 这类附加信息的调用方（devproxy sync 的后台工作进程）
        直接构造 UpRequest 调这里。
        """
        return self._execute(req)

    def _execute(self, req: UpRequest) -> int:
        self._validate_request(req)
        if not self.admin.is_alive():
            raise PreconditionError(
                "Caddy 未运行（Admin API 不可达），请先执行 devproxy setup 或启动 Caddy"
            )

        # 同名路由先清理（旧子进程一并收掉）
        self._reclaim_existing(req.name)
        port = self._choose_port(req.port)

        stop = threading.Event()
        child = _ChildHandle()
        old_handlers = self._install_handlers(stop, child)
        route: Route | None = None
        registered = False
        watcher: threading.Thread | None = None
        try:
            proc = self._spawn(req, port, child)
            self._wait_ready(proc, port, self._timeout(req))

            route = self._make_route(req, port, child)
            # 先置位再注册：信号可能在 mutate（validate+reload，秒级窗口）内部
            # 落下并抛 SystemExit，此时 finally 仍要尝试注销，不能留下指向死
            # 端口的路由；条件删除保证不会误删同名的新路由。
            registered = True
            self.store.mutate(lambda t: t.upsert(route))
            watcher = threading.Thread(
                target=self._watch, args=(route, stop), daemon=True
            )
            watcher.start()
            if req.on_ready is not None:
                req.on_ready(route.domain, port)
            rc = _wait_leader(proc)
        finally:
            stop.set()
            # 先杀进程组再 join/注销：缩短清理窗口，也避免 watcher 在子进程
            # 死后仍把路由补写回去。
            stop_process_group(
                child.proc, child.pgid, expected_start=child.start
            )
            if watcher is not None:
                watcher.join(timeout=10)
            if registered and route is not None:
                self._deregister(route)
            for s, handler in old_handlers.items():
                signal.signal(s, handler)
        # 被信号杀死时 wait() 返回 -n；映射成 shell 约定的 128+n
        return 128 - rc if rc < 0 else rc

    def _validate_request(self, req: UpRequest) -> None:
        try:
            validate_name(req.name)
        except ValueError as e:
            raise UpError(str(e)) from e
        if req.port is not None and not (1 <= req.port <= 65535):
            raise UpError(f"非法端口 {req.port}：必须在 1..65535 之间")

    def _timeout(self, req: UpRequest) -> int:
        return (
            req.ready_timeout
            if req.ready_timeout is not None
            else self.config.ready_timeout
        )

    def _reclaim_existing(self, name: str) -> None:
        """注销同名旧路由并杀掉旧子进程。

        「读旧路由 + 删除」必须在同一个 mutate 回调里完成（持 flock），
        否则并发 up/down 可以在检查与写入之间插入，删掉对方的活路由。
        旧路由通过闭包容器带出锁外，杀进程放在锁外做（可能耗时数秒）。
        """
        if self.store.get(name) is None:
            return
        removed: list[Route] = []

        def apply(t: RouteTable) -> None:
            old = t.get(name)
            if old is not None:
                removed.append(old)
                t.remove(name)

        self.store.mutate(apply)
        if not removed:
            return
        old = removed[0]
        if old.pid:
            kill_pid_group(old.pid, old.proc_start)

    def _choose_port(self, explicit: int | None) -> int:
        if explicit is None:
            return alloc_port()
        # 旧进程已在 _reclaim_existing 里等死（kill_pid_group 返回时端口
        # 已释放），这里再探测才是可靠的「真的被别的服务占着」。
        if reachable(explicit, timeout=0.2):
            raise UpError(f"端口 {explicit} 已被占用")
        return explicit

    def _spawn(self, req: UpRequest, port: int, child: _ChildHandle) -> subprocess.Popen:
        env = dict(os.environ, PORT=str(port))
        try:
            # cwd=None 时与从前完全一致（继承当前工作目录）；
            # 给了 cwd 就把子进程放进那个目录（项目根目录）。
            proc = subprocess.Popen(
                req.cmd, env=env, cwd=req.cwd, start_new_session=True
            )
        except OSError as e:
            # 覆盖 FileNotFoundError / PermissionError / Exec format error：
            # 都映射成 UpError，CLI 才会给出格式化错误而不是裸 traceback。
            raise UpError(f"无法启动命令 {req.cmd[0]!r}: {e}") from e
        child.proc = proc
        child.pgid = proc.pid  # start_new_session=True → 子进程是进程组 leader
        child.start = proc_start_time(proc.pid)
        return proc

    def _wait_ready(
        self, proc: subprocess.Popen, port: int, timeout: int
    ) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = _leader_status(proc)
            if status is not None:
                raise UpError(f"子进程先退出（code={status}），未注册")
            if reachable(port, timeout=1):
                return
            time.sleep(0.3)
        raise UpError(
            f"就绪超时（{timeout}s）——应用可能未使用 $PORT，可用 --port 显式指定"
        )

    def _make_route(
        self, req: UpRequest, port: int, child: _ChildHandle
    ) -> Route:
        proc = child.proc
        if proc is None:  # pragma: no cover - _spawn 成功后才调用
            raise UpError("内部错误：子进程尚未启动")
        return Route(
            name=req.name,
            domain=domain_for(req.name, self.config.domain_suffix),
            port=port,
            mode="up",
            upstream=f"127.0.0.1:{port}",
            command=list(req.cmd),
            pid=proc.pid,
            proc_start=child.start,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            source=req.source,
        )

    def _deregister(self, route: Route) -> None:
        """注销本进程注册的那条路由 —— 同名的新路由不得被误删。"""

        def apply(t: RouteTable) -> None:
            current = t.get(route.name)
            if current is not None and _same_owner(current, route):
                t.remove(route.name)

        try:
            self.store.mutate(apply)
        except Exception:
            # 静默吞掉会让残留路由指向已被杀死的子进程 —— 必须提示用户
            print(
                f"⚠️  devproxy: 注销失败，残留路由请执行 devproxy down {route.name}",
                file=sys.stderr,
            )

    def _install_handlers(
        self, stop: threading.Event, child: _ChildHandle
    ) -> dict[int, object]:
        def on_signal(signum, frame):
            # 清理期间收到第二个信号时直接忽略，避免中断 finally 留下残留
            if stop.is_set():
                return
            stop.set()
            if child.pgid is not None:
                child_sig = signal.SIGINT if signum == signal.SIGINT else signal.SIGTERM
                try:
                    os.killpg(child.pgid, child_sig)
                except (ProcessLookupError, PermissionError):
                    pass
            raise SystemExit(128 + signum)

        return {
            s: signal.signal(s, on_signal)
            for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }

    def _watch(self, route: Route, stop: threading.Event) -> None:
        """每 heartbeat_interval 秒确认路由还在 Caddy 里。

        路由是文件化的，正常情况下 Caddy 重启会自动加载；这个自检兜住
        "Caddy 是用别的配置启动的" 与 "有人手改了渲染产物" 两种情况。
        「路由表里还有没有它」的检查必须放在 mutate 回调里、与写回同一把
        flock 之下 —— 检查在锁外的话，down 可以恰好插在检查与写回之间，
        把刚注销的路由复活成指向死端口的条目。

        失败不能被无声吞掉：Caddy reload 持续失败时用户必须知道，否则
        路由 404/502 而 `devproxy list` 仍显示一切正常。相同的错误只报
        一次（按错误文本去重），未知异常记录后让线程结束。
        """
        last_error: str | None = None
        while not stop.wait(self.heartbeat_interval):
            try:
                if _route_live(self.admin, route.domain):
                    continue

                def apply(t: RouteTable) -> None:
                    current = t.get(route.name)
                    if current is None:
                        raise _RouteGone  # 用户已注销 —— 不复活
                    if not _same_owner(current, route):
                        raise _RouteGone  # 已被同名的新 up 接管 —— 旧心跳不得覆盖

                    t.upsert(route)

                # 异常从回调里抛出会先于渲染/落盘/reload 穿出 mutate，
                # 事务什么都不写就结束 —— 既不复活路由，也不空转 reload
                try:
                    self.store.mutate(apply)
                except _RouteGone:
                    return  # 路由已不存在，线程结束
            except (RouteStoreError, RouteTableError, CaddyError, OSError) as e:
                message = f"{type(e).__name__}: {e}"
                if message != last_error:
                    print(
                        f"⚠️  devproxy: 路由 {route.name} 心跳失败"
                        f"（将继续重试）：{message}",
                        file=sys.stderr,
                    )
                    last_error = message
            except Exception as e:  # noqa: BLE001 - 未知异常不能杀死守护线程而无声
                print(
                    f"⚠️  devproxy: 路由 {route.name} 心跳异常，已停止：{e}",
                    file=sys.stderr,
                )
                return
