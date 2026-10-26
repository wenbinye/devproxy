"""Fake platform for tests and dry-run. Never touches the real system.

"Do not touch the real system" also holds for the **elevated session**:
`FakeElevated`'s write / read / copy all go through
`FakePlatform._sandbox_target()`, mapping system paths such as /etc,
/usr/local/share, and /Library/LaunchDaemons into the sandbox directory. Only
the test's own temporary directories (pytest's tmp_path, TemporaryDirectory)
are allowed to write to disk for real, so the test can read the content back
and assert on it.
"""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from pathlib import Path

from devproxy.platform.base import ElevatedSession
from devproxy.platform.base import nss_db_dir


def _certutil_opt(argv: list[str], name: str) -> str | None:
    """Get a certutil option value (`-i <path>` / `-n <nickname>` / `-d <database>` / `-t <flags>`)."""
    if name not in argv:
        return None
    index = argv.index(name)
    if index + 1 >= len(argv):
        return None
    return argv[index + 1]


def _norm(path: Path | str) -> Path:
    """Normalize a path (no symlink resolution, only collapse `..` and redundant separators)."""
    return Path(os.path.normpath(str(path)))


def _read_bytes(path: Path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _flags_ssl_trusted(flags: str) -> bool:
    """Same semantics as `linux._nss_flags_trusted`: only check whether the SSL column is "C"."""
    return bool(flags) and flags[0] == "C"


def _repair_flags(flags: str) -> str:
    """Same semantics as `linux._nss_repair_flags`: only repair the SSL column to "C"."""
    parts = flags.split(",")
    if len(parts) != 3:
        return flags
    return ",".join(["C", parts[1], parts[2]])


class FakeElevated:
    def __init__(self, platform: FakePlatform) -> None:
        self.platform = platform

    def run(self, argv: list[str]) -> tuple[int, str]:
        self.platform.commands.append(list(argv))
        if argv[:1] == ["rm"]:
            # Simulate `rm -f/-rf -- <path>`: the uninstall flow relies on it to
            # delete the DNS drop-in and system trust store files under /etc;
            # without modeling it, the fake platform could never delete them.
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
        """Consistent with write: write to disk for real (inside the sandbox), and record commands and written.

        System CA installation goes through this path: after a successful copy,
        immediately record the source CA in `trusted`, so that the
        "install → recheck" chain really closes on the fake platform (instead of
        relying on the test pre-seeding `trusted` to bypass it).
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
        # Compatibility with old tests: adding a CA directly here means
        # "NSS considers it trusted".
        self.nss: set[Path] = set()
        # More realistic NSS state: (database path, nickname) -> (certificate
        # path, three-column trust flags). What the component changes via
        # `-A/-D/-M` is exactly this table; `nss_trusted` decides by content +
        # SSL column.
        self.nss_entries_db: dict[tuple[Path, str], tuple[Path, str]] = {}
        self.dns_backends: set[str] = set()
        self.commands: list[list[str]] = []
        self.written: dict[Path, str] = {}
        self._elevated: FakeElevated | None = None
        # Platform default NSS database. When a test points components at a
        # different database, change this attribute in sync, so that
        # `CertComponent._require_same_nss_db` considers both sides consistent.
        self.nss_db = _norm(nss_db_dir())
        # auto DNS backend: simulate resolved running by default
        self.resolved_running = True
        # linger: disabled by default; set to true after enable_linger()
        self.linger = False
        # Feature probes for verify(): these "canned answers" can be changed as needed
        self.dns_answer: str | None = "127.0.0.1"
        self.dns_answers: dict[str, str | None] = {}
        self.dns_queries: list[str] = []
        self.http_result = True
        self.http_results: dict[str, bool] = {}
        self.http_probes: list[str] = []
        # Isolated directory for unit files on disk — never read/write the real ~/.config/systemd/user
        self._unit_tmp = tempfile.TemporaryDirectory(prefix="devproxy-fake-units-")
        self.unit_dir = Path(self._unit_tmp.name)
        # Sandbox root for privileged paths — all absolute system paths are mapped here
        self._sandbox_tmp = tempfile.TemporaryDirectory(prefix="devproxy-fake-root-")
        self.sandbox = Path(self._sandbox_tmp.name)

    # -- Path isolation ------------------------------------------------

    def _sandbox_target(self, path: Path) -> Path:
        """Map a privileged path to a safe location.

        - The test's own temporary directories (pytest tmp_path /
          TemporaryDirectory) are allowed to write to disk for real — the case
          needs to read the content back;
        - All other absolute paths (/etc, /usr/local/share,
          /Library/LaunchDaemons…) are mapped under `self.sandbox` and never
          touch the real machine;
        - Relative paths are rejected outright: the fake platform accepts only
          explicit absolute paths.
        """
        target = _norm(path)
        if not target.is_absolute():
            raise AssertionError(f"fake platform privilege paths must be absolute: {path}")
        # Compare against the ``resolve()``d form: on macOS, pytest's tmp_path is
        # `/private/var/...` while tempfile.gettempdir() returns `/var/...`
        # (`/var` is a symlink to `/private/var`). Without resolving, the test's
        # own temporary directory is misclassified as a "system path", so the
        # write goes into the sandbox and the test cannot read the content back
        # — on macOS whole component test suites turn red because of this,
        # while Linux is fine.
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

    # -- Commands and certificates -------------------------------------

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
            raw = raw[len("sql:") :]
        return _norm(Path(raw).expanduser())

    def _apply_certutil(self, argv: list[str]) -> None:
        """Map certutil's -A / -D / -M onto the fake platform's NSS state.

        Real-machine semantics: `-A` installs a certificate into the database
        given by `-d`; `-D` deletes the entry with the same name from that
        database; `-M` rewrites that entry's three-column trust flags. Both
        `-t` and `-d` must take effect — the regression where the component
        changes `-t "C,,"` to `-t ",,"`, and the bug of "writing database A,
        rechecking database B", can only be caught by tests when modeled with
        the real semantics.
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
        """Render the unit and write it to the isolated directory.

        The unit text preserves systemd's field shape (Type= / ExecReload= /
        WantedBy= …); those fields are exactly what component tests assert on —
        collapsing it into a one-line summary would make the assertions
        meaningless.
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
        """Idempotently remove the service and unit file, recording commands like the real platform."""
        self.commands.append(["remove-service", name])
        self.services.pop(name, None)
        self.active.discard(name)
        self.service_unit_path(name).unlink(missing_ok=True)

    def tail_service_log(self, name: str) -> None:
        pass

    def open_in_editor(self, path: Path, editor: str) -> int:
        """The fake platform does not really open an editor; it only records the call."""
        self.commands.append(["editor", *shlex.split(editor), str(path)])
        return 0

    def enable_linger(self) -> tuple[int, str]:
        self.linger = True
        self.commands.append(["loginctl", "enable-linger"])
        return 0, ""

    def linger_status(self) -> bool | None:
        """The fake platform always knows the answer (there is no "cannot determine" branch)."""
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
        """Non-interactive read: return content if readable, None otherwise (never prompt for a password).

        Uses the same sandbox mapping as privileged writes — real config files
        on the host cannot change test results.
        """
        try:
            return self._sandbox_target(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, AssertionError):
            return None

    # -- NSS (aligned with the three-state semantics in linux.py) ------

    def nss_db_path(self) -> Path:
        """Default NSS database path as the fake platform sees it (**not a protocol method**)."""
        return _norm(self.nss_db)

    def nss_entries(self, ca: Path, nss_dir: Path | None = None) -> list[tuple[str, str]] | None:
        """Entries in the database whose content matches this CA; the fake platform is never "unable to determine" (returns [])."""
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
        """Whether a **different** CA is left over under this nickname; return None when the content cannot be read."""
        entry = self.nss_entries_db.get((self._nss_db_for(nss_dir), nickname))
        if entry is None:
            return False
        stored = _read_bytes(entry[0])
        expected = _read_bytes(ca)
        if stored is None or expected is None:
            return None
        return stored != expected

    def nss_has_nickname(self, nickname: str, nss_dir: Path | None = None) -> bool | None:
        return (self._nss_db_for(nss_dir), nickname) in self.nss_entries_db

    def nss_trust_flags(self, flags: str) -> str:
        return _repair_flags(flags)

    def nss_trusted(self, ca: Path) -> bool:
        """Decide by **certificate content + SSL column**, not "this path was passed to -A once".

        `self.nss` remains an explicit test precondition (old tests use it to
        mean "the platform considers it trusted"), and it is OR'ed with the
        real-entry check.
        """
        if Path(ca) in self.nss:
            return True
        target = _read_bytes(ca)
        if target is None:
            return False
        db = self._nss_db_for(None)
        return any(
            entry_db == db and _flags_ssl_trusted(flags) and _read_bytes(path) == target
            for (entry_db, _nickname), (path, flags) in self.nss_entries_db.items()
        )

    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        return backend in self.dns_backends
