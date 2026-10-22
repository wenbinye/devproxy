import pytest

from devproxy.paths import Paths


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )


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
