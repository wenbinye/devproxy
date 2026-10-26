from __future__ import annotations

from pathlib import Path

import typer

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_PRECONDITION
from devproxy.cli._shared import EXIT_USAGE
from devproxy.core.config import READY_TIMEOUT_MAX
from devproxy.core.config import READY_TIMEOUT_MIN
from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import validate_name
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.runner import PreconditionError
from devproxy.gateway.runner import UpError
from devproxy.gateway.store import RouteStoreError


def _validate_cli_args(name: str, port: int | None, ready_timeout: int | None) -> None:
    """参数错误按全局约定退出 2，而不是运行失败 1。

    UpRunner 也会校验 name/port，但那是网关层的最后一道闸；CLI 先校验
    才能把“命令写错了”与“环境跑挂了”分开（CI/脚本靠退出码分流）。
    """
    try:
        validate_name(name)
    except InvalidNameError as e:
        s.fail(str(e), EXIT_USAGE)
    if port is not None and not (1 <= port <= 65535):
        s.fail(f"非法端口 {port}：必须在 1..65535 之间", EXIT_USAGE)
    if ready_timeout is not None and not (READY_TIMEOUT_MIN <= ready_timeout <= READY_TIMEOUT_MAX):
        s.fail(
            f"非法就绪超时 {ready_timeout}：必须在 {READY_TIMEOUT_MIN}..{READY_TIMEOUT_MAX} 秒之间",
            EXIT_USAGE,
        )


@app.command(context_settings={"ignore_unknown_options": True})
def up(
    ctx: typer.Context,
    name: str = typer.Argument(
        None,
        help="路由名（\\[a-z0-9-]，单标签）。"
        "省略 = 应用本项目 devproxy.toml 的全部声明（compose 的 up -d）",
    ),
    port: int = typer.Option(None, "--port", help="显式端口（默认自动分配）"),
    ready_timeout: int = typer.Option(None, "--ready-timeout", help="就绪超时秒数"),
    file: Path = typer.Option(
        None, "--file", help="[项目模式] 显式指定 devproxy.toml（默认向上查找）"
    ),
    prune: bool = typer.Option(
        False, "--prune", help="[项目模式] 移除本文件里已删掉的路由（非交互时须加 --yes）"
    ),
    yes: bool = typer.Option(False, "--yes", help="[项目模式] 非交互模式，不询问"),
) -> None:
    """分配端口、启动命令并注册 https://<name>.test。"""
    data = ctx.ensure_object(dict)
    if name is None:
        # 项目模式（compose 的 up -d）：应用 devproxy.toml 的全部声明
        if not data.get("has_sep"):
            from devproxy.cli.sync import run_project_sync

            run_project_sync(file=file, prune=prune, yes=yes)
            raise typer.Exit(EXIT_OK)
        s.fail(
            "缺少路由名：起单个应用用 devproxy up <name> -- CMD...；"
            "起本项目的全部应用直接 devproxy up",
            EXIT_USAGE,
        )
    if not data.get("has_sep"):
        s.fail("缺少 '--'：devproxy up <name> [选项] -- CMD...", EXIT_USAGE)
    cmd = list(data.get("trailing") or [])
    if not cmd:
        s.fail("缺少启动命令：-- 之后需要 CMD...", EXIT_USAGE)
    _validate_cli_args(name, port, ready_timeout)

    paths, config, store, admin = s.build_context()
    s._require_caddyfile(paths)
    runner = s.UpRunner(store, config, admin)

    def on_ready(domain: str, allocated: int) -> None:
        s.console.print(f"✅ https://{domain}  (端口 {allocated})")
        s._print_trust_hint_if_needed(paths, config)

    try:
        rc = runner.run(
            name,
            cmd,
            port=port,
            ready_timeout=ready_timeout,
            on_ready=on_ready,
        )
    except PreconditionError as e:
        # Caddy 未运行是前置条件不满足，不是运行失败 —— 退出码 3（§9.1）
        s.fail(str(e), EXIT_PRECONDITION)
    except (UpError, RouteStoreError, RouteTableError) as e:
        # 参数已在上面校验过；这里的 UpError 是子进程/就绪超时等运行失败
        s.fail(str(e))
    raise typer.Exit(rc)
