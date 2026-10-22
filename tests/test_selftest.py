"""selftest 的 fail-closed 路径单测 —— 不需要真的 caddy。

版本地板与「宿主信任库安装已禁用」的日志证据，是隔离沙箱不污染宿主
信任库的唯一屏障，必须锁死在测试里（I-5）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devproxy.proxy import selftest
from devproxy.proxy.selftest import (
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
