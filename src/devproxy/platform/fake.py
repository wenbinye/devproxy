"""假平台：测试与 dry-run 用。不碰真实系统。"""

from __future__ import annotations

from pathlib import Path

from devproxy.platform.base import ElevatedSession


class FakeElevated:
    def __init__(self, platform: "FakePlatform") -> None:
        self.platform = platform

    def run(self, argv: list[str]) -> tuple[int, str]:
        self.platform.commands.append(list(argv))
        return 0, ""

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.platform.written[path] = content

    def read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def copy(self, src: Path, dest: Path) -> None:
        """与 write 保持一致：真实落盘，并记录 commands 与 written。"""
        content = src.read_text(encoding="utf-8")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        self.platform.commands.append(["cp", str(src), str(dest)])
        self.platform.written[dest] = content


class FakePlatform:
    name = "fake"
    dns_port = 5353

    def __init__(self) -> None:
        self.services: dict[str, str] = {}
        self.active: set[str] = set()
        self.trusted: set[Path] = set()
        self.nss: set[Path] = set()
        self.dns_backends: set[str] = set()
        self.commands: list[list[str]] = []
        self.written: dict[Path, str] = {}
        self._elevated: FakeElevated | None = None

    def privilege(self) -> ElevatedSession:
        if self._elevated is None:
            self._elevated = FakeElevated(self)
        return self._elevated

    def run(self, argv: list[str]) -> tuple[int, str]:
        self.commands.append(list(argv))
        return 0, ""

    def binary_version(self, binary: Path) -> str:
        return "v2.11.7"

    def service_unit_path(self, name: str) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"

    def service_active(self, name: str) -> bool:
        return name in self.active

    def install_service(self, name: str, unit: str) -> None:
        self.services[name] = unit

    def enable_service(self, name: str) -> None:
        self.active.add(name)

    def restart_service(self, name: str) -> None:
        self.active.add(name)

    def tail_service_log(self, name: str) -> None:
        pass

    def ca_trusted(self, ca: Path) -> bool:
        return ca in self.trusted

    def nss_trusted(self, ca: Path) -> bool:
        """NSS 库是另一份信任库，单独记 —— 与系统信任库互不相干。"""
        return ca in self.nss

    def dns_route_installed(self, backend: str) -> bool:
        return backend in self.dns_backends
