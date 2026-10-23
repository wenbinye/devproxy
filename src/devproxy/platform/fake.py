"""假平台：测试与 dry-run 用。不碰真实系统。

"不碰真实系统"对**提权会话**同样成立：`FakeElevated` 的 write / read /
copy 一律经过 `FakePlatform._sandbox_target()`，把 /etc、/usr/local/share、
/Library/LaunchDaemons 这类系统路径映射到沙箱目录。只有测试自己的临时
目录（pytest 的 tmp_path、TemporaryDirectory）允许真实落盘，用例才能
读回内容做断言。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from devproxy.platform.base import ElevatedSession, nss_db_dir


def _certutil_opt(argv: list[str], name: str) -> str | None:
    """取 certutil 的选项值（`-i <路径>` / `-n <昵称>` / `-d <库>` / `-t <标志>`）。"""
    if name not in argv:
        return None
    index = argv.index(name)
    if index + 1 >= len(argv):
        return None
    return argv[index + 1]


def _norm(path: Path | str) -> Path:
    """规范化路径（不解析软链，只消除 `..` 与多余分隔符）。"""
    return Path(os.path.normpath(str(path)))


def _read_bytes(path: Path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _flags_ssl_trusted(flags: str) -> bool:
    """与 `linux._nss_flags_trusted` 同一语义：只看 SSL 列是否 "C"。"""
    return bool(flags) and flags[0] == "C"


def _repair_flags(flags: str) -> str:
    """与 `linux._nss_repair_flags` 同一语义：只把 SSL 列补成 "C"。"""
    parts = flags.split(",")
    if len(parts) != 3:
        return flags
    return ",".join(["C", parts[1], parts[2]])


class FakeElevated:
    def __init__(self, platform: "FakePlatform") -> None:
        self.platform = platform

    def run(self, argv: list[str]) -> tuple[int, str]:
        self.platform.commands.append(list(argv))
        if argv[:1] == ["rm"]:
            # 模拟 `rm -f/-rf -- <path>`：卸载流程靠它删除 /etc 下的
            # DNS drop-in 与系统信任库文件，假平台不模拟就永远删不掉。
            for raw in argv[1:]:
                if raw.startswith("-"):
                    continue
                target = self.platform._sandbox_target(Path(raw))
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
        return 0, ""

    def write(self, path: Path, content: str) -> None:
        target = self.platform._sandbox_target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.platform.written[Path(path)] = content

    def read(self, path: Path) -> str:
        return self.platform._sandbox_target(path).read_text(encoding="utf-8")

    def copy(self, src: Path, dest: Path) -> None:
        """与 write 保持一致：真实落盘（沙箱内），并记录 commands 与 written。

        系统 CA 安装走这条路径：复制成功后立刻把源 CA 记入 `trusted`，
        让"安装 → 复查"这条链路在假平台上真的闭合（而不是靠测试提前
        预置 `trusted` 绕过）。
        """
        content = Path(src).read_text(encoding="utf-8")
        target = self.platform._sandbox_target(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.platform.commands.append(["cp", str(src), str(dest)])
        self.platform.written[Path(dest)] = content
        self.platform.trusted.add(Path(src))


class FakePlatform:
    name = "fake"
    dns_port = 5353

    def __init__(self) -> None:
        self.services: dict[str, str] = {}
        self.active: set[str] = set()
        self.trusted: set[Path] = set()
        # 兼容旧测试：直接往里加 CA 表示"NSS 判定已信任"。
        self.nss: set[Path] = set()
        # 更真实的 NSS 状态：(库路径, 昵称) -> (证书路径, 三列信任标志)。
        # 组件用 `-A/-D/-M` 改的就是这张表，`nss_trusted` 按内容 + SSL 列判定。
        self.nss_entries_db: dict[tuple[Path, str], tuple[Path, str]] = {}
        self.dns_backends: set[str] = set()
        self.commands: list[list[str]] = []
        self.written: dict[Path, str] = {}
        self._elevated: FakeElevated | None = None
        # 平台默认 NSS 库。测试把组件指向别的库时同步改这个属性，
        # `CertComponent._require_same_nss_db` 才会认为两边一致。
        self.nss_db = _norm(nss_db_dir())
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
        # 特权路径的沙箱根 —— 系统绝对路径一律映射到这里
        self._sandbox_tmp = tempfile.TemporaryDirectory(prefix="devproxy-fake-root-")
        self.sandbox = Path(self._sandbox_tmp.name)

    # -- 路径隔离 -------------------------------------------------------

    def _sandbox_target(self, path: Path) -> Path:
        """把提权路径映射到安全位置。

        - 测试自己的临时目录（pytest tmp_path / TemporaryDirectory）允许
          真实落盘 —— 用例要读回内容；
        - 其余绝对路径（/etc、/usr/local/share、/Library/LaunchDaemons…）
          映射到 `self.sandbox` 下，绝不碰真机；
        - 相对路径直接拒绝：假平台只接受明确的绝对路径。
        """
        target = _norm(path)
        if not target.is_absolute():
            raise AssertionError(f"假平台的提权路径必须是绝对路径: {path}")
        # 必须按 ``resolve()`` 后的形态比较：macOS 上 pytest 的 tmp_path 是
        # `/private/var/...`，而 tempfile.gettempdir() 给的是 `/var/...`
        # （`/var` 是指向 `/private/var` 的符号链接）。不解析就把测试自己的
        # 临时目录误判成"系统路径"，于是写进沙箱、用例读不回内容 —— 在
        # macOS 上整片组件测试因此变红，而 Linux 上正常。
        resolved = target.resolve()
        allowed_roots = (self.sandbox.resolve(), Path(tempfile.gettempdir()).resolve())
        for root in allowed_roots:
            try:
                resolved.relative_to(root)
                return target
            except ValueError:
                continue
        return self.sandbox / target.relative_to("/")

    def privilege(self) -> ElevatedSession:
        if self._elevated is None:
            self._elevated = FakeElevated(self)
        return self._elevated

    # -- 命令与证书 -----------------------------------------------------

    def run(self, argv: list[str]) -> tuple[int, str]:
        self.commands.append(list(argv))
        if argv[:1] == ["certutil"]:
            self._apply_certutil(argv)
        return 0, ""

    def _nss_db_for(self, nss_dir: Path | None) -> Path:
        if nss_dir is None:
            return _norm(self.nss_db)
        return _norm(Path(nss_dir).expanduser())

    @staticmethod
    def _certutil_db(argv: list[str], default: Path) -> Path:
        raw = _certutil_opt(argv, "-d")
        if raw is None:
            return default
        if raw.startswith("sql:"):
            raw = raw[len("sql:"):]
        return _norm(Path(raw).expanduser())

    def _apply_certutil(self, argv: list[str]) -> None:
        """把 certutil 的 -A / -D / -M 映射到假平台的 NSS 状态上。

        真机语义：`-A` 把证书装进 `-d` 指定的库；`-D` 删掉该库里同名条目；
        `-M` 改写该条目的三列信任标志。`-t` 与 `-d` 都必须生效 ——
        组件把 `-t "C,,"` 改成 `-t ",,"` 的回归、以及"写库 A、复查库 B"
        的 bug，只有照真语义建模才能被测试发现。
        """
        db = self._certutil_db(argv, _norm(self.nss_db))
        nickname = _certutil_opt(argv, "-n")
        flags = _certutil_opt(argv, "-t")
        if "-A" in argv:
            src = _certutil_opt(argv, "-i")
            if src and nickname:
                self.nss_entries_db[(db, nickname)] = (
                    Path(src),
                    flags if flags is not None else ",,",
                )
        elif "-D" in argv:
            if nickname:
                self.nss_entries_db.pop((db, nickname), None)
        elif "-M" in argv and nickname is not None and flags is not None:
            entry = self.nss_entries_db.get((db, nickname))
            if entry is not None:
                self.nss_entries_db[(db, nickname)] = (entry[0], flags)

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

    def remove_service(self, name: str) -> None:
        """幂等地移除服务与单元文件，并像真实平台一样记录命令。"""
        self.commands.append(["remove-service", name])
        self.services.pop(name, None)
        self.active.discard(name)
        self.service_unit_path(name).unlink(missing_ok=True)

    def tail_service_log(self, name: str) -> None:
        pass

    def enable_linger(self) -> tuple[int, str]:
        self.linger = True
        self.commands.append(["loginctl", "enable-linger"])
        return 0, ""

    def linger_status(self) -> bool | None:
        """假平台永远知道答案（没有"判不了"的分支）。"""
        return self.linger

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
        return Path(ca) in self.trusted

    def privilege_read(self, path: Path) -> str | None:
        """非交互读：读得到就返回，读不到返回 None（绝不提示密码）。

        与提权写入使用同一套沙箱映射 —— 宿主机上的真实配置文件不会
        改变测试结果。
        """
        try:
            return self._sandbox_target(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, AssertionError):
            return None

    # -- NSS（与 linux.py 的三态语义对齐） ------------------------------

    def nss_db_path(self) -> Path:
        """假平台认定的默认 NSS 库路径（**非协议方法**）。"""
        return _norm(self.nss_db)

    def nss_entries(
        self, ca: Path, nss_dir: Path | None = None
    ) -> list[tuple[str, str]] | None:
        """库里与这张 CA 内容相同的条目；假平台不会"判不了"（返回 []）。"""
        db = self._nss_db_for(nss_dir)
        target = _read_bytes(ca)
        if target is None:
            return []
        return [
            (nickname, flags)
            for (entry_db, nickname), (path, flags) in self.nss_entries_db.items()
            if entry_db == db and _read_bytes(path) == target
        ]

    def nss_stale_nickname(
        self, ca: Path, nickname: str, nss_dir: Path | None = None
    ) -> bool | None:
        """该昵称下是否残留着**别的** CA；内容读不到时返回 None。"""
        entry = self.nss_entries_db.get((self._nss_db_for(nss_dir), nickname))
        if entry is None:
            return False
        stored = _read_bytes(entry[0])
        expected = _read_bytes(ca)
        if stored is None or expected is None:
            return None
        return stored != expected

    def nss_has_nickname(
        self, nickname: str, nss_dir: Path | None = None
    ) -> bool | None:
        return (self._nss_db_for(nss_dir), nickname) in self.nss_entries_db

    def nss_trust_flags(self, flags: str) -> str:
        return _repair_flags(flags)

    def nss_trusted(self, ca: Path) -> bool:
        """按**证书内容 + SSL 列**判定，不是"这个路径被 -A 过"。

        `self.nss` 仍作为测试的显式预置（老用例用它表示"平台认为已信任"），
        与真实条目判定是"或"的关系。
        """
        if Path(ca) in self.nss:
            return True
        target = _read_bytes(ca)
        if target is None:
            return False
        db = self._nss_db_for(None)
        return any(
            entry_db == db
            and _flags_ssl_trusted(flags)
            and _read_bytes(path) == target
            for (entry_db, _nickname), (path, flags) in self.nss_entries_db.items()
        )

    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        return backend in self.dns_backends
