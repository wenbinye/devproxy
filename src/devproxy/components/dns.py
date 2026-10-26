"""DNS route component: point *.test at this machine.

Two backends:
- resolved (the Linux default): writes /etc/systemd/resolved.conf.d/devproxy.conf
- system: writes /etc/dnsmasq.d/devproxy.conf (you must handle port 53 conflicts yourself)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.platform.base import RESOLVED_CONF
from devproxy.platform.base import RESOLVER_DIR
from devproxy.platform.base import SYSTEM_DNSMASQ_CONF
from devproxy.platform.base import Action
from devproxy.platform.base import Platform
from devproxy.platform.base import Status
from devproxy.platform.base import render_resolver_file

# The path constants are defined in `platform/base.py` (single source); import and use them here.

# Machine-wide fallback resolver for resolved. The disclosure text and what is
# actually written into the config must be the same thing -- writing it twice
# would drift, and what the user sees would no longer match what takes effect.
FALLBACK_DNS = "1.1.1.1 8.8.8.8"

# Record the hash of the route content last written -- a normal user cannot
# necessarily read files under /etc, so the hash in our own state directory
# decides "is it already ready", letting a second setup run be a no-op.
STATE_FILE = "dns-route.json"


def global_scope_disclosure(port: int) -> str:
    """Machine-wide scope disclosure for the resolved backend (multi-line short sentences, easy to read in a terminal).

    `DNS=` is written in the global section of the drop-in -- it makes
    **every domain on this machine** ask the user-scope dnsmasq, not just
    `*.test`. `FallbackDNS=` is the machine-wide fallback: if dnsmasq cannot
    start, resolution across the machine degrades to public DNS.

    Both facts must appear in setup's change list: the user should know what
    they are changing before confirming.
    """
    return (
        "⚠ machine-wide scope (every domain on this machine, not just *.test)\n"
        f"  · DNS=127.0.0.1:{port}: machine-wide resolution asks the user-scope dnsmasq first\n"
        f"  · FallbackDNS={FALLBACK_DNS}: machine-wide fallback\n"
        "    when dnsmasq cannot start, machine-wide resolution degrades to public DNS"
    )


def render_resolved_conf(*, port: int, suffix: str) -> str:
    """Contents of the systemd-resolved drop-in.

    `Domains=` is a **space-separated** list of domains; commas are not
    separators. The tool only needs to maintain one suffix, so the parameter
    stays single-valued -- this prevents a future caller passing two domains
    from generating `Domains=~.test,~.dev`, an entirely invalid line that
    neither side reports.
    """
    return (
        "[Resolve]\n"
        f"DNS=127.0.0.1:{port}\n"
        f"FallbackDNS={FALLBACK_DNS}\n"
        f"Domains=~.{suffix.lstrip('.')}\n"
    )


def render_system_dnsmasq_conf(*, suffix: str) -> str:
    s = suffix.lstrip(".")
    return f"address=/.{s}/127.0.0.1\naddress=/.{s}/::1\n"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DnsComponent:
    name = "dns"

    def __init__(self, platform: Platform, paths: Paths, config: Config, backend: str = "auto"):
        self.platform = platform
        self.paths = paths
        self.config = config
        self.backend = backend

    @property
    def resolved_backend(self) -> bool:
        """Prefer resolved for auto (does not occupy port 53, user-space daemon).

        The criterion must be that resolved is **actually running**, not that
        the directory exists -- /etc/systemd/resolved.conf.d may have been
        created by hand; conversely, on a clean machine a missing directory
        does not mean resolved is not installed. Whether the directory exists
        is not a reliable signal either way.

        macOS has no systemd-resolved at all: auto silently uses resolver,
        but when the user **explicitly** asks for resolved it must raise --
        otherwise explicit input would be silently changed to different
        behavior, and the user would never know their config did not take
        effect.
        """
        if self.backend == "resolved":
            if self.platform.name == "mac":
                raise RuntimeError(
                    "macOS does not support the resolved backend (no systemd-resolved); "
                    "use auto or system"
                )
            return True
        if self.platform.name == "mac" or self.backend == "system":
            return False
        return self.platform.resolved_active()

    @property
    def target_path(self) -> Path:
        """The DNS route file to write/check this time.

        On macOS this is the per-domain resolver file (/etc/resolver/<suffix>)
        -- it does not touch /etc/resolv.conf.
        """
        if self.resolved_backend:
            return RESOLVED_CONF
        if self.platform.name == "mac":
            return RESOLVER_DIR / self.config.domain_suffix.lstrip(".")
        return SYSTEM_DNSMASQ_CONF

    def _backend_key(self, resolved: bool | None = None) -> str:
        """The DNS routing backend to write/check this time.

        `resolved` may be supplied by the caller from an already-computed
        result, to avoid probing twice in the same round. macOS has no
        systemd-resolved and uses the resolver mechanism outside launchd
        (/etc/resolver/<suffix>); the backend name is "resolver".
        """
        if resolved is None:
            resolved = self.resolved_backend
        if resolved:
            return "resolved"
        if self.platform.name == "mac":
            return "resolver"
        return "system"

    # -- disclosure ----------------------------------------------------
    def global_scope_note(self) -> str | None:
        """Machine-wide scope disclosure text; None when machine-wide scope is not involved.

        Only the resolved backend has machine-wide scope: `DNS=` and
        `FallbackDNS=` are written in the drop-in's global section and change
        **this machine's** resolution path. macOS's per-domain resolver file
        and the `system` backend's dnsmasq snippet only affect this suffix,
        not the whole machine.

        setup prints this paragraph separately -- it does not fit in the
        change list table, nor would the emphasis.
        """
        if not self.resolved_backend:
            return None
        return global_scope_disclosure(self.platform.dns_port)

    # -- check ---------------------------------------------------------
    def check(self) -> Status:
        """The readiness criterion must match plan -- otherwise doctor lies.

        `platform.dns_route_installed` only answers "is the file there": on
        macOS, `/etc/resolver/test` may be a `nameserver 127.0.0.1` (without
        `port 5300`) left by another tool years ago; on Linux the drop-in may
        have been hand-edited too. Reporting "ready" when the file exists but
        its content is wrong leaves the user debugging resolution for a long
        time on a machine where `doctor` is all green.

        So: when the content is readable, go by the content; only when it
        cannot be read (permissions) fall back to the platform's existence
        criterion -- at that point it cannot be disproven, so it should not
        error out of thin air. This fallback is implemented once in
        `_route_in_place()`, shared by check and plan, to avoid "doctor green
        but setup wants to rewrite and restart the resolver again".
        """
        try:
            resolved = self.resolved_backend
            key = self._backend_key(resolved)
            content = self._expected_content(resolved)
        except RuntimeError as e:
            # Config does not match the platform (e.g. resolved explicitly
            # requested on macOS): report the config error truthfully, but
            # check still does not raise.
            return Status(
                ok=False,
                detail=str(e),
                hint="change dns_backend to auto or system",
            )
        if self._route_in_place(content):
            return Status(ok=True, detail=f"DNS route configured ({key} backend)")
        if self._read_target() is not None:
            return Status(
                ok=False,
                detail=f"DNS route content is not as expected ({key} backend): {self.target_path}",
                hint="devproxy setup will rewrite it",
            )
        return Status(
            ok=False,
            detail=f"DNS route missing ({key} backend): {self.target_path}",
            hint="devproxy setup will write it (requires sudo)",
        )

    def _read_target(self) -> str | None:
        """Contents of the target file; None when it does not exist or cannot be read (both mean "unknown")."""
        try:
            return self.target_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    # -- plan ----------------------------------------------------------
    def _expected_content(self, resolved: bool | None = None) -> str:
        """The content the route file should have under the current platform/backend.

        check and plan must use the **same** criterion -- computing it twice
        would eventually drift, and drift shows up as "doctor says ready but
        setup wants to rewrite". `resolved` may be supplied by the caller to
        avoid probing twice in the same round.
        """
        # Resolve resolved_backend first to complete the platform applicability
        # check (explicit resolved on macOS raises).
        if resolved is None:
            resolved = self.resolved_backend
        if self.platform.name == "mac":
            return render_resolver_file(self.platform.dns_port)
        if resolved:
            return render_resolved_conf(
                port=self.platform.dns_port, suffix=self.config.domain_suffix
            )
        return render_system_dnsmasq_conf(suffix=self.config.domain_suffix)

    def plan(self) -> list[Action]:
        """Return an empty list when the route already has the expected content -- a repeated setup should not ask for sudo again, let alone restart the system resolver (that would cause machine-wide DNS flapping)."""
        # Compute content only once: the branch only decides the wording and
        # reload action. Computing it twice is not just wasted probing; under
        # concurrent state changes it could even disagree with the backend
        # choice.
        resolved = self.resolved_backend
        content = self._expected_content(resolved)
        if self.platform.name == "mac":
            # macOS per-domain resolver files: mDNSResponder reloads itself,
            # so there is no need (and no way) to restart any resolution
            # service.
            desc = f"write {self.target_path} (macOS per-domain resolver)"
            if self._route_in_place(content):
                return []
            return [
                Action(
                    description=desc,
                    kind="write",
                    path=self.target_path,
                    content=content,
                    privileged=True,
                )
            ]
        if resolved:
            desc = (
                "write the systemd-resolved route"
                f" (*.{self.config.domain_suffix} -> 127.0.0.1, machine-wide DNS scope)"
            )
            reload_action = Action(
                description="reload systemd-resolved to activate the route",
                kind="run",
                command=["systemctl", "restart", "systemd-resolved"],
                privileged=True,
            )
        else:
            desc = f"write the system dnsmasq route (*.{self.config.domain_suffix} -> 127.0.0.1)"
            reload_action = Action(
                description="reload system dnsmasq to activate the route",
                kind="run",
                command=["systemctl", "restart", "dnsmasq"],
                privileged=True,
            )
        if self._route_in_place(content):
            return []
        return [
            Action(
                description=desc,
                kind="write",
                path=self.target_path,
                content=content,
                privileged=True,
            ),
            reload_action,
        ]

    # -- apply ---------------------------------------------------------
    def apply(self, actions: list[Action]) -> None:
        """Acquire the elevated session on demand -- never trigger a sudo password prompt when there are no privileged actions.

        State is recorded only **after all actions succeed**: if a reload
        fails after state was recorded, the next plan would return an empty
        list and misreport "not in effect" as "already ready".
        """
        elevated = None
        written: tuple[Path, str] | None = None
        for action in actions:
            if action.kind == "write" and action.path is not None:
                if action.privileged:
                    if elevated is None:
                        elevated = self.platform.privilege()
                    elevated.write(action.path, action.content)
                else:
                    action.path.parent.mkdir(parents=True, exist_ok=True)
                    action.path.write_text(action.content, encoding="utf-8")
                if action.path == self.target_path:
                    written = (action.path, action.content)
                continue
            if action.privileged:
                if elevated is None:
                    elevated = self.platform.privilege()
                code, msg = elevated.run(action.command)
                if code != 0:
                    raise RuntimeError(f"failed to run {' '.join(action.command)}: {msg}")
                continue
            raise RuntimeError(f"unknown action: {action.command}")
        if written is not None:
            self._record_state(*written)

    # -- verify --------------------------------------------------------
    def verify(self) -> Status:
        """The route file being present is not enough -- resolution through the system resolver must actually reach this machine."""
        key = self._backend_key()
        mac = self.platform.name == "mac"
        if not self.platform.dns_route_installed(key, self.config.domain_suffix):
            return Status(
                ok=False,
                detail="DNS route is not in effect",
                hint=(
                    f"check that {self.target_path} exists and has the right content"
                    if mac
                    else "systemctl restart systemd-resolved"
                ),
            )
        name = f"foo.{self.config.domain_suffix}"
        answer = self.platform.dns_query(name)
        if answer not in ("127.0.0.1", "::1"):
            return Status(
                ok=False,
                detail=(
                    f"the system resolver does not point {name} at this machine "
                    f"({answer or 'no response'}); the route file was written but is not in effect"
                ),
                hint=(
                    f"dscacheutil -q host -a name {name}"
                    if mac
                    else "systemctl restart systemd-resolved; resolvectl query " + name
                ),
            )
        return Status(ok=True, detail=f"DNS route is in effect ({name} -> {answer})")

    # -- state recording -----------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.paths.state_dir / STATE_FILE

    def _read_state(self) -> dict:
        path = self.state_path
        if not path.exists():
            return {}
        try:
            state_payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return state_payload if isinstance(state_payload, dict) else {}

    def _record_state(self, path: Path, content: str) -> None:
        payload = {
            "backend": self._backend_key(),
            "path": str(path),
            "content_sha256": _sha256(content),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _route_in_place(self, content: str) -> bool:
        """Whether the route already has the expected content.

        The criterion steps back level by level according to the available
        information:
        1. File readable -> compare content directly (rewrite if the user
           edited it under /etc);
        2. File unreadable / invisible (a normal user cannot necessarily read
           files under /etc) -> compare the hash recorded last time in the
           state directory;
        3. Not even the state matches -> fall back to the platform's
           existence criterion. At that point it cannot be disproven, so it
           should not error out of thin air; plan and check must share this
           fallback, otherwise doctor reports green while setup wants sudo to
           rewrite and restart the machine-wide resolver.
        """
        digest = _sha256(content)
        current = None
        if self.target_path.exists():
            try:
                current = self.target_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                current = None
            if current is not None:
                return _sha256(current) == digest
            state = self._read_state()
            if state.get("content_sha256") == digest and state.get("path") == str(self.target_path):
                return True
        # The file is missing or unreadable by a normal user (e.g. root:root
        # 0600), and the state hash is missing/stale: the platform's "file
        # exists" criterion is the only information available at this point.
        # check and plan must share it -- otherwise doctor reports green while
        # setup wants sudo to rewrite and restart the machine-wide resolver.
        return self.platform.dns_route_installed(self._backend_key(), self.config.domain_suffix)
