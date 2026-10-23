"""前台托管 Caddy：起进程、接管信号、干净收尾。

分工：
- `setup` 装的是**后台服务**（systemd / launchd，开机自启）；
- `selftest` 起的是**隔离实例**（临时端口、临时 storage、跑完即删）；
- 本模块跑的是**现役配置**（主 Caddyfile），而且**在前台** —— 用户要的
  是实时日志与「Ctrl-C 即撤、不留痕迹」。

前台模式的意义就在 stdio 直通终端：不重定向，Caddy 的日志实时打在终端上
（`selftest` 那种重定向到文件的做法在这里是反效果）。

信号模型：子进程以 `start_new_session=True` 启动 —— 它自成一个会话与进程组，
终端里的 Ctrl-C **不会**打到 Caddy 身上，只打到 devproxy。于是收尾的节奏
由我们决定：SIGTERM 让 Caddy 优雅退出（写完 autosave），超时才升级 SIGKILL。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# 退出码约定与 cli/_runtime.py 一致；gateway 层不反向依赖 cli 层。
EXIT_OK = 0
EXIT_FAIL = 1

# SIGTERM 之后留给 Caddy 优雅退出的时间（秒）；超时才升级到 SIGKILL。
GRACEFUL_TIMEOUT = 10.0

# SIGKILL 之后仍不退出时的最后期限（进程卡在 D 状态等极端情况）。
KILL_GRACE = 2.0

# 轮询间隔：短轮询才能及时察觉「Caddy 退出」与「优雅退出超时」。
POLL_INTERVAL = 0.2


class _Managed:
    """被托管的 Caddy 进程及其信号状态。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.signum: int | None = None
        self._term_sent = False

    def attach(self, proc: subprocess.Popen) -> None:
        """绑定子进程。信号可能落在「已装处理器、还没起进程」的窗口里 ——
        那时补一次转发，别让 Caddy 漏掉这次收尾。"""
        self.proc = proc
        if self.signum is not None:
            self.request_stop()

    def request_stop(self) -> None:
        """请 Caddy 优雅退出：向**进程组**发一次 SIGTERM（只发一次）。"""
        if self._term_sent or self.proc is None:
            return
        self._term_sent = True
        self._signal_group(signal.SIGTERM)

    def force_stop(self) -> None:
        """优雅退出超时 → SIGKILL 整个进程组（孙进程也不留）。"""
        self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig: int) -> None:
        # start_new_session=True → 子进程是会话与进程组的组长，pgid == pid。
        # 只有在子进程尚未被回收时才会走到这里（回收后 pid 可能被复用），
        # 所以按组发信号不会误伤无关进程组。
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _handler(managed: _Managed):
    """信号处理函数：记录信号并请 Caddy 优雅退出。

    刻意**不抛异常** —— 抛出去会打断下面的等待循环，反而来不及收尾。
    """

    def on_signal(signum, frame):
        managed.signum = signum
        managed.request_stop()

    return on_signal


def _install_handlers(managed: _Managed) -> dict[int, object]:
    """接管 SIGINT / SIGTERM / SIGHUP。

    终端的 Ctrl-C 只送到前台进程组（devproxy 自己），Caddy 在另一个会话里
    收不到 —— 收尾的节奏因此完全由我们掌控。
    """
    old: dict[int, object] = {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            old[sig] = signal.signal(sig, _handler(managed))
        except ValueError:
            # 非主线程装不了信号处理器（CLI 不会走到；嵌入/测试可能）
            pass
    return old


def _restore_handlers(old: dict[int, object]) -> None:
    for sig, handler in old.items():
        try:
            signal.signal(sig, handler)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            pass


def _supervise(managed: _Managed, graceful_timeout: float) -> int | None:
    """等 Caddy 退出；期间收到信号则优雅收尾，超时升级 SIGKILL。

    返回 Caddy 的退出码；None 表示「连 SIGKILL 都没能让它退出」。
    """
    proc = managed.proc
    if proc is None:  # pragma: no cover —— 只有 attach 之后才会调用
        return None
    deadline: float | None = None
    killed = False
    while True:
        try:
            return proc.wait(timeout=POLL_INTERVAL)
        except subprocess.TimeoutExpired:
            pass
        if managed.signum is None:
            continue
        now = time.monotonic()
        if deadline is None:
            # 刚收到信号：给 Caddy 优雅退出的时间（它要写 autosave）
            deadline = now + graceful_timeout
            continue
        if now < deadline:
            continue
        if killed:
            # SIGKILL 之后还不退出（卡在 D 状态等极端情况）：再等也等不到，
            # 不能让终端永远回不来。
            return None
        killed = True
        managed.force_stop()
        deadline = now + KILL_GRACE


def _ensure_gone(managed: _Managed) -> None:
    """兜底：Caddy 绝不能被留在后台。

    正常路径上它早就退出了；这里只防异常路径（抛异常、没等到退出）——
    前台模式的承诺是「Ctrl-C 即撤，不留痕迹」，留个野进程就破了。
    """
    proc = managed.proc
    if proc is None or proc.poll() is not None:
        return
    managed.force_stop()
    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass


def _describe_exit(rc: int) -> str:
    """把子进程的退出状态说成人话。"""
    if rc < 0:
        return f"被信号 {-rc} 终止"
    return f"退出码 {rc}"


def run_foreground(
    caddy_bin: Path,
    caddyfile: Path,
    graceful_timeout: float = GRACEFUL_TIMEOUT,
) -> int:
    """前台运行 `caddy run --config <caddyfile>`，返回 devproxy 的退出码。

    - 0：Caddy 被 Ctrl-C 收掉（已优雅退出，状态写完）
    - 1：Caddy 自己退出了（已报告它的退出码），或根本起不来

    全程不写任何系统状态：不装服务、不改配置、不动路由表。
    """
    managed = _Managed()
    old = _install_handlers(managed)
    try:
        argv = [str(caddy_bin), "run", "--config", str(caddyfile)]
        try:
            # stdio 不重定向：继承终端 —— 前台模式的意义就在这里。
            # start_new_session=True：Caddy 自成进程组，Ctrl-C 不会直接打到它。
            proc = subprocess.Popen(argv, start_new_session=True)
        except OSError as e:
            # 文件缺失 / 不可执行 / 格式不对（Exec format error）都归到这里
            print(f"devproxy: 无法启动 caddy: {e}", file=sys.stderr)
            return EXIT_FAIL
        managed.attach(proc)
        rc = _supervise(managed, graceful_timeout)
    finally:
        _ensure_gone(managed)
        _restore_handlers(old)

    if rc is None:
        # 连 SIGKILL 都没能让它退出 —— 不能当成「正常收掉了」
        print("devproxy: caddy 未能退出，已放弃等待", file=sys.stderr)
        return EXIT_FAIL
    if managed.signum is not None:
        # 用户主动收的：Caddy 已退出（优雅退出时 autosave 也写完了）
        return EXIT_OK
    print(f"devproxy: caddy 已退出（{_describe_exit(rc)}）", file=sys.stderr)
    return EXIT_FAIL
