"""CA 根证书信任组件。

Linux 需要两处：系统信任库（curl / Go / Java 等）与 NSS 库（Chrome/Chromium）。
两者是**独立的两步**：只装系统信任库，Chrome 照样不认；只装 NSS 库，curl 又不认。
macOS 只需要系统钥匙串（由 Plan 3 的 macOS 平台实现提供）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import (
    CA_NICKNAME,
    NSS_DIR,
    SYSTEM_CA_DEST,
    Action,
    Platform,
    Status,
)

CA_NAME = CA_NICKNAME


class CertComponent:
    name = "cert"

    def __init__(
        self,
        platform: Platform,
        paths: Paths,
        config: Config,
        system_dest: Path | None = None,
        nss_dir: Path | None = None,
    ):
        """system_dest / nss_dir 允许注入 —— 测试用 tmp_path，绝不写真实系统。"""
        self.platform = platform
        self.paths = paths
        self.config = config
        self.system_dest = Path(system_dest) if system_dest else SYSTEM_CA_DEST
        self.nss_dir = Path(nss_dir) if nss_dir else NSS_DIR

    @property
    def ca_path(self) -> Path:
        return self.config.storage_path / "pki" / "authorities" / "local" / "root.crt"

    # -- 平台判定（永不抛异常） ---------------------------------------------

    @staticmethod
    def _trusted(checker, ca: Path) -> bool:
        """check() 永不抛异常的前提：平台判定失败一律当作"没信任"。"""
        try:
            return bool(checker(ca))
        except Exception:  # noqa: BLE001 —— 判定异常不能带崩 doctor
            return False

    def _system_trusted(self) -> bool:
        checker = getattr(self.platform, "ca_trusted", None)
        if checker is None:
            return False
        return self._trusted(checker, self.ca_path)

    def _nss_trusted(self) -> bool:
        # 平台协议可能有变（Plan 3 的 macOS 实现）；属性访问本身也要在
        # try 之外兜住的话，缺方法会让 check() 抛异常 —— doctor 就炸了。
        checker = getattr(self.platform, "nss_trusted", None)
        if checker is None:
            return False
        return self._trusted(checker, self.ca_path)

    # -- check ---------------------------------------------------------

    def check(self) -> Status:
        # CA 根证书是 Caddy 的内部 CA：只有在有站点用 tls internal 时才生成。
        # 首次部署时它还不存在 —— 这是"还没到那一步"，不是缺陷。
        if not self.ca_path.exists():
            return Status(
                ok=True, detail="尚无 CA 根证书（将在首条路由注册时由 Caddy 生成）"
            )
        missing = []
        if not self._system_trusted():
            missing.append("系统信任库")
        if not self._nss_trusted():
            missing.append("NSS 库")
        if missing:
            return Status(
                ok=False,
                detail=f"CA 根证书未被信任（缺少：{'、'.join(missing)}）",
                hint="devproxy trust",
            )
        return Status(ok=True, detail="CA 根证书已信任（系统信任库 + NSS 库）")

    # -- plan ----------------------------------------------------------

    def plan(self) -> list[Action]:
        """CA 还没生成时无事可做；否则按缺哪补哪。

        非特权的那步排在前面：用户若拒绝 sudo，NSS（Chrome 信任）仍能装上。
        """
        if not self.ca_path.exists():
            return []
        actions: list[Action] = []
        if not self._nss_trusted():
            actions.append(
                Action(
                    description="安装 CA 根证书到 NSS 库（Chrome/Chromium）",
                    kind="run",
                    command=["install-nss-ca", str(self.ca_path), str(self.nss_dir)],
                    # NSS 库在当前用户家目录，不需要 root
                    privileged=False,
                )
            )
        if not self._system_trusted():
            actions.append(
                Action(
                    description="安装 CA 根证书到系统信任库并 update-ca-certificates",
                    kind="run",
                    command=[
                        "install-system-ca",
                        str(self.ca_path),
                        str(self.system_dest),
                    ],
                    privileged=True,
                )
            )
        return actions

    # -- apply ---------------------------------------------------------

    def apply(self, actions: list[Action]) -> None:
        """两个信任库互相独立：任一步失败，另一步仍要跑完。

        失败不在中途抛出，最后汇总成一条错误 —— 否则先跑的 NSS 步一旦
        失败（如缺 certutil），系统信任库就永远装不上，反之亦然。
        """
        failures: list[tuple[Action, Exception]] = []
        for action in actions:
            try:
                self._apply_one(action)
            except Exception as e:  # noqa: BLE001 —— 先收集，最后统一报
                failures.append((action, e))
        if failures:
            detail = "；".join(f"{a.description}（{e}）" for a, e in failures)
            message = f"{len(failures)} 个步骤失败：{detail}"
            succeeded = len(actions) - len(failures)
            if succeeded:
                message += f"；其余 {succeeded} 个步骤已执行"
            # 提权失败要保留类型：CLI 据此给退出码 4（需要提权），而不是 1
            if all(isinstance(e, PermissionError) for _, e in failures):
                raise PermissionError(message)
            raise RuntimeError(message)

    def _apply_one(self, action: Action) -> None:
        if action.command[:1] == ["install-system-ca"]:
            elevated = self.platform.privilege()
            src, dest = Path(action.command[1]), Path(action.command[2])
            code, msg = elevated.run(["install", "-d", str(dest.parent)])
            if code != 0:
                raise RuntimeError(f"创建信任库目录失败: {msg}")
            elevated.copy(src, dest)
            code, msg = elevated.run(["update-ca-certificates"])
            if code != 0:
                # 不检查返回码的话，CA 只是躺在暂存目录里，系统并不认它
                raise RuntimeError(
                    f"update-ca-certificates 失败（CA 未进入系统信任库）: {msg}"
                )
            return
        if action.command[:1] == ["install-nss-ca"]:
            src = Path(action.command[1])
            nss = Path(action.command[2]).expanduser()
            # 探测只能看二进制在不在：certutil -V 需要 -n，真实机器上永远非零
            if shutil.which("certutil") is None:
                raise RuntimeError(
                    "缺少 certutil（Chrome 信任需 NSS 库）：sudo apt install libnss3-tools"
                )
            nss.mkdir(parents=True, exist_ok=True)
            # 库还不存在时先建一个空库，否则 certutil -A 会失败
            if not (nss / "cert9.db").exists():
                code, msg = self.platform.run(
                    ["certutil", "-N", "-d", f"sql:{nss}", "--empty-password"]
                )
                if code != 0:
                    raise RuntimeError(f"创建 NSS 库失败: {msg}")
            # 先删同名再装，保证幂等
            self.platform.run(["certutil", "-d", f"sql:{nss}", "-D", "-n", CA_NAME])
            code, msg = self.platform.run(
                [
                    "certutil",
                    "-d",
                    f"sql:{nss}",
                    "-A",
                    "-n",
                    CA_NAME,
                    "-t",
                    "C,,",
                    "-i",
                    str(src),
                ]
            )
            if code != 0:
                raise RuntimeError(f"安装 NSS 证书失败: {msg}")
            return
        raise RuntimeError(f"未知动作: {action.command}")

    # -- verify --------------------------------------------------------

    def _probe_url(self) -> str:
        """真实 HTTPS 探测的 URL：优先首条已注册路由，其次 localhost。

        用 localhost 去验一个只有 codeup.test 的机器会因“无 localhost 站点”
        而握手失败 —— 那验的是站点匹配，不是证书信任链。
        """
        try:
            data = json.loads(self.paths.routes_json.read_text(encoding="utf-8"))
            for route in data.get("routes") or []:
                domain = route.get("domain") if isinstance(route, dict) else None
                if isinstance(domain, str) and domain:
                    return f"https://{domain}"
        except (OSError, ValueError, AttributeError):
            pass
        return "https://localhost"

    def verify(self) -> Status:
        if not self.ca_path.exists():
            return Status(ok=True, detail="尚无 CA 根证书（跳过验证）")
        if not (self._system_trusted() and self._nss_trusted()):
            return Status(ok=False, detail="CA 仍未被完全信任", hint="devproxy trust")
        # openssl 验证信任链只是第一步；再发一次真实 HTTPS 请求确认端到端可用。
        url = self._probe_url()
        if not self.platform.http_ok(url):
            return Status(
                ok=False,
                detail=f"CA 已进信任库，但 {url} 仍握手失败",
                hint="devproxy trust；systemctl --user status caddy",
            )
        return Status(ok=True, detail=f"CA 已信任，{url} 握手成功")
