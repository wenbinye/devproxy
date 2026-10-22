"""Dnsmasq 组件测试：配置增量合并 + 用户级服务单元。

全程不碰真实系统：配置路径注入 tmp_path，服务操作走 FakePlatform。
"""

import pytest

from pathlib import Path

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


# -- 上游与解析环路防护（Important B） ---------------------------------


def test_merge_empty_adds_no_resolv_and_default_upstreams():
    """空配置必须自带上游：否则 dnsmasq 拿 /etc/resolv.conf（resolved 桩）
    当上游，而 resolved 又指向本机 dnsmasq —— 解析环路。"""
    out = merge_dnsmasq_config("", port=5353, suffix="test")
    assert "no-resolv" in out.splitlines()
    servers = [ln for ln in out.splitlines() if ln.startswith("server=")]
    assert len(servers) >= 1
    # 上游不能是本机桩
    assert all("127.0.0.53" not in s for s in servers)


def test_merge_keeps_user_upstream_untouched():
    out = merge_dnsmasq_config("server=223.5.5.5\n", port=5353, suffix="test")
    servers = [ln for ln in out.splitlines() if ln.startswith("server=")]
    assert servers == ["server=223.5.5.5"]


# -- 全局上游 vs 域名限定上游（dnsmasq 手册：server= 不阻止读 resolv.conf） --


def _global_servers(lines):
    """全局上游 = server= 的值里没有域名部分。"""
    out = []
    for line in lines:
        s = line.strip()
        if not s.startswith("server="):
            continue
        value = s[len("server="):].strip()
        if value and "/" not in value and "," not in value:
            out.append(s)
    return out


def test_merge_domain_scoped_server_is_not_a_global_upstream():
    """server=/corp/10.0.0.1 只服务于该域名 —— 不能算全局上游。

    否则配置里既没有全局上游也没有 no-resolv，普通域名会走
    /etc/resolv.conf（resolved 机器上是 127.0.0.53 桩），而 resolved 又被
    工具的 drop-in 指回本机 dnsmasq → 全机解析失败。
    """
    out = merge_dnsmasq_config(
        "server=/corp.example.com/10.0.0.1\n", port=5353, suffix="test"
    )
    lines = out.splitlines()
    assert "no-resolv" in lines
    assert _global_servers(lines)
    # 用户的域名限定上游原样保留
    assert "server=/corp.example.com/10.0.0.1" in lines


def test_merge_global_server_keeps_it_and_adds_no_resolv():
    """server= 从不抑制读取 /etc/resolv.conf —— 手册明说要 --no-resolv。"""
    out = merge_dnsmasq_config("server=223.5.5.5\n", port=5353, suffix="test")
    lines = out.splitlines()
    assert "no-resolv" in lines
    assert [ln for ln in lines if ln.startswith("server=")] == ["server=223.5.5.5"]


def test_merge_resolv_file_prevents_no_resolv():
    """显式 resolv-file= 是用户自选解析器文件 —— 与 no-resolv 互斥，不加。"""
    out = merge_dnsmasq_config(
        "resolv-file=/etc/resolv.d/custom\n", port=5353, suffix="test"
    )
    assert "no-resolv" not in out.splitlines()


def test_merge_idempotent_for_domain_scoped_server():
    once = merge_dnsmasq_config(
        "server=/corp.example.com/10.0.0.1\n", port=5353, suffix="test"
    )
    twice = merge_dnsmasq_config(once, port=5353, suffix="test")
    assert once == twice


def test_merge_idempotent_for_global_server():
    once = merge_dnsmasq_config("server=223.5.5.5\n", port=5353, suffix="test")
    twice = merge_dnsmasq_config(once, port=5353, suffix="test")
    assert once == twice


def test_merge_idempotent_for_resolv_file():
    once = merge_dnsmasq_config(
        "resolv-file=/etc/resolv.d/custom\n", port=5353, suffix="test"
    )
    twice = merge_dnsmasq_config(once, port=5353, suffix="test")
    assert once == twice


# -- 只监听回环，避免变成局域网上的开放递归解析器 ----------------------


def test_merge_ensures_loopback_only_listener():
    out = merge_dnsmasq_config("", port=5353, suffix="test")
    lines = out.splitlines()
    assert "listen-address=127.0.0.1" in lines
    assert "bind-interfaces" in lines


def test_merge_keeps_user_listen_address():
    out = merge_dnsmasq_config(
        "listen-address=127.0.0.2\nbind-interfaces\n", port=5353, suffix="test"
    )
    lines = out.splitlines()
    assert "listen-address=127.0.0.2" in lines
    assert "listen-address=127.0.0.1" not in lines
    assert lines.count("bind-interfaces") == 1


def test_merge_does_not_duplicate_existing_no_resolv():
    out = merge_dnsmasq_config("no-resolv\n", port=5353, suffix="test")
    assert out.splitlines().count("no-resolv") == 1
    servers = [ln for ln in out.splitlines() if ln.startswith("server=")]
    assert len(servers) >= 1


def test_merge_with_upstream_is_idempotent():
    once = merge_dnsmasq_config("", port=5353, suffix="test")
    twice = merge_dnsmasq_config(once, port=5353, suffix="test")
    assert once == twice
    assert twice.splitlines().count("no-resolv") == 1
    assert len([ln for ln in twice.splitlines() if ln.startswith("server=")]) == len(
        [ln for ln in once.splitlines() if ln.startswith("server=")]
    )


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


def test_unit_keeps_dnsmasq_in_foreground(tmp_path):
    """dnsmasq 默认 fork 退出 —— Type=simple 会立刻判定单元 inactive。"""
    comp, platform, _conf = _component(tmp_path)
    comp.apply(comp.plan())
    unit = platform.services["dnsmasq"]
    assert "Type=simple" in unit
    assert "--keep-in-foreground" in unit
    assert "--pid-file" not in unit
    assert "ExecReload=/bin/kill -HUP $MAINPID" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit


def test_apply_preserves_existing_entries_on_disk(tmp_path):
    comp, _platform, conf_path = _component(
        tmp_path, conf="address=/.develop/127.0.0.1\nno-resolv\n"
    )
    comp.apply(comp.plan())
    content = conf_path.read_text(encoding="utf-8")
    assert "address=/.develop/127.0.0.1" in content
    assert "no-resolv" in content


def test_generated_conf_has_upstream_to_avoid_resolution_loop(tmp_path):
    """新机器上生成的配置必须自带上游 —— 否则 resolved ↔ dnsmasq 死循环。"""
    comp, _platform, conf_path = _component(tmp_path)
    comp.apply(comp.plan())
    lines = conf_path.read_text(encoding="utf-8").splitlines()
    assert "no-resolv" in lines
    assert [ln for ln in lines if ln.startswith("server=")]


def test_generated_conf_only_listens_on_loopback(tmp_path):
    """没有 listen-address/bind-interfaces 时 dnsmasq 会绑 0.0.0.0:5353 ——
    变成局域网上的开放递归解析器。"""
    comp, _platform, conf_path = _component(tmp_path)
    comp.apply(comp.plan())
    lines = conf_path.read_text(encoding="utf-8").splitlines()
    assert "listen-address=127.0.0.1" in lines
    assert "bind-interfaces" in lines


def test_unit_conf_dir_is_absolute_for_relative_conf_path(tmp_path, monkeypatch):
    """systemd 把相对路径按 / 解析 —— 单元里必须是绝对路径或 %h/... 。"""
    monkeypatch.chdir(tmp_path)
    platform = FakePlatform()
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    comp = DnsmasqComponent(
        platform, paths, Config(), conf_path=Path("rel/dnsmasq.conf")
    )
    comp.apply(comp.plan())
    unit = platform.services["dnsmasq"]
    conf_dir = unit.split("--conf-dir=")[1].split()[0]
    # 相对路径会被 systemd 按 / 解析 —— 必须是绝对路径或 %h/...
    assert conf_dir.startswith("/") or conf_dir.startswith("%h/")
    assert conf_dir != "rel" and not conf_dir.startswith("rel/")


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


def test_plan_empty_when_already_in_place(tmp_path):
    """已就绪时 plan 必须为空 —— setup 才能判定为 no-op。"""
    comp, _platform, _conf = _component(tmp_path)
    comp.apply(comp.plan())
    assert comp.plan() == []


def test_plan_non_empty_when_service_down(tmp_path):
    comp, platform, _conf = _component(tmp_path)
    comp.apply(comp.plan())
    platform.active.discard("dnsmasq")
    assert comp.plan() != []


def test_plan_non_empty_when_conf_changed(tmp_path):
    comp, _platform, conf_path = _component(tmp_path)
    comp.apply(comp.plan())
    conf_path.write_text("port=5353\n", encoding="utf-8")
    assert comp.plan() != []


def test_apply_unknown_action_raises(tmp_path):
    from devproxy.platform.base import Action

    comp, _platform, _conf = _component(tmp_path)
    with pytest.raises(RuntimeError):
        comp.apply([Action(description="?", kind="run", command=["bogus"])])


# -- verify：真的能解析 *.test ----------------------------------------------


def test_verify_ok_when_dns_query_returns_loopback(tmp_path):
    comp, platform, _conf = _component(tmp_path)
    platform.active.add("dnsmasq")
    status = comp.verify()
    assert status.ok is True
    assert platform.dns_queries == ["foo.test"]


def test_verify_fails_when_dns_query_returns_nothing(tmp_path):
    comp, platform, _conf = _component(tmp_path)
    platform.active.add("dnsmasq")
    platform.dns_answer = None
    status = comp.verify()
    assert status.ok is False
    assert "foo.test" in status.detail
    assert status.hint


def test_verify_fails_when_dns_query_returns_wrong_address(tmp_path):
    comp, platform, _conf = _component(tmp_path)
    platform.active.add("dnsmasq")
    platform.dns_answer = "10.0.0.5"
    assert comp.verify().ok is False


# -- 备份：改写用户既有配置前必须先备份 --------------------------------------


def test_apply_backs_up_changed_user_config(tmp_path):
    original = "port=53\naddress=/.develop/127.0.0.1\n"
    comp, _platform, conf_path = _component(tmp_path, conf=original)
    comp.apply(comp.plan())

    backups = sorted((tmp_path / "s" / "backups").glob("dnsmasq.conf.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_apply_does_not_backup_when_content_unchanged(tmp_path):
    """内容没变时不得落备份 —— 否则每次 setup 都堆垃圾。"""
    comp, platform, conf_path = _component(tmp_path)
    merged = merge_dnsmasq_config(
        "", port=platform.dns_port, suffix=Config().domain_suffix
    )
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(merged, encoding="utf-8")

    from devproxy.platform.base import Action

    comp.apply(
        [
            Action(
                description="写同样的内容",
                kind="write",
                path=conf_path,
                content=merged,
            )
        ]
    )
    backups = list((tmp_path / "s" / "backups").glob("*.bak"))
    assert backups == []


def test_apply_creates_no_backup_for_new_config(tmp_path):
    comp, _platform, _conf_path = _component(tmp_path)
    comp.apply(comp.plan())
    assert list((tmp_path / "s" / "backups").glob("*.bak")) == []
