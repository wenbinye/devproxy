"""devproxy init: generate devproxy.toml.

Only tests "what the file is written as" -- the path that actually starts services is
covered by tests/test_sync.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.gateway.project import ProjectRoute
from devproxy.gateway.project import detect_project_root
from devproxy.gateway.project import load_project_file
from devproxy.gateway.project import render_project_file
from devproxy.gateway.project import route_name_from_dir
from devproxy.gateway.project import suggest_command


def _invoke(args: list[str]):
    return CliRunner().invoke(app, args)


# -- scaffolding logic --------------------------------------------------


def test_detect_project_root_finds_git_root(tmp_path):
    """The project file must live at the repository root -- alongside pyproject.toml."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert detect_project_root(nested) == root.resolve()


def test_detect_project_root_without_git_is_cwd(tmp_path):
    """Without git, use the current directory and do not search upwards."""
    assert detect_project_root(tmp_path) == tmp_path.resolve()


def test_route_name_from_dir_sanitizes():
    assert route_name_from_dir(Path("/x/My App")) == "my-app"
    assert route_name_from_dir(Path("/x/codeup_manager")) == "codeup-manager"
    assert route_name_from_dir(Path("/x/--weird--")) == "weird"


def test_route_name_from_dir_handles_empty():
    assert route_name_from_dir(Path("/")) == "app"


def test_suggest_command_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8"
    )
    argv, reason = suggest_command(tmp_path)
    # The port must be controllable: if the app does not listen on $PORT, devproxy's
    # readiness check can never pass
    assert argv == ["sh", "-c", "npm run dev -- --port $PORT"]
    assert "dev script" in reason


def test_suggest_command_falls_back_to_start(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"start": "node ."}}), encoding="utf-8"
    )
    argv, _ = suggest_command(tmp_path)
    assert argv == ["sh", "-c", "npm run start -- --port $PORT"]


def test_suggest_command_without_evidence_returns_none(tmp_path):
    """Do not guess without evidence -- a wrong guess is worse than none."""
    argv, reason = suggest_command(tmp_path)
    assert argv is None
    assert reason


def test_suggest_command_survives_broken_package_json(tmp_path):
    """A broken package.json must not raise -- it is the user's file, not ours."""
    (tmp_path / "package.json").write_text("{ not json", encoding="utf-8")
    argv, _ = suggest_command(tmp_path)
    assert argv is None


def test_render_project_file_escapes_quotes():
    content = render_project_file([ProjectRoute(name="web", command=["sh", "-c", 'echo "hi"'])])
    assert r'"echo \"hi\""' in content


def test_render_project_file_marks_expose():
    content = render_project_file([ProjectRoute(name="legacy", mode="expose", port=3000)])
    assert 'mode = "expose"' in content
    assert "port = 3000" in content
    # No command line (do not search the whole text for "command" -- the header comment contains that word)
    assert not any(line.strip().startswith("command") for line in content.splitlines())


# -- command layer ------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A git repository root + change into it."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_init_creates_file_in_project_root(project):
    result = _invoke(["init", "--name", "web", "--command", "npm run dev"])
    assert result.exit_code == 0, result.output
    target = project / "devproxy.toml"
    assert target.exists()
    project_file = load_project_file(target)
    assert len(project_file.routes) == 1
    assert project_file.routes[0].name == "web"
    assert project_file.routes[0].command == ["npm", "run", "dev"]


def test_init_guesses_command_from_project_type(project):
    (project / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8"
    )
    result = _invoke(["init", "--name", "web"])
    assert result.exit_code == 0, result.output
    project_file = load_project_file(project / "devproxy.toml")
    assert project_file.routes[0].command == ["sh", "-c", "npm run dev -- --port $PORT"]


def test_init_refuses_to_overwrite_without_force(project):
    (project / "devproxy.toml").write_text("# existing content\n", encoding="utf-8")
    result = _invoke(["init", "--name", "web", "--command", "npm run dev"])
    assert result.exit_code == 2, result.output
    assert (project / "devproxy.toml").read_text(encoding="utf-8") == "# existing content\n"


def test_init_force_overwrites(project):
    (project / "devproxy.toml").write_text("# old content\n", encoding="utf-8")
    result = _invoke(["init", "--name", "web", "--command", "npm run dev", "--force"])
    assert result.exit_code == 0, result.output
    assert "old content" not in (project / "devproxy.toml").read_text(encoding="utf-8")


def test_init_rejects_illegal_route_name(project):
    result = _invoke(["init", "--name", "Bad_Name", "--command", "npm run dev"])
    assert result.exit_code == 2, result.output
    assert not (project / "devproxy.toml").exists()


def test_init_splits_command_with_quotes(project):
    result = _invoke(["init", "--name", "web", "--command", 'sh -c "echo hi there"'])
    assert result.exit_code == 0, result.output
    project_file = load_project_file(project / "devproxy.toml")
    assert project_file.routes[0].command == ["sh", "-c", "echo hi there"]


def test_init_imports_command_from_existing_route(project, monkeypatch, tmp_path):
    """--from: copy the start command of an already running route into the declaration."""
    paths = tmp_path / "state"
    paths.mkdir()
    (paths / "routes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "domain_suffix": "test",
                "routes": [
                    {
                        "name": "legacy-svc",
                        "domain": "legacy-svc.test",
                        "port": 1234,
                        "mode": "up",
                        "upstream": "127.0.0.1:1234",
                        "command": ["npm", "run", "serve"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    from devproxy.core.paths import Paths

    fake = Paths(tmp_path / "cfg", paths, tmp_path / "data")
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (fake, None, None, None),
    )

    result = _invoke(["init", "--name", "web", "--from", "legacy-svc"])
    assert result.exit_code == 0, result.output
    project_file = load_project_file(project / "devproxy.toml")
    assert project_file.routes[0].command == ["npm", "run", "serve"]


def test_init_reports_unknown_route_for_from(project, monkeypatch, tmp_path):
    paths = tmp_path / "state"
    paths.mkdir()
    (paths / "routes.json").write_text(
        json.dumps({"version": 1, "domain_suffix": "test", "routes": []}),
        encoding="utf-8",
    )

    from devproxy.core.paths import Paths

    fake = Paths(tmp_path / "cfg", paths, tmp_path / "data")
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (fake, None, None, None),
    )

    result = _invoke(["init", "--name", "web", "--from", "nope"])
    assert result.exit_code == 2, result.output
    assert "nope" in result.output
    assert not (project / "devproxy.toml").exists()


def test_init_generated_file_parses_back(project):
    """The generated file must be **parseable by ourselves** -- otherwise the user's first sync explodes."""
    result = _invoke(["init", "--name", "web", "--command", "npm run dev"])
    assert result.exit_code == 0, result.output
    project_file = load_project_file(project / "devproxy.toml")
    assert project_file.routes[0].name == "web"


# -- PHP projects -------------------------------------------------------


def test_suggest_command_laravel(tmp_path):
    """An artisan file -> Laravel's official start command, with a controllable port."""
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "artisan").write_text("#!/usr/bin/env php\n", encoding="utf-8")
    argv, reason = suggest_command(tmp_path)
    assert argv == ["sh", "-c", "php artisan serve --port=$PORT"]
    assert "artisan" in reason


def test_suggest_command_php_public_docroot(tmp_path):
    """public/index.php -> the built-in server under public (Symfony etc.)."""
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "index.php").write_text("<?php\n", encoding="utf-8")
    argv, _ = suggest_command(tmp_path)
    assert argv == ["sh", "-c", "php -S 127.0.0.1:$PORT -t public"]


def test_suggest_command_php_root_index(tmp_path):
    """index.php at the root -> the built-in server at the project root."""
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "index.php").write_text("<?php\n", encoding="utf-8")
    argv, _ = suggest_command(tmp_path)
    assert argv == ["sh", "-c", "php -S 127.0.0.1:$PORT"]


def test_suggest_command_php_composer_script(tmp_path):
    """A serve script in composer.json -> offer it, but say plainly that the port is not controllable."""
    (tmp_path / "composer.json").write_text(
        json.dumps({"scripts": {"serve": "php src/index.php start"}}), encoding="utf-8"
    )
    argv, reason = suggest_command(tmp_path)
    assert argv == ["sh", "-c", "composer serve"]
    assert "the port is not controllable" in reason


def test_suggest_command_php_names_env_port_vars(tmp_path):
    """Port variables in .env must be named -- users need to know which one to change."""
    (tmp_path / "composer.json").write_text(
        json.dumps({"scripts": {"serve": "x"}}), encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "APP_WEB_PORT=18130\nAPP_JSONRPC_PORT=10130\nDB_PORT=3306\n", encoding="utf-8"
    )
    _, reason = suggest_command(tmp_path)
    assert "APP_WEB_PORT" in reason
    assert "DB_PORT" not in reason, (
        "database ports have nothing to do with devproxy; do not add noise"
    )


def test_suggest_command_php_unknown_does_not_guess(tmp_path):
    """An unrecognized PHP project is not guessed -- only a hint is given."""
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    argv, reason = suggest_command(tmp_path)
    assert argv is None
    assert "composer.json" in reason


def test_suggest_command_makefile_requires_real_target(tmp_path):
    """Only guess when the Makefile really has a dev target (it used to check only file existence -> wrong guesses)."""
    (tmp_path / "Makefile").write_text("build:\n\techo hi\n", encoding="utf-8")
    argv, reason = suggest_command(tmp_path)
    assert argv is None
    assert "Makefile" in reason

    (tmp_path / "Makefile").write_text("dev:\n\techo hi\n", encoding="utf-8")
    argv, _ = suggest_command(tmp_path)
    assert argv == ["make", "dev"]


def test_suggest_command_php_wins_over_makefile(tmp_path):
    """Both composer.json and a Makefile -- treat it as PHP."""
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "Makefile").write_text("dev:\n\techo hi\n", encoding="utf-8")
    argv, reason = suggest_command(tmp_path)
    assert argv is None
    assert "PHP" in reason
