from pathlib import Path

import pytest

from devproxy.config import Config, ConfigError, load_config


def test_defaults_when_file_missing(tmp_path):
    cfg = load_config(tmp_path / "absent.toml")
    assert cfg.domain_suffix == "test"
    assert cfg.dns_backend == "auto"
    assert cfg.admin_addr == "127.0.0.1:2019"
    assert cfg.http_port == 80
    assert cfg.https_port == 443
    assert cfg.ready_timeout == 60
    assert cfg.admin_base == "http://127.0.0.1:2019"


def test_file_overrides_only_named_keys(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('domain_suffix = "dev"\nready_timeout = 5\n', encoding="utf-8")
    cfg = load_config(f)
    assert cfg.domain_suffix == "dev"
    assert cfg.ready_timeout == 5
    assert cfg.http_port == 80, "未在文件里出现的键保持默认值"


def test_unknown_key_raises_config_error(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("nope = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert "nope" in str(cm.value)


def test_storage_path_expands_user():
    assert Config(storage="~/x").storage_path == Path.home() / "x"


def test_env_data_dir_provides_storage_default(tmp_path, monkeypatch):
    """config.toml 未设置 storage 时，DEVPROXY_DATA_DIR 提供其默认值（单一权威）。"""
    env_dir = tmp_path / "env-data"
    monkeypatch.setenv("DEVPROXY_DATA_DIR", str(env_dir))
    f = tmp_path / "config.toml"
    f.write_text('domain_suffix = "dev"\n', encoding="utf-8")
    cfg = load_config(f)
    assert cfg.storage == str(env_dir)
    assert cfg.storage_path == env_dir
    # 配置文件不存在时同理：文件必然没设置 storage，环境变量照常兜底
    assert load_config(tmp_path / "absent.toml").storage == str(env_dir)


def test_storage_in_file_wins_over_env(tmp_path, monkeypatch):
    """config.toml 显式设置 storage 时，文件值优先于环境变量。"""
    monkeypatch.setenv("DEVPROXY_DATA_DIR", str(tmp_path / "env-data"))
    f = tmp_path / "config.toml"
    f.write_text('storage = "/srv/caddy"\n', encoding="utf-8")
    assert load_config(f).storage == "/srv/caddy"


def test_malformed_toml_raises_config_error(tmp_path):
    """语法错误的 TOML 必须映射为 ConfigError，而不是裸抛 TOMLDecodeError。"""
    f = tmp_path / "config.toml"
    f.write_text("storage = [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert "语法错误" in str(cm.value)
