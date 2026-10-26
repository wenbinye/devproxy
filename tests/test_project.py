"""devproxy.toml：文件发现、解析与校验。"""

from pathlib import Path

import pytest

from devproxy.gateway.project import MODE_EXPOSE
from devproxy.gateway.project import MODE_UP
from devproxy.gateway.project import PROJECT_FILE_NAME
from devproxy.gateway.project import ProjectFileError
from devproxy.gateway.project import find_project_file
from devproxy.gateway.project import load_project_file
from devproxy.gateway.project import resolve_project_file

VALID = """
[[routes]]
name = "web"
command = ["npm", "run", "dev"]

[[routes]]
name = "legacy"
mode = "expose"
port = 3000
"""


def _write(path: Path, text: str = VALID) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _has_ancestor_file(start: Path) -> bool:
    for directory in (start, *start.parents):
        if (directory / PROJECT_FILE_NAME).is_file():
            return True
    return False


# ------------------------------- 文件发现


def test_find_project_file_in_current_directory(tmp_path):
    target = _write(tmp_path / PROJECT_FILE_NAME)
    assert find_project_file(tmp_path) == target


def test_find_project_file_walks_up_to_project_root(tmp_path):
    """用户在子目录里执行 sync，也必须找到项目根目录的那一份。"""
    target = _write(tmp_path / PROJECT_FILE_NAME)
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    assert find_project_file(nested) == target


def test_find_project_file_returns_none_when_missing(tmp_path):
    if _has_ancestor_file(tmp_path):
        pytest.skip("上层目录里存在 devproxy.toml，无法断言「找不到」")
    assert find_project_file(tmp_path) is None


def test_find_project_file_prefers_the_closest_one(tmp_path):
    outer = _write(tmp_path / PROJECT_FILE_NAME)
    inner_dir = tmp_path / "inner"
    inner_dir.mkdir()
    inner = _write(inner_dir / PROJECT_FILE_NAME)
    assert find_project_file(inner_dir) == inner
    assert outer != inner


def test_resolve_project_file_finds_file_upwards(tmp_path, monkeypatch):
    target = _write(tmp_path / PROJECT_FILE_NAME)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert resolve_project_file(None) == target


def test_resolve_project_file_uses_explicit_file(tmp_path, monkeypatch):
    """--file 显式指定时不做向上查找。"""
    _write(tmp_path / PROJECT_FILE_NAME)
    other = _write(tmp_path / "other.toml", VALID)
    monkeypatch.chdir(tmp_path)
    assert resolve_project_file(other) == other


def test_resolve_project_file_accepts_relative_explicit_path(tmp_path, monkeypatch):
    other = _write(tmp_path / "other.toml", VALID)
    monkeypatch.chdir(tmp_path)
    assert resolve_project_file(Path("other.toml")) == other


def test_resolve_project_file_reports_missing_explicit_file(tmp_path):
    with pytest.raises(ProjectFileError) as cm:
        resolve_project_file(tmp_path / "absent.toml")
    assert "不存在" in str(cm.value)


def test_resolve_project_file_reports_not_found(tmp_path, monkeypatch):
    if _has_ancestor_file(tmp_path):
        pytest.skip("上层目录里存在 devproxy.toml，无法断言「找不到」")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ProjectFileError) as cm:
        resolve_project_file(None)
    assert PROJECT_FILE_NAME in str(cm.value)


# --------------------------------- 解析


def test_load_project_file_parses_up_and_expose(tmp_path):
    path = _write(tmp_path / PROJECT_FILE_NAME)
    project = load_project_file(path)
    assert [r.name for r in project.routes] == ["web", "legacy"]
    web = project.get("web")
    assert web.mode == MODE_UP
    assert web.command == ["npm", "run", "dev"]
    assert web.port is None
    legacy = project.get("legacy")
    assert legacy.mode == MODE_EXPOSE
    assert legacy.port == 3000
    assert legacy.command is None


def test_load_project_file_exposes_project_dir(tmp_path):
    path = _write(tmp_path / PROJECT_FILE_NAME)
    project = load_project_file(path)
    assert project.project_dir == tmp_path


def test_load_project_file_accepts_empty_file(tmp_path):
    path = _write(tmp_path / PROJECT_FILE_NAME, "")
    project = load_project_file(path)
    assert project.routes == []


def test_load_project_file_reads_ready_timeout(tmp_path):
    path = _write(
        tmp_path / PROJECT_FILE_NAME,
        '[[routes]]\nname = "web"\ncommand = ["x"]\nready_timeout = 5\n',
    )
    assert load_project_file(path).get("web").ready_timeout == 5


# --------------------------------- 错误


def test_toml_syntax_error_reports_file_and_position(tmp_path):
    path = _write(tmp_path / PROJECT_FILE_NAME, "[[routes]\nname = 'web'\n")
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    message = str(cm.value)
    assert str(path) in message
    assert "TOML" in message
    assert "line" in message  # tomllib 自带行列号


def test_unknown_top_level_key_is_rejected(tmp_path):
    path = _write(tmp_path / PROJECT_FILE_NAME, "routesx = []\n")
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "routesx" in str(cm.value)


def test_unknown_route_key_is_rejected(tmp_path):
    path = _write(
        tmp_path / PROJECT_FILE_NAME,
        '[[routes]]\nname = "web"\ncommand = ["x"]\nprot = 3000\n',
    )
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "prot" in str(cm.value)
    assert "routes[0]" in str(cm.value)


def test_missing_name_is_rejected(tmp_path):
    path = _write(tmp_path / PROJECT_FILE_NAME, '[[routes]]\ncommand = ["x"]\n')
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "name" in str(cm.value)


def test_invalid_name_is_rejected(tmp_path):
    path = _write(tmp_path / PROJECT_FILE_NAME, '[[routes]]\nname = "Bad_Name"\ncommand = ["x"]\n')
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "Bad_Name" in str(cm.value)


def test_up_mode_requires_command(tmp_path):
    path = _write(tmp_path / PROJECT_FILE_NAME, '[[routes]]\nname = "web"\n')
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "command" in str(cm.value)


def test_expose_mode_requires_port(tmp_path):
    path = _write(tmp_path / PROJECT_FILE_NAME, '[[routes]]\nname = "web"\nmode = "expose"\n')
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "port" in str(cm.value)


def test_expose_mode_rejects_command(tmp_path):
    path = _write(
        tmp_path / PROJECT_FILE_NAME,
        '[[routes]]\nname = "web"\nmode = "expose"\nport = 3000\ncommand = ["x"]\n',
    )
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "command" in str(cm.value)


def test_command_must_be_a_list(tmp_path):
    path = _write(
        tmp_path / PROJECT_FILE_NAME,
        '[[routes]]\nname = "web"\ncommand = "npm run dev"\n',
    )
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "command" in str(cm.value)


def test_command_items_must_be_strings(tmp_path):
    path = _write(
        tmp_path / PROJECT_FILE_NAME,
        '[[routes]]\nname = "web"\ncommand = ["npm", 3]\n',
    )
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "command" in str(cm.value)


def test_invalid_mode_is_rejected(tmp_path):
    path = _write(
        tmp_path / PROJECT_FILE_NAME,
        '[[routes]]\nname = "web"\nmode = "start"\ncommand = ["x"]\n',
    )
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "mode" in str(cm.value)


def test_port_out_of_range_is_rejected(tmp_path):
    path = _write(
        tmp_path / PROJECT_FILE_NAME,
        '[[routes]]\nname = "web"\nmode = "expose"\nport = 70000\n',
    )
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "port" in str(cm.value)


def test_port_must_be_integer(tmp_path):
    path = _write(
        tmp_path / PROJECT_FILE_NAME,
        '[[routes]]\nname = "web"\nmode = "expose"\nport = "3000"\n',
    )
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "port" in str(cm.value)


def test_ready_timeout_out_of_range_is_rejected(tmp_path):
    path = _write(
        tmp_path / PROJECT_FILE_NAME,
        '[[routes]]\nname = "web"\ncommand = ["x"]\nready_timeout = 99999\n',
    )
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "ready_timeout" in str(cm.value)


def test_duplicate_names_are_rejected(tmp_path):
    path = _write(
        tmp_path / PROJECT_FILE_NAME,
        '[[routes]]\nname = "web"\ncommand = ["x"]\n\n[[routes]]\nname = "web"\ncommand = ["y"]\n',
    )
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(path)
    assert "重复" in str(cm.value)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ProjectFileError) as cm:
        load_project_file(tmp_path / "absent.toml")
    assert "不存在" in str(cm.value)
