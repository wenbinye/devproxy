"""平台能力协议。组件层只依赖这些能力，不直接调用系统命令。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class Status:
    """组件检查结果。ok=False 时 detail 说明现状，hint 给出可执行的修复命令。"""

    ok: bool
    detail: str = ""
    hint: str = ""


@dataclass
class Action:
    """一条待执行动作。"""

    description: str
    kind: str = "run"  # "run" | "write"
    command: list[str] = field(default_factory=list)
    path: Path | None = None
    content: str = ""
    privileged: bool = False


@runtime_checkable
class ElevatedSession(Protocol):
    """一次提权会话：一次密码验证，后续操作复用同一会话。"""

    def run(self, argv: list[str]) -> tuple[int, str]: ...

    def write(self, path: Path, content: str) -> None: ...

    def read(self, path: Path) -> str: ...

    def copy(self, src: Path, dest: Path) -> None: ...


@runtime_checkable
class Platform(Protocol):
    name: str
    dns_port: int

    def privilege(self) -> ElevatedSession: ...

    def run(self, argv: list[str]) -> tuple[int, str]: ...

    def binary_version(self, binary: Path) -> str: ...

    def service_active(self, name: str) -> bool: ...

    def install_service(self, name: str, unit: str) -> None: ...

    def enable_service(self, name: str) -> None: ...

    def restart_service(self, name: str) -> None: ...

    def service_unit_path(self, name: str) -> Path: ...

    def tail_service_log(self, name: str) -> None: ...

    def ca_trusted(self, ca: Path) -> bool: ...

    def dns_route_installed(self, backend: str) -> bool: ...
