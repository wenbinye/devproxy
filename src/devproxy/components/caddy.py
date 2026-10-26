"""Caddy component: binary download and verification, setcap authorization, systemd unit, main Caddyfile.

The Caddy version is pinned and the sha256 is built into the source -- the
checksums.txt published upstream does not match the actually hosted assets
(measured: neither the tar.gz nor the deb matches), so that file cannot be
trusted.
"""

from __future__ import annotations

import hashlib
import os
import platform as _platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from devproxy.components.base import has_localhost_site
from devproxy.components.base import probe_url
from devproxy.core.caddy_env import load_extra_env
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.platform.base import Action
from devproxy.platform.base import Platform
from devproxy.platform.base import Status
from devproxy.platform.base import unit_exec_argv

CADDY_VERSION = "2.11.7"

CADDY_SHA256 = {
    ("linux", "amd64"): "727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b",
    ("linux", "arm64"): "d8fc6d179a5d283028a472a5618564f6ad8a86fed513e64f032b3b0b7cc45e42",
    ("mac", "arm64"): "cda3030e5d5b13eb9f0b6fb541037f633bddd958c070c5f561d7a5cccb581665",
    ("mac", "amd64"): "110c53e5c093e4cfce6eda787f94f23bef913c309f620fab26b4f678031841ad",
}

RELEASE_BASE = "https://github.com/caddyserver/caddy/releases/download/v{version}"


def caddy_asset_name(os_name: str, arch: str, version: str = CADDY_VERSION) -> str:
    """Asset name. Note that macOS uses mac, not darwin."""
    return f"caddy_{version}_{os_name}_{arch}.tar.gz"


def current_platform_key() -> tuple[str, str]:
    """Return (os, arch). Unknown platforms raise outright; never silently fall back to amd64."""
    system = _platform.system().lower()
    machine = _platform.machine().lower()
    if system == "darwin":
        os_name = "mac"
    elif system == "linux":
        os_name = "linux"
    else:
        raise RuntimeError(f"unsupported platform: {system}/{machine}")
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64"):
        arch = "amd64"
    else:
        raise RuntimeError(f"unsupported platform: {system}/{machine}")
    return os_name, arch


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _exec_line_argv(text: str, field: str) -> list[str] | None:
    """Extract the argv of a given `Exec*=` field from systemd unit text.

    launchd plists have no such fields (return None) -- the caller skips the
    check accordingly.
    """
    prefix = f"{field}="
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            args = line[len(prefix) :].split()
            return args or None
    return None


def _binary_version(binary: Path) -> str:
    """Run `caddy version` and return the first field (e.g. v2.11.7); return an empty string on failure."""
    try:
        proc = subprocess.run([str(binary), "version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    out = (proc.stdout or proc.stderr).strip()
    return out.split()[0] if out else ""


def download_caddy(dest: Path, version: str = CADDY_VERSION) -> None:
    """Download, verify, then extract the caddy binary. A failed verification must raise and leave no half-written file.

    Verification has two layers: the built-in sha256 (the downloaded package)
    and `caddy version` (the extracted binary). Write with a same-directory
    temp file + os.replace -- overwriting an old binary that is still running
    triggers ETXTBSY, and a half-written binary would be treated as usable by
    subsequent actions.
    """
    os_name, arch = current_platform_key()
    key = (os_name, arch)
    if key not in CADDY_SHA256:
        raise RuntimeError(f"unsupported platform: {os_name}/{arch}")
    asset = caddy_asset_name(os_name, arch, version)
    url = RELEASE_BASE.format(version=version) + "/" + asset
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".caddy-download-", dir=dest.parent)
    tmp_dest = Path(tmp_name)
    try:
        with tempfile.TemporaryDirectory(prefix="devproxy-caddy-") as tmp:
            tgz = Path(tmp) / asset
            with urllib.request.urlopen(url, timeout=120) as r, open(tgz, "wb") as f:
                shutil.copyfileobj(r, f)
            actual = _sha256_file(tgz)
            expected = CADDY_SHA256[key]
            if actual != expected:
                raise RuntimeError(
                    f"caddy package verification failed ({asset})\n"
                    f"  expected sha256: {expected}\n  actual sha256: {actual}"
                )
            with tarfile.open(tgz) as tf:
                member = tf.getmember("caddy")
                src = tf.extractfile(member)
                if src is None:
                    raise RuntimeError(f"cannot read the caddy file from the caddy package: {tgz}")
                with src, os.fdopen(fd, "wb") as out:
                    fd = -1  # Now owned by fdopen; avoid closing it twice
                    shutil.copyfileobj(src, out)
        tmp_dest.chmod(0o755)
        actual_version = _binary_version(tmp_dest)
        if actual_version != f"v{version}":
            raise RuntimeError(
                f"caddy binary version mismatch: expected v{version}, "
                f"actual {actual_version or 'not executable'}"
            )
        os.replace(tmp_dest, dest)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp_dest.unlink(missing_ok=True)


class CaddyComponent:
    name = "caddy"

    def __init__(
        self,
        platform: Platform,
        paths: Paths,
        config: Config,
        downloader: Callable[[Path], None] | None = None,
    ):
        self.platform = platform
        self.paths = paths
        self.config = config
        # Downloading is a network operation; default to the real downloader,
        # but tests can inject a fake to stay offline.
        self._downloader = downloader or download_caddy
        # Environment variables migrated over (such as MINIKUBE_IP) -- user
        # sites' {$MINIKUBE_IP:…} depends on them and they must be carried
        # over when reinstalling the unit.
        self.extra_env: dict[str, str] = load_extra_env(paths)

    # -- criteria ------------------------------------------------------
    def _caddyfile_current(self) -> bool:
        """Whether the main Caddyfile exists and its content matches what this component would generate."""
        expected = self._main_caddyfile()
        try:
            return (
                self.paths.caddyfile.exists()
                and self.paths.caddyfile.read_text(encoding="utf-8") == expected
            )
        except (OSError, UnicodeDecodeError):
            return False

    def _read_unit_text(self, unit: Path) -> str | None:
        """Read unit content; when normal reads fail (insufficient permissions), fall back to a **non-interactive** privileged read.

        macOS system-scope units live in /Library/LaunchDaemons -- a normal
        user cannot read them when permissions or ownership are wrong.
        "Cannot read" must never mean "unit is stale": otherwise doctor would
        report red forever and setup would reinstall the unit and ask for a
        password every time.

        This must go through the protocol's **non-interactive** privileged
        read (`sudo -n` on Linux): doctor promises read-only, side-effect-free
        behavior and must never pop a password prompt in the user's face just
        because of an unreadable unit. `privilege()` constructs an interactive
        elevated session (which may prompt for a password), so this never
        falls back to it.
        """
        try:
            return unit.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass
        try:
            return self.platform.privilege_read(unit)
        except (OSError, RuntimeError):
            # Even if the platform implementation itself fails, honor check's
            # "never raises" contract.
            return None

    def _unit_current(self) -> bool:
        """Whether the installed unit points ExecStart at this tool's main Caddyfile and binary.

        Checking only --config is not enough: after changing caddy_bin in the
        config, the installed unit still runs the old binary -- "unit is
        stale" can never be detected and the service keeps using the old
        binary.

        The criterion is the **entire command line** (the argv prefix must
        match exactly), not a substring: just checking "the path appears"
        would also declare units with mismatched argument pairing as current
        -- and "is the unit current" is exactly what setup uses to decide
        whether to reinstall.

        Both unit formats must be recognized: systemd's ExecStart= line and
        launchd's ProgramArguments array (the latter is an array, compared
        element by element).

        ExecReload is also checked for systemd units only (launchd has no such
        field): when `admin_addr` changes but ExecStart does not, the old
        unit's `--address` is already stale and the unit must be reinstalled.
        Units installed by older versions may have no ExecReload line; treat
        that case as "compatible" and let it pass -- otherwise doctor would
        needlessly report red once after an upgrade.
        """
        unit = self.platform.service_unit_path("caddy")
        if not unit.exists():
            return False
        text = self._read_unit_text(unit)
        if text is None:
            # If it cannot be read, it cannot be called stale -- better to let
            # it pass than have doctor report red forever.
            return True
        argv = unit_exec_argv(text)
        if argv is None:
            return False
        expected = [
            str(self.config.caddy_bin_path),
            "run",
            "--config",
            str(self.paths.caddyfile),
        ]
        if argv[: len(expected)] != expected:
            return False
        installed_reload = _exec_line_argv(text, "ExecReload")
        if installed_reload is None:
            return True
        return installed_reload == [
            str(self.config.caddy_bin_path),
            "reload",
            "--config",
            str(self.paths.caddyfile),
            "--address",
            self.config.admin_addr,
        ]

    def _need_binary(self, version: str | None = None) -> bool:
        """Binary criterion: missing, or version not matching the pinned version (including "version unreadable").

        check and plan must share it -- otherwise the same line would say both
        "ready" and "download", and every setup would download again.
        `version` may be supplied by the caller from an already-measured
        result, to avoid running `caddy version` again in the same round.
        """
        binary = self.config.caddy_bin_path
        if not binary.exists():
            return True
        if version is None:
            version = self.platform.binary_version(binary)
        return version != f"v{CADDY_VERSION}"

    def _service_hint(self) -> str:
        """Service-related repair hint (different commands per platform)."""
        if self.platform.name == "mac":
            return "launchctl print system/com.devproxy.caddy"
        return "systemctl --user status caddy"

    # -- check ---------------------------------------------------------
    def check(self) -> Status:
        binary = self.config.caddy_bin_path
        if not binary.exists():
            return Status(
                ok=False,
                detail=f"caddy binary does not exist: {binary}",
                hint=f"devproxy setup will download {CADDY_VERSION} automatically",
            )
        version = self.platform.binary_version(binary)
        if self._need_binary(version):
            return Status(
                ok=False,
                detail=(
                    f"caddy version mismatch: expected v{CADDY_VERSION}, "
                    f"actual {version or 'unreadable'}"
                ),
                hint="devproxy setup will replace it with the pinned version",
            )
        if not self._caddyfile_current():
            return Status(
                ok=False,
                detail=f"main Caddyfile is missing or stale: {self.paths.caddyfile}",
                hint="devproxy setup will regenerate the main Caddyfile",
            )
        if not self._unit_current():
            return Status(
                ok=False,
                detail="caddy service unit does not point at this tool's Caddyfile or binary",
                hint=(
                    "devproxy setup will reinstall the caddy LaunchDaemon"
                    if self.platform.name == "mac"
                    else "devproxy setup will reinstall the systemd user unit"
                ),
            )
        if not self.platform.service_active("caddy"):
            return Status(
                ok=False,
                detail="caddy service is not running",
                hint=(
                    "launchctl kickstart -k system/com.devproxy.caddy"
                    if self.platform.name == "mac"
                    else "systemctl --user start caddy"
                ),
            )
        # linger must also be part of the readiness criterion: without linger
        # the user service does not start at boot, and after a reboot caddy
        # would not be there at all -- keep this consistent with plan's
        # criterion.
        if not self.platform.linger_enabled():
            return Status(
                ok=False,
                detail="linger is not enabled (user services will not start at boot)",
                hint="devproxy setup (runs loginctl enable-linger, requires sudo)",
            )
        return Status(ok=True, detail=f"caddy {CADDY_VERSION} is ready")

    # -- plan ----------------------------------------------------------
    def plan(self) -> list[Action]:
        """Return actions for missing items; return an empty list when everything is ready.

        The main Caddyfile and the unit content are **independent criteria**:
        this machine has actually seen the binary version right and the
        service active, while the unit still pointed at the old Caddyfile and
        the main Caddyfile did not exist at all, making setup report "ready"
        while `up`'s reload was bound to fail. setcap is tied to
        download/service -- re-authorization is only needed when the binary
        was newly downloaded or the service is not up.
        """
        binary = self.config.caddy_bin_path
        # One snapshot: probing repeatedly within the same round would produce
        # self-contradictory branches if state changes.
        version = self.platform.binary_version(binary) if binary.exists() else ""
        need_binary = self._need_binary(version)
        need_caddyfile = not self._caddyfile_current()
        need_unit = not self._unit_current()
        caddy_active = self.platform.service_active("caddy")
        need_service = not caddy_active
        need_linger = not self.platform.linger_enabled()

        actions: list[Action] = []
        if need_binary:
            actions.append(
                Action(
                    description=f"download caddy {CADDY_VERSION} and verify sha256",
                    kind="run",
                    command=["download-caddy", str(binary)],
                )
            )
        if need_binary or need_service:
            # Re-authorize unless fully ready: when the previous round's
            # download succeeded but setcap failed (the user declined sudo),
            # the binary is in place but the service is not up -- this round
            # must redo setcap. macOS has no setcap: binding 80/443 relies on
            # the LaunchDaemon running as root.
            if self.platform.name != "mac":
                actions.append(
                    Action(
                        description="authorize caddy to bind 80/443 (setcap)",
                        kind="run",
                        command=["setcap", "cap_net_bind_service=+ep", str(binary)],
                        privileged=True,
                    )
                )
        if need_caddyfile:
            actions.append(
                Action(
                    description="generate the main Caddyfile",
                    kind="write",
                    path=self.paths.caddyfile,
                    content=self._main_caddyfile(),
                )
            )
        if need_service or need_unit:
            actions.append(
                Action(
                    description=(
                        "install and start the caddy LaunchDaemon (macOS)"
                        if self.platform.name == "mac"
                        else "install and start caddy.service (systemd user)"
                    ),
                    kind="run",
                    command=["install-service", "caddy"],
                    # macOS's caddy is a root daemon under /Library/LaunchDaemons
                    # (binding 80/443 requires root); on Linux it is a **user**
                    # unit under ~/.config/systemd/user and needs no elevation.
                    # Marking it the wrong way has serious consequences in both
                    # directions:
                    # missing -> the sudo list lies, the non-interactive guard
                    # is bypassed, and a password prompt appears mid-run;
                    # extra -> a phantom sudo entry on Linux, and
                    # non-interactive environments wrongly exit with 4.
                    privileged=self.platform.name == "mac",
                )
            )
        if need_linger:
            actions.append(
                Action(
                    description="enable linger (user services start at boot without login)",
                    kind="run",
                    command=["enable-linger"],
                    privileged=True,
                )
            )
        if (need_binary or need_caddyfile) and caddy_active and not need_unit:
            # A running Caddy does not notice that the binary or main
            # Caddyfile changed:
            # - binary replaced: the running process is still the old version;
            # - only the main Caddyfile changed (http_port / admin_addr /
            #   storage ...): neither the unit nor the service changed, so only
            #   writing the file means nothing takes effect.
            # Cases where the unit/service itself must be reinstalled are not
            # restarted here -- the install-service branch handles that
            # (was_active -> restart), avoiding a double restart.
            # Put this last: setcap must be done first, otherwise it cannot
            # bind 80/443 after the restart.
            if need_binary and need_caddyfile:
                description = "restart the caddy service to use the new binary and main Caddyfile"
            elif need_binary:
                description = (
                    f"restart the caddy service to use the newly downloaded {CADDY_VERSION}"
                )
            else:
                description = "restart the caddy service to load the new main Caddyfile"
            actions.append(
                Action(
                    description=description,
                    kind="run",
                    command=["restart-service", "caddy"],
                )
            )
        return actions

    # -- apply ---------------------------------------------------------
    def apply(self, actions: list[Action]) -> None:
        binary = self.config.caddy_bin_path
        for action in actions:
            if action.kind == "write" and action.path is not None:
                action.path.parent.mkdir(parents=True, exist_ok=True)
                action.path.write_text(action.content, encoding="utf-8")
                continue
            if action.command[:1] == ["download-caddy"]:
                # Downloading is the component's job: pinned version + sha256
                # verification, raise on failure, leave no half-written file,
                # so later actions (setcap etc.) never run against a bad
                # binary.
                binary.parent.mkdir(parents=True, exist_ok=True)
                self._downloader(binary)
                continue
            if action.command[:1] == ["setcap"]:
                code, msg = self.platform.privilege().run(action.command)
                if code != 0:
                    raise RuntimeError(
                        "setcap failed (caddy cannot bind 80/443): "
                        f"{msg}; install libcap2-bin first (sudo apt install libcap2-bin)"
                    )
                continue
            if action.command[:1] == ["install-service"]:
                was_active = self.platform.service_active("caddy")
                self.platform.install_service(
                    "caddy",
                    exec_start=[
                        str(binary),
                        "run",
                        "--config",
                        str(self.paths.caddyfile),
                    ],
                    exec_reload=[
                        str(binary),
                        "reload",
                        "--config",
                        str(self.paths.caddyfile),
                        "--address",
                        self.config.admin_addr,
                    ],
                    exec_stop=[str(binary), "stop"],
                    env=self.extra_env,
                    description="Caddy local HTTP/HTTPS server (managed by devproxy)",
                )
                self.platform.enable_service("caddy")
                if was_active:
                    # The unit file has changed (e.g. ExecStart points at the
                    # new Caddyfile); enable --now does not restart an
                    # already-active unit, so an explicit restart is required.
                    self.platform.restart_service("caddy")
                continue
            if action.command[:1] == ["enable-linger"]:
                code, msg = self.platform.enable_linger()
                if code != 0:
                    raise RuntimeError(f"loginctl enable-linger failed: {msg}")
                continue
            if action.command[:2] == ["restart-service", "caddy"]:
                self.platform.restart_service("caddy")
                continue
            raise RuntimeError(f"unknown action: {action.command}")

    def _has_configured_site(self) -> bool:
        """Whether there is any real site (comments/blank lines do not count).

        On a clean machine the Caddyfile only imports two empty files, so
        Caddy does not listen on 443 -- TLS probing is not possible then, and
        this should stay "ok + note" rather than marking setup as failed.
        """
        for path in (self.paths.routes_caddy, self.paths.sites_caddy):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(line.strip() and not line.strip().startswith("#") for line in text.splitlines()):
                return True
        return False

    def _has_localhost_site(self) -> bool:
        """Whether sites.caddy contains a localhost site block (exact match on the site address)."""
        return has_localhost_site(self.paths.sites_caddy)

    def _probe_url(self) -> str | None:
        """TLS probe target: prefer the first registered route, otherwise localhost when a localhost site actually exists.

        The criterion is provided in one place by `components.base.probe_url`
        -- the certificates component uses the same implementation, avoiding
        drift between the two again. Return None when there is nothing to
        probe.
        """
        return probe_url(self.paths.routes_json, self.paths.sites_caddy)

    # -- verify --------------------------------------------------------
    def verify(self) -> Status:
        """Post-apply self-check: binary present, service active, and the TLS handshake actually succeeds."""
        if not self.config.caddy_bin_path.exists():
            return Status(
                ok=False,
                detail=f"caddy binary is missing: {self.config.caddy_bin_path}",
                hint=(
                    f"devproxy setup will download it; "
                    f"use {self._service_hint()} to inspect the service"
                ),
            )
        if not self.platform.service_active("caddy"):
            return Status(
                ok=False,
                detail="caddy service is not active",
                hint=self._service_hint(),
            )
        if not self._has_configured_site():
            return Status(
                ok=True,
                detail="caddy service is running (no sites yet, skipping TLS probe)",
            )
        url = self._probe_url()
        if url is None:
            return Status(
                ok=True,
                detail=(
                    "caddy service is running (no routes and no localhost site, skipping TLS probe)"
                ),
            )
        if not self.platform.http_ok(url):
            return Status(
                ok=False,
                detail=f"TLS handshake failed for {url}",
                hint=f"{self._service_hint()}; check the Caddyfile and whether port 443 is in use",
            )
        return Status(ok=True, detail=f"caddy service is running, {url} is reachable")

    def _main_caddyfile(self) -> str:
        from devproxy.gateway.caddy import MainCaddyfileSpec
        from devproxy.gateway.caddy import build_main_caddyfile

        return build_main_caddyfile(
            MainCaddyfileSpec(
                admin_addr=self.config.admin_addr,
                storage=self.config.storage_path,
                routes_caddy=self.paths.routes_caddy,
                sites_caddy=self.paths.sites_caddy,
                http_port=self.config.http_port,
                https_port=self.config.https_port,
            )
        )
