"""平台实现集合。

组件层只依赖 `Platform` 协议，不直接调用系统命令；
`get_platform()` 按当前系统挑实现。
"""

from __future__ import annotations

import sys

from devproxy.platform.base import Platform


def get_platform() -> Platform:
    """按当前系统返回对应平台实现。"""
    if sys.platform == "darwin":
        from devproxy.platform.macos import MacOSPlatform

        return MacOSPlatform()
    from devproxy.platform.linux import LinuxPlatform

    return LinuxPlatform()
