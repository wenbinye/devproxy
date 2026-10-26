import os

import pytest

from devproxy.core.paths import Paths

# CLI 的输出断言不能依赖「跑测试的这台机器」的渲染环境：
#   - 有终端/设了 FORCE_COLOR 时 Click 会给 `--help` 这类选项上色，
#     把 `--` 拆成两段带转义序列的片段，`"--" in output` 直接失效；
#   - 没有终端时 Rich 退回 80 列，帮助表与错误面板会被截断。
# 在 import 任何会构造 Console 的模块之前钉死这两个变量。
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

    # Click 自己的着色（错误/用法提示里的选项高亮）由这两个变量控制
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
