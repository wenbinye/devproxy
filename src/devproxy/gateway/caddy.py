"""与 Caddy 交互：Admin API 客户端、配置校验、热加载、主 Caddyfile 生成。"""

from __future__ import annotations

import http.client
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
                status = r.status
        except urllib.error.HTTPError as e:
            return e.code, None
        except (OSError, http.client.HTTPException):
            # URLError 是 OSError 的子类；HTTPException（BadStatusLine /
            # IncompleteRead 等）不是 —— admin 端口被非 HTTP 服务占用时
            # 必须同样返回 (0, None)，不能穿出 api() 的「不抛异常」契约
            return 0, None
        try:
            return status, (json.loads(raw) if raw else None)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 200 但响应体不是 JSON —— 契约是 (code, body)，不能裸抛
            return status, None

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
        return self._run("validate", caddyfile or self.caddyfile, False, timeout)

    def reload(self, timeout: float = 30.0) -> tuple[bool, str]:
        """热加载配置；二进制缺失/不可执行或超时同样返回 (False, 说明)，不抛异常。"""
        return self._run("reload", self.caddyfile, True, timeout)

    def _run(
        self, subcommand: str, path: Path, with_address: bool, timeout: float
    ) -> tuple[bool, str]:
        """validate / reload 的共同实现：拼参数、执行、把失败映射成 (False, 说明)。"""
        argv = [str(self.caddy_bin), subcommand, "--config", str(path)]
        if with_address:
            argv += ["--address", self.admin_addr]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except OSError as e:
            # 文件缺失 / 不可执行 / 格式不对（Exec format error）都归到这里
            return False, f"caddy 二进制不存在或不可执行: {self.caddy_bin}（{e}）"
        except subprocess.TimeoutExpired:
            return False, f"caddy {subcommand} 超时（{timeout}s）"
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def _caddy_token(value: str) -> str:
    """把值渲染成 Caddyfile 的单个 token。

    路径/地址会直接进入 Caddyfile 这种配置语言：含空白时若不引用，会被
    拆成多个 token（`/Volumes/My Disk`）；含换行时甚至能注入额外的配置行。
    简单 token 原样输出（保持既有生成结果逐字节不变），其余用 JSON 字符串
    引用 —— JSON 会把引号与换行转义，Caddy 的词法器只把 `\\"` 当转义，
    未知转义留在 token 内，因此换行永远不会变成真正的行分隔符。
    """
    safe = value and all(
        not ch.isspace() and ch not in '"`{}#' and 0x20 <= ord(ch) < 0x7F
        for ch in value
    )
    return value if safe else json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class MainCaddyfileSpec:
    """生成主 Caddyfile 所需的全部输入。

    参数收敛成一个值对象：调用点不会再把两个同类型的路径传反，
    新增字段也只需改这里。
    """

    admin_addr: str
    storage: Path
    routes_caddy: Path
    sites_caddy: Path
    http_port: int = 80
    https_port: int = 443


def build_main_caddyfile(spec: MainCaddyfileSpec) -> str:
    """生成主 Caddyfile：唯一全局块 + 两个 import。

    被 import 的文件不能含全局块 —— 全局块只能有一个。

    http_port / https_port 是 Caddy 的全局选项，写在这里才真正生效；
    不写就永远是 Caddy 的默认 80/443，配置里的这两个键形同虚设。
    默认值就是 80/443，所以生成结果与旧行为一致。
    """
    return (
        "{\n"
        f"\tadmin {_caddy_token(str(spec.admin_addr))}\n"
        f"\tstorage file_system {_caddy_token(str(spec.storage))}\n"
        f"\thttp_port {spec.http_port}\n"
        f"\thttps_port {spec.https_port}\n"
        "}\n"
        "\n"
        f"import {_caddy_token(str(spec.routes_caddy))}\n"
        f"import {_caddy_token(str(spec.sites_caddy))}\n"
    )
