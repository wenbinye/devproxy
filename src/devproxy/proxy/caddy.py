"""与 Caddy 交互：Admin API 客户端、配置校验、热加载、主 Caddyfile 生成。"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class CaddyError(RuntimeError):
    """Caddy 交互失败：不可达、配置非法或 reload 失败。"""


@dataclass
class CaddyAdmin:
    """Caddy Admin API 客户端。"""

    base: str = "http://127.0.0.1:2019"
    timeout: float = 5.0

    def api(self, method: str, path: str, body: object | None = None) -> tuple[int, object]:
        """HTTP 错误返回 (code, None)；连接失败返回 (0, None)。"""
        url = self.base.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, OSError):
            return 0, None

    def is_alive(self) -> bool:
        code, _ = self.api("GET", "/config/")
        return code == 200


@dataclass
class CaddyConfig:
    """主 Caddyfile 的校验与热加载。"""

    caddy_bin: Path
    caddyfile: Path
    admin_addr: str = "127.0.0.1:2019"

    def validate(self, caddyfile: Path | None = None, timeout: float = 30.0) -> tuple[bool, str]:
        """校验配置；二进制缺失/不可执行或超时同样返回 (False, 说明)，不抛异常。"""
        path = caddyfile or self.caddyfile
        try:
            proc = subprocess.run(
                [str(self.caddy_bin), "validate", "--config", str(path)],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (FileNotFoundError, PermissionError):
            return False, f"caddy 二进制不存在或不可执行: {self.caddy_bin}"
        except subprocess.TimeoutExpired:
            return False, f"caddy validate 超时（{timeout}s）"
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()

    def reload(self, caddyfile: Path | None = None, timeout: float = 30.0) -> tuple[bool, str]:
        """热加载配置；二进制缺失/不可执行或超时同样返回 (False, 说明)，不抛异常。"""
        path = caddyfile or self.caddyfile
        try:
            proc = subprocess.run(
                [
                    str(self.caddy_bin),
                    "reload",
                    "--config",
                    str(path),
                    "--address",
                    self.admin_addr,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (FileNotFoundError, PermissionError):
            return False, f"caddy 二进制不存在或不可执行: {self.caddy_bin}"
        except subprocess.TimeoutExpired:
            return False, f"caddy reload 超时（{timeout}s）"
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def build_main_caddyfile(
    *,
    admin_addr: str,
    storage: Path,
    routes_caddy: Path,
    sites_caddy: Path,
) -> str:
    """生成主 Caddyfile：唯一全局块 + 两个 import。

    被 import 的文件不能含全局块 —— 全局块只能有一个。
    """
    return (
        "{\n"
        f"\tadmin {admin_addr}\n"
        f"\tstorage file_system {storage}\n"
        "}\n"
        "\n"
        f"import {routes_caddy}\n"
        f"import {sites_caddy}\n"
    )
