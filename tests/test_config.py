from pathlib import Path

import pytest

from devproxy.core.config import Config
from devproxy.core.config import ConfigError
from devproxy.core.config import load_config
from devproxy.core.config import validate_config


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
    assert cfg.http_port == 80, "keys absent from the file keep their defaults"


def test_unknown_key_raises_config_error(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("nope = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert "nope" in str(cm.value)


def test_storage_path_expands_user():
    assert Config(storage="~/x").storage_path == Path.home() / "x"


def test_env_data_dir_provides_storage_default(tmp_path, monkeypatch):
    """When config.toml does not set storage, DEVPROXY_DATA_DIR provides its default (single source of truth)."""
    env_dir = tmp_path / "env-data"
    monkeypatch.setenv("DEVPROXY_DATA_DIR", str(env_dir))
    f = tmp_path / "config.toml"
    f.write_text('domain_suffix = "example"\n', encoding="utf-8")
    cfg = load_config(f)
    assert cfg.storage == str(env_dir)
    assert cfg.storage_path == env_dir
    # Same when the config file is absent: the file certainly did not set storage, so the env var remains the fallback.
    assert load_config(tmp_path / "absent.toml").storage == str(env_dir)


def test_storage_in_file_wins_over_env(tmp_path, monkeypatch):
    """When config.toml explicitly sets storage, the file value wins over the env var."""
    monkeypatch.setenv("DEVPROXY_DATA_DIR", str(tmp_path / "env-data"))
    f = tmp_path / "config.toml"
    f.write_text('storage = "/srv/caddy"\n', encoding="utf-8")
    assert load_config(f).storage == "/srv/caddy"


def test_malformed_toml_raises_config_error(tmp_path):
    """Malformed TOML must map to ConfigError, not raise a bare TOMLDecodeError."""
    f = tmp_path / "config.toml"
    f.write_text("storage = [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert "syntax error" in str(cm.value)


# ---------------------------------- I-6: config value validation (guards against Caddyfile metacharacter injection)


@pytest.mark.parametrize(
    "suffix",
    [
        "test }\nimport /tmp/evil\n#",  # Caddyfile metacharacters
        "a.b",  # contains a dot: must be a single label
        ".test",
        "-abc",  # leading hyphen
        "ab-",  # trailing hyphen
        "Test",  # uppercase
        "x" * 64,  # too long
        "a b",
    ],
)
def test_invalid_domain_suffix_is_rejected(tmp_path, suffix):
    """domain_suffix flows straight into the Caddyfile site address -- it must be a single DNS label."""
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
    """Top-level domains on the whitelist pass directly (hyphens are still enforced by the label-format check)."""
    f = tmp_path / "config.toml"
    f.write_text('domain_suffix = "example"\n', encoding="utf-8")
    assert load_config(f).domain_suffix == "example"


@pytest.mark.parametrize(
    "key,value",
    [
        ("dns_backend", '"resolve"'),  # only auto | resolved | system are allowed
        ("dns_backend", "3"),
        ("http_port", "0"),
        ("http_port", "70000"),
        ("http_port", '"80"'),  # string type
        ("https_port", "0"),
        ("ready_timeout", "0"),
        ("ready_timeout", "7200"),
        ("admin_addr", '"127.0.0.1"'),  # missing port
        ("admin_addr", '"2019"'),  # missing host
        ("admin_addr", '"127.0.0.1:abc"'),
        ("admin_addr", '"127.0.0.1:70000"'),
    ],
)
def test_invalid_config_values_are_rejected(tmp_path, key, value):
    f = tmp_path / "config.toml"
    f.write_text(f"{key} = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert key in str(cm.value), f"the error should name {key}"


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


# ------------------------------------- top-level domain whitelist (guard for machine-wide DNS routing)


@pytest.mark.parametrize("suffix", ["test", "example", "localhost", "invalid"])
def test_reserved_local_tlds_are_accepted(tmp_path, suffix):
    """RFC 2606 / 6761 top-level domains reserved for local use pass directly."""
    f = tmp_path / "config.toml"
    f.write_text(f'domain_suffix = "{suffix}"\n', encoding="utf-8")
    assert load_config(f).domain_suffix == suffix


@pytest.mark.parametrize("suffix", ["com", "net", "org", "cn", "dev", "local"])
def test_public_tlds_are_rejected(tmp_path, suffix):
    """Real public top-level domains must be rejected -- they would be written into machine-wide DNS routing."""
    f = tmp_path / "config.toml"
    f.write_text(f'domain_suffix = "{suffix}"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    message = str(cm.value)
    assert "domain_suffix" in message
    assert suffix in message, "the error must include the value the user supplied"
    assert "test" in message, "the error must say what to use instead"
    assert "allowed_domain_suffixes" in message, "the error must show how to allow it explicitly"


def test_rejection_message_explains_the_risk(tmp_path):
    """The rejection reason must be clear: machine-wide resolution hijacking / cookie bleed / HSTS."""
    f = tmp_path / "config.toml"
    f.write_text('domain_suffix = "com"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    message = str(cm.value)
    assert "machine-wide" in message
    assert "cookie" in message
    assert "HSTS" in message


def test_allowed_domain_suffixes_opens_the_gate(tmp_path):
    """An explicit allow in config.toml means the user takes responsibility; the tool must permit it."""
    f = tmp_path / "config.toml"
    f.write_text(
        'domain_suffix = "dev"\nallowed_domain_suffixes = ["dev"]\n',
        encoding="utf-8",
    )
    cfg = load_config(f)
    assert cfg.domain_suffix == "dev"
    assert cfg.allowed_domain_suffixes == ("dev",)


def test_allowed_domain_suffixes_default_is_empty(tmp_path):
    """No public top-level domain is allowed by default."""
    assert load_config(tmp_path / "absent.toml").allowed_domain_suffixes == ()


def test_allowed_domain_suffixes_only_applies_to_listed_values(tmp_path):
    """Allowing dev does not allow com -- the list is exact, not a switch."""
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
    """Every entry in the allow list must still be a single DNS label -- it is a candidate value for domain_suffix."""
    f = tmp_path / "config.toml"
    f.write_text(f"allowed_domain_suffixes = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert "allowed_domain_suffixes" in str(cm.value)


def test_allowed_domain_suffixes_wrong_type_is_rejected(tmp_path):
    """A string instead of an array must be rejected -- do not treat "dev" as allowing dev."""
    f = tmp_path / "config.toml"
    f.write_text('allowed_domain_suffixes = "dev"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert "allowed_domain_suffixes" in str(cm.value)


def test_validate_config_rejects_public_tld_directly():
    """validate_config is the only entry point -- a directly constructed Config is rejected the same way."""
    with pytest.raises(ConfigError) as cm:
        validate_config(Config(domain_suffix="com"))
    assert "domain_suffix" in str(cm.value)


def test_validate_config_accepts_explicitly_allowed_suffix():
    cfg = Config(domain_suffix="com", allowed_domain_suffixes=("com",))
    assert validate_config(cfg) is None
