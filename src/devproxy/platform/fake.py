"""假平台：测试与 dry-run 用。不碰真实系统。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from devproxy.platform.base import ElevatedSession


def _certutil_opt(argv: list[str], name: str) -> str | None:
    """取 certutil 的选项值（`-i <路径>` / `-n <昵称>`）。"""
    if name not in argv:
        return None
    index = argv.index(name)
    if index + 1 >= len(argv):
        return None
    return argv[index + 1]


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
        # NSS 条目的昵称 → 证书路径（模拟 certutil 的 -A / -D）
        self.nss_nicknames: dict[str, Path] = {}
        self.dns_backends: set[str] = set()
        self.commands: list[list[str]] = []
        self.written: dict[Path, str] = {}
        self._elevated: FakeElevated | None = None
        # auto DNS 后端：默认模拟 resolved 在运行
        self.resolved_running = True
        # linger：默认未启用，enable_linger() 后置真
        self.linger = False
        # verify() 的功能探测：可按需改这些“罐头答案”
        self.dns_answer: str | None = "127.0.0.1"
        self.dns_answers: dict[str, str | None] = {}
        self.dns_queries: list[str] = []
        self.http_result = True
        self.http_results: dict[str, bool] = {}
        self.http_probes: list[str] = []
        # 单元落盘的隔离目录 —— 绝不读写真实的 ~/.config/systemd/user
        self._unit_tmp = tempfile.TemporaryDirectory(prefix="devproxy-fake-units-")
        self.unit_dir = Path(self._unit_tmp.name)

    def privilege(self) -> ElevatedSession:
        if self._elevated is None:
            self._elevated = FakeElevated(self)
        return self._elevated

    def run(self, argv: list[str]) -> tuple[int, str]:
        self.commands.append(list(argv))
        if argv[:1] == ["certutil"]:
            self._apply_certutil(argv)
        return 0, ""

    def _apply_certutil(self, argv: list[str]) -> None:
        """把 certutil 的 -A / -D 映射到假平台的 NSS 状态上。

        真机语义：`-A` 把证书装进 NSS 库，`-D` 删掉同名条目。
        组件在安装之后会**复查**"这张 CA 到底进库了没" —— 假平台若不跟着变，
        复查永远失败，安装这一步就永远报错。
        """
        if "-A" in argv:
            src = _certutil_opt(argv, "-i")
            nickname = _certutil_opt(argv, "-n")
            if src:
                path = Path(src)
                self.nss.add(path)
                if nickname:
                    self.nss_nicknames[nickname] = path
        elif "-D" in argv:
            nickname = _certutil_opt(argv, "-n")
            path = self.nss_nicknames.pop(nickname, None) if nickname else None
            if path is not None and path not in self.nss_nicknames.values():
                self.nss.discard(path)

    def binary_version(self, binary: Path) -> str:
        return "v2.11.7"

    def service_unit_path(self, name: str) -> Path:
        return self.unit_dir / f"{name}.service"

    def service_active(self, name: str) -> bool:
        return name in self.active

    def install_service(
        self,
        name: str,
        *,
        exec_start: list[str],
        exec_reload: list[str] | None = None,
        exec_stop: list[str] | None = None,
        env: dict[str, str],
        description: str = "",
    ) -> None:
        """渲染单元并落盘到隔离目录。

        单元文本保留 systemd 的字段形态（Type= / ExecReload= / WantedBy= …），
        组件测试要断言的就是这些字段 —— 压成一行摘要会让断言失去意义。
        """
        program = Path(exec_start[0]).name if exec_start else ""
        service_type = "notify" if program.startswith("caddy") else "simple"
        lines = [
            f"# {name}",
            "[Unit]",
            f"Description={description or f'{name} (managed by devproxy)'}",
            "",
            "[Service]",
            f"Type={service_type}",
        ]
        lines += [f"Environment={k}={v}" for k, v in env.items()]
        lines.append(f"ExecStart={' '.join(exec_start)}")
        if exec_reload:
            lines.append(f"ExecReload={' '.join(exec_reload)}")
        if exec_stop:
            lines.append(f"ExecStop={' '.join(exec_stop)}")
        lines += [
            "Restart=on-failure",
            "RestartSec=2s",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
        unit = "\n".join(lines)
        self.services[name] = unit
        path = self.service_unit_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unit, encoding="utf-8")

    def enable_service(self, name: str) -> None:
        self.active.add(name)

    def restart_service(self, name: str) -> None:
        self.active.add(name)

    def tail_service_log(self, name: str) -> None:
        pass

    def enable_linger(self) -> tuple[int, str]:
        self.linger = True
        self.commands.append(["loginctl", "enable-linger"])
        return 0, ""

    def linger_enabled(self) -> bool:
        return self.linger

    def resolved_active(self) -> bool:
        return self.resolved_running

    def dns_query(self, name: str) -> str | None:
        self.dns_queries.append(name)
        return self.dns_answers.get(name, self.dns_answer)

    def http_ok(self, url: str) -> bool:
        self.http_probes.append(url)
        return self.http_results.get(url, self.http_result)

    def ca_trusted(self, ca: Path) -> bool:
        return ca in self.trusted

    def privilege_read(self, path: Path) -> str | None:
        """非交互读：普通读得到就返回，读不到返回 None（绝不提示密码）。"""
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def nss_trusted(self, ca: Path) -> bool:
        """NSS 库是另一份信任库，单独记 —— 与系统信任库互不相干。"""
        return ca in self.nss

    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        return backend in self.dns_backends
