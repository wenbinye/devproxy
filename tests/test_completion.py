"""shell 补全：`logs <名字>` / `down <名字>` 的值域候选。

命令名与选项的补全由 Typer/Click 自带（`--install-completion`），这里只测
我们自己补的「参数值」：已注册的路由名 + 服务日志名。候选来源必须
**读不到路由表就当没有**，绝不报错 —— bash 会把 stdout 每行都当补全项。

驱动方式：`typer.completion.shell_complete`（typer 0.24 与 0.27 的公开入口，
签名一致；0.27 起 click 被收编为 typer._click，不再是独立依赖，
所以**不能** `from click.shell_completion import ShellComplete`）。
"""

from __future__ import annotations

import json

import pytest

from devproxy.cli import app
from devproxy.core.paths import default_paths


def _complete_via_shell(monkeypatch, capsys, words: list[str]) -> list[str]:
    """按 bash 按 <TAB> 的同一条路径驱动一次补全，返回候选。

    words 是含 prog_name 的完整词表，最后一个词是「正在敲的半个词」：
    _complete_via_shell(..., ["devproxy", "logs", "wr"]) ≈ `devproxy logs wr<TAB>`。
    候选读 stdout —— 那正是 bash 收到的内容。
    """
    monkeypatch.setenv("COMP_WORDS", " ".join(words))
    monkeypatch.setenv("COMP_CWORD", str(len(words) - 1))
    monkeypatch.setenv("_DEVPROXY_COMPLETE", "complete_bash")

    import typer.main
    from typer.completion import shell_complete

    shell_complete(
        typer.main.get_command(app),
        {},
        "devproxy",
        "_DEVPROXY_COMPLETE",
        "complete_bash",
    )
    return capsys.readouterr().out.split()


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


def test_logs_completes_services_and_route_names(monkeypatch, capsys):
    _seed_routes(["wrs-api", "codeup"])
    out = _complete_via_shell(monkeypatch, capsys, ["devproxy", "logs", ""])
    assert "caddy" in out and "dnsmasq" in out, "服务日志名要能补"
    assert "wrs-api" in out and "codeup" in out, "路由名要能补"


def test_down_completes_route_names_only(monkeypatch, capsys):
    _seed_routes(["wrs-api", "codeup"])
    out = _complete_via_shell(monkeypatch, capsys, ["devproxy", "down", ""])
    assert "wrs-api" in out and "codeup" in out
    assert "caddy" not in out, "down 的参数不是服务名"


def test_completion_filters_by_prefix(monkeypatch, capsys):
    _seed_routes(["wrs-api", "wrs-web", "codeup"])
    out = _complete_via_shell(monkeypatch, capsys, ["devproxy", "logs", "wr"])
    assert out == ["wrs-api", "wrs-web"], "按已敲的前缀过滤"


@pytest.mark.parametrize("content", [None, "{broken", '{"routes": [1]}'])
def test_completion_with_unreadable_route_table_is_empty_and_quiet(
    monkeypatch, capsys, content
):
    """没 setup / 路由表损坏：补全安静地给出空候选，不报错不打印垃圾。"""
    if content is not None:
        paths = default_paths()
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        paths.routes_json.write_text(content, encoding="utf-8")
    out = _complete_via_shell(monkeypatch, capsys, ["devproxy", "down", ""])
    assert out == []
