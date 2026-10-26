"""User-scope dnsmasq component: incremental config merge + systemd user unit.

Config is modified incrementally only -- the user's own
`~/.config/dnsmasq/dnsmasq.conf` may contain domain entries such as
`.develop`, `no-resolv`, upstream servers, `filter-AAAA`; the tool must not
rewrite the whole file, only guarantee that `port=`, `address=` entries for
this tool's domain, and (when needed) an upstream server exist.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.platform.base import Action
from devproxy.platform.base import Platform
from devproxy.platform.base import Status

DNSMASQ_CONF = Path("~/.config/dnsmasq/dnsmasq.conf")

# Linux distributions install dnsmasq at this fixed path. It is a separate
# constant so tests can point at a temporary file -- check verifies that it
# actually exists.
LINUX_DNSMASQ_BINARY = "/usr/sbin/dnsmasq"

# macOS does not ship dnsmasq -- Homebrew installs it under /opt/homebrew/sbin
# (Apple Silicon) or /usr/local/sbin (Intel). launchd does not honor PATH, so
# an absolute path is required.
MACOS_DNSMASQ_CANDIDATES = (
    "/opt/homebrew/sbin/dnsmasq",
    "/usr/local/sbin/dnsmasq",
    "/usr/sbin/dnsmasq",
)

# Public DNS added when there is no global upstream. Never add 127.0.0.53 (the
# systemd-resolved stub) -- resolved would be pointed back at the local
# dnsmasq, creating a resolution loop.
DEFAULT_UPSTREAMS = ("server=1.1.1.1", "server=8.8.8.8")


def _option_present(lines: list[str], name: str) -> bool:
    """Whether the config already has this option (`name=` with a value or the bare option `name`)."""
    prefix = name + "="
    for line in lines:
        s = line.strip()
        if s == name or s.startswith(prefix):
            return True
    return False


def _is_global_server(line: str) -> bool:
    """Whether `server=` is a **global** upstream.

    dnsmasq manual: `--server=/<domain>/<ip>` only serves that domain and the
    rest still use /etc/resolv.conf -- it does not constitute a global
    upstream (same for `server=1.2.3.4,domain`).
    """
    s = line.strip()
    if not s.startswith("server="):
        return False
    value = s[len("server=") :].strip()
    if not value:
        return False
    if "/" in value or "," in value:
        return False
    return True


def _ensure_port(out: list[str], port: int) -> None:
    """Keep only one `port=` and its value must be this round's port; all other lines are kept as-is."""
    kept: list[str] = []
    seen_port = False
    for line in out:
        if line.strip().startswith("port="):
            if not seen_port:
                kept.append(f"port={port}")
                seen_port = True
            continue
        kept.append(line)
    if not seen_port:
        kept.append(f"port={port}")
    out[:] = kept


def _ensure_addresses(out: list[str], suffix: str) -> None:
    """Ensure exactly one IPv4 and one IPv6 `address=` entry exist for this tool's domain."""
    suffix = suffix.lstrip(".")
    present = {line.strip() for line in out}
    for addr in (f"address=/.{suffix}/127.0.0.1", f"address=/.{suffix}/::1"):
        if addr not in present:
            out.append(addr)
            present.add(addr)


def _ensure_loopback(out: list[str]) -> None:
    """Ensure it listens on loopback only.

    Without these two lines dnsmasq binds 0.0.0.0:5353 and becomes an open
    recursive resolver on the LAN (a DNS amplification attack surface). If
    the user already set them, keep them unchanged.
    """
    if not _option_present(out, "listen-address"):
        out.append("listen-address=127.0.0.1")
    if not _option_present(out, "bind-interfaces"):
        out.append("bind-interfaces")


def _ensure_upstream(out: list[str]) -> None:
    """Ensure the upstream config cannot form a resolved <-> dnsmasq resolution loop.

    Upstream rules (per the dnsmasq manual):
    - The manual states that `server=` does **not** suppress reading
      /etc/resolv.conf; only `--no-resolv` does. This tool's resolved drop-in
      already points global DNS at dnsmasq, so /etc/resolv.conf (the
      127.0.0.53 stub on resolved machines) must never be used as an
      upstream -- otherwise resolved <-> dnsmasq point at each other and
      resolution of ordinary domains fails machine-wide. Therefore: unless
      the user explicitly sets `resolv-file=` (a self-chosen resolver file,
      mutually exclusive with no-resolv), always ensure `no-resolv` exists.
    - Domain-specific upstreams (`server=/corp/10.0.0.1`) do not count as
      global upstreams; add public DNS when there is no global upstream, and
      never add local stubs such as 127.0.0.53.
    - When the user explicitly writes `resolv-file=`, never touch upstreams:
      it is a self-chosen resolver file, neither coexists with no-resolv nor
      should be mixed with public DNS.
    """
    has_resolv_file = _option_present(out, "resolv-file")
    if not has_resolv_file and not _option_present(out, "no-resolv"):
        out.append("no-resolv")
    if not has_resolv_file and not any(_is_global_server(line) for line in out):
        present = {line.strip() for line in out}
        for upstream in DEFAULT_UPSTREAMS:
            if upstream not in present:
                out.append(upstream)
                present.add(upstream)


def merge_dnsmasq_config(existing: str, *, port: int, suffix: str) -> str:
    """Incrementally modify the dnsmasq config: ensure port, address, listen scope and upstream exist, keeping everything else as-is.

    Never rewrite the whole file -- the user may have their own domain entries
    such as .develop. Each kind of normalization is split into its own small
    function; this one only orchestrates the order.
    """
    out = existing.splitlines()
    _ensure_port(out, port)
    _ensure_addresses(out, suffix)
    _ensure_loopback(out)
    _ensure_upstream(out)
    return "\n".join(out) + "\n"


class DnsmasqComponent:
    name = "dnsmasq"

    def __init__(
        self,
        platform: Platform,
        paths: Paths,
        config: Config,
        conf_path: Path | None = None,
    ):
        self.platform = platform
        self.paths = paths
        self.config = config
        # The config path is injectable: tests point at tmp_path and never
        # write the real ~/.config/dnsmasq/
        self.conf_path = conf_path or DNSMASQ_CONF.expanduser()

    # -- check ---------------------------------------------------------
    def _service_hint(self) -> str:
        """Service-related repair hint (different commands per platform).

        macOS users cannot be told to run systemctl -- there is no systemd
        there.
        """
        if self.platform.name == "mac":
            return f"launchctl kickstart -k gui/{os.getuid()}/com.devproxy.dnsmasq"
        return "systemctl --user start dnsmasq"

    def _binary_hint(self) -> str:
        """Actionable hint for installing dnsmasq (different package managers per platform)."""
        if self.platform.name == "mac":
            return "on macOS, run brew install dnsmasq first"
        return "install dnsmasq with your distribution's package manager (e.g. sudo apt install dnsmasq)"

    def _binary_status(self) -> Status | None:
        """Return a failure status when the executable is missing; None when it is fine.

        Without this check, plan on a clean macOS would write a made-up
        Homebrew path into the launchd plist, and the error would only surface
        at bootstrap with completely unrelated wording.
        """
        try:
            binary = self._dnsmasq_binary()
        except RuntimeError as e:
            return Status(ok=False, detail=str(e), hint=self._binary_hint())
        if not Path(binary).exists():
            return Status(
                ok=False,
                detail=f"dnsmasq executable not found: {binary}",
                hint=self._binary_hint(),
            )
        return None

    def check(self) -> Status:
        # Check the binary first: without dnsmasq installed, the later
        # "service not running" hint would only mislead.
        missing_binary = self._binary_status()
        if missing_binary is not None:
            return missing_binary
        if not self.platform.service_active("dnsmasq"):
            return Status(
                ok=False,
                detail="dnsmasq service is not running",
                hint=self._service_hint(),
            )
        if not self.conf_path.exists():
            return Status(
                ok=False,
                detail=f"dnsmasq config does not exist: {self.conf_path}",
                hint="devproxy setup will create it",
            )
        # The readiness criterion must match plan: when the file exists but
        # its content was modified, doctor must not report green on a machine
        # that cannot resolve *.test.
        try:
            existing = self.conf_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # When it cannot be read, it cannot be disproven; fall back to the
            # existence criterion (same as the dns component).
            return Status(
                ok=True, detail="dnsmasq is running (config unreadable, content not verified)"
            )
        merged = merge_dnsmasq_config(
            existing, port=self.platform.dns_port, suffix=self.config.domain_suffix
        )
        if existing != merged:
            return Status(
                ok=False,
                detail="dnsmasq config is missing or deviates from tool-managed entries",
                hint="devproxy setup will rewrite it",
            )
        return Status(ok=True, detail="dnsmasq is running")

    # -- plan ----------------------------------------------------------
    def plan(self) -> list[Action]:
        """Return an empty list when both config and service are ready -- only then can setup consider it a no-op."""
        existing = ""
        if self.conf_path.exists():
            existing = self.conf_path.read_text(encoding="utf-8")
        merged = merge_dnsmasq_config(
            existing, port=self.platform.dns_port, suffix=self.config.domain_suffix
        )
        if self.platform.service_active("dnsmasq") and existing == merged:
            return []
        return [
            Action(
                description="merge dnsmasq config (keep existing domain entries)",
                kind="write",
                path=self.conf_path,
                content=merged,
            ),
            Action(
                description=(
                    "install and start the dnsmasq LaunchAgent (macOS)"
                    if self.platform.name == "mac"
                    else "install and start dnsmasq.service (systemd user)"
                ),
                kind="run",
                command=["install-service", "dnsmasq"],
            ),
        ]

    # -- apply ---------------------------------------------------------
    def apply(self, actions: list[Action]) -> None:
        for action in actions:
            if action.kind == "write" and action.path is not None:
                action.path.parent.mkdir(parents=True, exist_ok=True)
                self._backup_if_changed(action.path, action.content)
                action.path.write_text(action.content, encoding="utf-8")
                continue
            if action.command[:1] == ["install-service"]:
                was_active = self.platform.service_active("dnsmasq")
                self.platform.install_service(
                    "dnsmasq",
                    exec_start=self._exec_start(),
                    exec_reload=self._exec_reload(),
                    env={},
                    description=(
                        "dnsmasq for devproxy (LaunchAgent)"
                        if self.platform.name == "mac"
                        else "dnsmasq for devproxy (user scope)"
                    ),
                )
                self.platform.enable_service("dnsmasq")
                if was_active:
                    # enable --now does not restart an already-active unit, so
                    # the new config would stay on disk without taking effect.
                    # Keep this consistent with caddy's install branch.
                    self.platform.restart_service("dnsmasq")
                continue
            raise RuntimeError(f"unknown action: {action.command}")

    def _backup_if_changed(self, path: Path, content: str) -> None:
        """Back up the user's existing config before rewriting it (Plan 2 Global Constraints).

        Never write a backup file when the content is unchanged -- otherwise
        every setup would pile garbage into the state directory. Read failure
        must block the write: the contract is "back up, then overwrite", and
        silently returning would make the caller think no backup is needed and
        then clobber the original file directly.
        """
        if path != self.conf_path or not path.exists():
            return
        try:
            original = path.read_bytes()
        except OSError as e:
            raise RuntimeError(
                f"failed to read the existing dnsmasq config; refusing to overwrite: {path}: {e}"
            ) from e
        if original == content.encode("utf-8"):
            return
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = self.paths.backup_dir / f"dnsmasq.conf.{stamp}.bak"
        # Consecutive applies within the same second (scripts/CI) must not
        # overwrite each other's backup.
        serial = 1
        while backup.exists():
            backup = self.paths.backup_dir / f"dnsmasq.conf.{stamp}.{serial}.bak"
            serial += 1
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(original)

    # -- verify --------------------------------------------------------
    def verify(self) -> Status:
        """Beyond the service being active, it must actually resolve *.test -- writing the config correctly does not mean dnsmasq loaded it (port conflicts, startup failures, or a unit pointing at another conf-dir can all fool check)."""
        if not self.platform.service_active("dnsmasq"):
            return Status(
                ok=False,
                detail="dnsmasq service is not active",
                hint=self._service_hint(),
            )
        name = f"foo.{self.config.domain_suffix}"
        answer = self.platform.dns_query(name)
        if answer not in ("127.0.0.1", "::1"):
            hint = (
                f"launchctl kickstart -k gui/{os.getuid()}/com.devproxy.dnsmasq"
                if self.platform.name == "mac"
                else "systemctl --user restart dnsmasq"
            )
            return Status(
                ok=False,
                detail=(
                    f"dnsmasq does not resolve {name} to this machine ({answer or 'no response'})"
                ),
                hint=f"{hint}; dig @127.0.0.1 -p {self.platform.dns_port} {name}",
            )
        return Status(ok=True, detail=f"dnsmasq is running ({name} -> {answer})")

    # -- unit ----------------------------------------------------------
    def _conf_dir_spec(self) -> str:
        """Express the home directory as %h in the unit; use an absolute path when the config directory is not under the home directory.

        Must resolve first: systemd resolves relative paths against /, so
        `--conf-dir=rel` would point under the root directory and the unit
        would not start at all.

        macOS launchd does not understand %h -- always write an absolute path.
        """
        conf_dir = self.conf_path.resolve().parent
        if self.platform.name == "mac":
            return str(conf_dir)
        try:
            rel = conf_dir.relative_to(Path.home().resolve())
        except ValueError:
            return str(conf_dir)
        return "%h" if str(rel) == "." else f"%h/{rel}"

    def _dnsmasq_binary(self) -> str:
        """Absolute path of the dnsmasq executable.

        On Linux it is fixed at /usr/sbin/dnsmasq; macOS does not ship dnsmasq
        and Homebrew installs it under /opt/homebrew/sbin or /usr/local/sbin
        -- pick whichever exists.

        Fixed paths take precedence over PATH: launchd does not honor PATH,
        and the one on PATH may not be the Homebrew install (an old version
        with a different prefix, or one installed by another package manager).
        """
        if self.platform.name != "mac":
            return LINUX_DNSMASQ_BINARY
        for candidate in MACOS_DNSMASQ_CANDIDATES:
            if Path(candidate).exists():
                return candidate
        found = shutil.which("dnsmasq")
        if found:
            return found
        # Never return a made-up path: it would be written into the launchd
        # plist, and the error would only surface at bootstrap with unrelated
        # wording.
        raise RuntimeError("dnsmasq executable not found; on macOS run brew install dnsmasq first")

    def _exec_start(self) -> list[str]:
        """The dnsmasq start command.

        By default dnsmasq forks and the parent exits -- Type=simple would
        consider the main process finished immediately and the unit would go
        inactive. Use `--keep-in-foreground` so systemd watches the real
        process. With `--keep-in-foreground` a pid file is no longer needed
        (which also removes the dependency on the `~/.cache` directory
        existing).
        """
        return [
            self._dnsmasq_binary(),
            "--keep-in-foreground",
            f"--conf-dir={self._conf_dir_spec()}",
        ]

    def _exec_reload(self) -> list[str] | None:
        """Reload config: send HUP to dnsmasq so it re-reads conf-dir.

        launchd plists have no ExecReload -- return None on macOS and rely on
        kickstart to restart the service.
        """
        if self.platform.name == "mac":
            return None
        return ["/bin/kill", "-HUP", "$MAINPID"]
