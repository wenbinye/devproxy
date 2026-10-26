from pathlib import Path

from devproxy.core.paths import Paths
from devproxy.core.paths import default_paths
from devproxy.core.paths import default_state_dir


def test_derived_paths(tmp_path):
    p = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    assert p.config_toml == tmp_path / "c" / "config.toml"
    assert p.caddyfile == tmp_path / "c" / "Caddyfile"
    assert p.sites_caddy == tmp_path / "c" / "sites.caddy"
    assert p.routes_json == tmp_path / "s" / "routes.json"
    assert p.routes_caddy == tmp_path / "s" / "routes.caddy"
    assert p.lock_path == tmp_path / "s" / "routes.lock"
    assert p.backup_dir == tmp_path / "s" / "backups"
    assert p.log_dir == tmp_path / "s" / "logs"


def test_ensure_creates_config_and_state_only(tmp_path):
    p = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    p.ensure()
    assert p.config_dir.is_dir()
    assert p.state_dir.is_dir()
    assert not p.data_dir.exists(), "data_dir 由 Caddy 自行创建，工具不预建"


def test_ensure_files_creates_import_targets(tmp_path):
    p = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    p.ensure_files()
    assert p.sites_caddy.is_file()
    assert p.routes_caddy.is_file()
    assert p.sites_caddy.read_text() == ""


def test_default_paths_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVPROXY_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("DEVPROXY_STATE_DIR", str(tmp_path / "st"))
    monkeypatch.setenv("DEVPROXY_DATA_DIR", str(tmp_path / "data"))
    p = default_paths()
    assert p.config_dir == tmp_path / "cfg"
    assert p.state_dir == tmp_path / "st"
    assert p.data_dir == tmp_path / "data"


def test_default_paths_stay_inside_isolated_home():
    """家目录被夹具指向临时目录后，默认路径绝不能落在真实 HOME 里。

    否则一次 ensure_files() 就会在用户真实的 ~/.config/devproxy 与
    ~/.local/state/devproxy 里凭空造出文件。
    """
    home = Path.home()
    p = default_paths()
    for path in (p.config_dir, p.state_dir, p.data_dir):
        assert str(path).startswith(str(home)), f"{path} 跑到了 HOME 之外"


def test_default_state_dir_is_the_single_source_of_truth(tmp_path, monkeypatch):
    """状态目录只有一个来源 —— 平台层不许自己拼路径。"""
    monkeypatch.setenv("DEVPROXY_STATE_DIR", str(tmp_path / "st"))
    assert default_state_dir() == tmp_path / "st"
    assert default_paths().state_dir == tmp_path / "st"


def test_default_state_dir_defaults_under_home(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVPROXY_STATE_DIR", raising=False)
    assert default_state_dir(tmp_path) == tmp_path / ".local" / "state" / "devproxy"
    assert default_state_dir() == Path.home() / ".local" / "state" / "devproxy"
