"""devproxy 命令行入口。

命令按模块拆分（一命令一文件）。接缝（平台、组件装配、交互判定等）
统一放在 `_shared`，命令模块通过 `devproxy.cli._shared` 在**调用时**查找，
所以测试对 `devproxy.cli._shared.<名字>` 打桩一定能生效。
"""

from __future__ import annotations

import sys

import typer
from rich.markup import escape

from devproxy.cli._shared import EXIT_FAIL, DevProxyGroup, err_console
from devproxy.core.caddy_env import CaddyEnvArchiveError
from devproxy.core.config import ConfigError
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.store import RouteStoreError

app = typer.Typer(
    cls=DevProxyGroup,
    help="本地开发环境一键部署与域名网关",
    no_args_is_help=True,
)

# 导入即注册：每个模块用 @app.command() 把命令挂到 app 上。
from devproxy.cli import (  # noqa: E402,F401
    up,
    expose,
    down,
    ls,
    config,
    selftest,
    setup,
    doctor,
    trust,
    logs,
    uninstall,
    sync,
    run,
    init,
)


def main() -> None:
    try:
        app()
    except (CaddyEnvArchiveError, ConfigError, RouteStoreError, RouteTableError) as e:
        # 兜底：领域异常绝不能以 traceback 形态漏到终端
        err_console.print(f"devproxy: {escape(str(e))}")
        sys.exit(EXIT_FAIL)
