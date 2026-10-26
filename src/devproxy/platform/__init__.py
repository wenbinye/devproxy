"""Platform implementations.

The component layer depends only on the `Platform` protocol and never invokes
system commands directly; `get_platform()` picks the implementation for the
current system.
"""

from __future__ import annotations

import sys

from devproxy.platform.base import Platform


def get_platform() -> Platform:
    """Return the platform implementation for the current system."""
    if sys.platform == "darwin":
        from devproxy.platform.macos import MacOSPlatform

        return MacOSPlatform()
    from devproxy.platform.linux import LinuxPlatform

    return LinuxPlatform()
