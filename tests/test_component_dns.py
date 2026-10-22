"""DNS 路由组件测试：resolved / system 双后端。

不写 /etc：apply 相关用例通过 monkeypatch 目标路径到 tmp_path，
服务操作走 FakePlatform。
"""

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
    comp = DnsComponent(FakePlatform(), paths, cfg, backend="auto")
    assert comp.resolved_backend is True


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


def _component(tmp_path, backend="resolved", suffix="test"):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    platform = FakePlatform()
    return (
        DnsComponent(platform, paths, Config(domain_suffix=suffix), backend=backend),
        platform,
    )


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


def test_auto_falls_back_to_system_without_resolved_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dns_module, "RESOLVED_CONF_DIR", tmp_path / "no-such-resolved-dir"
    )
    comp, _platform = _component(tmp_path, backend="auto")
    assert comp.resolved_backend is False
    assert comp.target_path == dns_module.SYSTEM_DNSMASQ_CONF


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


def test_plan_non_empty_without_state_file(tmp_path, monkeypatch):
    target = tmp_path / "etc" / "resolved.conf.d" / "devproxy.conf"
    monkeypatch.setattr(dns_module, "RESOLVED_CONF", target)
    comp, _platform = _component(tmp_path, backend="resolved")
    comp.apply(comp.plan())
    comp.paths.state_dir.joinpath("dns-route.json").unlink()
    assert comp.plan() != []


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
