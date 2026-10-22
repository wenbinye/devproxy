"""测试里共用的小工具：模拟"普通用户读不了 root 文件"的路径。

macOS 的 system scope 单元在 /Library/LaunchDaemons —— 权限或归属不对时
普通用户读不了。工具必须能自己把这类文件读出来（走提权会话），否则
"读不到"会被误判成"没有单元"，进而让 doctor 永远报红、让迁移把用户
手工维护的环境悄悄接管掉。
"""

from pathlib import Path


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
