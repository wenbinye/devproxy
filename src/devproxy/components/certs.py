"""CA 根证书信任组件。

Linux 需要两处：系统信任库（curl / Go / Java 等）与 NSS 库（Chrome/Chromium）。
macOS 只需要系统钥匙串（由 Plan 3 的 macOS 平台实现提供）。
"""

from __future__ import annotations

from pathlib import Path

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Action, Platform, Status

CA_NAME = "devproxy Local CA"
SYSTEM_CA_DEST = Path("/usr/local/share/ca-certificates/devproxy-local.crt")
NSS_DIR = Path("~/.pki/nssdb")


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

    def check(self) -> Status:
        # 信任与否以平台判定为准（平台读的是信任库里的内容）；
        # 源文件缺失只影响"还没生成 CA"这一种情形。
        if self.platform.ca_trusted(self.ca_path):
            return Status(ok=True, detail="CA 根证书已信任")
        if not self.ca_path.exists():
            return Status(
                ok=False,
                detail=f"CA 根证书不存在: {self.ca_path}",
                hint="先启动 caddy 生成内部 CA（devproxy setup 会做）",
            )
        return Status(
            ok=False,
            detail="CA 根证书未被系统信任",
            hint="devproxy trust",
        )

    def plan(self) -> list[Action]:
        if self.platform.ca_trusted(self.ca_path):
            return []
        return [
            Action(
                description="安装 CA 根证书到系统信任库并 update-ca-certificates",
                kind="run",
                command=["install-system-ca", str(self.ca_path), str(self.system_dest)],
                privileged=True,
            ),
            Action(
                description="安装 CA 根证书到 NSS 库（Chrome/Chromium）",
                kind="run",
                command=["install-nss-ca", str(self.ca_path), str(self.nss_dir)],
                # 信任库安装整体需要提权，两个动作放进同一次提权流程（规格 §6.1）。
                # 注意：NSS 库在用户家目录，执行时仍以当前用户身份写入 —— 以 root
                # 写出的 NSS 库 Chrome 读不了。
                privileged=True,
            ),
        ]

    def apply(self, actions: list[Action]) -> None:
        for action in actions:
            if action.command[:1] == ["install-system-ca"]:
                elevated = self.platform.privilege()
                src, dest = Path(action.command[1]), Path(action.command[2])
                code, msg = elevated.run(["install", "-d", str(dest.parent)])
                if code != 0:
                    raise RuntimeError(f"创建信任库目录失败: {msg}")
                elevated.copy(src, dest)
                elevated.run(["update-ca-certificates"])
                continue
            if action.command[:1] == ["install-nss-ca"]:
                src = Path(action.command[1])
                nss = Path(action.command[2]).expanduser()
                nss.mkdir(parents=True, exist_ok=True)
                code, _ = self.platform.run(["certutil", "-V"])
                if code != 0:
                    raise RuntimeError(
                        "缺少 certutil（Chrome 信任需 NSS 库）：sudo apt install libnss3-tools"
                    )
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
                continue
            raise RuntimeError(f"未知动作: {action.command}")

    def verify(self) -> Status:
        if not self.platform.ca_trusted(self.ca_path):
            return Status(ok=False, detail="CA 仍未被信任", hint="devproxy trust")
        return Status(ok=True, detail="CA 已信任")
