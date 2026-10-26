"""Shell completion: the value candidates for `logs <name>` / `down <name>`.

Command names and options are completed by Typer/Click itself (`--install-completion`);
here we only test the "argument values" we add ourselves: registered route names plus
service log names. If the route table **cannot be read, treat it as empty** and never
error out -- bash treats every line of stdout as a completion item.

Driven through `typer.completion.shell_complete` (the public entry point in both
typer 0.24 and 0.27 with the same signature; from 0.27 on, click was folded into
typer._click and is no longer a separate dependency, so **do not** use
`from click.shell_completion import ShellComplete`).
"""

from __future__ import annotations

import json

import pytest

from devproxy.cli import app
from devproxy.core.paths import default_paths


def _complete_via_shell(monkeypatch, capsys, words: list[str]) -> list[str]:
    """Drive one completion through the same path as bash pressing <TAB> and return the candidates.

    words is the full word list including prog_name; the last word is the "half-typed
    word": _complete_via_shell(..., ["devproxy", "logs", "wr"]) is roughly
    `devproxy logs wr<TAB>`. Candidates are read from stdout -- exactly what bash receives.
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
    assert "caddy" in out and "dnsmasq" in out, "service log names must be completable"
    assert "wrs-api" in out and "codeup" in out, "route names must be completable"


def test_down_completes_route_names_only(monkeypatch, capsys):
    _seed_routes(["wrs-api", "codeup"])
    out = _complete_via_shell(monkeypatch, capsys, ["devproxy", "down", ""])
    assert "wrs-api" in out and "codeup" in out
    assert "caddy" not in out, "down's argument is not a service name"


def test_completion_filters_by_prefix(monkeypatch, capsys):
    _seed_routes(["wrs-api", "wrs-web", "codeup"])
    out = _complete_via_shell(monkeypatch, capsys, ["devproxy", "logs", "wr"])
    assert out == ["wrs-api", "wrs-web"], "filter by the typed prefix"


@pytest.mark.parametrize("content", [None, "{broken", '{"routes": [1]}'])
def test_completion_with_unreadable_route_table_is_empty_and_quiet(monkeypatch, capsys, content):
    """Not set up / corrupt route table: completion quietly returns no candidates."""
    if content is not None:
        paths = default_paths()
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        paths.routes_json.write_text(content, encoding="utf-8")
    out = _complete_via_shell(monkeypatch, capsys, ["devproxy", "down", ""])
    assert out == []
