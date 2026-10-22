import json
import os

import pytest

from devproxy.proxy.routes import (
    Route,
    RouteTable,
    RouteTableError,
    atomic_write,
    load_table,
    render_caddyfile,
    save_table,
)


def _route(name="codeup", port=42133, mode="up"):
    return Route(
        name=name,
        domain=f"{name}.test",
        port=port,
        mode=mode,
        upstream=f"127.0.0.1:{port}",
    )


def test_render_empty_table_is_header_only():
    out = render_caddyfile(RouteTable())
    assert out == "# 由 devproxy 自动生成 —— 请勿手工编辑\n"


def test_render_single_route():
    out = render_caddyfile(RouteTable(routes=[_route()]))
    assert out == (
        "# 由 devproxy 自动生成 —— 请勿手工编辑\n"
        "\n"
        "codeup.test {\n"
        "\ttls internal\n"
        "\treverse_proxy 127.0.0.1:42133\n"
        "}\n"
    )


def test_render_two_routes_separated_by_blank_line():
    out = render_caddyfile(RouteTable(routes=[_route(), _route("legacy", 3000, "expose")]))
    assert out.count("tls internal") == 2
    assert "\n}\n\nlegacy.test {" in out


def test_render_is_deterministic():
    table = RouteTable(routes=[_route()])
    assert render_caddyfile(table) == render_caddyfile(table)


def test_render_contains_no_timestamp():
    out = render_caddyfile(RouteTable(routes=[_route()]))
    assert "生成于" not in out, "渲染必须确定性，不能含时间戳"


def test_route_roundtrip():
    r = _route()
    r.command = ["sh", "-c", "npx vite"]
    r.pid = 12345
    assert Route.from_dict(r.to_dict()) == r


def test_route_roundtrip_keeps_proc_start():
    """pid 的进程身份随路由持久化；老版本路由表没有该字段时为 None。"""
    r = _route()
    r.pid = 4242
    r.proc_start = "12345"
    d = r.to_dict()
    assert d["proc_start"] == "12345"
    assert Route.from_dict(d).proc_start == "12345"
    legacy = {k: v for k, v in d.items() if k != "proc_start"}
    assert Route.from_dict(legacy).proc_start is None


def test_table_roundtrip():
    t = RouteTable(routes=[_route()], domain_suffix="test")
    assert RouteTable.from_dict(t.to_dict()) == t


def test_table_upsert_replaces_same_name():
    t = RouteTable(routes=[_route(port=1)])
    t.upsert(_route(port=2))
    assert len(t.routes) == 1
    assert t.routes[0].port == 2


def test_table_remove_returns_whether_removed():
    t = RouteTable(routes=[_route()])
    assert t.remove("codeup") is True
    assert t.remove("codeup") is False


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "routes.json"
    t = RouteTable(routes=[_route()])
    save_table(p, t)
    assert load_table(p) == t


def test_load_missing_file_returns_empty_table(tmp_path):
    t = load_table(tmp_path / "absent.json")
    assert t.routes == []
    assert t.version == 1


def test_atomic_write_leaves_no_tmp_file(tmp_path):
    p = tmp_path / "x.txt"
    atomic_write(p, "hello")
    assert p.read_text() == "hello"
    assert list(tmp_path.iterdir()) == [p]


def test_atomic_write_uses_unique_temp_name(tmp_path):
    """临时文件名必须唯一 —— 否则两个写者会撞进同一个中间产物。"""
    p = tmp_path / "x.txt"
    # 别的进程（不同 pid）留下的临时文件不能被我们覆盖
    foreign = tmp_path / "x.txt.424242.tmp"
    foreign.write_text("别人的临时文件", encoding="utf-8")
    atomic_write(p, "hello")
    assert p.read_text() == "hello"
    assert foreign.read_text(encoding="utf-8") == "别人的临时文件"


def test_atomic_write_fsyncs_before_replace(tmp_path, monkeypatch):
    """落盘前必须 fsync —— 否则断电后 replace 可能指向没写进盘的内容。"""
    calls = []
    real_fsync = os.fsync

    def spy(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    atomic_write(tmp_path / "x.txt", "hello")
    assert calls, "写临时文件后必须 fsync"


def test_atomic_write_cleans_temp_on_failure(tmp_path, monkeypatch):
    """写失败时也不能留下临时文件。"""

    def boom(*a, **k):
        raise OSError("磁盘满了")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(tmp_path / "x.txt", "hello")
    assert list(tmp_path.iterdir()) == []


# --------------------- I-6: 渲染期输入校验（routes.json 是真相但不是免检通道）


def _bad_route(**overrides):
    base = dict(
        name="x",
        domain="x.test",
        port=1,
        mode="up",
        upstream="127.0.0.1:1",
    )
    base.update(overrides)
    return Route(**base)


def test_render_rejects_domain_with_caddyfile_metacharacters():
    """domain 直通站点块地址 —— 换行/大括号可伪造额外站点块，必须拒绝。"""
    bad = _bad_route(domain="x.test }\nimport /tmp/evil\n#")
    with pytest.raises(RouteTableError) as cm:
        render_caddyfile(RouteTable(routes=[bad]))
    assert "domain" in str(cm.value) and "x" in str(cm.value)


def test_render_rejects_upstream_with_caddyfile_metacharacters():
    bad = _bad_route(upstream="127.0.0.1:1 }\n}\nimport /tmp/evil")
    with pytest.raises(RouteTableError) as cm:
        render_caddyfile(RouteTable(routes=[bad]))
    assert "upstream" in str(cm.value)


def test_render_rejects_domain_with_dots_or_uppercase():
    for domain in ("X.TEST", "x..test", "x.test.", "-x.test"):
        with pytest.raises(RouteTableError):
            render_caddyfile(RouteTable(routes=[_bad_route(domain=domain)]))


def test_render_rejects_bad_upstream_forms():
    for upstream in ("127.0.0.1", ":8080", "127.0.0.1:99999", "http://127.0.0.1:1"):
        with pytest.raises(RouteTableError):
            render_caddyfile(RouteTable(routes=[_bad_route(upstream=upstream)]))
