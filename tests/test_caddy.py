import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from devproxy.gateway.caddy import (
    CaddyAdmin,
    CaddyConfig,
    build_main_caddyfile,
)


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
    """用假 caddy 脚本验证参数拼装与返回码处理。"""
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
    assert "超时" in msg


def test_reload_timeout_returns_false(tmp_path):
    fake = tmp_path / "caddy"
    fake.write_text("#!/bin/sh\nsleep 5\n")
    fake.chmod(0o755)
    cfg = CaddyConfig(caddy_bin=fake, caddyfile=tmp_path / "Caddyfile")
    ok, msg = cfg.reload(timeout=0.5)
    assert ok is False
    assert "超时" in msg


def test_build_main_caddyfile_shape(tmp_path):
    out = build_main_caddyfile(
        admin_addr="127.0.0.1:2019",
        storage=Path("/data/caddy"),
        routes_caddy=Path("/state/routes.caddy"),
        sites_caddy=Path("/config/sites.caddy"),
    )
    assert "admin 127.0.0.1:2019" in out
    assert "storage file_system /data/caddy" in out
    assert "import /state/routes.caddy" in out
    assert "import /config/sites.caddy" in out
    assert out.count("{") == 1, "全局块只能有一个（被 import 的文件不能再有全局块）"


def test_build_main_caddyfile_emits_ports(tmp_path):
    """http_port / https_port 必须真的进 Caddyfile —— 否则配置里的键形同虚设。"""
    out = build_main_caddyfile(
        admin_addr="127.0.0.1:2019",
        storage=Path("/data/caddy"),
        routes_caddy=Path("/state/routes.caddy"),
        sites_caddy=Path("/config/sites.caddy"),
        http_port=8080,
        https_port=8443,
    )
    assert "http_port 8080" in out
    assert "https_port 8443" in out


def test_build_main_caddyfile_default_ports(tmp_path):
    """默认值 80/443 —— 生成结果与旧行为一致。"""
    out = build_main_caddyfile(
        admin_addr="127.0.0.1:2019",
        storage=Path("/data/caddy"),
        routes_caddy=Path("/state/routes.caddy"),
        sites_caddy=Path("/config/sites.caddy"),
    )
    assert "http_port 80" in out
    assert "https_port 443" in out


# ------------------------------------------ 边界：畸形二进制 / 畸形 Admin 响应


def _nonexec_format_binary(tmp_path) -> Path:
    """存在、有执行位、但不是可执行格式（Exec format error）的文件。"""
    fake = tmp_path / "caddy"
    fake.write_text("this is not an executable format\n")
    fake.chmod(0o755)
    return fake


def test_validate_wrong_format_binary_returns_false_not_exception(tmp_path):
    """二进制存在但格式不对（OSError: Exec format error）也要遵守 (False, msg) 契约。"""
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
    """200 但响应体不是 JSON → (200, None)，不能裸抛 JSONDecodeError。"""
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


# ------------------------------------------------ I6: 畸形 HTTP 响应不得穿出 api()


def test_api_malformed_status_line_returns_zero_not_exception():
    """Admin 端口被非 HTTP 服务占用（BadStatusLine）→ (0, None)，不抛异常。"""
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
        code, body = CaddyAdmin(
            base=f"http://127.0.0.1:{port}", timeout=2
        ).api("GET", "/config/")
        assert (code, body) == (0, None)
    finally:
        srv.close()
        worker.join(timeout=5)


# ------------------------------------- I7: Caddyfile token 引用（空格/换行路径）


def test_build_main_caddyfile_quotes_paths_with_spaces(tmp_path):
    """含空格的合法路径必须被引用成单个 token，否则 Caddyfile 语法错误。"""
    out = build_main_caddyfile(
        admin_addr="127.0.0.1:2019",
        storage=Path("/Volumes/My Disk/caddy"),
        routes_caddy=Path("/state/my routes.caddy"),
        sites_caddy=Path("/config/sites.caddy"),
    )
    assert 'storage file_system "/Volumes/My Disk/caddy"' in out
    assert 'import "/state/my routes.caddy"' in out
    # 不含空格的 token 保持原样，生成结果与旧行为逐字节一致
    assert "import /config/sites.caddy" in out


def test_build_main_caddyfile_escapes_newline_instead_of_injecting_lines():
    """路径里的换行不得变成额外配置行（否则等于配置注入）。"""
    out = build_main_caddyfile(
        admin_addr="127.0.0.1:2019",
        storage=Path("/data/a\nb"),
        routes_caddy=Path("/state/routes.caddy"),
        sites_caddy=Path("/config/sites.caddy"),
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
