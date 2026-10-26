"""shell 补全：`logs <名字>` / `down <名字>` 的值域候选。

命令名与选项的补全由 Typer/Click 自带（`--install-completion`），这里只测
我们自己补的「参数值」：已注册的路由名 + 服务日志名。候选来源必须
**读不到路由表就当没有**，绝不报错 —— bash 会把 stdout 每行都当补全项。
"""

from __future__ import annotations

import json

import pytest
from click.shell_completion import ShellComplete
import typer

from devproxy.cli import app
from devproxy.core.paths import default_paths


def _completions(args: list[str], incomplete: str = "") -> list[str]:
    """直接驱动 Click 的补全机器（等价于 bash 里按 <TAB>）。"""
    click_cmd = typer.main.get_command(app)
    complete = ShellComplete(click_cmd, {}, "devproxy", "_DEVPROXY_COMPLETE")
    return [c.value for c in complete.get_completions(args, incomplete)]


def _seed_routes(names: list[str]) -> None:
    paths = default_paths()
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.routes_json.write_text(
        json.dumps(
            {
                "version": 1,
                "domain_suffix": "test",
                "routes": [
                    {
                        "name": name,
                        "domain": f"{name}.test",
                        "port": 42100 + i,
                        "mode": "up",
                        "upstream": f"127.0.0.1:{42100 + i}",
                    }
                    for i, name in enumerate(names)
                ],
            }
        ),
        encoding="utf-8",
    )


def test_logs_completes_services_and_route_names():
    _seed_routes(["wrs-api", "codeup"])
    values = _completions(["logs"])
    assert "caddy" in values and "dnsmasq" in values, "服务日志名要能补"
    assert "wrs-api" in values and "codeup" in values, "路由名要能补"


def test_down_completes_route_names_only():
    _seed_routes(["wrs-api", "codeup"])
    values = _completions(["down"])
    assert "wrs-api" in values and "codeup" in values
    assert "caddy" not in values, "down 的参数不是服务名"


def test_completion_filters_by_prefix():
    _seed_routes(["wrs-api", "wrs-web", "codeup"])
    values = _completions(["logs"], "wr")
    assert values == ["wrs-api", "wrs-web"], "按已敲的前缀过滤"


@pytest.mark.parametrize("content", [None, "{broken", '{"routes": [1]}'])
def test_completion_with_unreadable_route_table_is_empty_and_quiet(content):
    """没 setup / 路由表损坏：补全安静地给出空候选，不报错不打印垃圾。"""
    if content is not None:
        paths = default_paths()
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        paths.routes_json.write_text(content, encoding="utf-8")
    assert _completions(["down"]) == []
