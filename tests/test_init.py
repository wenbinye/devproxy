"""devproxy init：生成 devproxy.toml。

只测「文件被写成什么样」—— 真正起服务的路径由 tests/test_sync.py 覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.gateway.project import detect_project_root
from devproxy.gateway.project import load_project_file
from devproxy.gateway.project import render_project_file
from devproxy.gateway.project import route_name_from_dir
from devproxy.gateway.project import suggest_command
from devproxy.gateway.project import ProjectRoute


def _invoke(args: list[str]):
    return CliRunner().invoke(app, args)


# -- 脚手架逻辑 ---------------------------------------------------------


def test_detect_project_root_finds_git_root(tmp_path):
    """项目文件要放在仓库根 —— 跟 pyproject.toml 一起。"""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert detect_project_root(nested) == root.resolve()


def test_detect_project_root_without_git_is_cwd(tmp_path):
    """没有 git 就用当前目录，不往上乱找。"""
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
    assert argv == ["npm", "run", "dev"]
    assert "dev 脚本" in reason


def test_suggest_command_falls_back_to_start(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"start": "node ."}}), encoding="utf-8"
    )
    argv, _ = suggest_command(tmp_path)
    assert argv == ["npm", "run", "start"]


def test_suggest_command_without_evidence_returns_none(tmp_path):
    """没证据就不猜 —— 猜错比不猜更糟。"""
    argv, reason = suggest_command(tmp_path)
    assert argv is None
    assert reason


def test_suggest_command_survives_broken_package_json(tmp_path):
    """package.json 坏了也不能抛 —— 那是用户文件，不是我们的。"""
    (tmp_path / "package.json").write_text("{ not json", encoding="utf-8")
    argv, _ = suggest_command(tmp_path)
    assert argv is None


def test_render_project_file_escapes_quotes():
    content = render_project_file(
        [ProjectRoute(name="web", command=["sh", "-c", 'echo "hi"'])]
    )
    assert r'"echo \"hi\""' in content


def test_render_project_file_marks_expose():
    content = render_project_file(
        [ProjectRoute(name="legacy", mode="expose", port=3000)]
    )
    assert 'mode = "expose"' in content
    assert "port = 3000" in content
    # 没有 command 行（不是靠全文找 "command" —— 头部注释里就有这个词）
    assert not any(
        line.strip().startswith("command") for line in content.splitlines()
    )


# -- 命令层 -------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    """一个 git 仓库根 + 切到里面。"""
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
    assert project_file.routes[0].command == ["npm", "run", "dev"]


def test_init_refuses_to_overwrite_without_force(project):
    (project / "devproxy.toml").write_text("# 已有内容\n", encoding="utf-8")
    result = _invoke(["init", "--name", "web", "--command", "npm run dev"])
    assert result.exit_code == 2, result.output
    assert (project / "devproxy.toml").read_text(encoding="utf-8") == "# 已有内容\n"


def test_init_force_overwrites(project):
    (project / "devproxy.toml").write_text("# 旧内容\n", encoding="utf-8")
    result = _invoke(["init", "--name", "web", "--command", "npm run dev", "--force"])
    assert result.exit_code == 0, result.output
    assert "旧内容" not in (project / "devproxy.toml").read_text(encoding="utf-8")


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
    """--from：把已经在跑的路由的启动命令抄进声明。"""
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
    """生成的必须是**能被自己解析**的 —— 否则用户第一次 sync 就炸。"""
    result = _invoke(["init", "--name", "web", "--command", "npm run dev"])
    assert result.exit_code == 0, result.output
    project_file = load_project_file(project / "devproxy.toml")
    assert project_file.routes[0].name == "web"
