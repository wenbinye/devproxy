"""macOS platform implementation: launchd, /etc/resolver, security keychain.

Three key differences from Linux:
- No setcap → ports 80/443 can only be bound via a root LaunchDaemon (caddy runs in system scope)
- Port 53 is occupied by mDNSResponder → the user-scope dnsmasq uses 5300
- DNS routing is written to /etc/resolver/<suffix>, leaving /etc/resolv.conf untouched

All system-mutating calls go through the `_assert_may_mutate()` guard: the test
process must not modify the real machine (the same guard as the Linux platform,
see platform/linux.py).
"""

from __future__ import annotations

import ipaddress
import os
import plistlib
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from devproxy.core.paths import default_state_dir
from devproxy.platform.base import RESOLVER_DIR
from devproxy.platform.base import ElevatedSession
from devproxy.platform.base import Platform
from devproxy.platform.base import render_resolver_file as _render_resolver_file

LAUNCH_AGENTS = "Library/LaunchAgents"
LAUNCH_DAEMONS = "/Library/LaunchDaemons"

# Upper bound for DNS resolution (seconds): a hung resolver must not drag
# setup's self-check down. dns_query is only called from verify() (doctor does
# not do DNS resolution). Matches Linux's DNS_QUERY_TIMEOUT.
DNS_QUERY_TIMEOUT = 3.0

# Default timeout for child processes (seconds): cross-process calls must be
# bounded; omitting the timeout means allowing an indefinite hang.
DEFAULT_TIMEOUT = 30.0

# Service state line in launchctl print ("state = running" / "state = not running").
_LAUNCHD_STATE = re.compile(r"^\s*state\s*=\s*(\S+)", re.MULTILINE)


def _run(argv: list[str], timeout: float | None = DEFAULT_TIMEOUT) -> tuple[int, str]:
    """Execute one argv list (no shell). Return (return code, output).

    The default timeout is `DEFAULT_TIMEOUT` rather than `None`: an omitted
    timeout is a bounded wait, not an indefinite one. Only calls that genuinely
    wait for a user password (the sudo password path) explicitly pass
    `timeout=None`. `stdin=DEVNULL` prevents interactive probes from stealing
    terminal input (`sudo` reads the password from `/dev/tty`, so it is
    unaffected).
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        # Command missing — callers handle the return code instead of getting a traceback
        return 127, f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"command timed out: {argv[0]}"
    except OSError as e:
        # Missing execute bit, etc.
        return 127, f"command unavailable: {argv[0]} ({e})"
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def _first_ip(text: str) -> str | None:
    """Take the first A/AAAA answer from the resolver output."""
    for line in text.splitlines():
        for token in line.replace(",", " ").split():
            candidate = token.strip("[];()")
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                continue
    return None


def _launchd_running(text: str) -> bool:
    """Whether the service is **really running**, according to launchctl print output.

    The output looks like::

        com.devproxy.caddy = {
            active count = 1
            path = /Library/LaunchDaemons/com.devproxy.caddy.plist
            state = running

    launchctl print also returns 0 for a service that is "registered but whose
    process has exited" — only state is evidence that "the process is running".
    When running is not visible, treat it as not running (fail closed): better
    to make the user investigate than to report a crashed service as running.
    """
    match = _LAUNCHD_STATE.search(text)
    return match is not None and match.group(1) == "running"


# Explicit exemption for the platform's own unit tests: those cases replace
# _run with a fake and point paths at tmp_path, and they cover exactly the
# failure handling of write operations — no guard is needed there.
_ALLOW_MUTATION = False


def _assert_may_mutate() -> None:
    """Forbid mutating the real system in tests.

    Write operations on macOS touch /Library/LaunchDaemons, /etc/resolver, and
    the system keychain — a single gap in isolation silently changes the user's
    machine. The guard lives in the platform layer: only the platform knows
    which calls mutate the system. Read-only probes (service_active /
    dns_query / http_ok …) are unaffected.
    """
    if _ALLOW_MUTATION:
        return
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("DEVPROXY_NO_MUTATION"):
        raise RuntimeError("write operations on the real platform are forbidden in tests")


def render_plist(
    name: str,
    exec_start: list[str],
    env: dict[str, str],
    log_path: Path,
    description: str = "",
) -> str:
    """Generate a launchd plist.

    Paths must be absolute — a LaunchDaemon runs as root, so `~` means root's
    home directory, and a relative path is interpreted relative to launchd's
    working directory; neither is the location the user wants.
    """
    if not exec_start:
        raise RuntimeError(f"launchd unit {name} is missing ProgramArguments")
    program = exec_start[0]
    if not Path(program).is_absolute():
        raise RuntimeError(f"launchd unit {name} program path must be absolute: {program}")
    data = {
        "Label": f"com.devproxy.{name}",
        "ProgramArguments": list(exec_start),
        "EnvironmentVariables": dict(env),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    if description:
        data["Description"] = description
    return plistlib.dumps(data).decode("utf-8")


def render_resolver_file(port: int) -> str:
    """/etc/resolver/<suffix> contents.

    macOS natively supports per-domain resolvers; there is no need to touch
    /etc/resolv.conf. The implementation lives in platform/base.py — the DNS
    component needs the same format too.
    """
    return _render_resolver_file(port)


class MacOSElevated:
    """Elevated session. The constructor runs `sudo -v` once to pre-authenticate.

    `sudo -v` is a "command-less" invocation and does not match a command
    specification in sudoers — passwordless access (`NOPASSWD: ALL`) applies
    only to specific commands and does not cover it. Hence on a passwordless
    machine `sudo -v` necessarily fails, even though that machine needs no
    password at all.

    So a failed `sudo -v` **must not** immediately mean "elevation failed":
    instead probe passwordless capability with `sudo -n true` — if that
    succeeds, the machine needs no password, and all subsequent privileged
    commands carry `-n` (never degrading to `sudo <cmd>`, which would pop a
    password prompt). Keep the same semantics as the Linux platform.
    """

    # Marker for the passwordless path. Stored as a class attribute: sessions
    # created in tests via __new__ have no instance attributes and must still
    # run.
    _noninteractive = False

    def __init__(self) -> None:
        # The guard must also cover the constructor: directly creating an
        # elevated session really runs sudo -v, and may later write into the
        # system keychain as well.
        _assert_may_mutate()
        self._authenticate()

    def _authenticate(self) -> None:
        # `sudo -v` may wait for the user to type a password — only this path
        # is allowed to have no timeout.
        code, msg = _run(["sudo", "-v"], timeout=None)
        if code == 0:
            # The timestamp is warmed up; subsequent `sudo <cmd>` reuses it.
            return
        # Passwordless probe: `-n` guarantees it never waits for input (in a
        # non-interactive environment it fails immediately instead of hanging).
        code_nopasswd, msg_nopasswd = _run(["sudo", "-n", "true"])
        if code_nopasswd == 0:
            # Passwordless access works. `sudo -v` warm-up is unavailable on
            # this machine, so subsequent commands must carry `-n` to avoid a
            # password prompt.
            self._noninteractive = True
            return
        # Both paths failed — the error must include both commands' output so
        # the user can distinguish "this machine has no passwordless access"
        # from "sudo itself is broken".
        raise PermissionError(f"elevation failed: sudo -v: {msg}; sudo -n true: {msg_nopasswd}")

    def run(self, argv: list[str]) -> tuple[int, str]:
        if self._noninteractive:
            return _run(["sudo", "-n", *argv])
        # The interactive path may wait for the user to type a password — no
        # timeout (every other call is bounded).
        return _run(["sudo", *argv], timeout=None)

    def write(self, path: Path, content: str) -> None:
        """Write via a file in the system temp directory plus `install -m 644`, avoiding shell quoting problems.

        The parent directory does not necessarily exist: `/etc/resolver` may
        not have been created yet on a clean macOS install. Run `install -d` to
        create the directory first, otherwise the write necessarily fails and
        setup can never converge. The temp file must live in a user-writable
        system temp directory (the target directory itself may not be
        writable), and must be cleaned up on both success and failure.

        Must use `install -m 644 -o root -g wheel` instead of `cp`: the temp
        file created by mkstemp is 0600, and `cp` carries that permission over,
        so files under /etc/resolver/<suffix> and /Library/LaunchDaemons end up
        as root:wheel 0600 — a regular user cannot even read them, and doctor's
        content criterion (`read_text`) would forever fail. The conventional
        form for `resolver(5)` is also root:wheel 0644. Aligns with Linux.
        """
        code, msg = self.run(["install", "-d", str(path.parent)])
        if code != 0:
            raise PermissionError(f"failed to create directory {path.parent}: {msg}")
        fd, tmp_name = tempfile.mkstemp(prefix="devproxy-elev-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            code, msg = self.run(
                [
                    "install",
                    "-m",
                    "644",
                    "-o",
                    "root",
                    "-g",
                    "wheel",
                    str(tmp),
                    str(path),
                ]
            )
            if code != 0:
                raise PermissionError(f"failed to write {path}: {msg}")
        finally:
            tmp.unlink(missing_ok=True)

    def read(self, path: Path) -> str:
        """Read a privileged file. Raise FileNotFoundError if the file is absent, PermissionError for other failures.

        Existence is decided by the **return code** of `test -e`, not by
        parsing `cat`'s error text: BSD/GNU coreutils localize their messages,
        so switching the language would misclassify a missing file as a
        permission error.
        """
        exists, _ = self.run(["test", "-e", str(path)])
        if exists != 0:
            raise FileNotFoundError(path)
        code, out = self.run(["cat", str(path)])
        if code != 0:
            raise PermissionError(f"failed to read {path}: {out}")
        return out

    def copy(self, src: Path, dest: Path) -> None:
        """Copy a file and write it as root:wheel 0644 (same reasoning as `write`)."""
        code, msg = self.run(
            [
                "install",
                "-m",
                "644",
                "-o",
                "root",
                "-g",
                "wheel",
                str(src),
                str(dest),
            ]
        )
        if code != 0:
            raise PermissionError(f"failed to copy {src} → {dest}: {msg}")


class MacOSPlatform:
    """macOS platform implementation."""

    name = "mac"
    dns_port = 5300

    def __init__(self, user_home: Path | None = None) -> None:
        # The design assumes "run as a regular user, sudo on demand
        # internally". When run as root, Path.home() is /var/root, the `gui/0`
        # domain usually does not exist, and services and units get installed
        # into root's home — this must fail explicitly at construction time.
        if os.geteuid() == 0:
            raise RuntimeError(
                "run devproxy as a regular user: privileged steps are performed by the tool via sudo internally; "
                "running as root installs services into root's home directory"
            )
        self.user_home = Path(user_home) if user_home else Path.home()
        self._elevated: MacOSElevated | None = None

    # -- Elevation -----------------------------------------------------

    def privilege(self) -> ElevatedSession:
        """One sudo -v authentication, reused for the whole session — otherwise every operation would prompt for the password again."""
        _assert_may_mutate()
        if self._elevated is None:
            self._elevated = MacOSElevated()
        return self._elevated

    # -- Read-only probes ----------------------------------------------

    def run(self, argv: list[str]) -> tuple[int, str]:
        # Aligned with Linux: regular commands have a timeout; the sudo path
        # waits for the user to type a password and has none.
        return _run(argv, timeout=60.0)

    def binary_version(self, binary: Path) -> str:
        if not binary.exists():
            return ""
        code, out = _run([str(binary), "version"])
        if code != 0:
            return ""
        return out.split()[0] if out else ""

    def service_active(self, name: str) -> bool:
        """Whether the service is **really running** (not just "loaded").

        launchctl print also returns 0 for a registered service that has
        exited — using the exit code directly as "running" would make doctor
        report a crashed caddy as healthy.
        """
        code, out = _run(["launchctl", "print", f"{self._domain(name)}/com.devproxy.{name}"])
        if code != 0:
            return False
        return _launchd_running(out)

    def resolved_active(self) -> bool:
        """macOS has no systemd-resolved — the auto backend must never choose it."""
        return False

    def dns_query(self, name: str) -> str | None:
        """Query via the system resolver. Must have an upper timeout bound: a hung resolver would drag setup's self-check down."""
        code, out = _run(
            ["dscacheutil", "-q", "host", "-a", "name", name], timeout=DNS_QUERY_TIMEOUT
        )
        if code == 0:
            answer = _first_ip(out)
            if answer is not None:
                return answer
        # When dscacheutil is unavailable or has no answer, fall back to dig
        # (available when the bind tools are installed)
        code, out = _run(["dig", "+short", name], timeout=DNS_QUERY_TIMEOUT)
        if code == 0:
            return _first_ip(out)
        return None

    def http_ok(self, url: str) -> bool:
        # Local loopback probes must bypass the user's proxy: curl reads
        # http_proxy/ALL_PROXY by default, and a corporate proxy config would
        # send requests for <.test domains resolved by dnsmasq> to the public
        # internet, making verify() report a false negative.
        code, _ = _run(["curl", "--noproxy", "*", "-fsS", "--max-time", "3", url])
        return code == 0

    def ca_trusted(self, ca: Path) -> bool:
        """Whether the CA is **really in the system trust chain**.

        Checking only the staged file is not enough: that file is also there
        when add-trusted-cert never succeeded, yet the system does not
        recognize it. So let security verify against the system trust settings.
        """
        code, _ = _run(["security", "verify-cert", "-c", str(ca), "-p", "ssl"])
        return code == 0

    def privilege_read(self, path: Path) -> str | None:
        """Read a privileged file non-interactively; return None if unreadable (never prompt for a password).

        /Library/LaunchDaemons on macOS is usually world-readable; if it really
        cannot be read, treat it as "cannot read" and let callers apply "rather
        miss than misreport".
        """
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def nss_trusted(self, ca: Path) -> bool:
        """macOS has no NSS database — always true, so the certificate component does not try to install an NSS certificate."""
        return True

    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        """macOS only has the resolver backend: the per-domain resolver file is configured when it exists."""
        if backend == "resolver":
            return (RESOLVER_DIR / suffix.lstrip(".")).exists()
        return False

    # -- linger (not applicable on macOS) ------------------------------

    def enable_linger(self) -> tuple[int, str]:
        """macOS has no systemd-linger — not applicable, and must not fail."""
        return 0, "macOS has no systemd-linger; not applicable"

    def linger_enabled(self) -> bool:
        """macOS has no linger to enable — always true, so setup never plans any linger action."""
        return True

    # -- Services ------------------------------------------------------

    def service_unit_path(self, name: str) -> Path:
        """caddy uses system scope; everything else uses user scope.

        macOS has no setcap: binding ports 80/443 requires running as root, so
        it can only live in LaunchDaemons.
        """
        if self._is_system_scope(name):
            return Path(LAUNCH_DAEMONS) / f"com.devproxy.{name}.plist"
        return self.user_home / LAUNCH_AGENTS / f"com.devproxy.{name}.plist"

    def log_path(self, name: str) -> Path:
        return self._state_dir() / "logs" / f"{name}.log"

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
        """Render the launchd plist and bootstrap it into the corresponding domain.

        The unit format is up to the platform — components only say "what to
        run and which environment variables to carry".

        `exec_reload` / `exec_stop` are **not applicable** under launchd
        (launchd manages the lifecycle via kickstart / KeepAlive and has no
        corresponding plist fields), so they are deliberately ignored here; the
        protocol allows a platform to ignore these two optional items. If
        reload semantics are needed in the future, add them as a separate
        non-protocol capability rather than stuffing pseudo-fields into the
        plist.
        """
        _assert_may_mutate()
        path = self.service_unit_path(name)
        args = [self._expand(a) for a in exec_start]
        content = render_plist(name, args, env, self.log_path(name), description)
        domain = self._domain(name)
        target = f"{domain}/com.devproxy.{name}"
        # The log directory must exist before bootstrap: when launchd opens
        # StandardOutPath, a missing parent directory prevents it from starting
        # at all. The component order is caddy → dnsmasq → …; caddy is
        # installed first, and at that point nobody has created the log
        # directory yet. The directory is under the user's home, so it can be
        # created as the user (a root process can write there too).
        self.log_path(name).parent.mkdir(parents=True, exist_ok=True)
        if self._is_system_scope(name):
            # /Library/LaunchDaemons belongs to root — privileged writes are
            # required. `MacOSElevated.write` writes to disk with
            # `install -m 644 -o root -g wheel`, so the unit is naturally
            # root:wheel 0644 (the tool can read it too) and no extra
            # chown/chmod is needed.
            self.privilege().write(path, content)
            self.privilege().run(["launchctl", "bootout", target])
            code, msg = self.privilege().run(["launchctl", "bootstrap", "system", str(path)])
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            # bootout just "unloads any old instance that may exist"; failure
            # when it was never installed is normal.
            _run(["launchctl", "bootout", target])
            code, msg = _run(["launchctl", "bootstrap", domain, str(path)])
        if code != 0:
            raise RuntimeError(f"launchctl bootstrap {name} failed: {msg}")

    def enable_service(self, name: str) -> None:
        """launchd starts the service automatically via RunAtLoad=True; no extra enable is needed.

        Deliberately performs no write operations — rewriting the plist would
        clear ProgramArguments.
        """
        _assert_may_mutate()
        return

    def restart_service(self, name: str) -> None:
        _assert_may_mutate()
        target = f"{self._domain(name)}/com.devproxy.{name}"
        argv = ["launchctl", "kickstart", "-k", target]
        if self._is_system_scope(name):
            code, msg = self.privilege().run(argv)
        else:
            code, msg = _run(argv)
        if code != 0:
            raise RuntimeError(f"launchctl kickstart {name} failed: {msg}")

    def remove_service(self, name: str) -> None:
        """Bootout and delete the plist; succeed silently when the service was never installed (idempotent).

        The system-scope (caddy) plist lives in /Library/LaunchDaemons, so
        deletion must go through the elevated session; user scope lives in the
        user's own home directory, so delete directly.
        """
        _assert_may_mutate()
        target = f"{self._domain(name)}/com.devproxy.{name}"
        path = self.service_unit_path(name)
        if self._is_system_scope(name):
            # bootout returns non-zero for a nonexistent service — idempotent,
            # ignore the return code.
            self.privilege().run(["launchctl", "bootout", target])
            code, msg = self.privilege().run(["rm", "-f", str(path)])
            if code != 0:
                raise RuntimeError(f"failed to delete the LaunchDaemon for {name}: {msg}")
            return
        _run(["launchctl", "bootout", target])
        path.unlink(missing_ok=True)

    def tail_service_log(self, name: str) -> None:
        """Stream-follow service logs: inherit the terminal's stdio (no capture); Ctrl-C propagates naturally."""
        subprocess.run(["tail", "-f", str(self.log_path(name))])

    def open_in_editor(self, path: Path, editor: str) -> int:
        """Open a file in the editor: inherit stdio (the editor takes over the terminal); return the exit code."""
        argv = shlex.split(editor) + [str(path)]
        return subprocess.run(argv).returncode

    # -- Internals -----------------------------------------------------

    def _is_system_scope(self, name: str) -> bool:
        return name == "caddy"

    def _domain(self, name: str) -> str:
        """launchd domain: system (root's daemons) or gui/<uid> (user-scope agents)."""
        if self._is_system_scope(name):
            return "system"
        return f"gui/{os.getuid()}"

    def _state_dir(self) -> Path:
        """State directory — the single source is paths.default_state_dir; the platform does not assemble paths itself."""
        return default_state_dir(self.user_home)

    def _expand(self, arg: str) -> str:
        """Expand `~` in arguments — launchd does not recognize it.

        Not just at the start: `--conf-dir=~/.config/dnsmasq` must be expanded
        too, otherwise when running as root it points into root's home
        directory.
        """
        if arg == "~":
            return str(self.user_home)
        if arg.startswith("~/"):
            return str(self.user_home / arg[2:])
        idx = arg.find("=~/")
        if idx >= 0:
            head = arg[: idx + 1]
            return f"{head}{self.user_home / arg[idx + 3 :]}"
        return arg


def get_platform() -> Platform:
    """Cross-platform dispatch factory (placed here per the brief; the implementation is in platform/__init__.py)."""
    from devproxy.platform import get_platform as _dispatch

    return _dispatch()
