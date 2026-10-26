"""Linux platform implementation: systemd(user) / setcap / update-ca-certificates / NSS / resolved."""

from __future__ import annotations

import getpass
import ipaddress
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from devproxy.platform.base import CA_NICKNAME
from devproxy.platform.base import RESOLVED_CONF
from devproxy.platform.base import SYSTEM_DNSMASQ_CONF
from devproxy.platform.base import SYSTEM_TRUST_DIR
from devproxy.platform.base import ElevatedSession
from devproxy.platform.base import file_fingerprints as _fingerprints_of_file
from devproxy.platform.base import nss_db_dir
from devproxy.platform.base import pem_fingerprints as _pem_fingerprints

# Possible locations of the CA in the system trust store (the live store).
#
# - Bundles: Debian/Ubuntu's /etc/ssl/certs/ca-certificates.crt,
#   RHEL's /etc/pki/tls/certs/ca-bundle.crt
# - Hashed index directory: *.0 (subject-hash symlinks) and *.pem under
#   /etc/ssl/certs
#
# Deliberately **excludes** the staging directory
# /usr/local/share/ca-certificates — a file being there only means someone
# put it there; it does not mean update-ca-certificates ever succeeded.
TRUST_BUNDLE_FILES = (
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
)

# Default timeout for child processes (seconds): cross-process calls must be
# bounded; omitting the timeout means allowing an indefinite hang.
DEFAULT_TIMEOUT = 30.0
# Upper bound for DNS resolution probes (aligned with macOS's
# DNS_QUERY_TIMEOUT): a hung resolver must not drag setup's self-check down.
DNS_QUERY_TIMEOUT = 3.0
# Criterion for whether systemd is running as init (absent in containers / WSL).
SYSTEMD_RUN_DIR = Path("/run/systemd/system")

# NSS trust flags (the Trust Attributes column of `certutil -L`) — **only the
# "C" in the SSL column counts**.
#
# The flags are three comma-separated columns: `SSL,S/MIME,JAR/XPI`; this tool
# cares about the first column (SSL). Letter semantics for the first (and
# second) column:
#   "C" = trusted CA that may issue **server** certificates (SSL column) /
#         email certificates (S/MIME column)
#   "T" = trusted CA that may issue **client** certificates (SSL column;
#         Chromium does not use it as an anchor)
#   "P" = trusted peer (leaf), **not** a CA anchor
#   "G" / "U" = valid but untrusted; "," / "." / empty = no trust at all
#
# Why only "C" in the first column counts: this tool's CA is the root CA that
# issues site certificates, and that is exactly the anchor Chrome wants.
# Measured (NSS 3.120, using `certutil -V -u V` to verify a leaf chain issued
# by that CA):
#   C,, / C,C, / C,C,C → valid
#   T,, / P,, / ,,      → "issuer has been marked as not trusted by the user"
# Meanwhile Chromium's TrustStoreNSS only references CERTDB_TRUSTED_CA (i.e.
# "C"); CERTDB_TRUSTED_CLIENT_CA ("T") is not referenced at all, and
# CERTDB_TRUSTED ("P") only counts as a "trusted leaf". Treating T / P as
# trusted is a **false positive**.
NSS_TRUSTED_FLAGS = ("C",)
NSS_TRUSTED_COLUMN = 0

# A table row of `certutil -L`: the nickname column is 55 wide (longer values
# overflow, leaving only one space between nickname and flags), and nicknames
# **may contain spaces** — so flags can only be split off from the end of the
# line, never by splitting the nickname at the "first space".
#
# Flags have **three columns** (`SSL,S/MIME,JAR/XPI`): `C,,`, `C,C,`, `C,C,C`,
# `,C,`, `,,` are all valid shapes. The regex must express "two commas, three
# columns of content": `C,C,C` cannot be matched by a `,{2,}` style pattern
# (there are no two consecutive commas), yet on the real machine
# `Caddy Local Authority` is exactly `C,C,C` — failing to parse it would turn
# "trusted" into "untrusted", and the following `-A -t C,,` would silently
# wipe the other two columns.
#
# Note that a `C,C,C` line has **no** trailing space (a `C,,` line has two
# trailing spaces); rstrip before matching.
#
# The regex only produces **candidates**: every candidate nickname must then
# actually be fetched with `certutil -L -n <nickname> -a`, and skipped if that
# fails — when parsing is off, under-reporting is better than misreporting.
_NSS_ROW_RE = re.compile(r"^(?P<name>.*?)[ ]+(?P<flags>[A-Za-z]*(?:,[A-Za-z]*){2})$")


def _store_candidates() -> list[Path]:
    """Files in the system trust store that may contain the CA (bundles + hashed index directory).

    Read-only, names only; any directory-listing error is swallowed — a failed
    criterion must degrade to "untrusted", and must never make doctor raise.
    """
    files: list[Path] = list(TRUST_BUNDLE_FILES)
    try:
        if SYSTEM_TRUST_DIR.is_dir():
            for pattern in ("*.0", "*.pem", "*.crt"):
                files.extend(sorted(SYSTEM_TRUST_DIR.glob(pattern)))
    except OSError:
        pass
    return files


def ca_in_trust_store(ca: Path) -> bool:
    """Whether the CA exists in the system trust store **by fingerprint**. Never raises.

    The criterion is "does the live store contain a certificate with exactly
    these bytes", not "can openssl verify this chain": openssl selects an
    anchor by **SUBJECT**, so when multiple roots with the same subject exist
    it may pick another one and report error 18 (self-signed certificate) —
    even though curl works fine. Using that as the criterion would make setup
    forever believe "untrusted", while reinstalling the same CA only adds more
    same-subject roots and never fixes it.
    """
    try:
        ca_text = ca.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    target = _pem_fingerprints(ca_text)
    if not target:
        # not a parseable PEM certificate — cannot compare by fingerprint, treat as untrusted
        return False
    for path in _store_candidates():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if target & _pem_fingerprints(text):
            return True
    return False


def _nss_rows(listing: str) -> list[tuple[str, str]]:
    """Parse the `certutil -L` table → [(nickname, trust flags)].

    The two header lines and blank lines are filtered out naturally: the flag
    shape is "three columns, two commas", while the header's
    `Certificate Nickname … Trust Attributes` has no commas, and
    `SSL,S/MIME,JAR/XPI` has two commas but its column contents contain `/`, so
    neither matches `[A-Za-z]*(?:,[A-Za-z]*){2}`.
    """
    rows: list[tuple[str, str]] = []
    for raw in listing.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        match = _NSS_ROW_RE.match(line)
        if not match:
            continue
        name = match.group("name").rstrip()
        if not name:
            continue
        rows.append((name, match.group("flags")))
    return rows


def _nss_missing_cert(text: str) -> bool:
    """Whether `certutil`'s output **explicitly** says "this entry does not exist".

    Only these messages count as "does not exist":
    - `could not find certificate named "…"` (`-D` / `-L` cannot find the nickname)
    - `could not find cert: …`
    - `PR_FILE_NOT_FOUND_ERROR`

    `SEC_ERROR_BAD_DATABASE` and the like when the database is locked /
    unreadable / corrupt do **not** count — that is "cannot determine" and must
    be reported upward, never treated as "absent".

    Match only the three **complete messages** above: a generic `"not found"`
    would also classify loading failures such as "could not find the module …"
    as "entry does not exist", and this function's return value is exactly what
    decides whether a delete failure can be treated as idempotent success.
    """
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "could not find certificate named",
            "could not find cert:",
            "pr_file_not_found_error",
        )
    )


def _nss_flags_trusted(flags: str) -> bool:
    """Whether the **SSL column** of this entry means "trusted CA".

    Only the first column is checked: "C" in the second column (S/MIME) only
    applies to email certificates and is useless for HTTPS; when the first
    character is ",", the whole column has no trust.
    """
    return bool(flags) and flags[NSS_TRUSTED_COLUMN] in NSS_TRUSTED_FLAGS


def _nss_repair_flags(flags: str) -> str:
    """Repair the SSL column to "C" while **preserving trust in the other two columns**.

    `certutil -M -t "C,,"` also clears the S/MIME / JAR columns — if the user's
    database has an entry like `,C,` ("trust email only"), repairing the flags
    that way would **rewrite** trust the user did not intend us to touch.
    `C,C,` changes only the column that should change.
    """
    parts = flags.split(",")
    if len(parts) != 3:
        # When the shape is unrecognized, **return it unchanged** so that
        # `certutil -M` writes the same string back: resetting to "C,," would
        # wipe S/MIME / JAR trust that may exist in an unknown shape, which is
        # exactly the "rewriting trust the user did not intend us to touch"
        # this function avoids. (The component layer feeds the return value
        # straight to `-M`; returning None here is not an option — it would
        # become the string "None" and corrupt the flags.)
        return flags
    return ",".join(["C", parts[1], parts[2]])


def _nss_db_state(nss: Path) -> bool | None:
    """Whether the NSS database exists: True / False (definitely absent) / None (cannot determine).

    `Path.exists()` swallows a PermissionError from `stat()` into False —
    "database exists but the directory is inaccessible" then becomes "database
    absent, so the entry definitely does not exist". A failed delete would be
    swallowed as idempotent success (the old root stays in the database while
    the tool reports ready), so the three-state result must be preserved here.
    """
    try:
        (nss / "cert9.db").stat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return True


def _nss_entry_matches(
    nickname: str,
    expected: set[str],
    nss: Path,
    cache: dict[str, str | None],
) -> bool:
    """Whether the certificate content under this nickname matches `expected`.

    Table parsing only produces candidates — when parsing is off,
    under-reporting is better than misreporting. The `certutil -L -n … -a`
    result for each nickname is fetched only once and kept in `cache`.
    """
    if nickname not in cache:
        code, pem = _run(["certutil", "-L", "-d", f"sql:{nss}", "-n", nickname, "-a"])
        cache[nickname] = pem if code == 0 else None
    cached = cache[nickname]
    if cached is None:
        return False
    return bool(expected & _pem_fingerprints(cached))


def _run(argv: list[str], timeout: float | None = DEFAULT_TIMEOUT) -> tuple[int, str]:
    """Run one argv. timeout is **hang prevention**, not performance tuning.

    A child-process call without a timeout can be dragged forever by a command
    stuck on an interactive prompt, and will **write to stdout without bound** —
    measured: `certutil -N` loops forever on the password prompt and wrote
    200GB into the redirect file, filling /home to 100%. Return code 124
    follows the coreutils timeout convention; callers can treat it as
    "command failed".

    The default is therefore `DEFAULT_TIMEOUT` rather than `None`: an omitted
    timeout is a bounded wait, not an indefinite one. Only calls that genuinely
    need user input (the sudo password path) explicitly pass `timeout=None`.

    `stdin=DEVNULL` prevents interactive probes from stealing terminal input
    (`sudo` reads the password from `/dev/tty`, so it is unaffected).
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 124, f"command timed out ({timeout}s): {argv[0]}"
    except OSError as e:
        # Missing command (FileNotFoundError), missing execute bit
        # (PermissionError), etc. are all normalized to "command unavailable",
        # so callers handle a return code instead of a traceback.
        return 127, f"command unavailable: {argv[0]} ({e})"
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def _first_ip(text: str) -> str | None:
    """Take the first A/AAAA answer from resolvectl / dig output."""
    for line in text.splitlines():
        for token in line.replace(",", " ").split():
            candidate = token.strip("[];()")
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                continue
    return None


# Explicit exemption for the platform's own unit tests: those cases replace
# _run with a fake and point paths at tmp_path, and they cover exactly the
# failure handling of write operations — no guard is needed there.
_ALLOW_MUTATION = False


def _assert_may_mutate() -> None:
    """Forbid mutating the real system in tests.

    Once a platform object gets the real HOME, it can rewrite units under
    ~/.config/systemd/user/, start/stop real services, and even sudo-write
    /etc. A single gap in isolation silently changes the user's machine (it
    once really overwrote the user's own caddy.service).

    The guard lives in the platform layer rather than test fixtures: only the
    platform knows which calls mutate the system. Read-only probes
    (service_active / dns_query / http_ok …) are unaffected.
    """
    if _ALLOW_MUTATION:
        return
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("DEVPROXY_NO_MUTATION"):
        raise RuntimeError("write operations on the real platform are forbidden in tests")


def render_systemd_unit(
    name: str,
    exec_start: list[str],
    exec_reload: list[str] | None,
    exec_stop: list[str] | None,
    env: dict[str, str],
    description: str = "",
    service_type: str | None = None,
) -> str:
    """Generate a systemd user unit. The shape matches the existing caddy.service.

    systemd units do not do shell quote parsing: whitespace inside an argument
    truncates the command line. The paths come from config (user-modifiable),
    so they must be blocked here.

    service_type is inferred by default from the program being launched: only
    caddy sends sd_notify; forcing Type=notify on programs like dnsmasq would
    hang forever at "start timed out".
    """
    for label, args in (
        ("ExecStart", exec_start),
        ("ExecReload", exec_reload),
        ("ExecStop", exec_stop),
    ):
        for value in args or []:
            if any(c.isspace() for c in value):
                raise RuntimeError(f"{label} arguments must not contain whitespace: {value}")

    # Environment= values come from user-modifiable config (paths.caddy_env's
    # JSON) and, like argv, are "external input". A newline would inject
    # arbitrary new lines into the unit (for example a forged ExecStartPre=),
    # or at the very least corrupt the unit, so it must be blocked like argv.
    def _reject_newline(label: str, value: str) -> None:
        if "\n" in value or "\r" in value:
            raise RuntimeError(f"{label} must not contain newlines: {value!r}")

    _reject_newline("Description", description)
    for key, value in env.items():
        _reject_newline("Environment variable name", key)
        _reject_newline(f"Environment={key}", value)

    if service_type is None:
        program = Path(exec_start[0]).name if exec_start else ""
        service_type = "notify" if program.startswith("caddy") else "simple"
    lines = [
        "[Unit]",
        f"Description={description or f'{name} (managed by devproxy)'}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        f"Type={service_type}",
    ]
    for k, v in env.items():
        lines.append(f"Environment={k}={v}")
    lines.append(f"ExecStart={' '.join(exec_start)}")
    if exec_reload:
        lines.append(f"ExecReload={' '.join(exec_reload)}")
    if exec_stop:
        lines.append(f"ExecStop={' '.join(exec_stop)}")
    lines += [
        "TimeoutStopSec=5s",
        "LimitNOFILE=1048576",
        "Restart=on-failure",
        "RestartSec=2s",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


class LinuxElevated:
    """Elevated session. The constructor runs `sudo -v` once to pre-authenticate.

    `sudo -v` is a "command-less" invocation: it does not match a command
    specification in sudoers, so it falls through to the default
    `(ALL : ALL) ALL` — which requires authentication. Meanwhile
    `(ALL) NOPASSWD: ALL` only grants passwordless access to **specific
    commands** and does not cover `sudo -v`. Hence on a passwordless machine
    `sudo -v` necessarily fails (without a TTY it reports
    "A terminal is required to authenticate"), even though that machine needs
    no password at all.

    So a failed `sudo -v` **must not** immediately mean "elevation failed":
    instead probe passwordless capability with `sudo -n true` — if that
    succeeds, the machine needs no password, and all subsequent privileged
    commands carry `-n` (never degrading to `sudo <cmd>`, which would pop a
    password prompt).
    """

    # Marker for the passwordless path. Stored as a class attribute: sessions
    # created in tests via __new__ have no instance attributes and must still
    # run.
    _noninteractive = False

    def __init__(self) -> None:
        # The guard must also cover the constructor: directly creating an
        # elevated session really runs sudo -v.
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
        """Write via a file in the system temp directory plus a privileged install, avoiding shell quoting problems.

        The parent directory does not necessarily exist:
        `/etc/systemd/resolved.conf.d` and `/etc/dnsmasq.d` may not have been
        created yet on a clean machine (they are not owned by any dpkg
        package). Run `install -d` to create the directory first, otherwise
        the write necessarily fails and setup can never converge. The temp file
        must live in a user-writable system temp directory (the target
        directory itself may not be writable), and must be cleaned up on both
        success and failure.

        Use `install -m 644 -o root -g root` instead of `cp`: the temp file
        created by mkstemp is 0600, and `cp` carries that permission over, so
        files written into /usr/local/share/ca-certificates/ and
        /etc/systemd/resolved.conf.d/ end up as root:root 0600 — the invoking
        regular user cannot even read them, and doctor would forever report
        "file unreadable". The macOS path already uses 0644; Linux must align.
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
                    "root",
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
        parsing `cat`'s error text: GNU coreutils localizes its messages via
        gettext, so with `LANG=zh_CN.UTF-8` the output is a translated string
        and matching on `"No such file"` would wrongly raise PermissionError
        for a missing file.
        """
        exists, _ = self.run(["test", "-e", str(path)])
        if exists != 0:
            raise FileNotFoundError(path)
        code, out = self.run(["cat", str(path)])
        if code != 0:
            raise PermissionError(f"failed to read {path}: {out}")
        return out

    def copy(self, src: Path, dest: Path) -> None:
        """Copy a file (the path used to install the CA into the system trust store).

        Also use `install -m 644 -o root -g root`: `cp` carries over the source
        file's permissions, so if the source is 0600, the destination becomes
        root:root 0600.
        """
        code, msg = self.run(
            [
                "install",
                "-m",
                "644",
                "-o",
                "root",
                "-g",
                "root",
                str(src),
                str(dest),
            ]
        )
        if code != 0:
            raise PermissionError(f"failed to copy {src} → {dest}: {msg}")


class LinuxPlatform:
    name = "linux"
    dns_port = 5353

    def __init__(self, user_home: Path | None = None) -> None:
        # The design assumes "run as a regular user, sudo on demand
        # internally". When run as root, Path.home() is /root, units get
        # installed into root's home, and systemctl --user operates root's user
        # manager — at worst overwriting a caddy.service of the same name in
        # root's home. This must fail explicitly at construction time, rather
        # than surfacing later as a pile of confusing systemd errors.
        if os.geteuid() == 0:
            raise RuntimeError(
                "run devproxy as a regular user: privileged steps are performed by the tool via sudo internally; "
                "running as root installs services into root's home directory"
            )
        self.user_home = user_home or Path.home()
        self._elevated: LinuxElevated | None = None

    def privilege(self) -> ElevatedSession:
        _assert_may_mutate()
        if self._elevated is None:
            self._elevated = LinuxElevated()
        return self._elevated

    def run(self, argv: list[str]) -> tuple[int, str]:
        # Regular commands always have a timeout; the sudo path does not — it
        # may wait for the user to type a password.
        return _run(argv, timeout=60.0)

    def privilege_read(self, path: Path) -> str | None:
        """Read a privileged file non-interactively (`sudo -n`). Return None if unreadable.

        doctor is a read-only command and must never pop a password prompt
        because of permissions — that would both break the "read-only health
        check" promise and make doctor hang on a password prompt in scripts.
        `sudo -n` fails immediately when there are no cached credentials and
        never waits for input.
        """
        try:
            proc = subprocess.run(
                ["sudo", "-n", "cat", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

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
        _assert_may_mutate()
        path = self.service_unit_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_systemd_unit(name, exec_start, exec_reload, exec_stop, env, description),
            encoding="utf-8",
        )
        code, msg = _run(["systemctl", "--user", "daemon-reload"])
        if code != 0:
            raise RuntimeError(f"systemctl daemon-reload failed after installing {name}: {msg}")

    def enable_service(self, name: str) -> None:
        _assert_may_mutate()
        code, msg = _run(["systemctl", "--user", "enable", "--now", name])
        if code != 0:
            raise RuntimeError(f"systemctl enable --now {name} failed: {msg}")

    def restart_service(self, name: str) -> None:
        _assert_may_mutate()
        code, msg = _run(["systemctl", "--user", "restart", name])
        if code != 0:
            raise RuntimeError(f"systemctl restart {name} failed: {msg}")

    def remove_service(self, name: str) -> None:
        """Stop and delete the user unit; a missing unit is normal (idempotent).

        `disable --now` returns non-zero for a nonexistent unit, which cannot
        be treated as failure across the board — but it must not be swallowed
        across the board either: only if the service is still active after the
        command failed is it a real failure; when the unit file is merely left
        over (the service is not running), it should still be deleted.
        """
        _assert_may_mutate()
        path = self.service_unit_path(name)
        code, msg = _run(["systemctl", "--user", "disable", "--now", name])
        if code != 0 and self.service_active(name):
            raise RuntimeError(f"failed to stop service {name}: {msg}")
        path.unlink(missing_ok=True)
        code, msg = _run(["systemctl", "--user", "daemon-reload"])
        if code != 0:
            raise RuntimeError(f"systemctl daemon-reload failed after removing {name}: {msg}")

    def tail_service_log(self, name: str) -> None:
        """Stream-follow service logs: inherit the terminal's stdio (no capture); Ctrl-C propagates naturally."""
        subprocess.run(["journalctl", "--user", "-u", name, "-f"])

    def open_in_editor(self, path: Path, editor: str) -> int:
        """Open a file in the editor: inherit stdio (the editor takes over the terminal); return the exit code."""
        argv = shlex.split(editor) + [str(path)]
        return subprocess.run(argv).returncode

    def enable_linger(self) -> tuple[int, str]:
        """Enable linger.

        Consistent with the platform's other privileged operations: try
        `sudo -n` first (never pop a password prompt). Fall back to `sudo`,
        which may prompt for a password, only when a controlling terminal
        really exists; in a non-interactive environment return an explainable
        error code instead of hanging on a password prompt.
        """
        _assert_may_mutate()
        user = getpass.getuser()
        code, msg = _run(["sudo", "-n", "loginctl", "enable-linger", user])
        if code == 0:
            return code, msg
        if not sys.stdin.isatty():
            return code, f"{msg} (non-interactive environment; did not attempt to enter a password)"
        return _run(["sudo", "loginctl", "enable-linger", user], timeout=None)

    def linger_status(self) -> bool | None:
        """Three-state linger (**not a protocol method**; components probe it via getattr).

        - `True`: enabled, or not applicable on this machine at all (no systemd / no loginctl);
        - `False`: definitely not enabled;
        - `None`: systemd is present but the probe failed — cannot determine;
          callers must handle this explicitly and must not silently treat it as
          "enabled".
        """
        if not SYSTEMD_RUN_DIR.is_dir():
            # Container / WSL / non-systemd host: linger is not applicable.
            return True
        code, out = _run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger", "--value"])
        if code == 127:
            # loginctl is not in PATH (minimal systemd environment): linger
            # cannot be enabled.
            return True
        if code != 0:
            # systemd is present but the probe failed (transient D-Bus
            # failure / permission): cannot determine.
            return None
        return out.strip() == "yes"

    def linger_enabled(self) -> bool:
        """Whether the current user has linger enabled (read-only probe).

        When `linger_status()` returns "cannot determine", treat it as **not
        enabled** (fail closed): better to add an extra `enable-linger` action
        to the plan (which fails loudly) than to silently skip it and leave
        user-scope services not starting after logout/reboot.
        """
        status = self.linger_status()
        return False if status is None else status

    def resolved_active(self) -> bool:
        code, out = _run(["systemctl", "is-active", "systemd-resolved"])
        if code == 0 and out.strip() == "active":
            return True
        # Some environments lack the systemd unit name for systemd-resolved
        # but resolvectl still works (e.g. containers); fall back to whether a
        # status query succeeds.
        code, _ = _run(["resolvectl", "status"])
        return code == 0

    def dns_query(self, name: str) -> str | None:
        """Prefer the system resolver (resolved); on failure, ask the local dnsmasq directly.

        Both paths must have an upper timeout bound: `resolvectl query` goes
        through D-Bus, and when systemd-resolved hangs the child process does
        not return on its own, so `verify()` would be dragged forever. The
        bound matches macOS's `DNS_QUERY_TIMEOUT`.
        """
        code, out = _run(["resolvectl", "query", name], timeout=DNS_QUERY_TIMEOUT)
        if code == 0:
            answer = _first_ip(out)
            if answer is not None:
                return answer
        code, out = _run(
            ["dig", "@127.0.0.1", "-p", str(self.dns_port), name, "+short"],
            timeout=DNS_QUERY_TIMEOUT,
        )
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
        """Whether the CA is **really in the system trust store**.

        The authoritative criterion is a **fingerprint match** (see
        ca_in_trust_store): trust only counts when the live store contains a
        certificate with exactly the same bytes as this CA.

        Comparing only the file in the staging directory
        (/usr/local/share/ca-certificates) is not enough: that file is also
        there when update-ca-certificates never succeeded, yet the system does
        not recognize it.

        openssl is used only as a **supplementary positive signal**: a
        successful verification shows the chain really holds; a failed one may
        just be the same-subject ambiguity above, and must never be used to
        negate a fingerprint match.
        """
        # The fingerprint check itself promises "never raises" (it already
        # turns read/decode failures into False internally); do not wrap
        # another broad except here — a real bug should surface rather than be
        # swallowed and then continue into openssl.
        if ca_in_trust_store(ca):
            return True
        code, _ = _run(["openssl", "verify", "-CApath", str(SYSTEM_TRUST_DIR), str(ca)])
        return code == 0

    def nss_db_path(self) -> Path:
        """NSS database path as this platform sees it (**not a protocol method**).

        The component layer needs to compare it with its own `nss_dir`: the
        database written to and the database rechecked must be the same —
        otherwise the "recheck" safety net is checking another database, i.e.
        it is useless.
        """
        return nss_db_dir(self.user_home)

    def nss_entries(self, ca: Path, nss_dir: Path | None = None) -> list[tuple[str, str]] | None:
        """Entries in the NSS database with **the same content** as this CA: [(nickname, trust flags), ...].

        Find by **content**, not by nickname — NSS deduplicates by (issuer,
        serial): installing the same certificate again with a different
        nickname via `certutil -A` is a **silent no-op** (returns 0 but no
        entry is added). So "is this CA installed" can only be answered by
        fingerprint comparison.

        Three-state return:
        - a list: the database was readable and has been filtered by content (may be empty);
        - `[]`: the database **definitely** does not exist, so entries naturally do not exist;
        - `None`: **cannot determine** (database locked / directory unreadable / DB corrupt / certutil failed)
          — callers must not treat it as "no entries".

        Note the fast path: when the tool's own nickname matches and is
        trusted, it **returns just that one entry** and does not guarantee
        listing all matches — callers should use it only as the criterion for
        "is it trusted"; enumerating all matching entries (e.g. to repair trust
        flags) presupposes that the tool's own nickname does not match, and in
        that case the full list is returned.

        `nss_dir` may explicitly specify the database path (defaults to this
        platform's nss_db_path): when the database injected by the component
        differs from the platform default, the recheck must query **the one
        that was written**.

        This is a **non-protocol method** (the component layer probes it via
        getattr): macOS has no NSS database, so not implementing it is fine.
        """
        nss = self._resolve_nss_dir(nss_dir)
        state = _nss_db_state(nss)
        if state is False:
            return []
        if state is None:
            return None
        expected = _fingerprints_of_file(ca)
        if not expected:
            return []
        code, listing = _run(["certutil", "-L", "-d", f"sql:{nss}"])
        if code != 0:
            # When `-L` fails, "empty database" and "unreadable" cannot be
            # distinguished — cannot determine, must not return [].
            return None
        rows = _nss_rows(listing)
        cache: dict[str, str | None] = {}

        # Fast path: own nickname matches and already carries trust flags — one
        # extra call is enough, no need to enumerate everything.
        for nickname, flags in rows:
            if nickname != CA_NICKNAME or not _nss_flags_trusted(flags):
                continue
            if _nss_entry_matches(nickname, expected, nss, cache):
                return [(nickname, flags)]
        return [
            (nickname, flags)
            for nickname, flags in rows
            if _nss_entry_matches(nickname, expected, nss, cache)
        ]

    def nss_stale_nickname(
        self, ca: Path, nickname: str, nss_dir: Path | None = None
    ) -> bool | None:
        """An entry exists under `nickname` and its content is **not** this CA (a stale entry).

        Purpose: if the tool's own nickname (`devproxy Local CA`) is left over
        with an **old CA**, browsers will trust that old root forever — and if
        the old private key is still obtainable (backups, old storage), that is
        a door left open for HTTPS man-in-the-middle. That nickname belongs to
        the tool and can be safely deleted.

        Three-state return: True = definitely stale; False = definitely not
        stale (same name and same content, or the entry definitely does not
        exist); None = cannot determine (database unreadable, certutil output
        is not "not found", not PEM). The deletion target is always the tool's
        own nickname, so there is no risk of "deleting a user entry by
        mistake"; collapsing "cannot determine" into False would leave the
        stale root in the database forever.
        """
        nss = self._resolve_nss_dir(nss_dir)
        state = _nss_db_state(nss)
        if state is False:
            return False
        if state is None:
            return None
        code, pem = _run(["certutil", "-L", "-d", f"sql:{nss}", "-n", nickname, "-a"])
        if code != 0:
            if _nss_missing_cert(pem):
                return False  # definitely no entry under this nickname
            return None  # database locked / corrupt — cannot determine
        stored = _pem_fingerprints(pem)
        expected = _fingerprints_of_file(ca)
        if not stored or not expected:
            return None  # content is not PEM, cannot compare
        return not (stored & expected)

    def nss_has_nickname(self, nickname: str, nss_dir: Path | None = None) -> bool | None:
        """Whether an entry exists under `nickname` (content not checked). **Non-protocol method**, three-state.

        - `True` / `False`: definitely present / definitely absent;
        - `None`: **cannot determine** (database locked, unreadable, DB corrupt…).

        Why three states are required: `certutil -L -n` returns non-zero
        **both** when "the entry does not exist" and when "the database cannot
        be read". Treating the latter as "does not exist" would swallow a
        delete failure as idempotent success (the old root stays in the
        database while the tool reports ready). Measured: when the database is
        unreadable the output is
        `SEC_ERROR_BAD_DATABASE: security library: bad database.` — a
        completely different message from "not found".
        """
        nss = self._resolve_nss_dir(nss_dir)
        state = _nss_db_state(nss)
        if state is False:
            return False  # database definitely absent, so the entry naturally does not exist
        if state is None:
            return None  # directory/database inaccessible — cannot determine
        code, msg = _run(["certutil", "-L", "-d", f"sql:{nss}", "-n", nickname])
        if code == 0:
            return True
        if _nss_missing_cert(msg):
            return False
        return None  # lock / permission / DB corrupt — cannot determine

    def nss_trust_flags(self, flags: str) -> str:
        """Full three-column flag string used when repairing SSL trust (**non-protocol method**).

        Only the platform knows the column semantics of the flag string; the
        component only feeds the result to `certutil -M`.
        """
        return _nss_repair_flags(flags)

    def nss_trusted(self, ca: Path) -> bool:
        """Whether this CA is in the NSS database **and carries the SSL trust flag**. Read-only, unprivileged.

        Two key points:
        - The criterion is **certificate content**, not the nickname. After
          Caddy regenerates its internal CA the old entry is still there, and
          deciding by nickname alone would falsely report "trusted";
          conversely, an old tool (devp) installed "Caddy Local Authority",
          whose nickname differs from this tool's, so looking only under our
          own nickname would never find it — and the tool would forever believe
          the CA is not installed.
        - A certificate alone **does not count** as trust: when the flags are
          ",," (no trust), Chrome still does not recognize it.
        """
        # `nss_entries` already turns read/parse failures into return values
        # (None/[]); do not wrap a broad except here — a real bug should
        # surface rather than be swallowed into "untrusted".
        entries = self.nss_entries(ca) or []
        return any(_nss_flags_trusted(flags) for _nickname, flags in entries)

    def _resolve_nss_dir(self, nss_dir: Path | None) -> Path:
        """Explicit database path wins; otherwise use this platform's default database."""
        return Path(nss_dir).expanduser() if nss_dir is not None else self.nss_db_path()

    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        """Both Linux backends use fixed paths and ignore the domain suffix (only macOS splits files by suffix)."""
        if backend == "resolved":
            return RESOLVED_CONF.exists()
        if backend == "system":
            return SYSTEM_DNSMASQ_CONF.exists()
        return False
