import json

import pytest

from devproxy.core.config import Config
from devproxy.gateway.caddy import CaddyConfig
from devproxy.gateway.routes import Route
from devproxy.gateway.store import RouteStore
from devproxy.gateway.store import RouteStoreError


class FakeCaddy:
    """Fake Caddy: records calls; validate/reload success or failure is configurable."""

    def __init__(self, validate_ok=True, reload_ok=True):
        self.validate_ok = validate_ok
        self.reload_ok = reload_ok
        self.validated = []
        self.reloaded = []

    def validate(self, caddyfile=None):
        self.validated.append(caddyfile)
        return (self.validate_ok, "ok" if self.validate_ok else "syntax error")

    def reload(self, caddyfile=None):
        self.reloaded.append(caddyfile)
        return (self.reload_ok, "ok" if self.reload_ok else "reload failed")


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
    """Nothing must be written to disk when validation fails."""
    paths.ensure_files()
    store = _store(paths, FakeCaddy(validate_ok=False))
    with pytest.raises(RouteStoreError) as cm:
        store.mutate(lambda t: t.upsert(_route()))
    assert "validation failed" in str(cm.value)
    assert not paths.routes_json.exists()
    assert paths.routes_caddy.read_text() == ""


def test_mutate_rolls_back_on_reload_failure(paths):
    paths.ensure_files()
    store = _store(paths, FakeCaddy(reload_ok=False))
    with pytest.raises(RouteStoreError) as cm:
        store.mutate(lambda t: t.upsert(_route()))
    assert "reload" in str(cm.value)
    assert not paths.routes_json.exists(), "a reload failure must roll back the route table"
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
    """A missing sites.caddy must not make Caddy's import fail."""
    store = _store(paths)
    store.mutate(lambda t: t.upsert(_route()))
    assert paths.sites_caddy.exists()


def test_validate_uses_temp_caddyfile_not_the_live_one(paths):
    """Validation must target the new content, not the live main Caddyfile directly."""
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
    """Temporary validation files must be cleaned up -- leaving them in the state directory is just noise."""
    paths.ensure_files()
    store = _store(paths)
    store.mutate(lambda t: t.upsert(_route()))
    assert not (paths.state_dir / "routes.caddy.new").exists()
    assert not (paths.state_dir / "Caddyfile.new").exists()


def test_mutate_removes_temp_validation_files_on_failure(paths):
    """Failure paths must clean up too."""
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


# --------------------------------- I1: transaction rollback must cover the second write (routes.caddy)


def test_mutate_returns_none(paths):
    """M1: mutate is a command and must not expose its internal mutable list to the caller."""
    paths.ensure_files()
    assert _store(paths).mutate(lambda t: t.upsert(_route())) is None


def test_mutate_rolls_back_json_when_caddyfile_write_fails(paths, monkeypatch):
    """routes.json was written but routes.caddy write failed -> both files must return to the old state."""
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

    assert "rolled back" in str(cm.value)
    assert isinstance(cm.value.__cause__, OSError)
    assert paths.routes_json.read_text() == before_json, "JSON must be rolled back"
    assert paths.routes_caddy.read_text() == before_caddy, "the Caddyfile must keep the old version"
    assert "second.test" not in paths.routes_json.read_text()


def test_mutate_rolls_back_when_json_save_fails(paths, monkeypatch):
    """A failing save_table (before writing the Caddyfile even starts) must likewise leave no half-written state."""
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

    assert "rolled back" in str(cm.value)
    assert paths.routes_json.read_text() == before_json
    assert "second.test" not in paths.routes_caddy.read_text()
