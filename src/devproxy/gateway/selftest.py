"""隔离 Caddy 实例的端到端自检 —— 不碰生产 443/2019。

沿用 devp selftest 的成熟做法：独立 admin 端口、独立 https/http 端口、
独立 storage，用沙箱自己的 CA 做真实主机名校验的 HTTPS 请求。

与 devp 的差异：沙箱用**精确站点块**（一个路由一个块 + tls internal），
没有通配块也没有 on-demand —— 未注册域名因此拿不到任何证书，握手直接失败。
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from devproxy.gateway.routes import Route, RouteTable, render_caddyfile

ADMIN_PORT = 20191
HTTPS_PORT = 14443
HTTP_PORT = 14480

UPSTREAM_BODY = b"hello-from-upstream"

# pki app（以及它的 install_trust 开关）自 Caddy 2.7 起才有。
# 更老的 caddy 会静默忽略 JSON 里的 install_trust 字段 —— 那时沙箱 CA
# 会被装进宿主的系统/NSS/Java 信任库，正是本任务要避免的事。
MIN_CADDY_VERSION = (2, 7, 0)

# Caddy 在 install_trust=false 时会打这行日志（modules/caddypki/pki.go）。
# 它是「宿主信任库安装确实被关掉了」的唯一外部可见证据。
TRUST_DISABLED_LOG = "root certificate trust store installation disabled"


@dataclass
class SelftestResult:
    checks: list[tuple[str, bool]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def record(self, desc: str, ok: bool) -> None:
        self.checks.append((desc, ok))
        if not ok:
            self.failed.append(desc)


class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
    """自检用的上游：任何 GET 都回一段固定文本。"""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(UPSTREAM_BODY)))
        self.end_headers()
        self.wfile.write(UPSTREAM_BODY)

    def log_message(self, *args):
        pass


def _fetch(host: str, port: int, ctx: ssl.SSLContext, timeout: float = 5.0):
    """用给定 CA 做真实主机名校验的 HTTPS GET，返回 (状态码, 响应体)。"""
    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)
    try:
        conn.request("GET", "/")
        r = conn.getresponse()
        body = r.read().decode()
        return r.status, body
    finally:
        conn.close()


def _probe_context() -> ssl.SSLContext:
    """不校验证书的探针上下文 —— 只用来回答「服务器到底给不给证书」。

    用校验 CA 的上下文去试未注册域名是不行的：通配块 *.test 也会让握手
    成功，只是主机名对不上，客户端抛的是校验错误而不是「没有证书」。
    探针上下文不校验任何东西，于是「握手成功」= 服务器确实发了一张证书，
    「握手失败」= 它手上没有能用的证书。
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _wait_admin(base: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/config/", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    return False


def _wait_file(path: Path, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.2)
    return False


def _caddy_version(caddy_bin: Path) -> tuple[int, int, int] | None:
    """解析 `caddy version` 的输出，形如 "v2.11.7 h1:..."。"""
    try:
        proc = subprocess.run(
            [str(caddy_bin), "version"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", (proc.stdout or "") + (proc.stderr or ""))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _read_log_tail(path: Path, limit: int = 800) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(读不到 caddy 日志)"
    return text[-limit:].strip()


def _wait_trust_disabled_log(log_path: Path, timeout: float = 10.0) -> bool:
    """等 caddy 打出「宿主信任库安装已禁用」那行日志。

    这是 install_trust=false 真正生效的唯一外部可见证据（老版本 caddy
    会静默忽略该字段）；等不到就当没生效 —— fail closed。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if TRUST_DISABLED_LOG in _read_log_tail(log_path, limit=100000):
            return True
        time.sleep(0.2)
    return False


def _adapt(caddy_bin: Path, caddyfile: Path, config_json: Path) -> tuple[bool, str]:
    """Caddyfile → JSON，并把沙箱 CA 的「装进系统信任库」关掉。

    关掉的原因有两个：沙箱是一次性的 CA，装进系统信任库会污染宿主机；
    而安装动作要走 sudo（可能提示密码），会把启动卡住好几十秒。
    这只影响信任库，不影响签发与路由行为 —— 被测的仍是 tls internal。
    """
    proc = subprocess.run(
        [str(caddy_bin), "adapt", "--config", str(caddyfile)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    try:
        cfg = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return False, f"adapt 输出不是合法 JSON: {e}"
    cfg.setdefault("apps", {})["pki"] = {
        "certificate_authorities": {"local": {"install_trust": False}}
    }
    config_json.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return True, ""


def _reload(caddy_bin: Path, config_json: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [
            str(caddy_bin),
            "reload",
            "--config",
            str(config_json),
            "--address",
            f"127.0.0.1:{ADMIN_PORT}",
        ],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def run_selftest(caddy_bin: Path) -> SelftestResult:
    """起隔离 Caddy，跑注册→转发→跳转→注销→证书策略的完整流程。"""
    result = SelftestResult()
    if not caddy_bin.exists():
        result.record(f"找到 caddy 二进制 {caddy_bin}", False)
        return result

    tmp = Path(tempfile.mkdtemp(prefix="devproxy-selftest-"))
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    uport = upstream.server_address[1]

    # 版本地板：老于 2.7 的 caddy 没有 pki app，install_trust 会被静默忽略，
    # 沙箱 CA 就会装进宿主信任库 —— 这种情况下宁可不跑。
    version = _caddy_version(caddy_bin)
    if version is None:
        result.record("读取 caddy 版本", False)
        upstream.shutdown()
        upstream.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
        return result
    ver_str = ".".join(str(x) for x in version)
    if version < MIN_CADDY_VERSION:
        floor = ".".join(str(x) for x in MIN_CADDY_VERSION)
        result.record(
            f"caddy 版本 {ver_str} 过旧（需要 ≥ {floor}：更老的 caddy 没有 pki app，"
            "沙箱 CA 会被装进宿主信任库）",
            False,
        )
        upstream.shutdown()
        upstream.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
        return result
    result.record(f"caddy 版本 {ver_str}（≥ {'.'.join(str(x) for x in MIN_CADDY_VERSION)}）", True)

    routes_file = tmp / "routes.caddy"
    sites_file = tmp / "sites.caddy"
    sites_file.write_text("", encoding="utf-8")

    def write_routes(routes: list[Route]) -> None:
        routes_file.write_text(
            render_caddyfile(RouteTable(routes=routes)), encoding="utf-8"
        )

    write_routes([])

    caddyfile = tmp / "Caddyfile"
    caddyfile.write_text(
        "{\n"
        f"\tadmin 127.0.0.1:{ADMIN_PORT}\n"
        f"\tstorage file_system {tmp / 'data'}\n"
        f"\thttp_port {HTTP_PORT}\n"
        f"\thttps_port {HTTPS_PORT}\n"
        "}\n"
        "\n"
        f"import {routes_file}\n"
        f"import {sites_file}\n",
        encoding="utf-8",
    )

    config_json = tmp / "config.json"
    ok, detail = _adapt(caddy_bin, caddyfile, config_json)
    if not ok:
        result.record(f"沙箱配置生成（caddy adapt）：{detail}", False)
        upstream.shutdown()
        upstream.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
        return result

    # 隔离：caddy 的配置目录/数据目录也指向 tmp，别去动生产那套 autosave.json
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp / "xdg-config"),
        "XDG_DATA_HOME": str(tmp / "xdg-data"),
    }
    # caddy 的输出不丢：install_trust 是否真的生效，只能从它自己的日志里看出来
    log_path = tmp / "caddy.log"
    log_file = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            [str(caddy_bin), "run", "--config", str(config_json)],
            stdout=log_file,
            stderr=log_file,
            env=env,
        )
    except OSError as e:
        log_file.close()
        result.record(f"沙箱 caddy 启动（{e}）", False)
        upstream.shutdown()
        upstream.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
        return result
    cafile = tmp / "data" / "pki" / "authorities" / "local" / "root.crt"
    try:
        base = f"http://127.0.0.1:{ADMIN_PORT}"
        if not _wait_admin(base):
            result.record("沙箱 caddy 启动", False)
            return result
        result.record("沙箱 caddy 启动", True)

        # 「没污染宿主」是本自检的核心承诺之一，必须有证据，不能靠假设。
        # 老版本 caddy 会静默忽略 install_trust，那时这行日志不会出现。
        if _wait_trust_disabled_log(log_path):
            result.record("已确认禁用宿主信任库安装（caddy 日志佐证）", True)
        else:
            result.record(
                "无法确认已禁用宿主信任库安装（Caddy 版本过旧？）—— caddy 输出尾部："
                + _read_log_tail(log_path),
                False,
            )

        ctx = None
        if _wait_file(cafile):
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    ctx = ssl.create_default_context(cafile=str(cafile))
                    break
                except ssl.SSLError:
                    time.sleep(0.2)
        result.record("沙箱 CA 根证书已生成并可加载", ctx is not None)
        if ctx is None:
            return result

        # 注册前：沙箱还不该对外服务 —— 明确断言「连接被拒绝」。
        # 这一项断言的是隔离性（此刻没有站点块，14443 上没人监听），
        # 真正验证「未注册域名拿不到证书」的是注册之后那一项。
        probe = _probe_context()
        try:
            _fetch("unregistered.test", HTTPS_PORT, probe)
            result.record("注册前沙箱未对外服务（连接应被拒绝）", False)
        except ConnectionRefusedError:
            result.record("注册前沙箱未对外服务（连接应被拒绝）", True)
        except (ssl.SSLError, OSError, http.client.HTTPException) as e:
            result.record(f"注册前沙箱未对外服务（连接应被拒绝）：{e}", False)

        # 已注册域名：精确块 + tls internal
        write_routes(
            [
                Route(
                    name="foo",
                    domain="foo.test",
                    port=uport,
                    mode="expose",
                    upstream=f"127.0.0.1:{uport}",
                )
            ]
        )
        ok, detail = _adapt(caddy_bin, caddyfile, config_json)
        if ok:
            ok, detail = _reload(caddy_bin, config_json)
        result.record(f"reload 生效：{detail}" if not ok else "reload 生效", ok)

        # 证书是 reload 后异步签发的，给它一点时间
        hit = False
        deadline = time.time() + 15
        last_err = ""
        while time.time() < deadline and not hit:
            try:
                status, body = _fetch("foo.test", HTTPS_PORT, ctx)
                hit = status == 200 and body == UPSTREAM_BODY.decode()
                if not hit:
                    last_err = f"状态 {status}"
            except (ssl.SSLError, OSError, http.client.HTTPException) as e:
                last_err = str(e)
            if not hit:
                time.sleep(0.3)
        result.record(
            "已注册域名命中上游" if hit else f"已注册域名命中上游（{last_err}）", hit
        )

        # 这次是在 TLS 服务已经起来之后测：未注册域名必须握手失败（真的没有证书）
        try:
            _fetch("unregistered.test", HTTPS_PORT, probe)
            result.record("已注册后未注册域名仍拿不到证书", False)
        except ssl.SSLError:
            result.record("已注册后未注册域名仍拿不到证书", True)
        except (OSError, http.client.HTTPException) as e:
            # 连不上或响应异常：服务器要么没起来，要么真的给出了证书
            result.record(f"已注册后未注册域名仍拿不到证书（{e}）", False)

        # 80 → 443 跳转（必须在注销之前测：注销后 Caddy 不再管理该域名的跳转）
        location = None
        deadline = time.time() + 10
        while time.time() < deadline and location is None:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", HTTP_PORT, timeout=5)
                conn.request("GET", "/", headers={"Host": "foo.test"})
                r = conn.getresponse()
                location = r.getheader("Location")
                r.read()
                conn.close()
            except (OSError, http.client.HTTPException):
                time.sleep(0.3)
        result.record(
            "http 跳转 https",
            location is not None and location.startswith("https://"),
        )

        # 注销后拿不到证书
        write_routes([])
        ok, detail = _adapt(caddy_bin, caddyfile, config_json)
        if ok:
            ok, detail = _reload(caddy_bin, config_json)
        result.record(f"注销 reload 生效：{detail}" if not ok else "注销 reload 生效", ok)
        try:
            status, body = _fetch("foo.test", HTTPS_PORT, ctx)
            result.record("注销后不再有路由", body != UPSTREAM_BODY.decode())
        except (ssl.SSLError, OSError, http.client.HTTPException):
            result.record("注销后不再有路由", True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            # 杀掉之后也要回收，否则留下僵尸进程
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        log_file.close()
        upstream.shutdown()
        upstream.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
    return result
