"""CLI 运行时：Rich 控制台、退出码与统一错误出口。

这些是**所有**命令模块共享的进程级设施，与配置、组件、渲染无关。
"""

from __future__ import annotations

import sys
from typing import NoReturn

import typer
from rich.console import Console
from rich.markup import escape

console = Console()

err_console = Console(stderr=True)

# 退出码语义（全项目一致）：
#   0 成功 / 1 运行失败 / 2 参数错误 / 3 前置条件不满足 / 4 需要人工交互或提权
EXIT_OK = 0

EXIT_FAIL = 1

EXIT_USAGE = 2

EXIT_PRECONDITION = 3

EXIT_SUDO = 4

EXIT_DECLINED = 4


def fail(msg: str, code: int = EXIT_FAIL) -> NoReturn:
    """以统一形态输出错误并退出（Rich 渲染的一行错误，而非 traceback）。

    标成 NoReturn 而不是 None：它永远抛 typer.Exit，类型检查器据此
    知道"这行之后不会继续" —— 否则每个 `s.fail(...)` 之后的代码都会被
    误报成"可能未绑定变量"。
    """
    # escape：错误消息里可能带 [a-z0-9-] 这类字符集或用户输入，
    # 不转义会被 Rich 当成标记吞掉
    err_console.print(f"devproxy: {escape(msg)}")
    raise typer.Exit(code)


def _interactive() -> bool:
    """是否有可交互的终端。

    命令模块必须经 `_shared._interactive()` 在调用时查找 —— 测试对
    `_shared._interactive` 打桩才能覆盖所有分支。
    """
    return sys.stdin.isatty()
