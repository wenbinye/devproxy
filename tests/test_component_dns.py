"""DNS 路由组件测试：resolved / system 双后端。

不写 /etc：apply 相关用例通过 monkeypatch 目标路径到 tmp_path，
服务操作走 FakePlatform。
"""

import os

import pytest

from devproxy.components import dns as dns_module
from devproxy.components.dns import (
    DnsComponent,
    render_resolved_conf,
    render_system_dnsmasq_conf,
)
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.fake import FakePlatform


def test_render_resolved_conf_shape():
    out = render_resolved_conf(port=5353, domains=["test"])
    assert "[Resolve]" in out
    assert "DNS=127.0.0.1:5353" in out
    assert "Domains=~.test" in out


def test_render_system_dnsmasq_conf_shape():
    out = render_system_dnsmasq_conf(suffix="test")
    assert "address=/.test/127.0.0.1" in out


def test_auto_backend_prefers_resolved(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    cfg = Config()
    platform = FakePlatform()
    platform.resolved_running = True
    comp = DnsComponent(platform, paths, cfg, backend="auto")
    assert comp.resolved_backend is True


def test_auto_backend_picks_system_when_resolved_inactive(tmp_path):
    """目录存在不代表 resolved 在跑 —— auto 必须探测进程状态。"""
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    platform = FakePlatform()
    platform.resolved_running = False
    comp = DnsComponent(platform, paths, Config(), backend="auto")
    assert comp.resolved_backend is False


def test_explicit_system_backend(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    comp = DnsComponent(FakePlatform(), paths, Config(), backend="system")
    assert comp.resolved_backend is False


def test_check_reports_missing_route(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    comp = DnsComponent(FakePlatform(), paths, Config(), backend="resolved")
    status = comp.check()
    assert status.ok is False
    assert "DNS" in status.detail or "解析" in status.detail


def test_plan_writes_resolved_conf(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    comp = DnsComponent(FakePlatform(), paths, Config(), backend="resolved")
    actions = comp.plan()
    assert any(a.privileged for a in actions), "写 /etc 需要提权"
    assert any(a.kind == "write" for a in actions)


# -- 组件 ---------------------------------------------------------------


def _component(tmp_path, backend="resolved", suffix="test", platform=None):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    platform = platform or FakePlatform()
    return (
        DnsComponent(platform, paths, Config(domain_suffix=suffix), backend=backend),
        platform,
    )


class _FailingElevated:
    """提权会话：写正常，跑命令失败 —— 模拟 systemctl restart 失败。"""

    def __init__(self, platform) -> None:
        self.platform = platform

    def run(self, argv):
        self.platform.commands.append(list(argv))
        return 1, "boom"

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def read(self, path):
        return path.read_text(encoding="utf-8")

    def copy(self, src, dest):
        pass


def test_resolved_plan_targets_resolved_conf_and_restarts_resolved(tmp_path):
    comp, _platform = _component(tmp_path, backend="resolved")
    actions = comp.plan()
    write_action = next(a for a in actions if a.kind == "write")
    assert write_action.path == dns_module.RESOLVED_CONF
    assert write_action.privileged is True
    assert "DNS=127.0.0.1:5353" in write_action.content
    assert "Domains=~.test" in write_action.content
    reload_action = next(a for a in actions if a.kind == "run")
    assert reload_action.command == ["systemctl", "restart", "systemd-resolved"]
    assert reload_action.privileged is True


def test_system_plan_targets_system_dnsmasq_and_restarts_dnsmasq(tmp_path):
    comp, _platform = _component(tmp_path, backend="system")
    actions = comp.plan()
    write_action = next(a for a in actions if a.kind == "write")
    assert write_action.path == dns_module.SYSTEM_DNSMASQ_CONF
    assert "address=/.test/127.0.0.1" in write_action.content
    reload_action = next(a for a in actions if a.kind == "run")
    assert reload_action.command == ["systemctl", "restart", "dnsmasq"]
    assert reload_action.privileged is True


def test_apply_writes_via_elevated_session_and_records_commands(
    tmp_path, monkeypatch
):
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    content = target.read_text(encoding="utf-8")
    assert "DNS=127.0.0.1:5353" in content
    assert "Domains=~.test" in content
    assert ["systemctl", "restart", "systemd-resolved"] in platform.commands


def test_check_ok_when_route_installed(tmp_path):
    comp, platform = _component(tmp_path, backend="resolved")
    platform.dns_backends.add("resolved")
    status = comp.check()
    assert status.ok is True
    assert "resolved" in status.detail


def test_verify_ok_when_route_installed(tmp_path):
    comp, platform = _component(tmp_path, backend="resolved")
    platform.dns_backends.add("resolved")
    assert comp.verify().ok is True
    assert platform.dns_queries == ["foo.test"]


def test_verify_fails_when_resolver_does_not_return_loopback(tmp_path):
    comp, platform = _component(tmp_path, backend="resolved")
    platform.dns_backends.add("resolved")
    platform.dns_answer = None
    status = comp.verify()
    assert status.ok is False
    assert "foo.test" in status.detail


def test_verify_fails_when_resolver_returns_wrong_address(tmp_path):
    comp, platform = _component(tmp_path, backend="resolved")
    platform.dns_backends.add("resolved")
    platform.dns_answer = "10.0.0.5"
    assert comp.verify().ok is False


def test_auto_falls_back_to_system_when_resolved_inactive(tmp_path):
    platform = FakePlatform()
    platform.resolved_running = False
    comp, _platform = _component(tmp_path, backend="auto", platform=platform)
    assert comp.resolved_backend is False
    assert comp.target_path == dns_module.SYSTEM_DNSMASQ_CONF


def test_auto_ignores_resolved_conf_dir_when_resolved_inactive(tmp_path, monkeypatch):
    """目录存在不是判据 —— 本机的目录就是手工创建的。"""
    monkeypatch.setattr(
        dns_module, "RESOLVED_CONF_DIR", tmp_path / "resolved.conf.d"
    )
    (tmp_path / "resolved.conf.d").mkdir()
    platform = FakePlatform()
    platform.resolved_running = False
    comp, _platform = _component(tmp_path, backend="auto", platform=platform)
    assert comp.resolved_backend is False


# -- setup 幂等：已就绪时 plan 必须为空（Important C） -------------------


def test_plan_empty_when_recorded_hash_matches(tmp_path, monkeypatch):
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    assert comp.plan() != []
    comp.apply(comp.plan())
    # 路由已按预期写入 → 再次 setup 必须是 no-op（不再要 sudo、不重启 resolved）
    assert comp.plan() == []


def test_plan_non_empty_when_recorded_hash_differs(tmp_path, monkeypatch):
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    # 改了域名后缀 → 内容变了 → 必须重写
    other, _p = _component(tmp_path, backend="resolved", suffix="dev")
    assert other.plan() != []


def test_plan_empty_when_file_content_matches_without_state(tmp_path, monkeypatch):
    """路由文件内容已是预期内容 —— 即使 state 文件丢了也不该重写。"""
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    comp.paths.state_dir.joinpath("dns-route.json").unlink()
    assert comp.plan() == []


def test_plan_uses_state_hash_when_file_unreadable(tmp_path, monkeypatch):
    """/etc 下的文件读不到时（普通用户），退回 state 目录里的哈希。"""
    if os.geteuid() == 0:
        pytest.skip("root 能读任意文件")
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    target.chmod(0o000)
    try:
        assert comp.plan() == []
    finally:
        target.chmod(0o600)


def test_plan_non_empty_when_target_file_removed(tmp_path, monkeypatch):
    """用户删掉 /etc 下的路由文件后，setup 不能还说“已就绪”。"""
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    target.unlink()
    assert comp.plan() != []


def test_plan_non_empty_when_target_file_edited(tmp_path, monkeypatch):
    """用户在 /etc 下改了路由文件 —— 状态哈希还指向旧内容，必须重写。"""
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    target.write_text("[Resolve]\nDNS=9.9.9.9\n", encoding="utf-8")
    assert comp.plan() != []


def test_state_not_recorded_when_reload_fails(tmp_path, monkeypatch):
    """重载失败就不能记成“已就绪”——否则下次 plan 空列表，实际没生效。"""
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, platform = _component(tmp_path, backend="resolved")
    monkeypatch.setattr(platform, "privilege", lambda: _FailingElevated(platform))
    with pytest.raises(RuntimeError):
        comp.apply(comp.plan())
    assert not (tmp_path / "s" / "dns-route.json").exists()


def test_state_not_recorded_for_other_write_paths(tmp_path):
    """状态文件只描述路由本身 —— 别的写动作不得污染它。"""
    from devproxy.platform.base import Action

    comp, _platform = _component(tmp_path, backend="resolved")
    other = tmp_path / "other.conf"
    comp.apply(
        [
            Action(
                description="无关写入",
                kind="write",
                path=other,
                content="hello",
                privileged=False,
            )
        ]
    )
    assert not (tmp_path / "s" / "dns-route.json").exists()


def test_apply_records_state_in_state_dir(tmp_path, monkeypatch):
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    state = tmp_path / "s" / "dns-route.json"
    assert state.exists()
    import json

    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["backend"] == "resolved"
    assert data["path"] == str(target)
    assert len(data["content_sha256"]) == 64


# -- 提权纪律（两个 minor） -----------------------------------------------


def test_apply_without_privileged_actions_does_not_request_privilege(
    tmp_path, monkeypatch
):
    comp, platform = _component(tmp_path, backend="resolved")

    def boom():
        raise AssertionError("无提权动作时不应请求提权")

    monkeypatch.setattr(platform, "privilege", boom)
    comp.apply([])


def test_non_privileged_write_uses_plain_filesystem(tmp_path):
    from devproxy.platform.base import Action

    comp, platform = _component(tmp_path, backend="resolved")
    target = tmp_path / "plain.conf"
    comp.apply(
        [
            Action(
                description="普通写入",
                kind="write",
                path=target,
                content="hello",
                privileged=False,
            )
        ]
    )
    assert target.read_text(encoding="utf-8") == "hello"
    assert target not in platform.written


def test_apply_unknown_action_raises(tmp_path):
    from devproxy.platform.base import Action

    comp, _platform = _component(tmp_path, backend="resolved")
    with pytest.raises(RuntimeError):
        comp.apply([Action(description="?", kind="run", command=["bogus"])])


# -- macOS：resolver 后端（每域解析器文件） ------------------------------


class _MacPlatform(FakePlatform):
    """macOS 平台：没有 systemd-resolved，走 /etc/resolver/<后缀>。"""

    name = "mac"
    dns_port = 5300


def test_macos_backend_key_is_resolver(tmp_path):
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    assert comp._backend_key() == "resolver"


def test_macos_resolved_backend_is_always_false(tmp_path):
    """macOS 没有 systemd-resolved —— 即使用户显式写 resolved 也不能选它。"""
    comp, _platform = _component(tmp_path, backend="resolved", platform=_MacPlatform())
    assert comp.resolved_backend is False


def test_macos_target_path_is_resolver_file(tmp_path, monkeypatch):
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", tmp_path / "etc" / "resolver")
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    assert comp.target_path == tmp_path / "etc" / "resolver" / "test"


def test_macos_plan_writes_resolver_file_with_platform_port(tmp_path, monkeypatch):
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", tmp_path / "etc" / "resolver")
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    actions = comp.plan()
    assert len(actions) == 1, "macOS 不需要重启系统解析器 —— mDNSResponder 自己重载"
    action = actions[0]
    assert action.kind == "write"
    assert action.privileged is True
    assert action.path == tmp_path / "etc" / "resolver" / "test"
    assert "nameserver 127.0.0.1" in action.content
    assert "port 5300" in action.content


def test_macos_plan_is_empty_when_resolver_file_in_place(tmp_path, monkeypatch):
    """路由已就位时 plan 必须为空 —— 重复 setup 不该再要 sudo。"""
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", tmp_path / "etc" / "resolver")
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    assert comp.plan() != []
    comp.apply(comp.plan())
    assert comp.plan() == []


def test_macos_apply_writes_the_resolver_file(tmp_path, monkeypatch):
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", tmp_path / "etc" / "resolver")
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    comp.apply(comp.plan())
    content = (tmp_path / "etc" / "resolver" / "test").read_text(encoding="utf-8")
    assert "nameserver 127.0.0.1" in content
    assert "port 5300" in content


def test_macos_check_reports_resolver_backend(tmp_path):
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    status = comp.check()
    assert status.ok is False
    assert "resolver" in status.detail


def test_macos_state_records_resolver_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(dns_module, "RESOLVER_DIR", tmp_path / "etc" / "resolver")
    comp, _platform = _component(tmp_path, backend="auto", platform=_MacPlatform())
    comp.apply(comp.plan())
    import json

    data = json.loads(comp.state_path.read_text(encoding="utf-8"))
    assert data["backend"] == "resolver"


def test_linux_plan_still_restarts_resolved(tmp_path):
    """Linux 路径不变：写 drop-in 之后仍要重启 systemd-resolved。"""
    comp, _platform = _component(tmp_path, backend="resolved")
    reload_action = next(a for a in comp.plan() if a.kind == "run")
    assert reload_action.command == ["systemctl", "restart", "systemd-resolved"]
