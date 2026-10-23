"""selftest 的 fail-closed 路径单测 —— 不需要真的 caddy。

版本地板与「宿主信任库安装已禁用」的日志证据，是隔离沙箱不污染宿主
信任库的唯一屏障，必须锁死在测试里（I-5）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devproxy.gateway import selftest
from devproxy.gateway.selftest import (
    MIN_CADDY_VERSION,
    TRUST_DISABLED_LOG,
    _caddy_version,
    _wait_trust_disabled_log,
    run_selftest,
)


class _FakeRun:
    """按子命令名返回编排好的结果；未编排的调用直接报错。"""

    def __init__(self, answers: dict[str, subprocess.CompletedProcess]):
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        for key, proc in self.answers.items():
            if key in cmd:
                return proc
        raise AssertionError(f"测试未编排这次调用: {cmd}")


def _completed(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


@pytest.fixture
def caddy_bin(tmp_path) -> Path:
    bin_path = tmp_path / "caddy"
    bin_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bin_path.chmod(0o755)
    return bin_path


def test_min_version_floor_is_2_7_and_log_string_is_pinned():
    """install_trust 自 Caddy 2.7.0 才存在；日志证据串必须能在真机日志里命中。"""
    assert MIN_CADDY_VERSION == (2, 7, 0)
    # 已对照本机 caddy v2.11.7 二进制逐字核实（strings 命中）：
    # "...root certificate trust store installation disabled; unconfigured
    #  clients may show warnings"。用前缀做子串匹配，容忍后半句措辞变化。
    assert TRUST_DISABLED_LOG == "root certificate trust store installation disabled"
    assert (
        "root certificate trust store installation disabled; "
        "unconfigured clients may show warnings"
    ).startswith(TRUST_DISABLED_LOG)


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ("v2.11.7 h1:yj0Y4fYZGPkSvibBJ1sTWE33xC0fxztVyXEW5IIdUT4=\n", (2, 11, 7)),
        ("v2.7.0\n", (2, 7, 0)),
        ("2.6.4 h1:xxx\n", (2, 6, 4)),
        ("v2.7.0-beta.1\n", (2, 7, 0)),
    ],
)
def test_caddy_version_parses_version_strings(monkeypatch, stdout, expected):
    fake = _FakeRun({"version": _completed(stdout=stdout)})
    monkeypatch.setattr(selftest.subprocess, "run", fake)
    assert _caddy_version(Path("/unused")) == expected


@pytest.mark.parametrize(
    "stdout", ["not a version at all\n", "", "unknown\n"], ids=["garbage", "empty", "unknown"]
)
def test_caddy_version_returns_none_on_unparseable_output(monkeypatch, stdout):
    fake = _FakeRun({"version": _completed(stdout=stdout)})
    monkeypatch.setattr(selftest.subprocess, "run", fake)
    assert _caddy_version(Path("/unused")) is None


def test_caddy_version_returns_none_when_binary_fails(monkeypatch):
    def broken_run(cmd, *args, **kwargs):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(selftest.subprocess, "run", broken_run)
    assert _caddy_version(Path("/unused")) is None


def test_selftest_fails_closed_on_caddy_older_than_floor(monkeypatch, caddy_bin):
    """低于 2.7 的 caddy 会静默忽略 install_trust —— 必须判失败且不起沙箱。"""
    started = []

    def no_popen(*args, **kwargs):
        started.append(args)
        raise AssertionError("版本过旧时不得启动沙箱 caddy")

    monkeypatch.setattr(
        selftest.subprocess,
        "run",
        _FakeRun({"version": _completed(stdout="v2.6.4 h1:xxx\n")}),
    )
    monkeypatch.setattr(selftest.subprocess, "Popen", no_popen)

    result = run_selftest(caddy_bin)

    assert result.failed, "低于版本地板必须判失败"
    assert any("过旧" in desc for desc, _ in result.checks)
    assert not started, "版本过旧时不得启动沙箱 caddy"


def test_selftest_accepts_caddy_at_floor(monkeypatch, caddy_bin):
    """达到 2.7.0 → 不出现版本地板失败（后续步骤失败与否与此无关）。"""
    monkeypatch.setattr(
        selftest.subprocess,
        "run",
        _FakeRun(
            {
                "version": _completed(stdout="v2.7.0 h1:xxx\n"),
                "adapt": _completed(rc=1, stderr="adapt 失败（测试编排）"),
            }
        ),
    )
    monkeypatch.setattr(
        selftest.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("不应启动沙箱")
        )
    )

    result = run_selftest(caddy_bin)

    version_checks = [ok for desc, ok in result.checks if "caddy 版本" in desc]
    assert version_checks and all(version_checks), "≥ 2.7 不得报版本地板失败"
    assert not any("过旧" in desc for desc, _ in result.checks)
    # 确实走到了版本门之后（adapt 是版本检查通过后的第一步）
    assert any("adapt" in desc for desc, _ in result.checks)


# ------------------------------------------- 日志证据：install_trust 真的关了


def test_trust_disabled_log_line_is_detected(tmp_path):
    log = tmp_path / "caddy.log"
    log.write_text(
        "{\"level\":\"info\",\"ts\":1,\"msg\":\"root certificate trust store "
        "installation disabled; unconfigured clients may show warnings\"}\n",
        encoding="utf-8",
    )
    assert _wait_trust_disabled_log(log, timeout=0.2) is True


def test_trust_disabled_log_missing_fails_closed(tmp_path):
    """日志里没有那行证据 → 判失败（宁可误报也不能假设没污染宿主）。"""
    log = tmp_path / "caddy.log"
    log.write_text("some unrelated caddy output\n", encoding="utf-8")
    assert _wait_trust_disabled_log(log, timeout=0.2) is False


def test_trust_disabled_log_absent_file_fails_closed(tmp_path):
    assert _wait_trust_disabled_log(tmp_path / "absent.log", timeout=0.2) is False


# --------------------------------- I10: 动态端口 + Admin 实例身份校验


def _serve_config(payload: dict):
    import http.server
    import json
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_wait_admin_rejects_instance_with_other_storage():
    """端口上活着别人的 Caddy → 不能认领（否则 reload 会改到别人）。"""
    srv, base = _serve_config(
        {"storage": {"module": "file_system", "root": "/someone/else"}}
    )
    try:
        assert selftest._wait_admin(base, "/our/sandbox", timeout=0.5) is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_wait_admin_accepts_matching_sandbox_storage():
    srv, base = _serve_config(
        {"storage": {"module": "file_system", "root": "/our/sandbox"}}
    )
    try:
        assert selftest._wait_admin(base, "/our/sandbox", timeout=2) is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_free_ports_are_distinct_and_bindable():
    import socket

    ports = selftest._free_ports(3)
    assert len(set(ports)) == 3, "同时持有的端口不得重复"
    for port in ports:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
        finally:
            s.close()


def test_selftest_uses_dynamic_ports_and_production_caddyfile(
    monkeypatch, caddy_bin
):
    """沙箱 Caddyfile 必须由 build_main_caddyfile 生成，端口全部动态分配。"""
    captured: dict[str, str] = {}
    monkeypatch.setattr(selftest, "_caddy_version", lambda _: (2, 7, 0))
    monkeypatch.setattr(selftest, "_free_ports", lambda n: [21001, 21002, 21003])
    monkeypatch.setattr(
        selftest, "build_main_caddyfile", lambda **kwargs: "MARKER-CADDYFILE\n"
    )

    def fake_adapt(caddy_bin_, caddyfile, config_json):
        captured["text"] = caddyfile.read_text(encoding="utf-8")
        return False, "adapt 失败（测试编排）"

    monkeypatch.setattr(selftest, "_adapt", fake_adapt)

    result = run_selftest(caddy_bin)

    assert captured["text"] == "MARKER-CADDYFILE\n", "必须复用生产生成器"
    assert any("adapt" in desc for desc, _ in result.checks)


# --------------------------------- I11: adapt / reload 必须有超时


def test_adapt_timeout_returns_false(monkeypatch, tmp_path):
    def timeout_run(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))

    monkeypatch.setattr(selftest.subprocess, "run", timeout_run)
    ok, msg = selftest._adapt(
        Path("/unused"), tmp_path / "Caddyfile", tmp_path / "config.json", timeout=1
    )
    assert ok is False
    assert "超时" in msg


def test_adapt_oserror_returns_false(monkeypatch, tmp_path):
    def broken_run(cmd, *args, **kwargs):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(selftest.subprocess, "run", broken_run)
    ok, msg = selftest._adapt(
        Path("/unused"), tmp_path / "Caddyfile", tmp_path / "config.json"
    )
    assert ok is False
    assert "无法执行" in msg


def test_reload_timeout_returns_false(monkeypatch, tmp_path):
    def timeout_run(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))

    monkeypatch.setattr(selftest.subprocess, "run", timeout_run)
    ok, msg = selftest._reload(
        Path("/unused"), tmp_path / "config.json", 12345, timeout=1
    )
    assert ok is False
    assert "超时" in msg


def test_reload_oserror_returns_false(monkeypatch, tmp_path):
    def broken_run(cmd, *args, **kwargs):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(selftest.subprocess, "run", broken_run)
    ok, msg = selftest._reload(Path("/unused"), tmp_path / "config.json", 12345)
    assert ok is False
    assert "无法执行" in msg


# ---------------------------- I12: mkdtemp 之后所有异常都必须走统一清理


def test_selftest_cleans_tmp_when_setup_raises_after_mkdtemp(
    monkeypatch, caddy_bin, tmp_path
):
    import tempfile

    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def fake_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(prefix=kwargs.get("prefix", ""), dir=tmp_path)
        created.append(Path(path))
        return path

    monkeypatch.setattr(selftest.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(selftest, "_caddy_version", lambda _: (2, 7, 0))

    def boom(*args, **kwargs):
        raise RuntimeError("setup boom")

    monkeypatch.setattr(selftest, "_adapt", boom)

    with pytest.raises(RuntimeError):
        run_selftest(caddy_bin)

    assert created, "未创建临时目录？"
    assert not created[0].exists(), "异常路径必须回收 /tmp 沙箱目录"
