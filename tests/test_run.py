"""devproxy run —— 前台托管 Caddy 的行为测试。

**不真起 Caddy**：用一个假的可执行文件（python 脚本）顶替。它扮演三种角色：

- `validate` 子命令：直接成功（配置校验通过）
- `run` 子命令：起来之后等 SIGTERM，写标记文件后退出
- 另有一个「犟」的版本：无视 SIGTERM —— 用来验证优雅退出超时后升级 SIGKILL

信号相关的用例必须跑在**独立进程**里：信号处理器只能装在主线程，
而 pytest 的主线程正被测试本身占着。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.core.config import Config
from devproxy.core.paths import Paths

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

# 假的 caddy：`validate` 立刻成功；`run` 的行为由 mode 决定。
_FAKE_CADDY = """#!__PYTHON__
import os
import signal
import sys
import time

cmd = sys.argv[1] if len(sys.argv) > 1 else ""
marker = os.environ.get("FAKE_CADDY_MARKER", "")
mode = os.environ.get("FAKE_CADDY_MODE", "wait")

if cmd == "validate":
    sys.exit(0)

if cmd == "run":
    if mode == "quit":
        # 立刻自己退出（模拟「caddy 起来了又退了」）
        sys.exit(0)

    def on_term(signum, frame):
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("term")
        if mode == "stubborn":
            # 记下「收到了 SIGTERM」，但**不退出** —— 逼出 SIGKILL
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            return
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    with open(marker + ".started", "w", encoding="utf-8") as fh:
        fh.write("started")
    while True:
        time.sleep(0.05)

sys.exit(0)
"""

# 在子进程里跑 gateway 层的 run_foreground（信号处理只能发生在主线程）。
_DRIVER = (
    "import sys;"
    "from pathlib import Path;"
    "from devproxy.gateway.foreground import run_foreground;"
    "sys.exit(run_foreground(Path(sys.argv[1]), Path(sys.argv[2]),"
    " float(sys.argv[3]) if len(sys.argv) > 3 else 10.0))"
)

# 在子进程里跑完整 CLI（平台换成假的，免得依赖宿主机上服务在不在跑）。
_CLI_DRIVER = (
    "import devproxy.cli._shared as s;"
    "from devproxy.platform.fake import FakePlatform;"
    "s._platform = lambda: FakePlatform();"
    "from devproxy.cli import main;"
    "main()"
)


def _flat(text: str) -> str:
    """去掉换行与空格。

    Rich 会在 80 列处折行，断言里的短语可能被折断 —— 折断与否不是被测
    行为，断言必须对此免疫。
    """
    return "".join(text.split())


def _write_fake_caddy(tmp_path: Path, marker: Path, mode: str = "wait") -> Path:
    """写一个假的 caddy 可执行文件。

    mode：wait（等 SIGTERM）/ quit（run 之后立刻自己退出）/
    stubborn（无视 SIGTERM，逼出 SIGKILL）。
    """
    path = tmp_path / "fake-caddy"
    path.write_text(_FAKE_CADDY.replace("__PYTHON__", sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


def _child_env(marker: Path, mode: str = "wait", **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC_DIR)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["FAKE_CADDY_MARKER"] = str(marker)
    env["FAKE_CADDY_MODE"] = mode
    env.update(extra)
    return env


def _wait_for(path: Path, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------ CLI 层的行为


class _FakeCaddyConfig:
    """假的 CaddyConfig：validate 的结果可编排，并记录校验的是哪个文件。"""

    def __init__(self, ok: bool = True, detail: str = ""):
        self.ok = ok
        self.detail = detail
        self.validated: list[Path | None] = []

    def validate(self, caddyfile=None, timeout=30.0):
        self.validated.append(caddyfile)
        return self.ok, self.detail


class _FakePlatform:
    """只提供 run 命令用到的两个能力：平台名与服务是否在跑。"""

    def __init__(self, name: str, active: bool):
        self.name = name
        self.active = active

    def service_active(self, name: str) -> bool:
        return self.active


@pytest.fixture
def env(tmp_path, monkeypatch):
    """devproxy 的三棵目录 + 一个存在的假 caddy 二进制。"""
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    paths.caddyfile.write_text("# 主 Caddyfile\n", encoding="utf-8")
    binary = tmp_path / "caddy"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    config = Config(caddy_bin=str(binary), storage=str(paths.data_dir))
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (paths, config, None, None),
    )
    return paths, config, binary


@pytest.fixture
def validator(monkeypatch):
    fake = _FakeCaddyConfig()
    monkeypatch.setattr("devproxy.cli._shared.CaddyConfig", lambda **kw: fake)
    return fake


@pytest.fixture
def foreground(monkeypatch):
    """挡住真的起进程：只记录调用。"""
    calls: list[tuple[Path, Path]] = []

    def fake_run(caddy_bin, caddyfile):
        calls.append((Path(caddy_bin), Path(caddyfile)))
        return 0

    monkeypatch.setattr("devproxy.gateway.foreground.run_foreground", fake_run)
    return calls


def _invoke(args: list[str]):
    return CliRunner().invoke(app, args)


def test_run_without_caddy_binary_exits_3(env, validator, foreground):
    """没装 caddy → 前置条件不满足（3），并指向 devproxy setup。"""
    paths, config, binary = env
    binary.unlink()
    res = _invoke(["run"])
    assert res.exit_code == 3, res.output
    assert "请先运行devproxysetup" in _flat(res.output)
    assert not foreground, "前置条件不满足时不该启动 caddy"


def test_run_without_main_caddyfile_exits_3(env, validator, foreground):
    """还没 setup（主 Caddyfile 不存在）→ 退出码 3。"""
    paths, config, binary = env
    paths.caddyfile.unlink()
    res = _invoke(["run"])
    assert res.exit_code == 3, res.output
    assert "请先运行devproxysetup" in _flat(res.output)
    assert not foreground


def test_run_with_missing_explicit_config_exits_2(env, validator, foreground):
    """显式指定的文件不存在是用法错误（2），与 sync --file 一致。"""
    res = _invoke(["run", "--config", str(env[0].config_dir / "nope")])
    assert res.exit_code == 2, res.output
    assert not foreground


def test_run_with_explicit_config_ignores_missing_main_caddyfile(env, validator, foreground):
    """给了 --config 就不再要求主 Caddyfile 存在。"""
    paths, config, binary = env
    paths.caddyfile.unlink()
    other = paths.config_dir / "other.Caddyfile"
    other.write_text("# 别的 Caddyfile\n", encoding="utf-8")
    res = _invoke(["run", "--config", str(other)])
    assert res.exit_code == 0, res.output
    assert validator.validated == [other]
    assert foreground == [(binary, other)]


@pytest.mark.parametrize(
    "platform_name,stop_command",
    [
        ("linux", "systemctl --user stop caddy"),
        ("mac", "launchctl bootout system/com.devproxy.caddy"),
    ],
)
def test_run_with_active_service_exits_3_with_stop_command(
    env, validator, foreground, monkeypatch, platform_name, stop_command
):
    """现役服务在跑 → 80/443 已被占 → 退出码 3，提示里给出该平台的停服务命令。"""
    monkeypatch.setattr(
        "devproxy.cli._shared._platform",
        lambda: _FakePlatform(platform_name, True),
    )
    res = _invoke(["run"])
    assert res.exit_code == 3, res.output
    assert _flat(stop_command) in _flat(res.output)
    assert not foreground, "端口已被占时不该还去起 caddy"


def test_run_with_active_service_on_unknown_platform_exits_3(
    env, validator, foreground, monkeypatch
):
    """认不出的平台也给得出结论（只是没有现成的命令可抄）。"""
    monkeypatch.setattr(
        "devproxy.cli._shared._platform",
        lambda: _FakePlatform("fake", True),
    )
    res = _invoke(["run"])
    assert res.exit_code == 3, res.output
    assert "caddy服务仍在运行" in _flat(res.output)


def test_run_with_invalid_config_exits_1_and_shows_detail(env, validator, foreground, monkeypatch):
    """配置校验不过 → 打印 Caddy 给的说明，退出码 1。"""
    validator.ok = False
    validator.detail = "Caddyfile:3 - unrecognized directive 'bogus'"
    res = _invoke(["run"])
    assert res.exit_code == 1, res.output
    assert "unrecognizeddirective" in _flat(res.output)
    assert not foreground, "校验不过就不该启动 caddy"


def test_run_validates_and_launches_the_main_caddyfile(env, validator, foreground):
    """默认校验并加载主 Caddyfile。"""
    paths, config, binary = env
    res = _invoke(["run"])
    assert res.exit_code == 0, res.output
    assert validator.validated == [paths.caddyfile]
    assert foreground == [(binary, paths.caddyfile)]


def test_run_propagates_foreground_exit_code(env, validator, monkeypatch):
    """Caddy 自己退出 → devproxy 退出码 1。"""
    monkeypatch.setattr("devproxy.gateway.foreground.run_foreground", lambda *a, **k: 1)
    res = _invoke(["run"])
    assert res.exit_code == 1, res.output


def test_run_does_not_write_any_state(env, validator, foreground):
    """前台模式 = 不留痕迹：不写路由表、不装服务、不改配置。"""
    paths, config, binary = env
    before = sorted(p.name for p in paths.config_dir.iterdir())
    res = _invoke(["run"])
    assert res.exit_code == 0, res.output
    assert not (paths.state_dir / "routes.json").exists()
    assert sorted(p.name for p in paths.config_dir.iterdir()) == before


# ------------------------------------------------------- gateway 层的行为


def test_run_foreground_passes_run_and_config_to_caddy(tmp_path):
    """命令行必须是 `caddy run --config <路径>`（与服务单元一致）。"""
    record = tmp_path / "argv"
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("# 空\n", encoding="utf-8")

    # 记录 argv 的假 caddy：validate 成功，run 立刻退出
    recorder = tmp_path / "recorder"
    recorder.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({str(record)!r}, 'w').write('\\0'.join(sys.argv))\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)

    from devproxy.gateway.foreground import run_foreground

    rc = run_foreground(recorder, caddyfile)
    assert rc == 1, "caddy 自己退出 → 退出码 1"
    argv = record.read_text(encoding="utf-8").split("\0")
    assert argv[1:] == ["run", "--config", str(caddyfile)]


def test_run_foreground_reports_caddy_exit_code(tmp_path, capsys, monkeypatch):
    """Caddy 自己退出 → 报告它的退出码，devproxy 退出码 1。"""
    monkeypatch.setenv("FAKE_CADDY_MODE", "quit")
    monkeypatch.setenv("FAKE_CADDY_MARKER", str(tmp_path / "marker"))
    marker = tmp_path / "marker"
    caddy = _write_fake_caddy(tmp_path, marker)
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("# 空\n", encoding="utf-8")

    from devproxy.gateway.foreground import run_foreground

    # 假的 caddy 在 run 之后立刻退出 0 —— 仍然算「自己退出」→ 1
    rc = run_foreground(caddy, caddyfile)
    assert rc == 1
    assert "caddy已退出" in _flat(capsys.readouterr().err)


def test_run_foreground_returns_1_when_binary_missing(tmp_path, capsys):
    """二进制起不来 → 干净报错 + 退出码 1，不是 traceback。"""
    from devproxy.gateway.foreground import run_foreground

    rc = run_foreground(tmp_path / "no-such-caddy", tmp_path / "Caddyfile")
    assert rc == 1
    assert "无法启动caddy" in _flat(capsys.readouterr().err)


def test_run_foreground_stops_caddy_cleanly_on_sigint(tmp_path):
    """Ctrl-C → 给 Caddy 进程组发 SIGTERM → 它优雅退出 → devproxy 退出码 0。"""
    marker = tmp_path / "marker"
    caddy = _write_fake_caddy(tmp_path, marker)
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("# 空\n", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, str(caddy), str(caddyfile), "10"],
        env=_child_env(marker),
    )
    try:
        assert _wait_for(tmp_path / "marker.started"), "caddy 没起来"
        proc.send_signal(signal.SIGINT)
        rc = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:  # pragma: no cover —— 失败时的兜底
            proc.kill()
            proc.wait(timeout=10)
    assert rc == 0, f"devproxy 退出码 {rc}"
    assert marker.read_text(encoding="utf-8") == "term", "caddy 没收到 SIGTERM"


def test_run_foreground_escalates_to_sigkill_after_graceful_timeout(tmp_path):
    """Caddy 不理 SIGTERM → 优雅退出超时后升级 SIGKILL（不留残进程）。"""
    marker = tmp_path / "marker"
    caddy = _write_fake_caddy(tmp_path, marker, mode="stubborn")
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("# 空\n", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, str(caddy), str(caddyfile), "1"],
        env=_child_env(marker, mode="stubborn"),
    )
    try:
        assert _wait_for(tmp_path / "marker.started"), "caddy 没起来"
        proc.send_signal(signal.SIGINT)
        started = time.monotonic()
        rc = proc.wait(timeout=30)
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:  # pragma: no cover —— 失败时的兜底
            proc.kill()
            proc.wait(timeout=10)
    assert rc == 0, f"devproxy 退出码 {rc}"
    assert marker.read_text(encoding="utf-8") == "term", "caddy 没收到 SIGTERM"
    assert elapsed >= 1.0, "不该在优雅退出超时之前就杀掉 caddy"


# ------------------------------------------------------------ CLI 端到端


def test_cli_run_starts_caddy_in_foreground_and_stops_on_sigint(tmp_path):
    """端到端：`devproxy run` 真的把 caddy 拉起来，Ctrl-C 干净收掉。"""
    marker = tmp_path / "marker"
    caddy = _write_fake_caddy(tmp_path, marker)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "Caddyfile").write_text("# 主 Caddyfile\n", encoding="utf-8")
    (config_dir / "config.toml").write_text(f'caddy_bin = "{caddy}"\n', encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-c", _CLI_DRIVER, "run", "--config", str(config_dir / "Caddyfile")],
        env=_child_env(
            marker,
            DEVPROXY_CONFIG_DIR=str(config_dir),
            DEVPROXY_STATE_DIR=str(tmp_path / "state"),
            DEVPROXY_DATA_DIR=str(tmp_path / "data"),
        ),
    )
    try:
        assert _wait_for(tmp_path / "marker.started"), "caddy 没起来"
        proc.send_signal(signal.SIGINT)
        rc = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:  # pragma: no cover —— 失败时的兜底
            proc.kill()
            proc.wait(timeout=10)
    assert rc == 0, f"devproxy 退出码 {rc}"
    assert marker.read_text(encoding="utf-8") == "term", "caddy 没收到 SIGTERM"
    # 前台模式不留痕迹
    assert not (tmp_path / "state" / "routes.json").exists()


def test_run_with_other_config_ignores_active_service(env, validator, foreground, monkeypatch):
    """`--config` 指向**别的** Caddyfile 时不该被"服务在跑"拦住。

    服务加载的是主 Caddyfile；另一份配置通常用另一组端口（调试、隔离实例
    就是为这个），此时端口并不冲突。真撞上了，Caddy 自己会报
    "address already in use" —— 那比这里的推测准确。
    """
    monkeypatch.setattr(
        "devproxy.cli._shared._platform",
        lambda: _FakePlatform("linux", True),
    )
    other = env[0].config_dir / "other.Caddyfile"
    other.write_text("# 另一份配置\n", encoding="utf-8")
    res = _invoke(["run", "--config", str(other)])
    assert res.exit_code == 0, res.output
    assert foreground == [(env[2], other)], "应该照常起前台实例"
