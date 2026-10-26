import os

import pytest

# CLI 的输出断言不能依赖「跑测试的这台机器」的渲染环境，两层都要钉死：
#
# 1) typer 自己渲染 help / 用法错误时，会用 highlighter 给 `--help` 这类选项名
#    **拆词上样式**；即便设了 NO_COLOR，「粗体」这类属性仍会发出转义序列，
#    把 `--dry-run` 拆成 `-` + `-dry` + `-run` 三段 —— `option in output` 直接失效。
#    关掉整个 color system，渲染出来的就是纯文本。这几个常量就是 typer
#    留给调用方覆盖的。
# 2) Rich 从环境推导宽度：有终端时取真实宽度，没有时退回 80 列，帮助表与
#    错误面板会被截断。钉成 200。
import typer.rich_utils as _rich_utils

from devproxy.core.paths import Paths

_rich_utils.COLOR_SYSTEM = None
_rich_utils.FORCE_TERMINAL = False

os.environ["NO_COLOR"] = "1"
os.environ["COLUMNS"] = "200"
os.environ.pop("FORCE_COLOR", None)
os.environ.pop("CLICOLOR_FORCE", None)


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Point HOME at a temporary directory -- tests must never write into the real home.

        Both Path.home() and os.path.expanduser("~") honour HOME: this keeps default_paths()
    from conjuring files inside the real ~/.config/devproxy or ~/.local/state/devproxy, and
    keeps home-directory paths such as the dnsmasq config and ~/.pki/nssdb away as well.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    return home


@pytest.fixture(autouse=True)
def isolated_cli_platform(monkeypatch):
    """Swap the CLI platform for FakePlatform -- tests must never touch the real system.

    The CLI's setup rewrites the units under ~/.config/systemd/user/ and the DNS config
    under /etc, and the migration restarts real services. All those paths are decided by the
    platform object, so as long as the platform is still the real LinuxPlatform, one
    `setup --yes` changes things on the user's machine (it once really overwrote the user's
    own caddy.service).

    This is the global fallback: every test gets an isolated fake platform by default; tests
    that genuinely need the real one monkeypatch over it themselves (later wins).
    """
    from devproxy.platform.fake import FakePlatform

    platform = FakePlatform()
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    return platform


@pytest.fixture(autouse=True)
def deterministic_console(monkeypatch):
    """让 CLI 的渲染在每台机器上都一样（无颜色、足够宽）。

    上面的环境变量只对**之后**构造的 Console 生效；devproxy 自己的两个 Console 是
    模块级单例，早在 import 时就建好了 —— 这里直接把它们的颜色与宽度钉死。
    断言看的是文案与行为，不该因为终端宽窄或有没有颜色而变红。
    """
    from devproxy.cli import _runtime

    for console in (_runtime.console, _runtime.err_console):
        console.no_color = True
        console.width = 200
