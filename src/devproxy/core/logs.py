"""后台应用的日志：轮转写入与跟随查看。

`sync` 拉起的应用是后台进程（独立会话），它的输出不能打在终端 ——
devproxy 自己收下，落到 `~/.local/state/devproxy/logs/routes/<名字>.log`。

**为什么要轮转**：Spring Boot 这类应用在开发时会以 MB/小时 的速度写 INFO 日志
（实测 ~4.4MB/小时）。日志文件没人管的话，跑几天就能吃掉几百 MB，
而 `uninstall` 也不会清理它（它删的是服务与配置，不碰用户的日志）。

轮转在**写入端**做（不是靠 logrotate）—— 因为这些日志是 devproxy 自己
拉起的进程写出来的，只有写它的人能保证"文件不会无限大"。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

# 单个日志文件的上限。超过就轮转：当前文件改名成 .1（覆盖旧的 .1），
# 新文件从头开始写 —— 总量因此有界（上限 × (1 + 保留份数)）。
MAX_LOG_BYTES = 10 * 1024 * 1024

# 保留几份历史。1 份 = 上限 10MB 的当前文件 + 10MB 的上一份 = 每条路由最多 20MB。
KEEP_HISTORY = 1


def history_path(path: Path, index: int) -> Path:
    """第 index 份历史文件的名字：<name>.log.1 / <name>.log.2 …"""
    return path.with_name(f"{path.name}.{index}")


def rotate_at_start(path: Path, max_bytes: int = MAX_LOG_BYTES) -> None:
    """启动前把超额的旧日志挪成 .1，让当前文件从头开始。

    只在**超过上限**时才动 —— 小日志留着（频繁重启也不会把有用的
    尾巴弄丢），大日志说明上一轮跑得久/话多，该分家了。

    只在**启动**时做：运行中的文件被子进程持着 fd，替换不了。
    """
    if not path.exists():
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    for index in range(KEEP_HISTORY, 0, -1):
        src = path if index == 1 else history_path(path, index - 1)
        dst = history_path(path, index)
        if not src.exists():
            continue
        try:
            if dst.exists():
                dst.unlink()
            src.replace(dst)
        except OSError:
            continue
        break


class RotatingLogWriter:
    """把字节写进日志文件，超过上限就轮转。

    必须**自己**持有文件句柄（不能靠继承的 fd）—— 只有这样才能在运行中
    换文件：关掉当前句柄、改名、再开一个新的。
    """

    def __init__(self, path: Path, max_bytes: int = MAX_LOG_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._handle: BinaryIO | None = None
        self._open()

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "ab")  # 追加模式：多进程写不会互相截断

    def write(self, data: bytes) -> None:
        if self._handle is None or not data:
            return
        try:
            self._handle.write(data)
            self._handle.flush()
            if self._handle.tell() >= self.max_bytes:
                self._rotate()
        except (OSError, ValueError):
            # 写不进去（磁盘满、句柄被关）—— 丢弃这段输出，但不能因此
            # 让子进程卡在写管道上（见 pump 的说明）。
            pass

    def _rotate(self) -> None:
        try:
            if self._handle is not None:
                self._handle.close()
        except OSError:
            pass
        self._handle = None
        for index in range(KEEP_HISTORY, 0, -1):
            src = self.path if index == 1 else history_path(self.path, index - 1)
            dst = history_path(self.path, index)
            try:
                if dst.exists():
                    dst.unlink()
                src.replace(dst)
            except OSError:
                continue
            break
        try:
            self._open()
        except OSError:
            self._handle = None

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


def follow_log(path: Path) -> None:
    """跟随输出日志文件（像 tail -f）。Ctrl-C 退出。

    自己读而不用 `tail -f` 子进程：文件被轮转时，`tail -f` 会跟着旧 inode
    读到文件末尾就停住，自己读可以发现「文件被换掉了」并重新打开。
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)  # 从末尾开始跟：新内容才打出来
        inode = os.fstat(handle.fileno()).st_ino
        while True:
            chunk = handle.readline()
            if chunk:
                print(chunk.decode("utf-8", "replace"), end="", flush=True)
                continue
            time.sleep(0.3)
            # 文件被轮转掉了（inode 变了）→ 重新打开，接着跟新的
            try:
                current = os.stat(path).st_ino
            except OSError:
                current = None
            if current is not None and current != inode:
                handle.close()
                handle = path.open("rb")
                inode = os.fstat(handle.fileno()).st_ino


def follow_many(targets: list[tuple[str, Path]], poll: float = 0.3) -> Iterator[tuple[str, str]]:
    """同时跟随多个日志文件，产出 (标签, 一行文本)。

    - 已存在的文件从**末尾**开始跟（不倒历史）；
    - 还不存在的文件**等它出现**，出现后从头读（那是应用刚启动的输出，
      正是你想看的）；轮转（inode 变了）会自动重开接着跟。
    - 产出顺序即到达顺序，不保证跨文件的行序。

    永不主动结束 —— 跟随是 Ctrl-C 打断的（KeyboardInterrupt 从 get() 冒出）。
    """
    import queue
    import threading

    queue_: queue.Queue[tuple[str, str]] = queue.Queue()

    def watch(tag: str, path: Path) -> None:
        handle = None
        inode = None
        carry = b""  # 还没等到换行的残行
        # 等文件出现（应用还没写过日志时它不存在）
        while handle is None:
            try:
                handle = path.open("rb")
            except OSError:
                time.sleep(poll)
        # **已有文件从末尾跟** —— 不倒历史。xxl-job-admin 这类日志能到 9MB，
        # 从头读会让用户以为"日志一直在刷"，其实是把历史全倒了一遍。
        handle.seek(0, os.SEEK_END)
        inode = os.fstat(handle.fileno()).st_ino
        try:
            while True:
                data = handle.read(65536)
                if data:
                    carry += data
                    # 只产出**完整的行**：残行留着等下一个换行，
                    # 否则同一条日志会被拆成几段、每段各带一个前缀
                    *complete, carry = carry.split(b"\n")
                    for line in complete:
                        queue_.put((tag, line.decode("utf-8", "replace") + "\n"))
                    continue
                time.sleep(poll)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if stat.st_ino != inode:
                    # 被轮转了：重开新文件，从头读（新文件是新会话的输出）
                    handle.close()
                    handle = path.open("rb")
                    inode = os.fstat(handle.fileno()).st_ino
                    carry = b""
                elif stat.st_size < handle.tell():
                    # 被截断了（`: > 文件`）：回到开头
                    handle.seek(0)
                    carry = b""
        finally:
            if handle is not None:
                handle.close()

    for tag, path in targets:
        threading.Thread(
            target=watch, args=(tag, path), name=f"devproxy-follow-{tag}", daemon=True
        ).start()
    while True:
        yield queue_.get()
