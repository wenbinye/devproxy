"""Platform capability protocol. The component layer depends only on these capabilities and never invokes system commands directly."""

from __future__ import annotations

import base64
import binascii
import hashlib
import plistlib
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Protocol
from typing import runtime_checkable

# Global convention for certificate trust: components and platform
# implementations share this single copy so the two sides cannot drift apart.
CA_NICKNAME = "devproxy Local CA"
SYSTEM_CA_DEST = Path("/usr/local/share/ca-certificates/devproxy-local.crt")
SYSTEM_TRUST_DIR = Path("/etc/ssl/certs")

# **Template** for the default NSS database location: `~` is left unexpanded
# (pathlib does not understand the tilde). New code should use `nss_db_dir()`;
# `NSS_DIR` is kept only for compatibility with existing imports, and any
# consumer must call expanduser() itself, or it will look up
# "~/.pki/nssdb under the current directory".
NSS_DIR_TEMPLATE = "~/.pki/nssdb"
NSS_DIR = Path(NSS_DIR_TEMPLATE)


def nss_db_dir(home: Path | None = None) -> Path:
    """Default NSS database path (expanded, absolute).

    `~` is expanded / `home` is read on every call — a test that redirects HOME
    must take effect, so this cannot be an import-time constant.
    """
    base = Path(home) if home is not None else Path.home()
    return base / ".pki" / "nssdb"


def _pem_der_blocks(text: str) -> list[bytes]:
    """Extract the DER bytes of every certificate in a PEM text.

    Only base64 decoding, no X.509 parsing — the criterion for trusting or
    untrusting the CA is "the same bytes"; parsing the certificate structure
    would only add extra failure points.
    """
    blocks: list[bytes] = []
    buf: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("-----BEGIN CERTIFICATE"):
            inside = True
            buf = []
            continue
        if line.startswith("-----END CERTIFICATE"):
            inside = False
            try:
                blocks.append(base64.b64decode("".join(buf), validate=False))
            except (binascii.Error, ValueError):
                pass
            buf = []
            continue
        if inside:
            buf.append(line)
    return blocks


def cert_fingerprint(der: bytes) -> str:
    """Certificate fingerprint: SHA-256 of the DER (same meaning as `openssl x509 -fingerprint -sha256`)."""
    return hashlib.sha256(der).hexdigest().upper()


def pem_fingerprints(text: str) -> set[str]:
    """Set of SHA-256 fingerprints of every certificate in a PEM text."""
    return {cert_fingerprint(der) for der in _pem_der_blocks(text)}


def file_fingerprints(path: Path) -> set[str] | None:
    """Read a file and return the fingerprints of the certificates in it; None if it cannot be read or is not PEM."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return pem_fingerprints(text)


# The macOS counterpart: the CA goes into the system keychain (both Safari and
# Chrome honor it), while DNS routing uses per-domain resolver files
# (/etc/resolver/<suffix>) and leaves /etc/resolv.conf untouched.
SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"

# **Single source of truth** for DNS routing files: the component writes them,
# the platform probes them, and migration backups all must reference this.
# Writing a separate literal in each of the three places is bound to drift
# eventually — the component writes to A, the platform checks B, the migration
# backs up C, which shows up as setup saying success, doctor saying missing,
# and rollback being unable to delete the file that was actually changed.
RESOLVER_DIR = Path("/etc/resolver")
RESOLVED_CONF_DIR = Path("/etc/systemd/resolved.conf.d")
RESOLVED_CONF = RESOLVED_CONF_DIR / "devproxy.conf"
SYSTEM_DNSMASQ_CONF = Path("/etc/dnsmasq.d/devproxy.conf")


@dataclass
class Status:
    """Result of a component check. When ok=False, detail describes the current state and hint gives a runnable repair command."""

    ok: bool
    detail: str = ""
    hint: str = ""


def render_resolver_file(port: int) -> str:
    """/etc/resolver/<suffix> contents (macOS's per-domain resolver format).

    macOS natively supports per-domain resolvers; there is no need to touch
    /etc/resolv.conf. This lives here because the component layer (the DNS
    component) also uses it — components do not depend directly on a platform
    implementation.
    """
    return f"nameserver 127.0.0.1\nport {port}\n"


def unit_exec_argv(text: str) -> list[str] | None:
    """Parse the launch command line from service unit text.

    Both formats must be recognized:
    - systemd: ``ExecStart=/path/caddy run --config /path/Caddyfile``
    - launchd plist: the ``ProgramArguments`` array

    This is how the component decides whether "the unit is the current one" —
    plain substring matching would miss both wrong ordering and wrong argument
    pairing ("run --config" and "--config … run" would both pass), and it could
    not tell when the unit points at a different Caddyfile.
    """
    try:
        data = plistlib.loads(text.encode("utf-8"))
    except Exception:  # noqa: BLE001 — not a plist, treat it as a systemd unit
        data = None
    if isinstance(data, dict):
        argv = data.get("ProgramArguments")
        if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):
            return list(argv)
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("ExecStart="):
            continue
        args = line[len("ExecStart=") :].split()
        if args:
            return args
    return None


@dataclass
class Action:
    """A pending action to execute."""

    description: str
    # run: execute command; write: write path/content; delete: delete path
    kind: str = "run"  # "run" | "write" | "delete"
    command: list[str] = field(default_factory=list)
    path: Path | None = None
    content: str = ""
    privileged: bool = False


@runtime_checkable
class ElevatedSession(Protocol):
    """An elevated session: one password verification, subsequent operations reuse the same session."""

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
        """Install and register a service. The unit format is up to the platform (systemd / launchd).

        The component layer only says "what to run and which environment
        variables to carry" and never touches the unit format — otherwise the
        differences between systemd and launchd would leak into components.

        `exec_reload` / `exec_stop` are **optional capabilities**: a platform may
        ignore them. launchd has no corresponding plist fields, so
        `MacOSPlatform` deliberately discards these two values (see the comment
        in its implementation); callers must not assume every platform supports
        them.
        """

    def enable_service(self, name: str) -> None: ...

    def restart_service(self, name: str) -> None: ...

    def remove_service(self, name: str) -> None:
        """Stop and remove the service unit (systemd unit / launchd plist).

        Must be idempotent: succeed silently and raise no exception when
        neither the service nor the unit exists.
        """

    def service_unit_path(self, name: str) -> Path: ...

    def tail_service_log(self, name: str) -> None: ...

    def open_in_editor(self, path: Path, editor: str) -> int:
        """Open a file in the editor and return the editor's exit code.

        The editor must **inherit stdio** (it needs to take over the terminal) —
        same approach as tail_service_log. `editor` is a command string
        (possibly with arguments, e.g. "code -w"), which the caller splits into
        words. Raise FileNotFoundError when the editor does not exist.
        """

    def enable_linger(self) -> tuple[int, str]:
        """`loginctl enable-linger <user>`: make user units start at boot without requiring a login."""

    def linger_enabled(self) -> bool:
        """Whether the current user has linger enabled (read-only probe)."""

    def resolved_active(self) -> bool:
        """Whether systemd-resolved is actually running (the criterion for the auto DNS backend)."""

    def dns_query(self, name: str) -> str | None:
        """Resolve a domain via the path the tool expects, returning the first A/AAAA answer; None on failure."""

    def http_ok(self, url: str) -> bool:
        """Local HTTP(S) probe with an upper timeout bound; returns True on success (2xx/3xx)."""

    def ca_trusted(self, ca: Path) -> bool: ...

    def privilege_read(self, path: Path) -> str | None:
        """Read a privileged file **non-interactively**; return None if unreadable.

        Read-only commands (doctor) must have no side effects: never pop up a
        password prompt. When a regular user cannot read a privileged file,
        prefer "cannot read" over asking the user for a password.
        """

    def nss_trusted(self, ca: Path) -> bool:
        """Whether the certificate **content** stored in the NSS database (Chrome/Chromium's trust store) is this CA.

        Note: this is a content comparison, not "does an entry with the same
        name exist" — after Caddy regenerates the CA the old entry is still
        there, and deciding by nickname alone would falsely report "already
        trusted".
        """

    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        """Whether the DNS route is already configured. backend is one of "resolved" / "system" / "resolver".

        suffix is the domain suffix: macOS's resolver file is named by suffix
        (/etc/resolver/<suffix>), while Linux's two backends do not care about
        it, but the interface stays consistent.
        """
