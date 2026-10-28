"""CA root certificate trust component.

On Linux two places are needed: the system trust store (curl / Go / Java etc.)
and the NSS DB (Chrome/Chromium). These are **two independent steps**: install
only the system trust store and Chrome still rejects it; install only the NSS
DB and curl rejects it. macOS only needs the system keychain (provided by the
macOS platform implementation in Plan 3).
"""

from __future__ import annotations

import inspect
import shutil
from dataclasses import dataclass
from pathlib import Path

from devproxy.components.base import probe_url
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.platform.base import CA_NICKNAME
from devproxy.platform.base import NSS_DIR
from devproxy.platform.base import SYSTEM_CA_DEST
from devproxy.platform.base import SYSTEM_KEYCHAIN
from devproxy.platform.base import Action
from devproxy.platform.base import Platform
from devproxy.platform.base import Status

CA_NAME = CA_NICKNAME

# Candidate Firefox profile roots:
# - official installer (this is it on Linux/macOS/Windows): ~/.mozilla/firefox
# - flatpak: ~/.var/app/org.mozilla.firefox/.mozilla/firefox
# - snap: ~/snap/firefox/common/.mozilla/firefox
FIREFOX_PROFILE_ROOTS = (
    ".mozilla/firefox",
    ".var/app/org.mozilla.firefox/.mozilla/firefox",
    "snap/firefox/common/.mozilla/firefox",
)

# Marker files in a profile directory. cert9.db is Firefox's **certificate
# DB** itself, and every profile has prefs.js -- neither exists in junk
# directories under ~/.mozilla/firefox (such as "Crash Reports").
FIREFOX_PROFILE_MARKERS = ("cert9.db", "prefs.js")

# Manual trust instructions for Firefox. Firefox does **not** read the NSS DB
# or the system trust store -- it only trusts the cert9.db in its own profile.
FIREFOX_HINT = (
    "Firefox has its own certificate DB (~/.mozilla/firefox/<profile>/cert9.db), "
    "and reads neither the NSS DB nor the system trust store, so it needs "
    "separate trust: import this CA manually, or enable "
    "security.enterprise_roots.enabled in about:config and restart Firefox"
)


def firefox_profile_dirs(home: Path | None = None) -> list[Path]:
    """Firefox profile directories used on this machine; return an empty list when none are found.

    The criterion is a **profile directory**, not an executable:
    - Firefox's certificate DB lives in the profile (cert9.db) -- no profile
      means no certificate DB, hence no "Firefox does not trust the CA" issue;
    - `which firefox` would also count "installed but never run", needlessly
      alarming the user.

    Anything not found (missing directories, insufficient permissions, home
    directory undeterminable) is treated as "no Firefox": this hint is a
    bonus and must never make doctor fail.
    """
    try:
        base = Path(home) if home is not None else Path.home()
    except (OSError, RuntimeError):
        return []
    found: list[Path] = []
    for rel in FIREFOX_PROFILE_ROOTS:
        try:
            children = sorted((base / rel).iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir():
                    continue
                if not any((child / m).exists() for m in FIREFOX_PROFILE_MARKERS):
                    continue
            except OSError:
                continue
            found.append(child)
    return found


def _nss_missing_output(text: str) -> bool:
    """Whether `certutil` output is saying "this entry does not exist".

    Fallback criterion when the platform has no `nss_has_nickname` (actual
    message on a real machine: `certutil: could not find certificate named "x": …`).
    """
    lowered = text.lower()
    return "could not find" in lowered or "not found" in lowered


# System messages from macOS when it refuses to modify system trust settings
# in a "non-GUI session". Wording varies slightly across versions; match by
# substring family (on a real macOS 26, the first one matched).
_MACOS_GUI_DENIED = (
    "no user interaction was possible",
    "user interaction is not allowed",
    "errauthorizationinteractionnotallowed",
)


def _macos_needs_gui(message: str) -> bool:
    low = (message or "").lower()
    return any(marker in low for marker in _MACOS_GUI_DENIED)


def _accepts_keyword(func, name: str) -> bool:
    """Whether the function explicitly accepts the keyword argument `name` (or has `**kwargs`).

    Judge by signature, not try/except TypeError: a TypeError raised
    **inside** the platform implementation must not be misread as "signature
    mismatch", otherwise it would silently switch to querying another DB and
    hide the real defect.
    """
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


@dataclass(frozen=True)
class _TrustProbe:
    """The result of one trust probe.

    `trusted is None` means **undeterminable**, which is entirely different
    from definitely untrusted (False): the former must not be used to assert
    the fact "not trusted", nor to silently skip cleanup.
    """

    trusted: bool | None
    error: str = ""

    def __bool__(self) -> bool:
        return self.trusted is True


class CertComponent:
    name = "cert"

    def __init__(
        self,
        platform: Platform,
        paths: Paths,
        config: Config,
        system_dest: Path | None = None,
        nss_dir: Path | None = None,
    ):
        """system_dest / nss_dir are injectable -- tests use tmp_path and never write the real system."""
        self.platform = platform
        self.paths = paths
        self.config = config
        self.system_dest = Path(system_dest) if system_dest else SYSTEM_CA_DEST
        # **`~` must be expanded here**: NSS_DIR is `Path("~/.pki/nssdb")` and
        # pathlib does not understand the tilde. Without expansion,
        # `(nss_dir / "cert9.db").exists()` checks "~/.pki/nssdb under the
        # current working directory" -- always False, blinding every NSS
        # decision in production construction (the three call sites that do
        # not inject nss_dir). Expand only once here, so every consumer inside
        # the component gets an absolute path.
        self.nss_dir = (Path(nss_dir) if nss_dir else NSS_DIR).expanduser()

    @property
    def ca_path(self) -> Path:
        return self.config.storage_path / "pki" / "authorities" / "local" / "root.crt"

    # -- platform decisions (never raises) ------------------------------

    @staticmethod
    def _trusted(checker, ca: Path) -> _TrustProbe:
        """Platform trust decision; exceptions are always turned into "undeterminable" (None), not "untrusted".

        This is the precondition for check() never raising, but a "probe tool
        is broken" must not be misreported as "CA is not trusted" -- that
        would push the user into an unnecessary elevated install.
        """
        try:
            result = checker(ca)
        except Exception as e:  # noqa: BLE001 -- a probe exception must not take doctor down
            return _TrustProbe(None, f"{type(e).__name__}: {e}")
        if result is None:
            return _TrustProbe(None, "platform returned None")
        return _TrustProbe(bool(result))

    def _system_trusted(self) -> _TrustProbe:
        checker = getattr(self.platform, "ca_trusted", None)
        if checker is None:
            return _TrustProbe(False, "platform does not provide ca_trusted")
        return self._trusted(checker, self.ca_path)

    def _nss_trusted(self, ca: Path | None = None) -> _TrustProbe:
        # The platform protocol may change (the macOS implementation in
        # Plan 3); attribute access itself must be guarded as well, or a
        # missing method makes check() raise -- and doctor blows up.
        checker = getattr(self.platform, "nss_trusted", None)
        if checker is None:
            return _TrustProbe(False, "platform does not provide nss_trusted")
        return self._trusted(checker, ca or self.ca_path)

    def _nss_entries(
        self, ca: Path | None = None, nss_dir: Path | None = None
    ) -> list[tuple[str, str]]:
        """Entries in the DB whose content matches this CA: [(nickname, trust flags), ...].

        `nss_entries` is a **non-protocol method** (macOS has no NSS DB) --
        when it is unavailable, degrade to "handle only our own nickname".
        """
        getter = getattr(self.platform, "nss_entries", None)
        if getter is None:
            return []
        target = ca or self.ca_path
        try:
            if nss_dir is not None and _accepts_keyword(getter, "nss_dir"):
                raw = getter(target, nss_dir=nss_dir)
            else:
                # The platform does not accept nss_dir (old implementation /
                # another platform): use its own DB -- consistency has already
                # been checked upstream via nss_db_path.
                raw = getter(target)
        except Exception:  # noqa: BLE001 -- treat failure to fetch as none
            return []
        entries: list[tuple[str, str]] = []
        for nickname_flags in raw or []:
            try:
                nickname, flags = nickname_flags
            except (TypeError, ValueError):
                continue
            entries.append((str(nickname), str(flags)))
        return entries

    def _nss_db_path(self) -> Path | None:
        """The platform's NSS DB path (a non-protocol method); return None when this method does not exist."""
        getter = getattr(self.platform, "nss_db_path", None)
        if getter is None:
            return None
        try:
            return Path(getter()).expanduser()
        except Exception:  # noqa: BLE001 -- a failed probe means "platform does not provide it"
            return None

    def _nss_stale(self, ca: Path, nss_dir: Path) -> bool | None:
        """Whether a **different** CA is left over under the tool's own nickname (a stale entry).

        The three-state result is **passed through unchanged**: True =
        definitely stale; False = definitely not stale; None = undeterminable
        (DB locked / unreadable / platform implementation raised). Squashing
        None to False would turn "cannot confirm" into "no stale entry" and
        silently skip cleanup -- the stale root would stay in the NSS DB
        forever.
        """
        checker = getattr(self.platform, "nss_stale_nickname", None)
        if checker is None:
            # The platform lacks this capability (macOS has no NSS DB): the
            # concept of a "stale entry" does not exist.
            return False
        try:
            if _accepts_keyword(checker, "nss_dir"):
                result = checker(ca, CA_NAME, nss_dir=nss_dir)
            else:
                result = checker(ca, CA_NAME)
        except Exception:  # noqa: BLE001 -- a failed decision means "undeterminable", not "none"
            return None
        if result is None:
            return None
        return bool(result)

    def _nss_flags_for_repair(self, flags: str) -> str:
        """The complete flag string used to add the SSL trust flag (preserving the S/MIME and JAR columns).

        When the platform cannot compute it, this **must raise**: falling
        back to `"C,,"` would silently wipe the S/MIME / JAR trust in
        columns 2/3.
        """
        flags_resolver = getattr(self.platform, "nss_trust_flags", None)
        if flags_resolver is None:
            raise RuntimeError(
                "platform does not provide nss_trust_flags; cannot add the SSL flag "
                "while preserving the S/MIME/JAR trust columns"
            )
        try:
            return str(flags_resolver(flags))
        except Exception as e:  # noqa: BLE001 -- turn it into an explicit failure; never continue with a destructive default
            raise RuntimeError(
                "platform nss_trust_flags failed (refusing to wipe the S/MIME/JAR "
                f"trust columns): {type(e).__name__}: {e}"
            ) from e

    def _nss_has_nickname(self, nss: Path, nickname: str) -> bool | None:
        """Whether an entry exists under the nickname; return None when undeterminable (the platform not supporting this probe is also None).

        **The three-state result must be passed through unchanged**: the
        platform's `None` means "DB unreadable/locked" and is entirely
        different from "definitely absent" (False) -- squashing None to a
        bool would turn "delete failed" back into "idempotent success".
        """
        getter = getattr(self.platform, "nss_has_nickname", None)
        if getter is None:
            return None
        try:
            if _accepts_keyword(getter, "nss_dir"):
                result = getter(nickname, nss_dir=nss)
            else:
                result = getter(nickname)
        except Exception:  # noqa: BLE001 -- a failed probe is treated as "undeterminable"
            return None
        if result is None:
            return None
        return bool(result)

    def _stale_own_entry(self) -> bool | None:
        """Whether a **different** CA is left over under the tool's own nickname; return None when undeterminable."""
        if not self.ca_path.exists():
            return False
        return self._nss_stale(self.ca_path, self.nss_dir)

    def fully_trusted(self) -> bool:
        """Whether the CA is trusted by the system trust store and the NSS DB **at the same time**.

        Returns False when the CA does not exist yet -- there is nothing to
        trust. Undeterminable (None) also returns "not trusted": hinting once
        about trust is better than silence.
        """
        if not self.ca_path.exists():
            return False
        return bool(self._system_trusted() and self._nss_trusted())

    @property
    def _is_macos(self) -> bool:
        return self.platform.name == "mac"

    def _system_label(self) -> str:
        """Name of the system trust point (used in errors and hints)."""
        return "system keychain" if self._is_macos else "system trust store"

    def _system_copy_readable(self) -> bool:
        """Is the installed copy readable by every user, not just root?

        Tools that walk the trust store directory follow the symlink in
        /etc/ssl/certs back to this file: mise reads every *.pem there,
        NODE_EXTRA_CA_CERTS users read it directly, and a root-only 0600
        copy makes them fail with "permission denied" even though the
        system itself trusts the CA. Installations before the mode was
        pinned to 644 used `cp`, which carried the source file's 0600 over
        -- such a leftover has to be reinstalled, not accepted as "already
        trusted".
        """
        if self._is_macos or not self.system_dest.exists():
            # macOS installs into the keychain (no staging file); a missing
            # file is handled by the ordinary "not trusted yet" path.
            return True
        try:
            mode = self.system_dest.stat().st_mode
        except OSError:
            return False
        return bool(mode & 0o004)

    def _trust_probe_hint(self) -> str:
        """Where to investigate when the probe fails -- not "go run devproxy trust"."""
        if self._is_macos:
            return (
                "check that the system keychain is unlocked and security is available; "
                "devproxy trust"
            )
        return "check that certutil is available and the NSS DB is not locked or unreadable; devproxy trust"

    def _unverifiable_labels(self, system: _TrustProbe, nss: _TrustProbe) -> list[str]:
        """Which trust points are "undeterminable" (with the probe error text)."""
        labels = []
        if system.trusted is None:
            labels.append(f"{self._system_label()} ({system.error})")
        if nss.trusted is None:
            labels.append(f"NSS DB ({nss.error})")
        return labels

    # -- Firefox -------------------------------------------------------

    def firefox_note(self) -> str | None:
        """Manual trust instructions when Firefox is in use; None when there is no Firefox.

        This is a **hint**, not a defect: Firefox not having the CA does not
        mean the tool is broken -- Chrome / curl may be perfectly fine. So it
        goes into hint (the "fix" column) and does not affect check()'s ok.

        Non-protocol method (probed via getattr like `global_scope_note`): the
        platform protocol must not gain or lose members, and Firefox need not
        be implemented on every platform.
        """
        try:
            profiles = firefox_profile_dirs()
        except Exception:  # noqa: BLE001 -- a failed probe is treated as "no Firefox"
            return None
        if not profiles:
            return None
        if not self.ca_path.exists():
            # No CA yet -- nothing to import, hence no hint
            return None
        return f"{FIREFOX_HINT} (CA path: {self.ca_path}; your Firefox profile: {profiles[0]})"

    def _with_firefox(self, hint: str) -> str:
        """Append the Firefox hint after hint (return it unchanged when there is no Firefox)."""
        note = self.firefox_note()
        if not note:
            return hint
        if not hint:
            return note
        return f"{hint}; {note}"

    # -- check ---------------------------------------------------------

    def check(self) -> Status:
        # The CA root certificate is Caddy's internal CA: it is only generated
        # when a site uses tls internal. On first deployment it does not exist
        # yet -- that is "not there yet", not a defect.
        if not self.ca_path.exists():
            return Status(
                ok=True,
                detail="no CA root certificate yet (Caddy will generate it when the first route is registered)",
            )
        system = self._system_trusted()
        nss = self._nss_trusted()
        unknown = self._unverifiable_labels(system, nss)
        missing = []
        if system.trusted is False:
            missing.append(self._system_label())
        if nss.trusted is False:
            missing.append("NSS DB")
        if unknown:
            detail = (
                f"cannot determine whether the CA is trusted (probe failed: {'; '.join(unknown)})"
            )
            if missing:
                detail += f"; definitely untrusted: {', '.join(missing)}"
            return Status(
                ok=False,
                detail=detail,
                hint=self._with_firefox(self._trust_probe_hint()),
            )
        if missing:
            return Status(
                ok=False,
                detail=f"CA root certificate is not trusted (missing: {', '.join(missing)})",
                hint=self._with_firefox("devproxy trust"),
            )
        if self._is_macos:
            return Status(
                ok=True,
                detail="CA root certificate is trusted (system keychain)",
                hint=self._with_firefox(""),
            )
        # A stale root is a "hygiene issue", not "not ready": HTTPS works fine,
        # so ok stays True; but it must not falsely claim everything is fine
        # either -- put the warning **first** in detail (the doctor table
        # truncates the end of long strings) and also into hint, so the "fix"
        # column gives the action directly.
        stale = self._stale_own_entry()
        if stale is None:
            # "Undeterminable" may neither silently skip cleanup nor falsely
            # claim "no stale entry".
            return Status(
                ok=False,
                detail=(
                    "cannot determine whether a stale CA root exists in the NSS DB "
                    "(the DB may be locked or unreadable)"
                ),
                hint=self._with_firefox(
                    "check that certutil/the NSS DB are readable; devproxy trust"
                ),
            )
        if stale:
            return Status(
                ok=True,
                detail=(
                    "a stale CA root is left under the tool's nickname in the NSS DB; "
                    "the CA root certificate is trusted (system trust store + NSS DB)"
                ),
                hint=self._with_firefox("devproxy trust (clean up the stale CA root)"),
            )
        return Status(
            ok=True,
            detail="CA root certificate is trusted (system trust store + NSS DB)",
            hint=self._with_firefox(""),
        )

    # -- plan ----------------------------------------------------------

    def plan(self) -> list[Action]:
        """Nothing to do while the CA has not been generated; otherwise fill in whatever is missing.

        The non-privileged step comes first: if the user declines sudo, NSS
        (Chrome trust) can still be installed.
        """
        if not self.ca_path.exists():
            return []
        if self._is_macos:
            # macOS has no NSS DB: the CA only needs to go into the system
            # keychain (both Safari/Chrome trust it).
            if self._system_trusted().trusted is True:
                return []
            return [
                Action(
                    description="add the CA root certificate to the system keychain (Safari/Chrome will trust it)",
                    kind="run",
                    command=["install-macos-ca", str(self.ca_path)],
                    privileged=True,
                )
            ]
        actions: list[Action] = []
        nss_trusted = self._nss_trusted().trusted
        if nss_trusted is not True:
            # Both False and "undeterminable" (None) conservatively install
            # once; apply re-checks whether it worked.
            actions.append(
                Action(
                    description="install the CA root certificate into the NSS DB (Chrome/Chromium)",
                    kind="run",
                    command=["install-nss-ca", str(self.ca_path), str(self.nss_dir)],
                    # The NSS DB is in the current user's home directory; no root needed
                    privileged=False,
                )
            )
        else:
            stale = self._stale_own_entry()
            if stale is None or stale:
                # The CA is trusted, but an old CA root is left under the
                # tool's nickname. plan() must produce this action -- in this
                # scenario everything was "already ready" and apply() would
                # never be called at all, so cleanup code living only on the
                # install path would never run.
                # Also produce it when stale is None (undeterminable): apply
                # raises an explicit error and never silently skips cleanup.
                actions.append(
                    Action(
                        description="remove the stale CA root under the tool's nickname in the NSS DB",
                        kind="run",
                        command=[
                            "remove-stale-nss-ca",
                            str(self.ca_path),
                            str(self.nss_dir),
                        ],
                        # The tool owns its own nickname; the NSS DB is in the
                        # user's home directory, so no root needed
                        privileged=False,
                    )
                )
        if self._system_trusted().trusted is not True or not self._system_copy_readable():
            actions.append(
                Action(
                    description="install the CA root certificate into the system trust store and run update-ca-certificates",
                    kind="run",
                    command=[
                        "install-system-ca",
                        str(self.ca_path),
                        str(self.system_dest),
                    ],
                    privileged=True,
                )
            )
        return actions

    # -- apply ---------------------------------------------------------

    def apply(self, actions: list[Action]) -> None:
        """The two trust stores are independent: if either step fails, the other must still run to completion.

        Failures are not raised midway; they are aggregated into one error at
        the end -- otherwise if the NSS step running first failed (e.g.
        certutil missing), the system trust store would never be installed,
        and vice versa.
        """
        failures: list[tuple[Action, Exception]] = []
        for action in actions:
            try:
                self._apply_one(action)
            except Exception as e:  # noqa: BLE001 -- collect first, report once at the end
                failures.append((action, e))
        if failures:
            detail = "; ".join(f"{a.description} ({e})" for a, e in failures)
            message = f"{len(failures)} step(s) failed: {detail}"
            succeeded = len(actions) - len(failures)
            if succeeded:
                message += f"; the other {succeeded} step(s) ran"
            # Preserve the type of elevation failures: the CLI uses it to exit
            # with code 4 (elevation required), not 1
            if all(isinstance(e, PermissionError) for _, e in failures):
                raise PermissionError(message)
            raise RuntimeError(message)

    def _apply_one(self, action: Action) -> None:
        """Dispatch by command; the four action types are independent and their failure semantics do not affect each other."""
        handlers = {
            "install-macos-ca": self._install_macos_ca,
            "install-system-ca": self._install_system_ca,
            "remove-stale-nss-ca": self._remove_stale_nss_ca,
            "install-nss-ca": self._install_nss_ca,
        }
        if not action.command:
            raise RuntimeError("unknown action: empty command")
        handler = handlers.get(action.command[0])
        if handler is None:
            raise RuntimeError(f"unknown action: {action.command}")
        handler(action.command)

    # -- action implementations ----------------------------------------

    def _install_macos_ca(self, command: list[str]) -> None:
        """macOS: add the CA to the system keychain and mark it as a trust root.

        -d adds it to the system domain, -r trustRoot makes it apply to all
        applications (including Safari/Chrome).
        """
        src = Path(command[1])
        code, msg = self.platform.privilege().run(
            [
                "security",
                "add-trusted-cert",
                "-d",
                "-r",
                "trustRoot",
                "-k",
                SYSTEM_KEYCHAIN,
                str(src),
            ]
        )
        if code != 0:
            if _macos_needs_gui(msg):
                # Measured on a real machine (macOS 26 + SSH): the certificate
                # has **already** entered the system keychain; what gets stuck
                # is SecTrustSettingsSetTrustSettings -- it wants to pop a
                # system authorization dialog, and an SSH session has no
                # graphical context, so it refuses outright. This is neither
                # insufficient permission (sudo succeeded) nor a wrong
                # command; the user has no option other than "go click on that
                # machine's GUI".
                # Write it on separate lines: Rich wraps an overlong single
                # paragraph into hard-to-read lines, and "which command to
                # run" must be a complete, copyable line.
                raise PermissionError(
                    "macOS does not allow modifying system trust settings in a "
                    "non-GUI session (the authorization dialog cannot appear over SSH).\n"
                    "Run this in a Terminal window on that Mac: devproxy trust\n"
                    f"system message: {msg}"
                )
            raise RuntimeError(f"failed to install the CA into the system keychain: {msg}")

    def _install_system_ca(self, command: list[str]) -> None:
        """Debian-family: copy into the system trust store directory and refresh the trust chain."""
        elevated = self.platform.privilege()
        src, dest = Path(command[1]), Path(command[2])
        code, msg = elevated.run(["install", "-d", str(dest.parent)])
        if code != 0:
            raise RuntimeError(f"failed to create the trust store directory: {msg}")
        elevated.copy(src, dest)
        code, msg = elevated.run(["update-ca-certificates"])
        if code != 0:
            # Without checking the return code, the CA just sits in the
            # staging directory and the system does not trust it
            raise RuntimeError(
                f"update-ca-certificates failed (the CA did not enter the system "
                f"trust store): {msg}"
            )

    def _remove_stale_nss_ca(self, command: list[str]) -> None:
        """Remove stale CA entries under the tool's own nickname."""
        src, nss = self._nss_command_targets(command)
        self._require_certutil()
        self._require_same_nss_db(nss)
        self._drop_stale_own_entry(src, nss)

    def _install_nss_ca(self, command: list[str]) -> None:
        """Install the CA into the NSS DB (Chrome/Chromium), choosing the smallest change based on the DB's current state."""
        src, nss = self._nss_command_targets(command)
        self._require_certutil()
        self._require_same_nss_db(nss)
        # The platform says trusted -> change not a single byte, only do a
        # **targeted** cleanup: if an old CA is left under our own nickname,
        # that root is permanently trusted by the browser, and if the old
        # private key is recoverable it is an HTTPS MITM backdoor. The tool
        # owns that nickname, so it is safe to delete.
        if self._nss_trusted(src).trusted is True:
            self._drop_stale_own_entry(src, nss)
            return
        self._ensure_nss_db(nss)
        # Entries in the DB with the same content as this CA (possibly under a
        # nickname we do not recognize). The criterion is **content**: NSS
        # deduplicates by (issuer, serial), so installing again under another
        # nickname does not get in.
        entries = self._nss_entries(src, nss_dir=nss)
        if any(self._flags_trusted(flags) for _nickname, flags in entries):
            # An entry found by content already has SSL trust -> likewise
            # change nothing (when the platform has no content-based
            # nss_trusted, this step catches it).
            self._drop_stale_own_entry(src, nss)
            return
        if entries:
            # The certificate is already in the DB but lacks SSL trust -> only
            # repair the flags, **do not use -A**: for an existing
            # certificate, -A -t "C,," rewrites the row as "C,,", silently
            # wiping the existing trust in the S/MIME / JAR columns.
            # Also drop stale entries under our own nickname: once the flags
            # are fixed this CA counts as trusted, and nobody will ever look
            # at that old root again.
            self._drop_stale_own_entry(src, nss)
            self._repair_flags(src, nss, entries)
            return
        self._install_nss_entry(src, nss)

    def _ensure_nss_db(self, nss: Path) -> None:
        """Create an empty DB first when it does not exist yet; otherwise certutil -A fails."""
        nss.mkdir(parents=True, exist_ok=True)
        if (nss / "cert9.db").exists():
            return
        code, msg = self.platform.run(["certutil", "-N", "-d", f"sql:{nss}", "--empty-password"])
        if code != 0:
            raise RuntimeError(f"failed to create the NSS DB: {msg}")

    def _install_nss_entry(self, src: Path, nss: Path) -> None:
        """The CA is not in the DB: first clear stale entries under our own nickname, then install and re-check."""
        self._drop_stale_own_entry(src, nss, unconditional=True)
        code, msg = self.platform.run(
            [
                "certutil",
                "-d",
                f"sql:{nss}",
                "-A",
                "-n",
                CA_NAME,
                "-t",
                "C,,",
                "-i",
                str(src),
            ]
        )
        if code != 0:
            raise RuntimeError(f"failed to install the NSS certificate: {msg}")
        # Re-check: **-A returning 0 does not mean it was actually installed**
        # (it is silent for an existing certificate).
        if self._nss_trusted(src).trusted is True:
            return
        # Still not working -> the CA is in the DB but lacks the SSL trust
        # flag; repair the flags and re-check
        self._repair_flags(src, nss, self._nss_entries(src, nss_dir=nss), initial_output=msg)

    @staticmethod
    def _nss_command_targets(command: list[str]) -> tuple[Path, Path]:
        """(CA path, DB path) from an NSS action command."""
        return Path(command[1]), Path(command[2]).expanduser()

    @staticmethod
    def _require_certutil() -> None:
        # The probe can only check whether the binary exists: certutil -V
        # requires -n and is always non-zero on real machines
        if shutil.which("certutil") is None:
            raise RuntimeError(
                "certutil is missing (Chrome trust requires the NSS DB): "
                "sudo apt install libnss3-tools"
            )

    # -- NSS install helpers -------------------------------------------

    def _require_same_nss_db(self, nss: Path) -> None:
        """The DB written to and the DB the platform re-checks must be the same.

        The re-check is exactly the safety net that catches `certutil -A`
        lying; checking the wrong DB disables the net (and silently reports
        "installed successfully"). When the two disagree, raise
        **explicitly**; never stay silent.
        """
        platform_db = self._nss_db_path()
        if platform_db is None:
            return  # The platform does not provide this capability (macOS / old implementation): nothing to verify
        if platform_db.resolve() != nss.resolve():
            raise RuntimeError(
                f"NSS DB path mismatch: component writes {nss}, platform re-checks "
                f"{platform_db} (the re-check would inspect the wrong DB; refusing "
                "to continue)"
            )

    @staticmethod
    def _flags_trusted(flags: str) -> bool:
        """Whether the SSL column of the flag string is trusted (starts with "C")."""
        return bool(flags) and flags[0] == "C"

    def _drop_stale_own_entry(self, ca: Path, nss: Path, *, unconditional: bool = False) -> None:
        """Delete the **stale** CA entry under the tool's own nickname.

        `unconditional=True` is used on the install path where "the current
        CA is not in the DB": there, any entry under our own nickname can
        only be a **different** CA.

        The return code of `certutil -D` **must** be checked: a stale root is
        permanently trusted by the browser, and if a deletion failure were
        treated as success, the old root would stay in the DB while the tool
        reported "ready". Only "the entry really does not exist" (concurrently
        removed elsewhere / never existed) counts as idempotent success.

        Both real "false successes" are blocked:
        - DB locked / unreadable -> `-D` non-zero, `nss_has_nickname` returns
          **None** (undeterminable) -> must be reported upward, not treated as
          "the entry is already gone";
        - DB **directory read-only** -> `certutil -D` returns **0 but deletes
          nothing** (measured) -> after deleting, re-verify that the entry is
          really gone.
        """
        if not unconditional:
            stale = self._nss_stale(ca, nss)
            if stale is False:
                return  # Definitely no stale entry -- idempotent
            if stale is None:
                # "Undeterminable" must not be treated as "none": that would
                # leave the stale root in the DB forever.
                raise RuntimeError(
                    "cannot determine whether a stale CA entry exists in the NSS DB "
                    "(the DB may be locked or unreadable); refusing to silently skip "
                    "cleanup"
                )
        code, msg = self.platform.run(["certutil", "-d", f"sql:{nss}", "-D", "-n", CA_NAME])
        if code != 0:
            has_probe = getattr(self.platform, "nss_has_nickname", None) is not None
            present = self._nss_has_nickname(nss, CA_NAME)
            if present is False:
                return  # The entry really does not exist -- the target state is reached; idempotent
            if not has_probe and _nss_missing_output(msg):
                # The platform offers no probe; only certutil's output is
                # available
                return
            if present is None and has_probe:
                raise RuntimeError(
                    "failed to delete the stale CA entry from the NSS DB, and cannot "
                    "confirm whether it is still there (the DB may be locked or "
                    f"unreadable): {msg}"
                )
            raise RuntimeError(f"failed to delete the stale CA entry from the NSS DB: {msg}")
        # Returning 0 does not mean it really deleted anything: with a
        # read-only NSS DB directory, certutil -D is silently ineffective
        # (measured).
        after = self._nss_stale(ca, nss)
        if after is None:
            raise RuntimeError(
                "cannot confirm whether the stale CA entry in the NSS DB was deleted "
                f"(the DB may be locked or unreadable): {msg}"
            )
        if after is True:
            raise RuntimeError(
                "deleting the stale CA entry from the NSS DB had no effect (certutil "
                "returned success but the entry is still there; the DB directory may "
                f"be unwritable): {msg}"
            )

    def _repair_flags(
        self,
        ca: Path,
        nss: Path,
        entries: list[tuple[str, str]],
        initial_output: str = "",
    ) -> None:
        """Add the SSL trust flag to those DB entries (preserving the S/MIME / JAR columns)."""
        last_output = initial_output
        for nickname, flags in entries:
            code, msg = self.platform.run(
                [
                    "certutil",
                    "-M",
                    "-d",
                    f"sql:{nss}",
                    "-n",
                    nickname,
                    "-t",
                    self._nss_flags_for_repair(flags),
                ]
            )
            last_output = msg
            if code != 0:
                last_output = f"({code}) {msg}"
            if self._nss_trusted(ca).trusted is True:
                return
        # Still not trusted -- the error message must carry the output of the
        # **last -M** plus the actual entries in the DB; keeping only -A's
        # output says nothing when it returned 0 successfully.
        detail = last_output or "certutil produced no output"
        raise RuntimeError(
            "failed to install the NSS certificate: cannot find the SSL trust flag "
            f"for this CA in the NSS DB (entries in DB: {entries or 'none'}; "
            f"certutil: {detail})"
        )

    # -- verify --------------------------------------------------------

    def _probe_url(self) -> str | None:
        """URL for the real HTTPS probe; None when there is nothing to probe.

        The criterion is shared with the Caddy component via
        `components.base.probe_url`: prefer the first registered route, then
        use localhost only **when a localhost site actually exists**. Using
        localhost to verify a machine that only has codeup.test fails the
        handshake because there is no localhost site -- that verifies site
        matching, not the certificate trust chain.
        """
        return probe_url(self.paths.routes_json, self.paths.sites_caddy)

    def verify(self) -> Status:
        if not self.ca_path.exists():
            return Status(ok=True, detail="no CA root certificate yet (skipping verification)")
        system = self._system_trusted()
        nss = self._nss_trusted()
        unknown = self._unverifiable_labels(system, nss)
        if unknown:
            return Status(
                ok=False,
                detail=(
                    f"cannot determine whether the CA is trusted "
                    f"(probe failed: {'; '.join(unknown)})"
                ),
                hint=self._trust_probe_hint(),
            )
        if system.trusted is not True or nss.trusted is not True:
            return Status(ok=False, detail="CA is still not fully trusted", hint="devproxy trust")
        # Verifying the trust chain with openssl is only the first step; send
        # one real HTTPS request to confirm end-to-end availability.
        url = self._probe_url()
        if url is None:
            # No probeable site at all (migrated CA, no routes registered yet)
            # -- the trust chain cannot be verified here; stay ok + explain,
            # rather than using localhost to manufacture a false failure.
            return Status(
                ok=True,
                detail="CA is trusted (no probeable site, skipping HTTPS verification)",
            )
        if not self.platform.http_ok(url):
            return Status(
                ok=False,
                detail=f"CA is in the trust store, but the handshake for {url} still fails",
                hint=(
                    "devproxy trust; launchctl print system/com.devproxy.caddy"
                    if self._is_macos
                    else "devproxy trust; systemctl --user status caddy"
                ),
            )
        return Status(ok=True, detail=f"CA is trusted, handshake for {url} succeeded")
