"""devproxy run —— 在前台托管 Caddy。

命令本体只做三件事：前置检查、启动前校验、把退出码交给 typer。
起进程与信号处理在 `gateway.foreground`（selftest 已经在那里起过 Caddy，
放 gateway 是既有先例）。

与 `setup` 互斥：setup 装的后台服务占着 80/443，前台实例起不来 ——
那是前置条件不满足（退出码 3），提示里给出本平台的停服务命令。
"""

from __future__ import annotations

from pathlib import Path

import typer

from devproxy.cli import app
from devproxy.cli._shared import EXIT_FAIL
from devproxy.cli._shared import EXIT_PRECONDITION
from devproxy.cli._shared import EXIT_USAGE
from devproxy.core.config import Config
from devproxy.core.paths import Paths

from devproxy.cli import _shared as s

# 各平台停掉现役 caddy 服务的命令 —— 提示里必须给一条能直接执行的。
# 键是 platform.name（linux.py 的 "linux" / macos.py 的 "mac"）；
# 其余平台（fake 只出现在测试里）落到通用提示。
_STOP_COMMAND = {
    "linux": "systemctl --user stop caddy",
    "mac": "launchctl bootout system/com.devproxy.caddy",
}
_GENERIC_STOP_HINT = "请先停掉正在运行的 caddy 服务"


def _resolve_caddyfile(paths: Paths, explicit: Path) -> Path:
    """决定要加载哪个 Caddyfile。

    显式指定了 --config 就用它（主 Caddyfile 在不在都无所谓）；没指定才用
    主 Caddyfile —— 它必须已由 setup 生成，缺了属于前置条件不满足（3）。
    显式指定的路径不存在则是用法错误（2），与 sync --file 的处理一致。
    """
    if explicit is None:
        if not paths.caddyfile.exists():
            s.fail(
                f"主 Caddyfile 不存在: {paths.caddyfile}；请先运行 devproxy setup",
                EXIT_PRECONDITION,
            )
        return paths.caddyfile
    if not explicit.exists():
        s.fail(f"--config 指定的文件不存在: {explicit}", EXIT_USAGE)
    return explicit


def _require_caddy_binary(config: Config) -> None:
    binary = config.caddy_bin_path
    if not binary.exists():
        s.fail(
            f"caddy 二进制不存在: {binary}；请先运行 devproxy setup",
            EXIT_PRECONDITION,
        )


def _require_no_active_service(paths: Paths, caddyfile: Path) -> None:
    """现役 caddy 服务在跑 → 80/443 已被占，前台实例必然绑不上端口。

    **只针对主 Caddyfile**：那份配置正是服务单元加载的，两边抢同一批端口。
    `--config` 指向别的文件时通常用的是另一组端口（调试、隔离实例就是为这个），
    此时不该拦 —— 真撞上端口冲突，让 Caddy 自己报 "address already in use"，
    那条信息比这里的推测准确。
    """
    if caddyfile != paths.caddyfile:
        return
    platform = s._platform()
    if not platform.service_active("caddy"):
        return
    stop = _STOP_COMMAND.get(getattr(platform, "name", ""))
    if stop:
        # 命令放在句首：Rich 在 80 列处折行，命令被折断就没法直接复制了
        s.fail(
            f"请先执行 {stop}（caddy 服务仍在运行，80/443 已被占用）",
            EXIT_PRECONDITION,
        )
    s.fail(
        f"caddy 服务仍在运行，80/443 已被占用：{_GENERIC_STOP_HINT}",
        EXIT_PRECONDITION,
    )


@app.command()
def run(
    config_path: Path = typer.Option(
        None, "--config", help="要加载的 Caddyfile（默认主 Caddyfile）"
    ),
) -> None:
    """前台托管 Caddy（不装服务、不留状态，Ctrl-C 退出）。"""
    paths, config, _, _ = s.build_context()
    caddyfile = _resolve_caddyfile(paths, config_path)
    _require_caddy_binary(config)
    _require_no_active_service(paths, caddyfile)

    # 启动前先校验：配置坏了当场说清楚，而不是让 Caddy 起不来再猜
    caddy = s.CaddyConfig(
        caddy_bin=config.caddy_bin_path,
        caddyfile=caddyfile,
        admin_addr=config.admin_addr,
    )
    ok, detail = caddy.validate(caddyfile)
    if not ok:
        s.fail(f"Caddy 配置校验失败：{detail}", EXIT_FAIL)

    from devproxy.gateway.foreground import run_foreground

    # 状态提示走 stderr：stdout 是 Caddy 日志的直通道，别混进去
    s.err_console.print(f"devproxy: 前台运行 caddy（配置 {caddyfile}），Ctrl-C 退出")
    raise typer.Exit(run_foreground(config.caddy_bin_path, caddyfile))
