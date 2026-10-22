"""Dnsmasq 组件测试：配置增量合并 + 用户级服务单元。

全程不碰真实系统：配置路径注入 tmp_path，服务操作走 FakePlatform。
"""

import pytest

from devproxy.components.dnsmasq import DnsmasqComponent, merge_dnsmasq_config
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.fake import FakePlatform


def test_merge_adds_port_and_addresses_to_empty():
    out = merge_dnsmasq_config("", port=5353, suffix="test")
    assert "port=5353" in out
    assert "address=/.test/127.0.0.1" in out
    assert "address=/.test/::1" in out


def test_merge_preserves_existing_address_entries():
    existing = "address=/.develop/127.0.0.1\naddress=/.develop/::1\nno-resolv\nserver=223.5.5.5\nfilter-AAAA\n"
    out = merge_dnsmasq_config(existing, port=5353, suffix="test")
    assert "address=/.develop/127.0.0.1" in out
    assert "address=/.develop/::1" in out
    assert "no-resolv" in out
    assert "server=223.5.5.5" in out
    assert "filter-AAAA" in out


def test_merge_replaces_existing_port():
    out = merge_dnsmasq_config("port=53\n", port=5353, suffix="test")
    # 注意：不能用 "port=53" not in out 判断 —— "port=5353" 本身包含该子串。
    # 旧端口必须整行消失，且只剩一行 port=。
    lines = out.splitlines()
    assert "port=53" not in lines
    assert "port=5353" in lines
    assert out.count("port=") == 1


def test_merge_is_idempotent():
    once = merge_dnsmasq_config("", port=5353, suffix="test")
    twice = merge_dnsmasq_config(once, port=5353, suffix="test")
    assert once == twice


def test_merge_does_not_duplicate_address():
    existing = "address=/.test/127.0.0.1\naddress=/.test/::1\n"
    out = merge_dnsmasq_config(existing, port=5353, suffix="test")
    assert out.count("address=/.test/127.0.0.1") == 1
    assert out.count("address=/.test/::1") == 1


def test_merge_handles_leading_dot_suffix():
    out = merge_dnsmasq_config("", port=5353, suffix=".test")
    assert "address=/.test/127.0.0.1" in out
    assert "address=/.test/::1" in out


# -- 组件 ---------------------------------------------------------------


def _component(tmp_path, conf=None, platform=None):
    platform = platform or FakePlatform()
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    conf_path = tmp_path / "dnsmasq" / "dnsmasq.conf"
    if conf is not None:
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        conf_path.write_text(conf, encoding="utf-8")
    return DnsmasqComponent(
        platform, paths, Config(), conf_path=conf_path
    ), platform, conf_path


def test_plan_keeps_user_entries_and_writes_to_conf_path(tmp_path):
    existing = "address=/.develop/127.0.0.1\nno-resolv\nserver=223.5.5.5\n"
    comp, _platform, conf_path = _component(tmp_path, conf=existing)
    actions = comp.plan()
    write_actions = [a for a in actions if a.kind == "write"]
    assert len(write_actions) == 1
    action = write_actions[0]
    assert action.path == conf_path
    assert "address=/.develop/127.0.0.1" in action.content
    assert "no-resolv" in action.content
    assert "server=223.5.5.5" in action.content
    assert "port=5353" in action.content
    assert "address=/.test/127.0.0.1" in action.content
    assert "address=/.test/::1" in action.content


def test_plan_contains_install_service_action(tmp_path):
    comp, _platform, _conf = _component(tmp_path)
    actions = comp.plan()
    assert any(a.command[:1] == ["install-service"] for a in actions)


def test_apply_writes_conf_and_installs_user_service(tmp_path):
    comp, platform, conf_path = _component(tmp_path)
    comp.apply(comp.plan())
    content = conf_path.read_text(encoding="utf-8")
    assert "port=5353" in content
    assert "address=/.test/127.0.0.1" in content
    # systemd user 单元（不是系统级）
    unit = platform.services["dnsmasq"]
    assert "[Service]" in unit
    assert "dnsmasq" in unit
    assert "dnsmasq" in platform.active


def test_apply_preserves_existing_entries_on_disk(tmp_path):
    comp, _platform, conf_path = _component(
        tmp_path, conf="address=/.develop/127.0.0.1\nno-resolv\n"
    )
    comp.apply(comp.plan())
    content = conf_path.read_text(encoding="utf-8")
    assert "address=/.develop/127.0.0.1" in content
    assert "no-resolv" in content


def test_check_reports_service_not_running(tmp_path):
    comp, _platform, _conf = _component(tmp_path)
    status = comp.check()
    assert status.ok is False
    assert "dnsmasq" in status.detail


def test_check_reports_missing_conf(tmp_path):
    comp, platform, _conf = _component(tmp_path)
    platform.active.add("dnsmasq")
    status = comp.check()
    assert status.ok is False
    assert "配置" in status.detail


def test_check_ok_after_apply(tmp_path):
    comp, _platform, _conf = _component(tmp_path)
    comp.apply(comp.plan())
    status = comp.check()
    assert status.ok is True


def test_apply_unknown_action_raises(tmp_path):
    from devproxy.platform.base import Action

    comp, _platform, _conf = _component(tmp_path)
    with pytest.raises(RuntimeError):
        comp.apply([Action(description="?", kind="run", command=["bogus"])])
