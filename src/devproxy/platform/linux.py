"""Linux 平台实现：systemd(user) / setcap / update-ca-certificates / NSS / resolved。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from devproxy.platform.base import ElevatedSession


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError:
        return 127, f"命令不存在: {argv[0]}"
    return proc.returncode, (proc.stdout or proc.stderr).strip()


class LinuxElevated:
    """提权会话。构造时执行一次 `sudo -v` 完成预认证。"""

    def __init__(self) -> None:
        self._authenticated = False
        self._authenticate()

    def _authenticate(self) -> None:
        code, msg = _run(["sudo", "-v"])
        if code != 0:
            raise PermissionError(f"提权失败：{msg}")
        self._authenticated = True

    def run(self, argv: list[str]) -> tuple[int, str]:
        return _run(["sudo", *argv])

    def write(self, path: Path, content: str) -> None:
        """通过系统临时目录中的文件 + sudo cp 写入，避免 shell 引号问题。

        临时文件必须放在用户可写的系统临时目录（目标目录本身可能不可写），
        且无论成功失败都要清理。
        """
        fd, tmp_name = tempfile.mkstemp(prefix="devproxy-elev-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            code, msg = self.run(["cp", str(tmp), str(path)])
            if code != 0:
                raise PermissionError(f"写入 {path} 失败: {msg}")
        finally:
            tmp.unlink(missing_ok=True)

    def read(self, path: Path) -> str:
        """读取特权文件。仅当输出表明文件不存在时抛 FileNotFoundError，
        其余失败（如权限被拒）抛 PermissionError。"""
        code, out = self.run(["cat", str(path)])
        if code != 0:
            if "No such file" in out:
                raise FileNotFoundError(path)
            raise PermissionError(f"读取 {path} 失败: {out}")
        return out

    def copy(self, src: Path, dest: Path) -> None:
        code, msg = self.run(["cp", str(src), str(dest)])
        if code != 0:
            raise PermissionError(f"复制 {src} → {dest} 失败：{msg}")


class LinuxPlatform:
    name = "linux"
    dns_port = 5353

    def __init__(self, user_home: Path | None = None) -> None:
        self.user_home = user_home or Path.home()
        self._elevated: LinuxElevated | None = None

    def privilege(self) -> ElevatedSession:
        if self._elevated is None:
            self._elevated = LinuxElevated()
        return self._elevated

    def run(self, argv: list[str]) -> tuple[int, str]:
        return _run(argv)

    def binary_version(self, binary: Path) -> str:
        if not binary.exists():
            return ""
        code, out = _run([str(binary), "version"])
        if code != 0:
            return ""
        return out.split()[0] if out else ""

    def service_unit_path(self, name: str) -> Path:
        return self.user_home / ".config" / "systemd" / "user" / f"{name}.service"

    def service_active(self, name: str) -> bool:
        code, out = _run(["systemctl", "--user", "is-active", name])
        return code == 0 and out.strip() == "active"

    def install_service(self, name: str, unit: str) -> None:
        path = self.service_unit_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unit, encoding="utf-8")
        code, msg = _run(["systemctl", "--user", "daemon-reload"])
        if code != 0:
            raise RuntimeError(f"安装 {name} 后 systemctl daemon-reload 失败: {msg}")

    def enable_service(self, name: str) -> None:
        code, msg = _run(["systemctl", "--user", "enable", "--now", name])
        if code != 0:
            raise RuntimeError(f"systemctl enable --now {name} 失败: {msg}")

    def restart_service(self, name: str) -> None:
        code, msg = _run(["systemctl", "--user", "restart", name])
        if code != 0:
            raise RuntimeError(f"systemctl restart {name} 失败: {msg}")

    def tail_service_log(self, name: str) -> None:
        """流式跟随服务日志：继承终端 stdio（不捕获），Ctrl-C 自然传播。"""
        subprocess.run(["journalctl", "--user", "-u", name, "-f"])

    def ca_trusted(self, ca: Path) -> bool:
        dest = Path("/usr/local/share/ca-certificates/devproxy-local.crt")
        if not dest.exists():
            return False
        try:
            return dest.read_text(encoding="utf-8") == ca.read_text(encoding="utf-8")
        except OSError:
            return False

    def dns_route_installed(self, backend: str) -> bool:
        if backend == "resolved":
            return Path("/etc/systemd/resolved.conf.d/devproxy.conf").exists()
        if backend == "system":
            return Path("/etc/dnsmasq.d/devproxy.conf").exists()
        return False
