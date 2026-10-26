"""Typer parameter-splitting group: hand everything after `--` to the subcommand."""

from __future__ import annotations

from typer.core import TyperGroup


class DevProxyGroup(TyperGroup):
    """Split off the arguments after '--' and pass them to the subcommand verbatim.

    By default Typer parses every argument of the subcommand with its own parser,
    so options like the subcommand's --port would be stolen by devproxy. Here we
    split before parsing: the tail goes into ctx.obj as "trailing" and the
    subcommand reads it itself.
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
