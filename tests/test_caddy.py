import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

from devproxy.gateway.caddy import CaddyAdmin
from devproxy.gateway.caddy import CaddyConfig
from devproxy.gateway.caddy import MainCaddyfileSpec
from devproxy.gateway.caddy import build_main_caddyfile


class _Quiet(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _stub_admin():
    srv = _Quiet(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_api_get_returns_200_and_body():
    srv, base = _stub_admin()
    try:
        code, body = CaddyAdmin(base=base).api("GET", "/config/")
        assert code == 200
        assert body == {"ok": True}
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_returns_zero_when_unreachable():
    code, body = CaddyAdmin(base="http://127.0.0.1:1", timeout=0.5).api("GET", "/config/")
    assert code == 0
    assert body is None


def test_is_alive_true_and_false():
    srv, base = _stub_admin()
    try:
        assert CaddyAdmin(base=base).is_alive() is True
    finally:
        srv.shutdown()
        srv.server_close()
    assert CaddyAdmin(base="http://127.0.0.1:1", timeout=0.5).is_alive() is False


def test_validate_uses_caddy_binary(tmp_path):
    """Use a fake caddy script to verify argument assembly and return-code handling."""
    fake = tmp_path / "caddy"
    fake.write_text('#!/bin/sh\necho "ARGS: $@" >&2\nexit 0\n')
    fake.chmod(0o755)
    cfg = CaddyConfig(caddy_bin=fake, caddyfile=tmp_path / "Caddyfile")
    ok, msg = cfg.validate(tmp_path / "Caddyfile")
    assert ok is True
    assert "validate" in msg and "--config" in msg


def test_validate_reports_failure(tmp_path):
    fake = tmp_path / "caddy"
    fake.write_text('#!/bin/sh\necho "boom" >&2\nexit 1\n')
    fake.chmod(0o755)
    cfg = CaddyConfig(caddy_bin=fake, caddyfile=tmp_path / "Caddyfile")
    ok, msg = cfg.validate(tmp_path / "Caddyfile")
    assert ok is False
    assert "boom" in msg


def test_reload_passes_address(tmp_path):
    fake = tmp_path / "caddy"
    fake.write_text('#!/bin/sh\necho "ARGS: $@" >&2\nexit 0\n')
    fake.chmod(0o755)
    cfg = CaddyConfig(caddy_bin=fake, caddyfile=tmp_path / "Caddyfile", admin_addr="127.0.0.1:2019")
    ok, msg = cfg.reload()
    assert ok is True
    assert "reload" in msg
    assert "--address" in msg and "127.0.0.1:2019" in msg


def test_validate_missing_binary_returns_false(tmp_path):
    cfg = CaddyConfig(caddy_bin=tmp_path / "absent", caddyfile=tmp_path / "Caddyfile")
    ok, msg = cfg.validate(tmp_path / "Caddyfile")
    assert ok is False
    assert str(tmp_path / "absent") in msg


def test_reload_missing_binary_returns_false(tmp_path):
    cfg = CaddyConfig(caddy_bin=tmp_path / "absent", caddyfile=tmp_path / "Caddyfile")
    ok, msg = cfg.reload()
    assert ok is False
    assert str(tmp_path / "absent") in msg


def test_validate_timeout_returns_false(tmp_path):
    fake = tmp_path / "caddy"
    fake.write_text("#!/bin/sh\nsleep 5\n")
    fake.chmod(0o755)
    cfg = CaddyConfig(caddy_bin=fake, caddyfile=tmp_path / "Caddyfile")
    ok, msg = cfg.validate(tmp_path / "Caddyfile", timeout=0.5)
    assert ok is False
    assert "timed out" in msg


def test_reload_timeout_returns_false(tmp_path):
    fake = tmp_path / "caddy"
    fake.write_text("#!/bin/sh\nsleep 5\n")
    fake.chmod(0o755)
    cfg = CaddyConfig(caddy_bin=fake, caddyfile=tmp_path / "Caddyfile")
    ok, msg = cfg.reload(timeout=0.5)
    assert ok is False
    assert "timed out" in msg


def test_build_main_caddyfile_shape(tmp_path):
    out = build_main_caddyfile(
        MainCaddyfileSpec(
            admin_addr="127.0.0.1:2019",
            storage=Path("/data/caddy"),
            routes_caddy=Path("/state/routes.caddy"),
            sites_caddy=Path("/config/sites.caddy"),
        )
    )
    assert "admin 127.0.0.1:2019" in out
    assert "storage file_system /data/caddy" in out
    assert "import /state/routes.caddy" in out
    assert "import /config/sites.caddy" in out
    assert out.count("{") == 1, (
        "there can be only one global block (imported files cannot have another)"
    )


def test_build_main_caddyfile_emits_ports(tmp_path):
    """http_port / https_port must really reach the Caddyfile -- otherwise the config keys are inert."""
    out = build_main_caddyfile(
        MainCaddyfileSpec(
            admin_addr="127.0.0.1:2019",
            storage=Path("/data/caddy"),
            routes_caddy=Path("/state/routes.caddy"),
            sites_caddy=Path("/config/sites.caddy"),
            http_port=8080,
            https_port=8443,
        )
    )
    assert "http_port 8080" in out
    assert "https_port 8443" in out


def test_build_main_caddyfile_default_ports(tmp_path):
    """Defaults are 80/443 -- output matches the legacy behavior."""
    out = build_main_caddyfile(
        MainCaddyfileSpec(
            admin_addr="127.0.0.1:2019",
            storage=Path("/data/caddy"),
            routes_caddy=Path("/state/routes.caddy"),
            sites_caddy=Path("/config/sites.caddy"),
        )
    )
    assert "http_port 80" in out
    assert "https_port 443" in out


# ------------------------------------------ Edge cases: malformed binary / malformed Admin response


def _nonexec_format_binary(tmp_path) -> Path:
    """A file that exists and has the execute bit set but is not an executable format (Exec format error)."""
    fake = tmp_path / "caddy"
    fake.write_text("this is not an executable format\n")
    fake.chmod(0o755)
    return fake


def test_validate_wrong_format_binary_returns_false_not_exception(tmp_path):
    """The binary exists but is in the wrong format (OSError: Exec format error); still follow the (False, msg) contract."""
    cfg = CaddyConfig(caddy_bin=_nonexec_format_binary(tmp_path), caddyfile=tmp_path / "C")
    ok, msg = cfg.validate(tmp_path / "C")
    assert ok is False
    assert "caddy" in msg


def test_reload_wrong_format_binary_returns_false_not_exception(tmp_path):
    cfg = CaddyConfig(caddy_bin=_nonexec_format_binary(tmp_path), caddyfile=tmp_path / "C")
    ok, msg = cfg.reload()
    assert ok is False
    assert "caddy" in msg


class _NonJsonHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"not-json-at-all"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_api_non_json_200_returns_code_and_none():
    """200 but the body is not JSON -> (200, None); must not raise JSONDecodeError."""
    srv = _Quiet(("127.0.0.1", 0), _NonJsonHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, body = CaddyAdmin(base=f"http://127.0.0.1:{srv.server_address[1]}").api(
            "GET", "/config/"
        )
        assert code == 200
        assert body is None
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------ I6: malformed HTTP responses must not leak out of api()


def test_api_malformed_status_line_returns_zero_not_exception():
    """The Admin port is occupied by a non-HTTP service (BadStatusLine) -> (0, None); no exception."""
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            conn.recv(4096)
            conn.sendall(b"BOGUS STATUS LINE\r\n\r\n")
        finally:
            conn.close()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        code, body = CaddyAdmin(base=f"http://127.0.0.1:{port}", timeout=2).api("GET", "/config/")
        assert (code, body) == (0, None)
    finally:
        srv.close()
        worker.join(timeout=5)


# ------------------------------------- I7: Caddyfile token quoting (paths with spaces/newlines)


def test_build_main_caddyfile_quotes_paths_with_spaces(tmp_path):
    """A valid path containing spaces must be quoted as a single token, or the Caddyfile is a syntax error."""
    out = build_main_caddyfile(
        MainCaddyfileSpec(
            admin_addr="127.0.0.1:2019",
            storage=Path("/Volumes/My Disk/caddy"),
            routes_caddy=Path("/state/my routes.caddy"),
            sites_caddy=Path("/config/sites.caddy"),
        )
    )
    assert 'storage file_system "/Volumes/My Disk/caddy"' in out
    assert 'import "/state/my routes.caddy"' in out
    # Tokens without spaces stay as they are; output is byte-for-byte identical to the legacy behavior.
    assert "import /config/sites.caddy" in out


def test_build_main_caddyfile_escapes_newline_instead_of_injecting_lines():
    """A newline in a path must not become an extra config line (that would be config injection)."""
    out = build_main_caddyfile(
        MainCaddyfileSpec(
            admin_addr="127.0.0.1:2019",
            storage=Path("/data/a\nb"),
            routes_caddy=Path("/state/routes.caddy"),
            sites_caddy=Path("/config/sites.caddy"),
        )
    )
    assert 'storage file_system "/data/a\\nb"' in out
    assert not any(line.strip() == "b" for line in out.splitlines())


def test_caddy_token_quotes_metacharacters_and_keeps_simple_tokens():
    from devproxy.gateway.caddy import _caddy_token

    assert _caddy_token("/plain/path-1.2") == "/plain/path-1.2"
    assert _caddy_token("/with space/x") == '"/with space/x"'
    assert _caddy_token('a"b') == '"a\\"b"'
    assert _caddy_token("/a{b") == '"/a{b"'
    assert _caddy_token("") == '""'
