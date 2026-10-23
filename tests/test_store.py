import json

import pytest

from devproxy.core.config import Config
from devproxy.gateway.caddy import CaddyConfig
from devproxy.gateway.routes import Route
from devproxy.gateway.store import RouteStore, RouteStoreError


class FakeCaddy:
    """假 Caddy：记录调用，validate/reload 可编排成功或失败。"""

    def __init__(self, validate_ok=True, reload_ok=True):
        self.validate_ok = validate_ok
        self.reload_ok = reload_ok
        self.validated = []
        self.reloaded = []

    def validate(self, caddyfile=None):
        self.validated.append(caddyfile)
        return (self.validate_ok, "ok" if self.validate_ok else "语法错误")

    def reload(self, caddyfile=None):
        self.reloaded.append(caddyfile)
        return (self.reload_ok, "ok" if self.reload_ok else "reload 失败")


def _store(paths, caddy=None):
    cfg = Config(storage=str(paths.data_dir))
    return RouteStore(paths, cfg, caddy or FakeCaddy())


def _route(name="codeup", port=42133):
    return Route(
        name=name,
        domain=f"{name}.test",
        port=port,
        mode="up",
        upstream=f"127.0.0.1:{port}",
    )


def test_mutate_creates_table_and_caddyfile(paths):
    paths.ensure_files()
    store = _store(paths)
    store.mutate(lambda t: t.upsert(_route()))
    assert paths.routes_json.exists()
    assert paths.routes_caddy.exists()
    assert "codeup.test" in paths.routes_caddy.read_text()
    assert json.loads(paths.routes_json.read_text())["routes"][0]["name"] == "codeup"


def test_list_returns_routes(paths):
    paths.ensure_files()
    store = _store(paths)
    store.mutate(lambda t: t.upsert(_route()))
    assert [r.name for r in store.list()] == ["codeup"]


def test_get_returns_none_for_unknown(paths):
    paths.ensure_files()
    assert _store(paths).get("nope") is None


def test_mutate_validates_before_writing(paths):
    """校验失败时不得落盘。"""
    paths.ensure_files()
    store = _store(paths, FakeCaddy(validate_ok=False))
    with pytest.raises(RouteStoreError) as cm:
        store.mutate(lambda t: t.upsert(_route()))
    assert "校验" in str(cm.value)
    assert not paths.routes_json.exists()
    assert paths.routes_caddy.read_text() == ""


def test_mutate_rolls_back_on_reload_failure(paths):
    paths.ensure_files()
    store = _store(paths, FakeCaddy(reload_ok=False))
    with pytest.raises(RouteStoreError) as cm:
        store.mutate(lambda t: t.upsert(_route()))
    assert "reload" in str(cm.value)
    assert not paths.routes_json.exists(), "reload 失败必须回滚路由表"
    assert paths.routes_caddy.read_text() == ""


def test_mutate_rolls_back_to_previous_state(paths):
    paths.ensure_files()
    good = _store(paths)
    good.mutate(lambda t: t.upsert(_route("first", 1111)))
    before = paths.routes_json.read_text()

    bad = _store(paths, FakeCaddy(reload_ok=False))
    with pytest.raises(RouteStoreError):
        bad.mutate(lambda t: t.upsert(_route("second", 2222)))
    assert paths.routes_json.read_text() == before
    assert "second.test" not in paths.routes_caddy.read_text()


def test_mutate_creates_import_targets_if_missing(paths):
    """sites.caddy 不存在时不能让 Caddy 的 import 失败。"""
    store = _store(paths)
    store.mutate(lambda t: t.upsert(_route()))
    assert paths.sites_caddy.exists()


def test_validate_uses_temp_caddyfile_not_the_live_one(paths):
    """校验必须针对新内容，不能直接用线上主 Caddyfile。"""
    paths.ensure_files()
    caddy = FakeCaddy()
    store = _store(paths, caddy)
    store.mutate(lambda t: t.upsert(_route()))
    validated = caddy.validated[0]
    assert validated != paths.caddyfile
    assert validated.name == "Caddyfile.new"


def test_lock_file_created(paths):
    paths.ensure_files()
    _store(paths).mutate(lambda t: None)
    assert paths.lock_path.exists()


def test_mutate_removes_temp_validation_files(paths):
    """校验用的临时文件必须清掉 —— 留在 state 目录里只是噪音。"""
    paths.ensure_files()
    store = _store(paths)
    store.mutate(lambda t: t.upsert(_route()))
    assert not (paths.state_dir / "routes.caddy.new").exists()
    assert not (paths.state_dir / "Caddyfile.new").exists()


def test_mutate_removes_temp_validation_files_on_failure(paths):
    """失败路径同样要清理。"""
    paths.ensure_files()
    store = _store(paths, FakeCaddy(validate_ok=False))
    with pytest.raises(RouteStoreError):
        store.mutate(lambda t: t.upsert(_route()))
    assert not (paths.state_dir / "routes.caddy.new").exists()
    assert not (paths.state_dir / "Caddyfile.new").exists()

    store = _store(paths, FakeCaddy(reload_ok=False))
    with pytest.raises(RouteStoreError):
        store.mutate(lambda t: t.upsert(_route()))
    assert not (paths.state_dir / "routes.caddy.new").exists()
    assert not (paths.state_dir / "Caddyfile.new").exists()


# --------------------------------- I1: 事务回滚必须覆盖第二次落盘（routes.caddy）


def test_mutate_returns_none(paths):
    """M1: mutate 是命令，不得把内部可变列表暴露给调用方。"""
    paths.ensure_files()
    assert _store(paths).mutate(lambda t: t.upsert(_route())) is None


def test_mutate_rolls_back_json_when_caddyfile_write_fails(paths, monkeypatch):
    """routes.json 已写、routes.caddy 写入失败 → 两份文件都要回到旧状态。"""
    import devproxy.gateway.store as store_mod

    paths.ensure_files()
    store = _store(paths)
    store.mutate(lambda t: t.upsert(_route("first", 1111)))
    before_json = paths.routes_json.read_text()
    before_caddy = paths.routes_caddy.read_text()

    real_atomic_write = store_mod.atomic_write
    failures = {"left": 1}

    def flaky_atomic_write(path, content):
        if path == paths.routes_caddy and failures["left"] > 0:
            failures["left"] -= 1
            raise OSError(28, "No space left on device")
        real_atomic_write(path, content)

    monkeypatch.setattr(store_mod, "atomic_write", flaky_atomic_write)
    with pytest.raises(RouteStoreError) as cm:
        store.mutate(lambda t: t.upsert(_route("second", 2222)))

    assert "已回滚" in str(cm.value)
    assert isinstance(cm.value.__cause__, OSError)
    assert paths.routes_json.read_text() == before_json, "JSON 必须回滚"
    assert paths.routes_caddy.read_text() == before_caddy, "Caddyfile 必须保持旧版本"
    assert "second.test" not in paths.routes_json.read_text()


def test_mutate_rolls_back_when_json_save_fails(paths, monkeypatch):
    """save_table 失败（尚未开始写 Caddyfile）同样不得留下半截状态。"""
    import devproxy.gateway.store as store_mod

    paths.ensure_files()
    store = _store(paths)
    store.mutate(lambda t: t.upsert(_route("first", 1111)))
    before_json = paths.routes_json.read_text()

    def broken_save(path, table):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(store_mod, "save_table", broken_save)
    with pytest.raises(RouteStoreError) as cm:
        store.mutate(lambda t: t.upsert(_route("second", 2222)))

    assert "已回滚" in str(cm.value)
    assert paths.routes_json.read_text() == before_json
    assert "second.test" not in paths.routes_caddy.read_text()
