"""Caddy 组件：二进制下载与校验、setcap 授权、systemd 单元、主 Caddyfile。

Caddy 版本固定，sha256 内置在源码里 —— 上游发布的 checksums.txt 与实际
托管的资产不一致（实测 tar.gz 与 deb 均对不上），不能信任该文件。
"""

from __future__ import annotations

import hashlib
import platform as _platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Action, Platform, Status

CADDY_VERSION = "2.11.7"

CADDY_SHA256 = {
    ("linux", "amd64"): "727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b",
    ("linux", "arm64"): "d8fc6d179a5d283028a472a5618564f6ad8a86fed513e64f032b3b0b7cc45e42",
    ("mac", "arm64"): "cda3030e5d5b13eb9f0b6fb541037f633bddd958c070c5f561d7a5cccb581665",
    ("mac", "amd64"): "110c53e5c093e4cfce6eda787f94f23bef913c309f620fab26b4f678031841ad",
}

RELEASE_BASE = "https://github.com/caddyserver/caddy/releases/download/v{version}"


def caddy_asset_name(os_name: str, arch: str, version: str = CADDY_VERSION) -> str:
    """资产名。注意 macOS 用 mac 而非 darwin。"""
    return f"caddy_{version}_{os_name}_{arch}.tar.gz"


def current_platform_key() -> tuple[str, str]:
    system = _platform.system().lower()
    os_name = "mac" if system == "darwin" else "linux"
    machine = _platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    return os_name, arch


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_caddy(dest: Path, version: str = CADDY_VERSION) -> None:
    """下载并校验后解包出 caddy 二进制。校验失败必须抛异常，不留半个文件。"""
    os_name, arch = current_platform_key()
    key = (os_name, arch)
    if key not in CADDY_SHA256:
        raise RuntimeError(f"不支持的平台: {os_name}/{arch}")
    asset = caddy_asset_name(os_name, arch, version)
    url = RELEASE_BASE.format(version=version) + "/" + asset
    with tempfile.TemporaryDirectory(prefix="devproxy-caddy-") as tmp:
        tgz = Path(tmp) / asset
        with urllib.request.urlopen(url, timeout=120) as r, open(tgz, "wb") as f:
            shutil.copyfileobj(r, f)
        actual = _sha256_file(tgz)
        expected = CADDY_SHA256[key]
        if actual != expected:
            raise RuntimeError(
                f"caddy 包校验失败（{asset}）\n  期望 sha256: {expected}\n  实际 sha256: {actual}"
            )
        with tarfile.open(tgz) as tf:
            member = tf.getmember("caddy")
            with tf.extractfile(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    dest.chmod(0o755)


def render_unit(
    *,
    caddy_bin: Path,
    caddyfile: Path,
    admin_addr: str,
    env: dict[str, str] | None = None,
) -> str:
    """生成 systemd user 单元。形态与现有的 caddy.service 保持一致。"""
    lines = [
        "[Unit]",
        "Description=Caddy local HTTP/HTTPS server (managed by devproxy)",
        "Documentation=https://caddyserver.com/docs/",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=notify",
    ]
    for k, v in (env or {}).items():
        lines.append(f"Environment={k}={v}")
    lines += [
        f"ExecStart={caddy_bin} run --config {caddyfile}",
        f"ExecReload={caddy_bin} reload --config {caddyfile} --address {admin_addr}",
        f"ExecStop={caddy_bin} stop",
        "TimeoutStopSec=5s",
        "LimitNOFILE=1048576",
        "Restart=on-failure",
        "RestartSec=2s",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


class CaddyComponent:
    name = "caddy"

    def __init__(
        self,
        platform: Platform,
        paths: Paths,
        config: Config,
        downloader: Callable[[Path], None] | None = None,
    ):
        self.platform = platform
        self.paths = paths
        self.config = config
        # 下载属网络操作，默认走真实下载器；测试可注入假实现，保持离线。
        self._downloader = downloader or download_caddy

    # -- check ---------------------------------------------------------
    def check(self) -> Status:
        binary = self.config.caddy_bin_path
        if not binary.exists():
            return Status(
                ok=False,
                detail=f"caddy 二进制不存在: {binary}",
                hint=f"devproxy setup 会自动下载 {CADDY_VERSION}",
            )
        version = self.platform.binary_version(binary)
        if version and version != f"v{CADDY_VERSION}":
            return Status(
                ok=False,
                detail=f"caddy 版本不符: 期望 v{CADDY_VERSION}，实际 {version}",
                hint="devproxy setup 会替换为固定版本",
            )
        if not self.platform.service_active("caddy"):
            return Status(
                ok=False,
                detail="caddy 服务未运行",
                hint="systemctl --user start caddy",
            )
        return Status(ok=True, detail=f"caddy {CADDY_VERSION} 已就绪")

    # -- plan ----------------------------------------------------------
    def plan(self) -> list[Action]:
        """就绪标准 = 二进制在位、版本匹配、服务 active。

        一切就绪时返回空列表；否则按缺失项给出动作。
        setcap 与下载绑定 —— 新下载的二进制才需要重新授权；
        Caddyfile 随服务安装一并生成（sites.caddy 属用户，只 import 不碰）。
        """
        actions: list[Action] = []
        binary = self.config.caddy_bin_path
        status = self.check()
        need_binary = "二进制不存在" in status.detail or "版本不符" in status.detail
        need_service = not self.platform.service_active("caddy")
        if need_binary:
            actions.append(
                Action(
                    description=f"下载 caddy {CADDY_VERSION} 并校验 sha256",
                    kind="run",
                    command=["download-caddy", str(binary)],
                )
            )
        if need_binary or need_service:
            # 没完全就绪就重新授权：上一轮下载成功但 setcap 失败（用户拒了
            # sudo）时，二进制已在位而服务没起 —— 这一轮必须补 setcap。
            actions.append(
                Action(
                    description="授权 caddy 绑定 80/443（setcap）",
                    kind="run",
                    command=["setcap", "cap_net_bind_service=+ep", str(binary)],
                    privileged=True,
                )
            )
        if need_service:
            actions.append(
                Action(
                    description="生成主 Caddyfile",
                    kind="write",
                    path=self.paths.caddyfile,
                    content=self._main_caddyfile(),
                )
            )
            actions.append(
                Action(
                    description="安装并启动 caddy.service（systemd user）",
                    kind="run",
                    command=["install-service", "caddy"],
                )
            )
        return actions

    # -- apply ---------------------------------------------------------
    def apply(self, actions: list[Action]) -> None:
        binary = self.config.caddy_bin_path
        for action in actions:
            if action.kind == "write" and action.path is not None:
                action.path.parent.mkdir(parents=True, exist_ok=True)
                action.path.write_text(action.content, encoding="utf-8")
                continue
            if action.command[:1] == ["download-caddy"]:
                # 下载属组件职责：固定版本 + sha256 校验，失败即抛，
                # 不留半个文件，后续动作（setcap 等）不会在坏二进制上执行。
                binary.parent.mkdir(parents=True, exist_ok=True)
                self._downloader(binary)
                continue
            if action.command[:1] == ["setcap"]:
                self.platform.privilege().run(action.command)
                continue
            if action.command[:1] == ["install-service"]:
                unit = render_unit(
                    caddy_bin=binary,
                    caddyfile=self.paths.caddyfile,
                    admin_addr=self.config.admin_addr,
                )
                self.platform.install_service("caddy", unit)
                self.platform.enable_service("caddy")
                continue
            raise RuntimeError(f"未知动作: {action.command}")

    # -- verify --------------------------------------------------------
    def verify(self) -> Status:
        """apply 后自检：二进制在位且服务 active，二者缺一不可。"""
        if not self.config.caddy_bin_path.exists():
            return Status(
                ok=False,
                detail=f"caddy 二进制缺失: {self.config.caddy_bin_path}",
                hint="devproxy setup 会下载；systemctl --user status caddy 查看服务",
            )
        if not self.platform.service_active("caddy"):
            return Status(
                ok=False,
                detail="caddy 服务未处于 active",
                hint="systemctl --user status caddy",
            )
        return Status(ok=True, detail="caddy 服务运行中")

    def _main_caddyfile(self) -> str:
        from devproxy.proxy.caddy import build_main_caddyfile

        return build_main_caddyfile(
            admin_addr=self.config.admin_addr,
            storage=self.config.storage_path,
            routes_caddy=self.paths.routes_caddy,
            sites_caddy=self.paths.sites_caddy,
        )
