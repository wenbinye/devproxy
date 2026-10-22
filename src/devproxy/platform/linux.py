"""Linux 平台实现：systemd(user) / setcap / update-ca-certificates / NSS / resolved。"""

from __future__ import annotations

import os
import shutil
import subprocess
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
        """通过临时文件 + sudo cp 写入，避免 shell 引号问题。"""
        tmp = path.with_name(path.name + ".devproxy.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(content, encoding="utf-8")
        code, msg = self.run(["cp", str(tmp), str(path)])
        tmp.unlink(missing_ok=True)
        if code != 0:
            raise PermissionError(f"写入 {path} 失败：{msg}")

    def read(self, path: Path) -> str:
        code, out = self.run(["cat", str(path)])
        if code != 0:
            raise FileNotFoundError(path)
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
        _run(["systemctl", "--user", "daemon-reload"])

    def enable_service(self, name: str) -> None:
        _run(["systemctl", "--user", "enable", "--now", name])

    def restart_service(self, name: str) -> None:
        _run(["systemctl", "--user", "restart", name])

    def tail_service_log(self, name: str) -> None:
        _run(["journalctl", "--user", "-u", name, "-f"])

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
