"""Typer 参数切分组：把 `--` 之后的参数原样交给子命令。"""

from __future__ import annotations

from typer.core import TyperGroup


class DevProxyGroup(TyperGroup):
    """把 '--' 之后的参数原样切给子命令。

    Typer 默认会用自己的解析器处理子命令的所有参数，导致子命令的
    --port 之类被 devproxy 抢走。这里在解析前先切分，尾部参数以
    "trailing" 原样放进 ctx.obj，子命令自行取用。
    """

    def parse_args(self, ctx, args):
        has_sep = "--" in args
        if has_sep:
            i = args.index("--")
            head, tail = args[:i], args[i + 1 :]
        else:
            head, tail = args, []
        data = ctx.ensure_object(dict)
        data["trailing"] = tail
        data["has_sep"] = has_sep
        return super().parse_args(ctx, head)
