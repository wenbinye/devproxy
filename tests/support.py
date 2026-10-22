"""测试里共用的小工具：模拟"普通用户读不了 root 文件"的路径。

macOS 的 system scope 单元在 /Library/LaunchDaemons —— 权限或归属不对时
普通用户读不了。工具必须能自己把这类文件读出来（走提权会话），否则
"读不到"会被误判成"没有单元"，进而让 doctor 永远报红、让迁移把用户
手工维护的环境悄悄接管掉。
"""

import time
from pathlib import Path


def wait_until(predicate, timeout=30.0, interval=0.02) -> bool:
    """轮询直到条件成立；超时返回 False。

    「先 sleep N 秒、再断言」这种写法在负载高的机器上会随机翻车：睡够了
    不代表另一端的动作真的发生了（子进程可能还没起来、守护线程可能还没跑
    到第一轮）。这里改成**等到事情发生为止**，并给出明确的超时上限 ——
    正常路径立刻返回，异常路径才会等到超时（测试因此变慢，但不会误报）。
    """
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def wait_for_file(path, timeout=30.0) -> bool:
    """等文件出现（子进程写标记文件之类）。"""
    return wait_until(lambda: Path(path).exists(), timeout=timeout)


class UnreadablePath(Path):
    """读起来像 0600 root 的文件：普通用户 read_text 直接被拒。"""

    if hasattr(type(Path()), "_flavour"):  # Python 3.11 的路径子类需要它
        _flavour = type(Path())._flavour

    def read_text(self, *args, **kwargs):  # noqa: D102 —— 故意覆盖
        raise PermissionError(13, "Permission denied", str(self))


class ElevatedReader:
    """提权会话：读得到普通用户读不了的文件（sudo cat 读得到）。"""

    def __init__(self, platform=None):
        self.platform = platform

    def run(self, argv):
        return 0, ""

    def read(self, path):
        return Path(str(path)).read_text(encoding="utf-8")

    def write(self, path, content):
        target = Path(str(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def copy(self, src, dest):
        target = Path(str(dest))
        target.write_text(
            Path(str(src)).read_text(encoding="utf-8"), encoding="utf-8"
        )
