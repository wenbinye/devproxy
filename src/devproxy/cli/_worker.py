"""后台 up 工作进程：`devproxy sync` 拉起应用时用它托管子进程。

为什么要有这个进程：`devproxy up` 是**前台**命令 —— 它一直陪着应用跑，
应用退出时顺手注销路由、Caddy 丢路由时心跳把它补回来。sync 要一次拉起
好几条路由还必须自己返回，不能阻塞在前台命令上，于是把同一套「陪伴」
逻辑搬到这个独立进程里：

- sync 启动它（独立会话），它启动应用、注册路由，然后一直陪着；
- 应用退出 → 路由自动注销（与前台 `devproxy up` 同语义）；
- sync 退出不影响它 —— 它在自己的会话里，继续陪着应用。

工作负载从环境变量 `DEVPROXY_UP_PAYLOAD` 读（JSON），命令行参数留给
`python -m` 自身，避免应用命令里的参数与 devproxy 的参数混在一起。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer

from devproxy.cli._context import _require_caddyfile
from devproxy.cli._context import build_context
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.runner import PreconditionError
from devproxy.gateway.runner import UpError
from devproxy.gateway.runner import UpRequest
from devproxy.gateway.runner import UpRunner
from devproxy.gateway.store import RouteStoreError

PAYLOAD_ENV = "DEVPROXY_UP_PAYLOAD"


def _load_payload() -> dict:
    raw = os.environ.get(PAYLOAD_ENV)
    if not raw:
        raise ValueError(f"缺少环境变量 {PAYLOAD_ENV}")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("工作负载必须是 JSON 对象")
    return payload


def main(argv: list[str] | None = None) -> int:
    """跑一次 up 并陪着应用。返回进程退出码。"""
    try:
        payload = _load_payload()
    except (ValueError, json.JSONDecodeError) as e:
        print(f"devproxy: {e}", file=sys.stderr)
        return 2
    try:
        name = str(payload["name"])
        command = [str(part) for part in payload["command"]]
    except (KeyError, TypeError) as e:
        print(f"devproxy: 工作负载缺少字段：{e}", file=sys.stderr)
        return 2

    cwd = payload.get("cwd")
    request = UpRequest(
        name=name,
        cmd=command,
        port=payload.get("port"),
        ready_timeout=payload.get("ready_timeout"),
        cwd=Path(cwd) if cwd else None,
        source=payload.get("source"),
    )
    try:
        paths, config, store, admin = build_context()
        _require_caddyfile(paths)
        runner = UpRunner(store, config, admin)
        return runner.run_request(request)
    except typer.Exit as e:
        # 前置条件不满足（Caddyfile 不存在等）—— fail() 抛的就是它
        return int(e.exit_code)
    except PreconditionError as e:
        print(f"devproxy: {e}", file=sys.stderr)
        return 3
    except (UpError, RouteStoreError, RouteTableError) as e:
        print(f"devproxy: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - 进程入口
    sys.exit(main())
