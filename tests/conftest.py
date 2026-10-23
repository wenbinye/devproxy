import pytest

from devproxy.core.paths import Paths


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """把 HOME 指向临时目录 —— 测试绝不写进用户家目录。

    Path.home() 与 os.path.expanduser("~") 都认 HOME：这挡住了
    default_paths() 在真实的 ~/.config/devproxy、~/.local/state/devproxy
    里凭空造文件，也挡住了 dnsmasq 配置、~/.pki/nssdb 这类家目录路径。
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    return home


@pytest.fixture(autouse=True)
def isolated_cli_platform(monkeypatch):
    """把 CLI 用的平台换成 FakePlatform —— 测试绝不允许碰真实系统。

    CLI 的 setup 会改写 ~/.config/systemd/user/ 下的单元、写 /etc 下的
    DNS 配置，迁移流程还会重启真实服务。这些路径全由平台对象决定，
    只要平台还是真的 LinuxPlatform，一次 `setup --yes` 就会改掉用户
    机器上的东西（曾经真的把用户自己的 caddy.service 覆盖掉）。

    这里全局兜底：任何测试默认拿到的都是隔离的假平台；确实需要真实
    平台的测试自己再 monkeypatch 覆盖（顺序在后，优先生效）。
    """
    from devproxy.platform.fake import FakePlatform

    platform = FakePlatform()
    monkeypatch.setattr("devproxy.cli._platform", lambda: platform)
    return platform
