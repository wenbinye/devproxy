"""Caddy 组件：二进制下载与校验、setcap 授权、systemd 单元、主 Caddyfile。

Caddy 版本固定，sha256 内置在源码里 —— 上游发布的 checksums.txt 与实际
托管的资产不一致（实测 tar.gz 与 deb 均对不上），不能信任该文件。
"""

from __future__ import annotations

import hashlib
import os
import platform as _platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

from devproxy.components.base import has_localhost_site, probe_url
from devproxy.core.config import Config
from devproxy.core.caddy_env import load_extra_env
from devproxy.core.paths import Paths
from devproxy.platform.base import Action, Platform, Status, unit_exec_argv

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
    """返回 (os, arch)。未知平台直接报错，绝不悄悄退回 amd64。"""
    system = _platform.system().lower()
    machine = _platform.machine().lower()
    if system == "darwin":
        os_name = "mac"
    elif system == "linux":
        os_name = "linux"
    else:
        raise RuntimeError(f"不支持的平台: {system}/{machine}")
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64"):
        arch = "amd64"
    else:
        raise RuntimeError(f"不支持的平台: {system}/{machine}")
    return os_name, arch


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _exec_line_argv(text: str, field: str) -> list[str] | None:
    """从 systemd 单元文本里取某个 `Exec*=` 字段的 argv。

    launchd 的 plist 没有这些字段（返回 None）—— 调用方据此跳过检查。
    """
    prefix = f"{field}="
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            args = line[len(prefix):].split()
            return args or None
    return None


def _binary_version(binary: Path) -> str:
    """运行 `caddy version`，返回首个字段（如 v2.11.7）；失败返回空串。"""
    try:
        proc = subprocess.run(
            [str(binary), "version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    out = (proc.stdout or proc.stderr).strip()
    return out.split()[0] if out else ""


def download_caddy(dest: Path, version: str = CADDY_VERSION) -> None:
    """下载并校验后解包出 caddy 二进制。校验失败必须抛异常，不留半个文件。

    校验分两层：内置 sha256（下载包）与 `caddy version`（解包后的二进制）。
    落盘用同目录临时文件 + os.replace —— 旧二进制正在运行时直接覆盖会
    触发 ETXTBSY，且半写的二进制会被后续动作当成可用。
    """
    os_name, arch = current_platform_key()
    key = (os_name, arch)
    if key not in CADDY_SHA256:
        raise RuntimeError(f"不支持的平台: {os_name}/{arch}")
    asset = caddy_asset_name(os_name, arch, version)
    url = RELEASE_BASE.format(version=version) + "/" + asset
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".caddy-download-", dir=dest.parent)
    tmp_dest = Path(tmp_name)
    try:
        with tempfile.TemporaryDirectory(prefix="devproxy-caddy-") as tmp:
            tgz = Path(tmp) / asset
            with urllib.request.urlopen(url, timeout=120) as r, open(tgz, "wb") as f:
                shutil.copyfileobj(r, f)
            actual = _sha256_file(tgz)
            expected = CADDY_SHA256[key]
            if actual != expected:
                raise RuntimeError(
                    f"caddy 包校验失败（{asset}）\n"
                    f"  期望 sha256: {expected}\n  实际 sha256: {actual}"
                )
            with tarfile.open(tgz) as tf:
                member = tf.getmember("caddy")
                with tf.extractfile(member) as src, os.fdopen(fd, "wb") as out:
                    fd = -1  # 已交给 fdopen 管理，避免重复关闭
                    shutil.copyfileobj(src, out)
        tmp_dest.chmod(0o755)
        actual_version = _binary_version(tmp_dest)
        if actual_version != f"v{version}":
            raise RuntimeError(
                f"caddy 二进制版本不符: 期望 v{version}，实际 {actual_version or '无法执行'}"
            )
        os.replace(tmp_dest, dest)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp_dest.unlink(missing_ok=True)


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
        # 迁移来的环境变量（如 MINIKUBE_IP）—— 用户站点里的 {$MINIKUBE_IP:…}
        # 依赖它，重装单元时必须一并带上。
        self.extra_env: dict[str, str] = load_extra_env(paths)

    # -- 判据 ----------------------------------------------------------
    def _caddyfile_current(self) -> bool:
        """主 Caddyfile 是否存在且内容与该组件会生成的一致。"""
        expected = self._main_caddyfile()
        try:
            return (
                self.paths.caddyfile.exists()
                and self.paths.caddyfile.read_text(encoding="utf-8") == expected
            )
        except (OSError, UnicodeDecodeError):
            return False

    def _read_unit_text(self, unit: Path) -> str | None:
        """读单元内容；普通读不到时（权限不足）改走**非交互**的特权读。

        macOS 的 system scope 单元在 /Library/LaunchDaemons —— 权限或归属
        不对时普通用户读不了。"读不到"绝不等于"单元过期"：那样 doctor 会
        永远报红，setup 每次都要重装单元并再要一次密码。

        这里必须走协议里的**非交互**特权读（Linux 上是 `sudo -n`）：
        doctor 承诺只读、无副作用，绝不能因为一个读不到的单元就把密码框
        弹到用户脸上。`privilege()` 会构造交互式提权会话（可能弹密码），
        因此这里绝不回退到它。
        """
        try:
            return unit.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass
        try:
            return self.platform.privilege_read(unit)
        except (OSError, RuntimeError):
            # 平台实现自身失败也要守住 check 的"永不抛异常"契约。
            return None

    def _unit_current(self) -> bool:
        """已安装的单元是否把 ExecStart 指向本工具的主 Caddyfile 与二进制。

        只看 --config 不够：改了配置里的 caddy_bin 之后，已装单元仍指着旧
        二进制跑 —— "单元过期"永远判不出来，服务会一直用旧二进制。

        判据是**整条命令行**（argv 前缀必须完全对上），不是子串：只看
        "路径出现过"会把参数配对错乱的单元也判成当前的 —— 而"单元是不是
        当前的"正是 setup 决定要不要重装的依据。

        两种单元格式都要认：systemd 的 ExecStart= 行与 launchd 的
        ProgramArguments 数组（后者是数组，逐个元素比对）。

        ExecReload 也只对 systemd 单元检查（launchd 没有该字段）：
        `admin_addr` 变了但 ExecStart 不变时，旧单元里的 `--address`
        已经过期，单元必须重装。老版本装出的单元可能没有 ExecReload 行，
        那种情况按"兼容"放过 —— 否则升级后 doctor 会无谓地报红一次。
        """
        unit = self.platform.service_unit_path("caddy")
        if not unit.exists():
            return False
        text = self._read_unit_text(unit)
        if text is None:
            # 读不到就不能说它过期 —— 宁可放过，也不能让 doctor 永远报红。
            return True
        argv = unit_exec_argv(text)
        if argv is None:
            return False
        expected = [
            str(self.config.caddy_bin_path),
            "run",
            "--config",
            str(self.paths.caddyfile),
        ]
        if argv[: len(expected)] != expected:
            return False
        installed_reload = _exec_line_argv(text, "ExecReload")
        if installed_reload is None:
            return True
        return installed_reload == [
            str(self.config.caddy_bin_path),
            "reload",
            "--config",
            str(self.paths.caddyfile),
            "--address",
            self.config.admin_addr,
        ]

    def _need_binary(self, version: str | None = None) -> bool:
        """二进制判据：不存在，或版本与固定版本不符（含"版本读不出来"）。

        check 与 plan 必须共用它 —— 否则同一行既写"已就绪"又写"下载"，
        而且每次 setup 都会重复下载。`version` 可由调用方传入已测得的
        结果，避免同一轮里再跑一次 `caddy version`。
        """
        binary = self.config.caddy_bin_path
        if not binary.exists():
            return True
        if version is None:
            version = self.platform.binary_version(binary)
        return version != f"v{CADDY_VERSION}"

    def _service_hint(self) -> str:
        """服务相关的修复提示（按平台给不同的命令）。"""
        if self.platform.name == "mac":
            return "launchctl print system/com.devproxy.caddy"
        return "systemctl --user status caddy"

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
        if self._need_binary(version):
            return Status(
                ok=False,
                detail=(
                    f"caddy 版本不符: 期望 v{CADDY_VERSION}，"
                    f"实际 {version or '无法读取'}"
                ),
                hint="devproxy setup 会替换为固定版本",
            )
        if not self._caddyfile_current():
            return Status(
                ok=False,
                detail=f"主 Caddyfile 不存在或已过期: {self.paths.caddyfile}",
                hint="devproxy setup 会重新生成主 Caddyfile",
            )
        if not self._unit_current():
            return Status(
                ok=False,
                detail="caddy 服务单元未指向本工具的 Caddyfile 或二进制",
                hint=(
                    "devproxy setup 会重装 caddy LaunchDaemon"
                    if self.platform.name == "mac"
                    else "devproxy setup 会重装 systemd user 单元"
                ),
            )
        if not self.platform.service_active("caddy"):
            return Status(
                ok=False,
                detail="caddy 服务未运行",
                hint=(
                    "launchctl kickstart -k system/com.devproxy.caddy"
                    if self.platform.name == "mac"
                    else "systemctl --user start caddy"
                ),
            )
        # linger 也必须是就绪判据的一部分：没开 linger 时用户服务不会
        # 开机自启，重启机器后 caddy 根本不在了 —— 与 plan 的判据保持一致。
        if not self.platform.linger_enabled():
            return Status(
                ok=False,
                detail="linger 未启用（用户服务不会开机自启）",
                hint="devproxy setup（会执行 loginctl enable-linger，需要 sudo）",
            )
        return Status(ok=True, detail=f"caddy {CADDY_VERSION} 已就绪")

    # -- plan ----------------------------------------------------------
    def plan(self) -> list[Action]:
        """按缺失项给出动作；全部就绪时返回空列表。

        主 Caddyfile 与单元内容都是**独立判据**：本机就出现过二进制度对、
        服务 active，但单元仍指向旧 Caddyfile、主 Caddyfile 根本不存在，
        导致 setup 报“已就绪”而 up 时 reload 必然失败。
        setcap 与下载/服务绑定 —— 新下载的二进制或服务没起来时才需要重新授权。
        """
        binary = self.config.caddy_bin_path
        # 一次快照：同一轮里重复探测会在状态变化时给出自相矛盾的分支。
        version = self.platform.binary_version(binary) if binary.exists() else ""
        need_binary = self._need_binary(version)
        need_caddyfile = not self._caddyfile_current()
        need_unit = not self._unit_current()
        caddy_active = self.platform.service_active("caddy")
        need_service = not caddy_active
        need_linger = not self.platform.linger_enabled()

        actions: list[Action] = []
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
            # macOS 没有 setcap：绑 80/443 靠以 root 运行的 LaunchDaemon。
            if self.platform.name != "mac":
                actions.append(
                    Action(
                        description="授权 caddy 绑定 80/443（setcap）",
                        kind="run",
                        command=["setcap", "cap_net_bind_service=+ep", str(binary)],
                        privileged=True,
                    )
                )
        if need_caddyfile:
            actions.append(
                Action(
                    description="生成主 Caddyfile",
                    kind="write",
                    path=self.paths.caddyfile,
                    content=self._main_caddyfile(),
                )
            )
        if need_service or need_unit:
            actions.append(
                Action(
                    description=(
                        "安装并启动 caddy LaunchDaemon（macOS）"
                        if self.platform.name == "mac"
                        else "安装并启动 caddy.service（systemd user）"
                    ),
                    kind="run",
                    command=["install-service", "caddy"],
                    # macOS 的 caddy 是 /Library/LaunchDaemons 下的 root 守护进程
                    # （绑 80/443 必须 root）；Linux 是 ~/.config/systemd/user 下的
                    # **用户** unit，不需要提权。标错方向的两种后果都不小：
                    # 漏标 → sudo 清单谎报、非交互护栏被绕过、中途弹密码框；
                    # 多标 → Linux 上凭空多一项 sudo，非交互环境误退 4。
                    privileged=self.platform.name == "mac",
                )
            )
        if need_linger:
            actions.append(
                Action(
                    description="启用 linger（用户服务开机自启，无需登录）",
                    kind="run",
                    command=["enable-linger"],
                    privileged=True,
                )
            )
        if (need_binary or need_caddyfile) and caddy_active and not need_unit:
            # 运行中的 Caddy 不会自己发现二进制或主 Caddyfile 变了：
            # - 换了二进制：跑着的还是旧版本；
            # - 只改了主 Caddyfile（http_port / admin_addr / storage …）：
            #   单元与服务都没变，只写文件等于什么都没生效。
            # 单元/服务本身要重装的不在这里重启 —— install-service 分支
            # 会处理（was_active → restart），避免重复重启两次。
            # 放在最后：setcap 必须先做完，否则重启后绑不上 80/443。
            if need_binary and need_caddyfile:
                description = "重启 caddy 服务以使用新二进制与新主 Caddyfile"
            elif need_binary:
                description = f"重启 caddy 服务以使用新下载的 {CADDY_VERSION}"
            else:
                description = "重启 caddy 服务以加载新的主 Caddyfile"
            actions.append(
                Action(
                    description=description,
                    kind="run",
                    command=["restart-service", "caddy"],
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
                code, msg = self.platform.privilege().run(action.command)
                if code != 0:
                    raise RuntimeError(
                        "setcap 失败（caddy 无法绑定 80/443）："
                        f"{msg}；请先安装 libcap2-bin（sudo apt install libcap2-bin）"
                    )
                continue
            if action.command[:1] == ["install-service"]:
                was_active = self.platform.service_active("caddy")
                self.platform.install_service(
                    "caddy",
                    exec_start=[
                        str(binary),
                        "run",
                        "--config",
                        str(self.paths.caddyfile),
                    ],
                    exec_reload=[
                        str(binary),
                        "reload",
                        "--config",
                        str(self.paths.caddyfile),
                        "--address",
                        self.config.admin_addr,
                    ],
                    exec_stop=[str(binary), "stop"],
                    env=self.extra_env,
                    description="Caddy local HTTP/HTTPS server (managed by devproxy)",
                )
                self.platform.enable_service("caddy")
                if was_active:
                    # 单元文件已换（如 ExecStart 指向新 Caddyfile），
                    # enable --now 对已 active 的单元不会重启，必须显式 restart。
                    self.platform.restart_service("caddy")
                continue
            if action.command[:1] == ["enable-linger"]:
                code, msg = self.platform.enable_linger()
                if code != 0:
                    raise RuntimeError(f"loginctl enable-linger 失败: {msg}")
                continue
            if action.command[:2] == ["restart-service", "caddy"]:
                self.platform.restart_service("caddy")
                continue
            raise RuntimeError(f"未知动作: {action.command}")

    def _has_configured_site(self) -> bool:
        """是否有任何真实站点（注释/空行不算）。

        干净机器上 Caddyfile 只 import 两个空文件，Caddy 不会监听 443 ——
        此时 TLS 探测不可执行，应保持“ok + 提示”而不是把 setup 判成失败。
        """
        for path in (self.paths.routes_caddy, self.paths.sites_caddy):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(
                line.strip() and not line.strip().startswith("#")
                for line in text.splitlines()
            ):
                return True
        return False

    def _has_localhost_site(self) -> bool:
        """sites.caddy 里是否有 localhost 站点块（精确匹配站点地址）。"""
        return has_localhost_site(self.paths.sites_caddy)

    def _probe_url(self) -> str | None:
        """TLS 探测目标：优先首条已注册路由，其次确有 localhost 站点时的 localhost。

        判据由 `components.base.probe_url` 统一提供 —— 证书组件用的是
        同一份实现，避免两处逻辑再次漂移。没有可探目标时返回 None。
        """
        return probe_url(self.paths.routes_json, self.paths.sites_caddy)

    # -- verify --------------------------------------------------------
    def verify(self) -> Status:
        """apply 后自检：二进制、服务 active、且真的能完成 TLS 握手。"""
        if not self.config.caddy_bin_path.exists():
            return Status(
                ok=False,
                detail=f"caddy 二进制缺失: {self.config.caddy_bin_path}",
                hint=f"devproxy setup 会下载；{self._service_hint()} 查看服务",
            )
        if not self.platform.service_active("caddy"):
            return Status(
                ok=False,
                detail="caddy 服务未处于 active",
                hint=self._service_hint(),
            )
        if not self._has_configured_site():
            return Status(
                ok=True,
                detail="caddy 服务运行中（尚无站点，跳过 TLS 探测）",
            )
        url = self._probe_url()
        if url is None:
            return Status(
                ok=True,
                detail="caddy 服务运行中（无路由且无 localhost 站点，跳过 TLS 探测）",
            )
        if not self.platform.http_ok(url):
            return Status(
                ok=False,
                detail=f"{url} TLS 握手失败",
                hint=f"{self._service_hint()}；检查 Caddyfile 与 443 端口占用",
            )
        return Status(ok=True, detail=f"caddy 服务运行中，{url} 可访问")

    def _main_caddyfile(self) -> str:
        from devproxy.gateway.caddy import MainCaddyfileSpec, build_main_caddyfile

        return build_main_caddyfile(
            MainCaddyfileSpec(
                admin_addr=self.config.admin_addr,
                storage=self.config.storage_path,
                routes_caddy=self.paths.routes_caddy,
                sites_caddy=self.paths.sites_caddy,
                http_port=self.config.http_port,
                https_port=self.config.https_port,
            )
        )
