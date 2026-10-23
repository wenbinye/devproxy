from pathlib import Path

import pytest

from devproxy.core.config import Config, ConfigError, load_config, validate_config


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
    f.write_text('domain_suffix = "example"\nready_timeout = 5\n', encoding="utf-8")
    cfg = load_config(f)
    assert cfg.domain_suffix == "example"
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
    f.write_text('domain_suffix = "example"\n', encoding="utf-8")
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


# ---------------------------------- I-6: 配置值校验（防 Caddyfile 元字符注入）


@pytest.mark.parametrize(
    "suffix",
    [
        'test }\nimport /tmp/evil\n#',  # Caddyfile 元字符
        "a.b",  # 含点：必须是单标签
        ".test",
        "-abc",  # 首字符连字符
        "ab-",  # 尾字符连字符
        "Test",  # 大写
        "x" * 64,  # 超长
        "a b",
    ],
)
def test_invalid_domain_suffix_is_rejected(tmp_path, suffix):
    """domain_suffix 直通 Caddyfile 站点地址 —— 必须是单个 DNS 标签。"""
    f = tmp_path / "config.toml"
    escaped = suffix.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    f.write_text(f'domain_suffix = "{escaped}"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert "domain_suffix" in str(cm.value)


def test_domain_suffix_wrong_type_is_rejected(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("domain_suffix = 123\n", encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert "domain_suffix" in str(cm.value)


def test_valid_domain_suffixes_pass(tmp_path):
    """白名单里的顶级域直接放行（连字符仍由标签格式那一条把关）。"""
    f = tmp_path / "config.toml"
    f.write_text('domain_suffix = "example"\n', encoding="utf-8")
    assert load_config(f).domain_suffix == "example"


@pytest.mark.parametrize(
    "key,value",
    [
        ("dns_backend", '"resolve"'),  # 只允许 auto | resolved | system
        ("dns_backend", "3"),
        ("http_port", "0"),
        ("http_port", "70000"),
        ("http_port", '"80"'),  # 字符串类型
        ("https_port", "0"),
        ("ready_timeout", "0"),
        ("ready_timeout", "7200"),
        ("admin_addr", '"127.0.0.1"'),  # 缺端口
        ("admin_addr", '"2019"'),  # 缺主机
        ("admin_addr", '"127.0.0.1:abc"'),
        ("admin_addr", '"127.0.0.1:70000"'),
    ],
)
def test_invalid_config_values_are_rejected(tmp_path, key, value):
    f = tmp_path / "config.toml"
    f.write_text(f"{key} = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert key in str(cm.value), f"报错应点名 {key}"


def test_valid_admin_addr_and_ports_pass(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(
        'admin_addr = "127.0.0.1:2020"\ndns_backend = "resolved"\n'
        "http_port = 8080\nhttps_port = 8443\nready_timeout = 90\n",
        encoding="utf-8",
    )
    cfg = load_config(f)
    assert cfg.admin_addr == "127.0.0.1:2020"
    assert cfg.dns_backend == "resolved"
    assert cfg.admin_base == "http://127.0.0.1:2020"


# ------------------------------------- 顶级域白名单（整机 DNS 路由的护栏）


@pytest.mark.parametrize("suffix", ["test", "example", "localhost", "invalid"])
def test_reserved_local_tlds_are_accepted(tmp_path, suffix):
    """RFC 2606 / 6761 保留给本地用的顶级域直接放行。"""
    f = tmp_path / "config.toml"
    f.write_text(f'domain_suffix = "{suffix}"\n', encoding="utf-8")
    assert load_config(f).domain_suffix == suffix


@pytest.mark.parametrize("suffix", ["com", "net", "org", "cn", "dev", "local"])
def test_public_tlds_are_rejected(tmp_path, suffix):
    """真实公共顶级域必须被拒 —— 它会被写进整机范围的 DNS 路由。"""
    f = tmp_path / "config.toml"
    f.write_text(f'domain_suffix = "{suffix}"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    message = str(cm.value)
    assert "domain_suffix" in message
    assert suffix in message, "报错要带上用户填的那个值"
    assert "test" in message, "报错要说清该改用什么"
    assert "allowed_domain_suffixes" in message, "报错要给出显式放行的写法"


def test_rejection_message_explains_the_risk(tmp_path):
    """拒绝理由必须说清楚：整机解析被劫持 / cookie 串味 / HSTS。"""
    f = tmp_path / "config.toml"
    f.write_text('domain_suffix = "com"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    message = str(cm.value)
    assert "整机" in message
    assert "cookie" in message
    assert "HSTS" in message


def test_allowed_domain_suffixes_opens_the_gate(tmp_path):
    """用户在 config.toml 里显式放行 = 用户明确担责，工具必须允许。"""
    f = tmp_path / "config.toml"
    f.write_text(
        'domain_suffix = "dev"\nallowed_domain_suffixes = ["dev"]\n',
        encoding="utf-8",
    )
    cfg = load_config(f)
    assert cfg.domain_suffix == "dev"
    assert cfg.allowed_domain_suffixes == ("dev",)


def test_allowed_domain_suffixes_default_is_empty(tmp_path):
    """默认不放行任何公共顶级域。"""
    assert load_config(tmp_path / "absent.toml").allowed_domain_suffixes == ()


def test_allowed_domain_suffixes_only_applies_to_listed_values(tmp_path):
    """放行 dev 不等于放行 com —— 名单是精确的，不是开关。"""
    f = tmp_path / "config.toml"
    f.write_text(
        'domain_suffix = "com"\nallowed_domain_suffixes = ["dev"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert "domain_suffix" in str(cm.value)


def test_allowed_domain_suffixes_accepts_multiple_values(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(
        'domain_suffix = "dev"\nallowed_domain_suffixes = ["dev", "internal"]\n',
        encoding="utf-8",
    )
    assert load_config(f).allowed_domain_suffixes == ("dev", "internal")


@pytest.mark.parametrize("value", ['["a.b"]', '["Dev"]', '["-x"]', '[""]'])
def test_allowed_domain_suffixes_entries_must_be_labels(tmp_path, value):
    """放行名单的每一项仍要是单个 DNS 标签 —— 它就是 domain_suffix 的候选值。"""
    f = tmp_path / "config.toml"
    f.write_text(f"allowed_domain_suffixes = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert "allowed_domain_suffixes" in str(cm.value)


def test_allowed_domain_suffixes_wrong_type_is_rejected(tmp_path):
    """写成字符串而不是数组要报错 —— 别把 "dev" 当成放行了 dev。"""
    f = tmp_path / "config.toml"
    f.write_text('allowed_domain_suffixes = "dev"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert "allowed_domain_suffixes" in str(cm.value)


def test_validate_config_rejects_public_tld_directly():
    """validate_config 是唯一入口 —— 直接构造的 Config 也一样被拦。"""
    with pytest.raises(ConfigError) as cm:
        validate_config(Config(domain_suffix="com"))
    assert "domain_suffix" in str(cm.value)


def test_validate_config_accepts_explicitly_allowed_suffix():
    cfg = Config(domain_suffix="com", allowed_domain_suffixes=("com",))
    assert validate_config(cfg) is None
