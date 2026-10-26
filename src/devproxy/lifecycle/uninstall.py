"""uninstall orchestration: stop processes -> remove services -> clean DNS -> uninstall CA -> delete state files.

The order is a hard constraint: leftover child processes must be reaped first
(they still hold ports), then service units, then DNS routes; the CA is
uninstalled only by exact fingerprint match, and the tool's own state files are
deleted last. `--purge` directory deletion comes last so that the backup does
not disappear first and make the dnsmasq config unrestorable.

Same as setup: this module only orchestrates; all concrete system calls go
through the Platform protocol.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import ssl
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from devproxy.components.certs import CertComponent
from devproxy.components.dns import STATE_FILE as DNS_STATE_FILE
from devproxy.components.dnsmasq import DNSMASQ_CONF
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.core.paths import atomic_write_text
from devproxy.gateway.routes import load_table
from devproxy.gateway.runner import kill_pid_group
from devproxy.platform.base import RESOLVED_CONF
from devproxy.platform.base import RESOLVER_DIR
from devproxy.platform.base import SYSTEM_CA_DEST
from devproxy.platform.base import SYSTEM_DNSMASQ_CONF
from devproxy.platform.base import SYSTEM_KEYCHAIN
from devproxy.platform.base import Action
from devproxy.platform.base import Platform
from devproxy.platform.base import file_fingerprints
from devproxy.platform.base import nss_db_dir
from devproxy.platform.base import pem_fingerprints

# Kept consistent with CertComponent.ca_path: Caddy's default CA location.
CA_RELATIVE_PATH = Path("pki") / "authorities" / "local" / "root.crt"

# Naming of dnsmasq config backups: dnsmasq.conf.<UTC timestamp>[.<sequence>].bak
_DNSMASQ_BACKUP_RE = re.compile(r"^dnsmasq\.conf\.(\d{8}T\d{6}Z)(?:\.(\d+))?\.bak$")

# Text emitted by `certutil -D` when the entry definitely does not exist
# (matching the platform layer's criterion).
_NSS_NOT_FOUND = (
    "could not find certificate named",
    "could not find cert:",
    "pr_file_not_found_error",
)

# Family of text emitted by `security delete-certificate` when the entry is not found.
_MACOS_NOT_FOUND = (
    "could not find",
    "could not be found",
    "not found",
    "unable to find",
    "no such",
)


@dataclass(frozen=True)
class UninstallTargets:
    """System paths that tests can override. Production always uses the defaults."""

    resolved_conf: Path = RESOLVED_CONF
    system_dnsmasq_conf: Path = SYSTEM_DNSMASQ_CONF
    resolver_dir: Path = RESOLVER_DIR
    system_ca_dest: Path = SYSTEM_CA_DEST
    system_keychain: str = SYSTEM_KEYCHAIN
    nss_dir: Path = field(default_factory=nss_db_dir)
    dnsmasq_conf: Path = field(default_factory=lambda: DNSMASQ_CONF.expanduser())


@dataclass
class StepResult:
    """Execution result of one action."""

    description: str
    ok: bool
    detail: str = ""


@dataclass
class UninstallPlan:
    """Complete plan for one uninstall.

    `actions` are ordered by execution; `kept` lists things that must be
    explicitly reported to the user as "untouched"; `notes` are conditional
    explanations (migration archive, skip reasons, etc.).
    """

    platform: Platform | None = None
    targets: UninstallTargets = field(default_factory=UninstallTargets)
    actions: list[Action] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    migration_backup: Path | None = None
    # The CA is kept because of the "existed before migration" heuristic -- CLI
    # uses this to decide whether to ask the user "uninstall it?". Use a
    # structured field rather than substring matching in notes: it breaks as
    # soon as the wording changes.
    ca_kept: bool = False

    @property
    def needs_sudo(self) -> list[Action]:
        return [a for a in self.actions if a.privileged]

    @property
    def is_noop(self) -> bool:
        return not self.actions


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def build_plan(
    platform: Platform,
    paths: Paths,
    config: Config,
    *,
    purge: bool,
    remove_ca: bool,
    dry_run: bool = False,
    targets: UninstallTargets | None = None,
) -> UninstallPlan:
    """Compute uninstall steps.

    When `dry_run=True` only **filesystem** checks are performed: no probes that
    spawn child processes such as `service_active` / `ca_trusted` /
    `nss_entries`, so the preview has no side effects and needs no sudo. Exact
    probing happens only on real execution.
    """
    plan = UninstallPlan(platform=platform, targets=targets or UninstallTargets())
    probe = not dry_run

    _plan_route_processes(plan, paths)
    _plan_services(plan, platform, probe=probe)
    _plan_dns(plan, platform, config)
    _plan_dnsmasq_restore(plan, paths)
    _plan_ca(plan, platform, paths, config, remove_ca=remove_ca, probe=probe)
    _plan_state_files(plan, paths)
    _plan_purge(plan, paths, config, purge=purge)
    _plan_kept(plan, paths, config, purge=purge)
    _plan_migration_hint(plan, paths, purge=purge)
    return plan


def _plan_route_processes(plan: UninstallPlan, paths: Paths) -> None:
    """Read routes.json and plan an identity-verified cleanup for each mode=up record.

    When `proc_start` is missing, do **not** degrade to killing by bare pid --
    pids are reused, so it is better to leave a stray process than to kill an
    unrelated process group by mistake.
    """
    for route in load_table(paths.routes_json).routes:
        if route.mode != "up" or route.pid is None:
            continue
        if not route.proc_start:
            plan.notes.append(
                f"route {route.name} has no proc_start, so process identity cannot "
                f"be verified; skipping cleanup (will not kill by bare pid)"
            )
            continue
        plan.actions.append(
            Action(
                description=(
                    f"terminate the leftover process group for route {route.name} "
                    f"(pid {route.pid}, with start-time verification)"
                ),
                kind="run",
                command=["kill-pid-group", str(route.pid), route.proc_start],
            )
        )


def _plan_services(plan: UninstallPlan, platform: Platform, *, probe: bool) -> None:
    """caddy, dnsmasq: plan removal only when the unit exists or the service is still running."""
    for name in ("caddy", "dnsmasq"):
        path = platform.service_unit_path(name)
        installed = path.exists() or path.is_symlink()
        active = False
        if probe:
            try:
                active = platform.service_active(name)
            except Exception:  # noqa: BLE001 -- probe failure is treated as "not running"; the unit can still be cleaned up
                active = False
        if installed or active:
            plan.actions.append(
                Action(
                    description=f"stop and remove the {name} service unit",
                    kind="run",
                    command=["remove-service", name],
                )
            )


def _plan_dns(plan: UninstallPlan, platform: Platform, config: Config) -> None:
    """Delete DNS route files and make the resolver reload."""
    targets = plan.targets
    if platform.name == "mac":
        path = targets.resolver_dir / config.domain_suffix.lstrip(".")
        if path.exists() or path.is_symlink():
            plan.actions.append(
                Action(
                    description=f"delete macOS per-domain resolver file {path}",
                    kind="delete",
                    path=path,
                    privileged=True,
                )
            )
        return
    for path, service in (
        (targets.resolved_conf, "systemd-resolved"),
        (targets.system_dnsmasq_conf, "dnsmasq"),
    ):
        if not (path.exists() or path.is_symlink()):
            continue
        plan.actions.append(
            Action(
                description=f"delete DNS route file {path}",
                kind="delete",
                path=path,
                privileged=True,
            )
        )
        plan.actions.append(
            Action(
                description=f"restart {service} so the DNS route no longer applies",
                kind="run",
                command=["systemctl", "restart", service],
                privileged=True,
            )
        )


def _plan_dnsmasq_restore(plan: UninstallPlan, paths: Paths) -> None:
    """dnsmasq.conf is merged incrementally, so it cannot be deleted; if a backup exists, restore it to the pre-takeover version."""
    conf = plan.targets.dnsmasq_conf
    backup = latest_dnsmasq_backup(paths.backup_dir)
    if backup is None:
        if conf.exists():
            plan.kept.append(f"{conf} (no pre-change backup found; kept as-is)")
        return
    try:
        backup_text = backup.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise RuntimeError(
            f"failed to read the dnsmasq config backup, refusing to continue: {backup}: {e}"
        ) from e
    current = None
    if conf.exists():
        try:
            current = conf.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = None
    if current == backup_text:
        plan.kept.append(f"{conf} (already restored to the backup content; unchanged)")
        return
    plan.actions.append(
        Action(
            description=f"restore dnsmasq config {conf} from backup {backup.name}",
            kind="write",
            path=conf,
            content=backup_text,
        )
    )


def _plan_ca(
    plan: UninstallPlan,
    platform: Platform,
    paths: Paths,
    config: Config,
    *,
    remove_ca: bool,
    probe: bool,
) -> None:
    """Two rules for CA uninstall: exact fingerprint match + keep a migrated-in CA by default."""
    ca = CertComponent(platform, paths, config).ca_path
    if not ca.exists():
        return
    archived = paths.caddy_storage.exists()
    if archived and not remove_ca:
        plan.ca_kept = True
        plan.notes.append(
            "found a storage archive; the CA may be yours, so it was kept; "
            "to remove it, pass --remove-ca explicitly (or answer yes when prompted)"
        )
        plan.kept.append(
            f"CA root certificate: {ca} (existed before migration; sites may still use it)"
        )
        return
    if archived and remove_ca:
        plan.notes.append("chose to force-uninstall a CA that existed before migration")

    if platform.name == "mac":
        trusted = _probe_ca_trusted(platform, ca) if probe else True
        if trusted is not False:
            plan.actions.append(
                Action(
                    description="remove the CA root certificate from the system keychain by fingerprint",
                    kind="run",
                    command=[
                        "remove-macos-ca",
                        str(ca),
                        plan.targets.system_keychain,
                    ],
                    privileged=True,
                )
            )
        return

    _plan_nss_ca(plan, platform, ca, probe=probe)
    _plan_system_ca(plan, platform, ca, probe=probe)


def _plan_nss_ca(plan: UninstallPlan, platform: Platform, ca: Path, *, probe: bool) -> None:
    """NSS databases find entries by certificate content (fingerprint), not by nickname."""
    if not hasattr(platform, "nss_entries"):
        return
    entries: list[tuple[str, str]] | None = None
    if probe:
        try:
            entries = platform.nss_entries(ca, plan.targets.nss_dir)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 -- if it cannot be determined, fall back to "attempt cleanup"; apply rechecks
            entries = None
    if probe and entries == []:
        return
    if entries:
        for nickname in sorted({name for name, _flags in entries}):
            plan.actions.append(
                Action(
                    description=f"remove CA entry '{nickname}' from the NSS database by fingerprint",
                    kind="run",
                    command=[
                        "remove-nss-ca",
                        str(ca),
                        str(plan.targets.nss_dir),
                        nickname,
                    ],
                )
            )
        return
    # Cannot determine (database locked/unreadable) or dry-run: emit one tentative
    # cleanup; apply rechecks.
    plan.actions.append(
        Action(
            description="remove matching CA entries from the NSS database by fingerprint (Chrome/Chromium)",
            kind="run",
            command=["remove-nss-ca", str(ca), str(plan.targets.nss_dir)],
        )
    )


def _plan_system_ca(plan: UninstallPlan, platform: Platform, ca: Path, *, probe: bool) -> None:
    """Linux system trust store: delete the staged file + update-ca-certificates."""
    dest = plan.targets.system_ca_dest
    if probe:
        trusted = _probe_ca_trusted(platform, ca)
        # None (cannot determine) is treated conservatively as an attempt; a
        # lingering dest is also worth cleaning up as stale.
        needed = trusted is not False or dest.exists()
    else:
        # dry-run does not probe the trust store; estimate from file existence only.
        needed = dest.exists() or dest.is_symlink()
    if not needed:
        return
    plan.actions.append(
        Action(
            description=f"remove the CA from the system trust store by fingerprint and refresh ({dest})",
            kind="run",
            command=["remove-linux-system-ca", str(ca), str(dest)],
            privileged=True,
        )
    )


def _plan_state_files(plan: UninstallPlan, paths: Paths) -> None:
    """Delete state files generated by the tool; the user's sites.caddy / config.toml are not included."""
    for path in (
        paths.caddyfile,
        paths.routes_json,
        paths.routes_caddy,
        paths.lock_path,
        paths.state_dir / DNS_STATE_FILE,
        paths.caddy_storage,
        paths.caddy_env,
    ):
        if path.exists() or path.is_symlink():
            plan.actions.append(
                Action(description=f"delete tool state file {path}", kind="delete", path=path)
            )
    if paths.log_dir.exists() or paths.log_dir.is_symlink():
        plan.actions.append(
            Action(
                description=f"delete tool log directory {paths.log_dir}",
                kind="delete",
                path=paths.log_dir,
            )
        )


def _plan_purge(plan: UninstallPlan, paths: Paths, config: Config, *, purge: bool) -> None:
    """Delete only when "wipe storage too" is selected: Caddy storage, backup directory, downloaded caddy binary."""
    if not purge:
        return
    storage = config.storage_path
    if storage.exists() or storage.is_symlink():
        plan.actions.append(
            Action(
                description=(
                    f"delete Caddy storage {storage} (CA, certificates, and site data are removed too)"
                ),
                kind="delete",
                path=storage,
            )
        )
    if paths.backup_dir.exists() or paths.backup_dir.is_symlink():
        plan.actions.append(
            Action(
                description=(
                    f"delete backup directory {paths.backup_dir} "
                    f"(includes migration backups; rollback is impossible after deletion)"
                ),
                kind="delete",
                path=paths.backup_dir,
            )
        )
    binary = config.caddy_bin_path
    if binary.exists() or binary.is_symlink():
        plan.actions.append(
            Action(
                description=f"delete downloaded caddy binary {binary}",
                kind="delete",
                path=binary,
            )
        )


def _plan_kept(plan: UninstallPlan, paths: Paths, config: Config, *, purge: bool) -> None:
    """Explicitly list what is left alone by default -- uninstall must not "conveniently" wipe user assets."""
    plan.kept.extend(
        [
            f"{paths.sites_caddy} (your personal site config)",
            f"{paths.config_toml} (your config file)",
            "loginctl enable-linger (machine-wide setting; other user services may depend on it)",
        ]
    )
    if purge:
        return
    plan.kept.extend(
        [
            f"{config.storage_path} (Caddy storage: CA and certificates; your own Caddy may be using it)",
            f"{config.caddy_bin_path} (may be a caddy you installed yourself)",
            f"{paths.backup_dir} (rollback basis)",
        ]
    )


def _plan_migration_hint(plan: UninstallPlan, paths: Paths, *, purge: bool) -> None:
    """On a migrated machine: tell the user where the original files are and how to roll back."""
    backup = latest_migration_backup(paths.backup_dir)
    if backup is None:
        return
    if purge:
        plan.notes.append(
            f"--purge will delete the migration backup {backup}; after that, "
            f"devproxy setup --rollback {backup} can no longer restore"
        )
        return
    plan.migration_backup = backup


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def apply_plan(plan: UninstallPlan) -> list[StepResult]:
    """Execute each step and return results; a single failure does not block the remaining steps.

    CLI decides the exit code from the results: any ✗ means a run failure (1).
    """
    platform = plan.platform
    if platform is None:
        raise RuntimeError("UninstallPlan is missing platform; cannot execute")
    results: list[StepResult] = []
    for action in plan.actions:
        try:
            detail = _apply_one(platform, plan, action)
        except Exception as e:  # noqa: BLE001 -- one failed step continues cleaning up the rest
            results.append(StepResult(action.description, False, str(e)))
        else:
            results.append(StepResult(action.description, True, detail))
    return results


def _apply_one(platform: Platform, plan: UninstallPlan, action: Action) -> str:
    if action.kind == "delete":
        return _delete_path(platform, action.path, action.privileged)
    if action.kind == "write":
        if action.path is None:
            raise RuntimeError("write action is missing path")
        if action.privileged:
            platform.privilege().write(action.path, action.content)
        else:
            atomic_write_text(action.path, action.content)
        return ""
    if action.kind != "run":
        raise RuntimeError(f"unknown action kind: {action.kind}")
    command = list(action.command)
    if not command:
        raise RuntimeError("run action is missing command")
    head = command[0]
    if head == "kill-pid-group":
        killed = kill_pid_group(int(command[1]), command[2] or None)
        return (
            "terminated leftover process group"
            if killed
            else "process is gone or identity mismatch; skipped"
        )
    if head == "remove-service":
        platform.remove_service(command[1])
        return ""
    if head == "remove-nss-ca":
        nickname = command[3] if len(command) > 3 else None
        return _remove_nss_ca(platform, Path(command[1]), Path(command[2]), nickname)
    if head == "remove-linux-system-ca":
        return _remove_linux_system_ca(platform, Path(command[1]), Path(command[2]))
    if head == "remove-macos-ca":
        return _remove_macos_ca(platform, Path(command[1]), command[2])
    code, msg = platform.privilege().run(command) if action.privileged else platform.run(command)
    if code != 0:
        raise RuntimeError(msg or f"command failed (exit code {code})")
    return ""


def _delete_path(platform: Platform, path: Path | None, privileged: bool) -> str:
    """Delete a file or a tool-owned directory.

    Privileged deletion always uses `rm -rf`: callers only pass system paths
    (DNS drop-ins). Non-privileged deletion checks for symlinks first to avoid
    `rmtree` following a link and deleting elsewhere.
    """
    if path is None:
        raise RuntimeError("delete action is missing path")
    if privileged:
        code, msg = platform.privilege().run(["rm", "-rf", "--", str(path)])
        if code != 0:
            raise RuntimeError(f"failed to delete {path}: {msg}")
        return ""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return ""
    if path.is_dir():
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
    return ""


def _remove_nss_ca(platform: Platform, ca: Path, nss: Path, nickname: str | None) -> str:
    """Find an entry by certificate content and remove it; the nickname is only a locator, never a criterion."""
    probe = getattr(platform, "nss_entries", None)
    if probe is None:
        return "platform does not provide NSS probing; skipped"
    entries = probe(ca, nss)
    if entries is None:
        raise RuntimeError(
            "cannot determine CA entries in the NSS database (database may be locked "
            "or unreadable); refusing to skip silently"
        )
    # Only delete entries whose content exactly matches the current CA; the
    # nickname carried in the plan only narrows the scope, and the fingerprint
    # recheck still decides before execution.
    targets = [(name, flags) for name, flags in entries if nickname is None or name == nickname]
    if not targets:
        return "no matching entry in the NSS database"
    removed: list[str] = []
    for name, _flags in targets:
        code, msg = platform.run(["certutil", "-d", f"sql:{nss}", "-D", "-n", name])
        if code != 0:
            remaining = probe(ca, nss)
            if remaining is not None and all(n != name for n, _f in remaining):
                continue
            if any(marker in (msg or "").lower() for marker in _NSS_NOT_FOUND):
                continue
            raise RuntimeError(f"failed to delete NSS entry {name}: {msg}")
        removed.append(name)
    remaining = probe(ca, nss)
    if remaining is None:
        raise RuntimeError(
            "cannot confirm whether the CA entry in the NSS database was deleted "
            "(database may be locked or unreadable)"
        )
    if remaining:
        names = [name for name, _flags in remaining]
        raise RuntimeError(f"NSS database still has entries matching the CA: {names}")
    return (
        f"removed {len(removed)} NSS entries"
        if removed
        else "no matching entry in the NSS database"
    )


def _remove_linux_system_ca(platform: Platform, ca: Path, dest: Path) -> str:
    """Delete only when the certificate in dest exactly matches the current CA fingerprint."""
    expected = file_fingerprints(ca)
    if not expected:
        raise RuntimeError(f"cannot read CA certificate (not a valid PEM): {ca}")
    text: str | None
    try:
        text = dest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = None
    if text is None:
        try:
            text = platform.privilege_read(dest)
        except Exception:  # noqa: BLE001 -- unreadable is treated as nonexistent
            text = None
    if text is None:
        return f"{dest} does not exist or is unreadable; skipped (no CA file installed by the tool was found)"
    if not (expected & pem_fingerprints(text)):
        return f"certificate in {dest} does not match the current CA fingerprint; not deleted"
    code, msg = platform.privilege().run(["rm", "-f", "--", str(dest)])
    if code != 0:
        raise RuntimeError(f"failed to delete system trust store file {dest}: {msg}")
    code, msg = platform.privilege().run(["update-ca-certificates"])
    if code != 0:
        raise RuntimeError(
            f"update-ca-certificates failed (the CA may still be in the trust store): {msg}"
        )
    return "removed the CA from the system trust store and refreshed the trust chain"


def _remove_macos_ca(platform: Platform, ca: Path, keychain: str) -> str:
    """Delete the certificate from the system keychain by SHA-1 fingerprint, not by nickname."""
    try:
        text = ca.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise RuntimeError(f"failed to read CA certificate: {ca}: {e}") from e
    try:
        der = ssl.PEM_cert_to_DER_cert(text)
    except ValueError as e:
        raise RuntimeError(f"CA certificate is not a valid PEM: {ca}: {e}") from e
    fingerprint = hashlib.sha1(der).hexdigest().upper()
    code, msg = platform.privilege().run(
        ["security", "delete-certificate", "-Z", fingerprint, "-t", keychain]
    )
    if code == 0:
        return "removed the CA from the system keychain"
    lowered = (msg or "").lower()
    if any(marker in lowered for marker in _MACOS_NOT_FOUND):
        return "CA not found in the system keychain (idempotently skipped)"
    raise RuntimeError(f"failed to delete the CA from the system keychain: {msg}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _probe_ca_trusted(platform: Platform, ca: Path) -> bool | None:
    """Return None when the trust decision fails (cannot determine); never falsely report "not trusted"."""
    try:
        return bool(platform.ca_trusted(ca))
    except Exception:  # noqa: BLE001 -- probe failure must not take down the uninstall plan
        return None


def latest_dnsmasq_backup(backup_dir: Path) -> Path | None:
    """Most recent dnsmasq config backup in the backup directory."""
    try:
        candidates = list(backup_dir.iterdir())
    except OSError:
        return None
    found: list[tuple[tuple[str, int], Path]] = []
    for path in candidates:
        match = _DNSMASQ_BACKUP_RE.match(path.name)
        if match is None or not path.is_file():
            continue
        key = (match.group(1), int(match.group(2) or 0))
        found.append((key, path))
    if not found:
        return None
    return max(found, key=lambda item: item[0])[1]


def latest_migration_backup(backup_dir: Path) -> Path | None:
    """Most recent migration backup directory (subdirectory with manifest.json)."""
    try:
        candidates = [
            path
            for path in backup_dir.iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        ]
    except OSError:
        return None
    return max(candidates, key=lambda path: path.name, default=None)
