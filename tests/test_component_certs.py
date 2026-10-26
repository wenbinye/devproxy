from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.components import certs as certs_module
from devproxy.components.certs import CertComponent
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.platform.base import Action
from devproxy.platform.fake import FakePlatform


def _component(tmp_path, platform=None):
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    platform = platform or FakePlatform()
    return CertComponent(platform, paths, cfg), platform, paths


def _write_ca(paths: Paths) -> Path:
    ca = paths.data_dir / "pki" / "authorities" / "local" / "root.crt"
    ca.parent.mkdir(parents=True, exist_ok=True)
    ca.write_text("FAKE CA\n", encoding="utf-8")
    return ca


def _trust_both(platform, ca: Path) -> None:
    platform.trusted.add(ca)
    platform.nss.add(ca)


# -- check -----------------------------------------------------------------


def test_check_ok_when_trusted(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    status = comp.check()
    assert status.ok is True
    assert "trusted" in status.detail


def test_check_missing_ca_is_a_note_not_a_failure(tmp_path):
    """Caddy generates the CA when the first route is registered -- its absence right now is not a defect."""
    comp, platform, paths = _component(tmp_path)
    status = comp.check()
    assert status.ok is True
    assert "no CA root certificate yet" in status.detail


def test_check_requires_nss_too(tmp_path):
    """Installed in the system trust store but not the NSS DB -> Chrome still rejects it; must be reported."""
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    platform.trusted.add(ca)
    status = comp.check()
    assert status.ok is False
    assert "NSS" in status.detail
    assert status.hint


def test_check_requires_system_trust_too(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    platform.nss.add(ca)
    status = comp.check()
    assert status.ok is False
    assert "system trust store" in status.detail


def test_check_never_raises(tmp_path):
    """check() never raises -- doctor relies on it to list every problem at once."""

    class Broken(FakePlatform):
        def ca_trusted(self, ca):
            raise RuntimeError("platform exploded")

        def nss_trusted(self, ca):
            raise RuntimeError("platform exploded")

    comp, platform, paths = _component(tmp_path, Broken())
    _write_ca(paths)
    status = comp.check()
    assert status.ok is False
    # "the probe is broken" is not "the CA is untrusted": the wording must distinguish them
    # so the user is not pushed into repeated elevation
    assert "cannot determine" in status.detail
    assert "platform exploded" in status.detail
    assert "is not trusted" not in status.detail


def test_verify_reports_unknown_trust_probe_not_as_untrusted(tmp_path):
    """verify must likewise not describe "undeterminable" as "still not fully trusted"."""

    class Broken(FakePlatform):
        def ca_trusted(self, ca):
            raise RuntimeError("keychain is locked")

        def nss_trusted(self, ca):
            return False

    comp, platform, paths = _component(tmp_path, Broken())
    _write_ca(paths)
    status = comp.verify()
    assert status.ok is False
    assert "cannot determine" in status.detail
    assert "keychain is locked" in status.detail
    assert "still not fully trusted" not in status.detail


def test_check_degrades_when_platform_lacks_nss_trusted(tmp_path):
    """When the platform protocol has no nss_trusted (e.g. a future macOS implementation), degrade to "untrusted"."""

    class NoNss:
        name = "no-nss"

        def ca_trusted(self, ca):
            return True

    comp, _platform, paths = _component(tmp_path, NoNss())
    _write_ca(paths)
    status = comp.check()
    assert status.ok is False
    assert "NSS" in status.detail


# -- plan ------------------------------------------------------------------


def test_plan_is_empty_when_trusted(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    assert comp.plan() == []


def test_plan_is_empty_when_ca_missing(tmp_path):
    """The CA has not been generated yet -> nothing to do, and it must not fail."""
    comp, platform, paths = _component(tmp_path)
    assert comp.plan() == []


def test_plan_only_nss_step_when_system_trusted(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    platform.trusted.add(ca)
    actions = comp.plan()
    assert len(actions) == 1
    assert actions[0].command[0] == "install-nss-ca"
    assert actions[0].privileged is False


def test_plan_only_system_step_when_nss_trusted(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    platform.nss.add(ca)
    actions = comp.plan()
    assert len(actions) == 1
    assert actions[0].command[0] == "install-system-ca"
    assert actions[0].privileged is True


def test_plan_includes_privileged_steps(tmp_path):
    """Only the system trust store needs root; the NSS DB lives in the user's home and needs no elevation."""
    comp, platform, paths = _component(tmp_path)
    _write_ca(paths)
    actions = comp.plan()
    assert len(actions) == 2
    privileged = [a for a in actions if a.privileged]
    assert len(privileged) == 1
    assert privileged[0].command[0] == "install-system-ca"
    nss_action = [a for a in actions if a.command[0] == "install-nss-ca"][0]
    assert nss_action.privileged is False, "the NSS DB belongs to the current user; no root needed"


def test_plan_puts_root_free_step_first(tmp_path):
    """If the user declines sudo, the NSS step can still complete -- so it comes first."""
    comp, platform, paths = _component(tmp_path)
    _write_ca(paths)
    actions = comp.plan()
    assert actions[0].command[0] == "install-nss-ca"
    assert actions[0].privileged is False
    assert actions[1].command[0] == "install-system-ca"


# -- apply: target paths are injectable; tests never touch the real system ----


def _injected(tmp_path, platform):
    """Point the system trust store and the NSS DB at tmp_path so the test does not touch the real system."""
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    # When re-checking the certificate the platform defaults to the NSS DB under HOME; here we
    # sync the fake platform's default DB to the injected one, otherwise `_require_same_nss_db`
    # would (correctly) refuse.
    platform.nss_db = tmp_path / "nssdb"
    comp = CertComponent(
        platform,
        paths,
        cfg,
        system_dest=tmp_path / "ca" / "devproxy-local.crt",
        nss_dir=tmp_path / "nssdb",
    )
    return comp, paths, cfg


def _certutil_installed(monkeypatch):
    monkeypatch.setattr("devproxy.components.certs.shutil.which", lambda name: "/usr/bin/certutil")


def _certutil_missing(monkeypatch):
    monkeypatch.setattr("devproxy.components.certs.shutil.which", lambda name: None)


def test_apply_writes_only_to_injected_targets(tmp_path, monkeypatch):
    _certutil_installed(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())
    assert (tmp_path / "ca" / "devproxy-local.crt").read_text() == "FAKE CA\n"
    assert ["update-ca-certificates"] in platform.commands
    assert any(c[:1] == ["certutil"] and "-A" in c for c in platform.commands)
    for written in platform.written:
        assert tmp_path in written.parents, f"wrote outside tmp_path: {written}"


def test_apply_runs_nss_before_system_ca(tmp_path, monkeypatch):
    _certutil_installed(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())
    names = [c[0] for c in platform.commands]
    assert names.index("certutil") < names.index("update-ca-certificates")


def test_apply_detects_certutil_by_binary_not_exit_code(tmp_path, monkeypatch):
    """certutil -V requires -n and is always non-zero on real machines -- the probe can only check the binary."""
    _certutil_installed(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())
    assert ["certutil", "-V"] not in platform.commands
    assert any(c[:1] == ["certutil"] and "-A" in c for c in platform.commands)


def test_apply_raises_when_certutil_missing(tmp_path, monkeypatch):
    _certutil_missing(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(comp.plan())
    assert "certutil is missing" in str(ei.value)


def test_apply_still_installs_system_ca_when_nss_fails(tmp_path, monkeypatch):
    """The two steps are independent: no certutil must not drag down the curl/Go system trust store."""
    _certutil_missing(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(comp.plan())
    assert "certutil is missing" in str(ei.value)
    # After the NSS failure, the system trust store step still ran to completion
    assert (tmp_path / "ca" / "devproxy-local.crt").exists()
    assert ["update-ca-certificates"] in platform.commands


def test_apply_still_installs_nss_when_system_ca_fails(tmp_path, monkeypatch):
    """When sudo is declined / elevation fails, the root-free NSS step must still complete."""
    _certutil_installed(monkeypatch)
    from devproxy.platform.fake import FakeElevated

    class CopyBoom(FakePlatform):
        def privilege(self):
            if self._elevated is None:

                class Elev(FakeElevated):
                    def copy(self, src, dest):
                        raise RuntimeError("copy failed")

                self._elevated = Elev(self)
            return self._elevated

    platform = CopyBoom()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError, match="copy failed"):
        comp.apply(comp.plan())
    assert any(c[:1] == ["certutil"] and "-A" in c for c in platform.commands)


def test_apply_preserves_permission_error(tmp_path, monkeypatch):
    """A denied sudo is an "elevation failure" (exit code 4) and must not be rewritten as a plain RuntimeError."""
    _certutil_installed(monkeypatch)
    from devproxy.platform.fake import FakeElevated

    class SudoDenied(FakePlatform):
        def privilege(self):
            if self._elevated is None:

                class Elev(FakeElevated):
                    def copy(self, src, dest):
                        raise PermissionError("sudo: wrong password")

                self._elevated = Elev(self)
            return self._elevated

    platform = SudoDenied()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(PermissionError) as ei:
        comp.apply(comp.plan())
    message = str(ei.value)
    assert "system trust store" in message and "sudo: wrong password" in message
    assert "the other 1 step(s) ran" in message
    # The NSS step was still installed
    assert any(c[:1] == ["certutil"] and "-A" in c for c in platform.commands)


def test_apply_reports_every_failed_step(tmp_path, monkeypatch):
    _certutil_missing(monkeypatch)
    from devproxy.platform.fake import FakeElevated

    class AllBoom(FakePlatform):
        def privilege(self):
            if self._elevated is None:

                class Elev(FakeElevated):
                    def copy(self, src, dest):
                        raise RuntimeError("copy failed")

                self._elevated = Elev(self)
            return self._elevated

    platform = AllBoom()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(comp.plan())
    message = str(ei.value)
    assert "certutil" in message
    assert "copy failed" in message


def test_apply_raises_when_update_ca_certificates_fails(tmp_path, monkeypatch):
    """A failed update-ca-certificates must error -- otherwise the CA just sits in the staging directory."""
    _certutil_installed(monkeypatch)
    from devproxy.platform.fake import FakeElevated

    class NoUpdateCa(FakePlatform):
        def privilege(self):
            if self._elevated is None:
                platform = self

                class Elev(FakeElevated):
                    def run(self, argv):
                        platform.commands.append(list(argv))
                        if argv[:1] == ["update-ca-certificates"]:
                            return 127, "command not found: update-ca-certificates"
                        return 0, ""

                self._elevated = Elev(self)
            return self._elevated

    platform = NoUpdateCa()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError, match="update-ca-certificates"):
        comp.apply(comp.plan())


# -- NSS install: must re-check; never trust only certutil -A's return code ----
#
# Real-machine behavior: NSS deduplicates by (issuer, serial) -- adding the same
# certificate under a different nickname makes `certutil -A` **silently do nothing**
# (returns 0, no new entry). Trusting the return code alone, the tool would keep
# believing it installed the entry while the DB has no entry under its own nickname.


class _StalePlatform(FakePlatform):
    """A stale entry exists under the tool's own nickname; it disappears after a successful `-D` (re-verification is what passes)."""

    def __init__(self):
        super().__init__()
        self.stale = True

    def nss_stale_nickname(self, ca, nickname, nss_dir=None):
        return self.stale

    def nss_has_nickname(self, nickname, nss_dir=None):
        return self.stale

    def run(self, argv):
        result = super().run(argv)
        if argv[:1] == ["certutil"] and "-D" in argv:
            self.stale = False
        return result


class _NssFlagPlatform(FakePlatform):
    """The DB has this CA but lacks the SSL trust flag -- only -M can add it."""

    def __init__(self):
        super().__init__()
        self.flagged = False
        self.entries: list[tuple[str, str]] = []

    def nss_trusted(self, ca):
        return self.flagged

    def nss_entries(self, ca):
        return self.entries

    def run(self, argv):
        result = super().run(argv)
        if argv[:1] == ["certutil"] and "-M" in argv:
            self.flagged = True
        return result


def test_apply_adds_trust_flag_to_existing_entry(tmp_path, monkeypatch):
    """The DB has this CA but no trust flag -> add the flag with certutil -M."""
    _certutil_installed(monkeypatch)
    platform = _NssFlagPlatform()
    platform.entries = [("Caddy Local Authority", ",,")]
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())
    mods = [c for c in platform.commands if c[:1] == ["certutil"] and "-M" in c]
    assert mods, "certutil -M must be used to add the flag when the trust flag is missing"
    assert "Caddy Local Authority" in mods[0], "it patches the nickname of the entry in the DB"
    assert mods[0][mods[0].index("-t") + 1] == "C,,"


def test_apply_raises_when_cert_never_becomes_trusted(tmp_path, monkeypatch):
    """After installation the re-check still is not "trusted" -> must error, not silently claim success."""

    class NeverTrusted(_NssFlagPlatform):
        def run(self, argv):
            FakePlatform.run(self, argv)
            return 0, ""

    _certutil_installed(monkeypatch)
    platform = NeverTrusted()
    platform.entries = [("Caddy Local Authority", ",,")]
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(comp.plan())
    assert "trust flag" in str(ei.value)


def test_apply_skips_nss_install_when_already_trusted(tmp_path, monkeypatch):
    """When already trusted it must not touch the user's DB again -- not a single certutil command may be issued."""
    _certutil_installed(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    platform.nss.add(ca)
    comp.apply(
        [
            Action(
                description="install the CA root certificate into the NSS DB",
                kind="run",
                command=["install-nss-ca", str(ca), str(tmp_path / "nssdb")],
                privileged=False,
            )
        ]
    )
    assert not [c for c in platform.commands if c[:1] == ["certutil"]], (
        "an already-trusted DB must not be reinstalled repeatedly"
    )


# -- fix round 1: drift points ---------------------------------------------


def test_apply_fails_loud_when_component_nss_dir_differs(tmp_path, monkeypatch):
    """The DB the component writes and the DB the platform re-checks differ -> explicit error; never silently check another DB.

    The re-check is the safety net for "`certutil -A` lies"; checking the wrong DB disables
    the net and silently reports "installed successfully".
    """
    _certutil_installed(monkeypatch)

    class WithDbPath(FakePlatform):
        def nss_db_path(self):
            return Path("/platform/nssdb")

    platform = WithDbPath()
    comp = CertComponent(
        platform,
        Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data"),
        Config(storage=str(tmp_path / "data")),
        system_dest=tmp_path / "ca" / "devproxy-local.crt",
        nss_dir=tmp_path / "nssdb",
    )
    comp.paths.ensure_files()
    _write_ca(comp.paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(
            [
                Action(
                    description="install the CA root certificate into the NSS DB",
                    kind="run",
                    command=["install-nss-ca", str(comp.ca_path), str(tmp_path / "nssdb")],
                    privileged=False,
                )
            ]
        )
    message = str(ei.value)
    assert "mismatch" in message
    assert "/platform/nssdb" in message and str(tmp_path / "nssdb") in message
    assert platform.commands == [], "must not write to either DB before erroring"


def test_apply_skips_when_platform_db_path_matches(tmp_path, monkeypatch):
    """When the two agree (same DB), proceed as usual without a false positive."""
    _certutil_installed(monkeypatch)

    class WithDbPath(FakePlatform):
        def nss_db_path(self):
            return tmp_path / "nssdb"

    platform = WithDbPath()
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())  # passing means no exception
    assert any(c[:1] == ["certutil"] and "-A" in c for c in platform.commands)


def test_apply_repair_error_keeps_last_modify_output(tmp_path, monkeypatch):
    """When flag repair fails, the error message must include the output of the **last -M**.

    When `-A` returns 0 its output is empty; keeping only that would report "certutil: ",
    which says nothing.
    """
    _certutil_installed(monkeypatch)

    class ModifyFails(_NssFlagPlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            if argv[:1] == ["certutil"] and "-M" in argv:
                return 255, "certutil: unable to modify trust"
            return 0, ""

        def nss_trusted(self, ca):
            return False  # never repairable

    platform = ModifyFails()
    platform.entries = [("Caddy Local Authority", ",,")]
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(comp.plan())
    message = str(ei.value)
    assert "unable to modify trust" in message, "-M's real output must not be dropped"
    assert "Caddy Local Authority" in message, "the error must report the actual entry in the DB"


def test_apply_repair_uses_platform_flag_helper(tmp_path, monkeypatch):
    """Flag repair uses the platform-provided three-column flags (preserving S/MIME / JAR), not a hardcoded C,,."""
    _certutil_installed(monkeypatch)

    class Flags(_NssFlagPlatform):
        def nss_trust_flags(self, flags):
            parts = flags.split(",")
            return ",".join(["C", *parts[1:]])

    platform = Flags()
    platform.entries = [("Caddy Local Authority", ",C,")]
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)
    comp.apply(comp.plan())
    mods = [c for c in platform.commands if c[:1] == ["certutil"] and "-M" in c]
    assert mods and mods[0][mods[0].index("-t") + 1] == "C,C,", (
        "only the SSL column is patched; S/MIME trust must be preserved"
    )


def test_check_fails_loud_when_stale_state_is_unknown(tmp_path):
    """The tri-state is passed through as-is: when undeterminable it must not report "no stale entry" or silently skip cleanup."""

    class UnknownStale(FakePlatform):
        def nss_stale_nickname(self, ca, nickname, nss_dir=None):
            return None

    platform = UnknownStale()
    comp, _platform, paths = _component(tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)

    status = comp.check()
    assert status.ok is False
    assert "cannot determine" in status.detail
    actions = comp.plan()
    assert [a.command[0] for a in actions] == ["remove-stale-nss-ca"], (
        "when undeterminable, plan must produce a cleanup action so apply errors explicitly instead of silently skipping"
    )


def test_plan_offers_stale_cleanup_when_ca_trusted(tmp_path, monkeypatch):
    """The CA is trusted but an old CA is left under the tool's own nickname -> plan() produces a non-privileged cleanup action.

    In this scenario there is no install action: if cleanup only lived in the install
    path, `apply()` would never be called (CLI and setup skip when plan is empty), so
    that code would never run in production.
    """
    _certutil_installed(monkeypatch)

    platform = _StalePlatform()
    comp, _platform, paths = _component(tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)

    status = comp.check()
    assert status.ok is True
    assert "stale" in status.detail
    assert "devproxy trust" in status.hint, "the fix column must give a directly actionable command"

    actions = comp.plan()
    assert [a.command[0] for a in actions] == ["remove-stale-nss-ca"]
    assert actions[0].privileged is False
    assert str(ca) in actions[0].command


def test_remove_stale_action_runs_certutil_delete(tmp_path, monkeypatch):
    _certutil_installed(monkeypatch)

    platform = _StalePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    comp.apply(
        [
            Action(
                description="remove the stale CA root under the tool's nickname in the NSS DB",
                kind="run",
                command=["remove-stale-nss-ca", str(ca), str(tmp_path / "nssdb")],
                privileged=False,
            )
        ]
    )
    deletes = [c for c in platform.commands if c[:1] == ["certutil"] and "-D" in c]
    assert deletes and certs_module.CA_NAME in deletes[0]


def test_remove_stale_action_raises_when_delete_fails(tmp_path, monkeypatch):
    """`certutil -D` fails and the entry is still there -> must error, not silently swallow it.

    The old root would stay trusted by browsers forever; swallowing the failure means the
    tool reports "ready" while the old root remains.
    """
    _certutil_installed(monkeypatch)

    class DeleteFails(FakePlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            if argv[:1] == ["certutil"] and "-D" in argv:
                return 255, "certutil: unable to delete (database locked)"
            return 0, ""

        def nss_stale_nickname(self, ca, nickname, nss_dir=None):
            return True

        def nss_has_nickname(self, nickname, nss_dir=None):
            return True  # the entry is indeed still there

    platform = DeleteFails()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(
            [
                Action(
                    description="remove the stale CA root under the tool's nickname in the NSS DB",
                    kind="run",
                    command=["remove-stale-nss-ca", str(ca), str(tmp_path / "nssdb")],
                    privileged=False,
                )
            ]
        )
    assert "database locked" in str(ei.value)


def test_remove_stale_action_raises_when_delete_unverifiable(tmp_path, monkeypatch):
    """When the DB is locked / unreadable `-L` is also non-zero -- undeterminable must error, not be taken as "entry absent"."""
    _certutil_installed(monkeypatch)

    class Locked(FakePlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            if argv[:1] == ["certutil"] and "-D" in argv:
                return 255, "certutil: function failed: SEC_ERROR_BAD_DATABASE"
            return 0, ""

        def nss_stale_nickname(self, ca, nickname, nss_dir=None):
            return True

        def nss_has_nickname(self, nickname, nss_dir=None):
            return None  # undeterminable

    platform = Locked()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(
            [
                Action(
                    description="remove the stale CA root under the tool's nickname in the NSS DB",
                    kind="run",
                    command=["remove-stale-nss-ca", str(ca), str(tmp_path / "nssdb")],
                    privileged=False,
                )
            ]
        )
    message = str(ei.value)
    assert "cannot confirm" in message
    assert "SEC_ERROR_BAD_DATABASE" in message


def test_remove_stale_action_raises_when_delete_reports_success_but_fails(tmp_path, monkeypatch):
    """`-D` returns 0 but the entry remains (DB directory read-only) -> must error."""
    _certutil_installed(monkeypatch)

    class SilentNoop(FakePlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            return 0, ""  # pretend success, but stale remains True below

        def nss_stale_nickname(self, ca, nickname, nss_dir=None):
            return True

        def nss_has_nickname(self, nickname, nss_dir=None):
            return True

    platform = SilentNoop()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    with pytest.raises(RuntimeError) as ei:
        comp.apply(
            [
                Action(
                    description="remove the stale CA root under the tool's nickname in the NSS DB",
                    kind="run",
                    command=["remove-stale-nss-ca", str(ca), str(tmp_path / "nssdb")],
                    privileged=False,
                )
            ]
        )
    assert "had no effect" in str(ei.value)


def test_remove_stale_action_tolerates_missing_entry(tmp_path, monkeypatch):
    """The entry is already gone due to concurrency -> the target state is reached; idempotent success."""
    _certutil_installed(monkeypatch)

    class AlreadyGone(FakePlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            if argv[:1] == ["certutil"] and "-D" in argv:
                return 255, 'certutil: could not find certificate named "x"'
            return 0, ""

        def nss_stale_nickname(self, ca, nickname, nss_dir=None):
            return True

        def nss_has_nickname(self, nickname, nss_dir=None):
            return False

    platform = AlreadyGone()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    comp.apply(
        [
            Action(
                description="remove the stale CA root under the tool's nickname in the NSS DB",
                kind="run",
                command=["remove-stale-nss-ca", str(ca), str(tmp_path / "nssdb")],
                privileged=False,
            )
        ]
    )  # no exception


def test_remove_stale_action_uses_message_when_platform_cannot_probe(tmp_path, monkeypatch):
    """When the platform provides no nss_has_nickname, judge "absent" from certutil's output."""
    _certutil_installed(monkeypatch)

    class NoProbe(FakePlatform):
        def run(self, argv):
            self.commands.append(list(argv))
            if argv[:1] == ["certutil"] and "-D" in argv:
                return 255, 'certutil: could not find certificate named "x"'
            return 0, ""

        def nss_stale_nickname(self, ca, nickname, nss_dir=None):
            return True

    platform = NoProbe()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    comp.apply(
        [
            Action(
                description="remove the stale CA root under the tool's nickname in the NSS DB",
                kind="run",
                command=["remove-stale-nss-ca", str(ca), str(tmp_path / "nssdb")],
                privileged=False,
            )
        ]
    )


def test_repair_flags_raises_when_platform_lacks_flag_helper(tmp_path, monkeypatch):
    """When the platform cannot produce complete flags, error out -- never fall back to "C,," and wipe the S/MIME/JAR columns."""
    _certutil_installed(monkeypatch)

    class NoFlagHelper(_NssFlagPlatform):
        nss_trust_flags = None

    platform = NoFlagHelper()
    platform.entries = [("Caddy Local Authority", ",,")]
    comp, paths, _ = _injected(tmp_path, platform)
    _write_ca(paths)

    with pytest.raises(RuntimeError, match="nss_trust_flags"):
        comp.apply(comp.plan())
    mods = [c for c in platform.commands if c[:1] == ["certutil"] and "-M" in c]
    assert mods == [], (
        "must not send -M when the flags cannot be computed, let alone overwrite with C,,"
    )


def test_signature_probe_does_not_swallow_internal_type_error(tmp_path):
    """A TypeError raised inside the platform implementation must not be misread as "signature mismatch"."""
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)

    calls = []

    def nss_entries(ca):
        calls.append(ca)
        raise TypeError("platform internal error")

    platform.nss_entries = nss_entries
    assert comp._nss_entries(ca, nss_dir=tmp_path / "nssdb") == []
    assert len(calls) == 1, "a failed signature probe must not cause duplicate calls"


def test_apply_unknown_action_is_a_clean_error(tmp_path):
    comp, _platform, _paths = _component(tmp_path)
    with pytest.raises(RuntimeError, match="unknown action"):
        comp.apply([Action(description="unknown step", kind="run", command=["bogus-action"])])


def test_apply_drops_stale_own_entry_when_trusted_elsewhere(tmp_path, monkeypatch):
    """Trusted, but an **old CA** is left under the tool's own nickname -> delete exactly that entry."""
    _certutil_installed(monkeypatch)

    platform = _StalePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    platform.nss.add(ca)  # the platform considers it trusted (different nickname)
    comp.apply(
        [
            Action(
                description="install the CA root certificate into the NSS DB",
                kind="run",
                command=["install-nss-ca", str(ca), str(tmp_path / "nssdb")],
                privileged=False,
            )
        ]
    )
    deletes = [c for c in platform.commands if c[:1] == ["certutil"] and "-D" in c]
    assert deletes, "the stale own entry must be deleted"
    assert certs_module.CA_NAME in deletes[0]
    assert not [c for c in platform.commands if c[:1] == ["certutil"] and "-A" in c], (
        "must not reinstall when already trusted"
    )


def test_verify_ok_after_trust(tmp_path, monkeypatch):
    _certutil_installed(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    comp.apply(comp.plan())
    _trust_both(platform, ca)
    assert comp.verify().ok is True


def test_verify_not_ok_when_only_system_trusted(tmp_path):
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    platform.trusted.add(ca)
    assert comp.verify().ok is False


def test_verify_checks_real_https_request(tmp_path, monkeypatch):
    """Trust-store installation is not enough -- send a real HTTPS request to confirm end-to-end availability."""
    _certutil_installed(monkeypatch)
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    comp.apply(comp.plan())
    _trust_both(platform, ca)
    paths.sites_caddy.write_text('localhost { respond "hi" }\n', encoding="utf-8")
    assert comp.verify().ok is True
    assert "https://localhost" in platform.http_probes


def test_verify_not_ok_when_https_probe_fails(tmp_path):
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    paths.sites_caddy.write_text('localhost { respond "hi" }\n', encoding="utf-8")
    platform.http_result = False
    status = comp.verify()
    assert status.ok is False
    assert "https://localhost" in status.detail
    assert status.hint


def test_verify_skips_https_probe_when_no_target(tmp_path):
    """With no localhost site and no route, localhost must not be probed blindly."""
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    status = comp.verify()
    assert status.ok is True
    assert "no probeable site" in status.detail
    assert platform.http_probes == []


def test_verify_does_not_treat_localhost_substring_as_site(tmp_path):
    """`foo.localhost.test` is not a localhost site -- it must not be treated as a probeable target."""
    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    paths.sites_caddy.write_text('foo.localhost.test { respond "hi" }\n', encoding="utf-8")
    status = comp.verify()
    assert status.ok is True
    assert "no probeable site" in status.detail
    assert platform.http_probes == []


def test_verify_probes_first_registered_route(tmp_path):
    """When a route exists, probe the real domain -- localhost has no site, so that tests matching, not trust."""
    import json

    platform = FakePlatform()
    comp, paths, _ = _injected(tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    paths.routes_json.write_text(
        json.dumps({"version": 1, "routes": [{"domain": "codeup.test"}]}),
        encoding="utf-8",
    )
    # Even if a localhost site also exists on the machine, the real route should be probed first
    paths.sites_caddy.write_text('localhost { respond "hi" }\n', encoding="utf-8")
    assert comp.verify().ok is True
    assert platform.http_probes == ["https://codeup.test"]


# -- CLI: trust / logs ------------------------------------------------------


def _patch_cli(monkeypatch, tmp_path, platform):
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context", lambda *a, **k: (paths, cfg, None, None)
    )
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    return paths, cfg


def _patch_cert_component(monkeypatch, tmp_path):
    def build(p, paths_, cfg_):
        # The NSS DB injected by the component must also become the fake platform's default DB,
        # otherwise `_require_same_nss_db` judges "writes DB A, re-checks DB B".
        p.nss_db = tmp_path / "nssdb"
        return CertComponent(
            p,
            paths_,
            cfg_,
            system_dest=tmp_path / "ca" / "devproxy-local.crt",
            nss_dir=tmp_path / "nssdb",
        )

    monkeypatch.setattr("devproxy.cli._shared.CertComponent", build)


def test_cli_trust_is_idempotent(tmp_path, monkeypatch):
    platform = FakePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    result = CliRunner().invoke(app, ["trust"])
    assert result.exit_code == 0
    assert "The CA root certificate is already trusted; nothing to do." in result.stdout
    assert platform.commands == []


def test_cli_trust_reports_cleanup_not_install(tmp_path, monkeypatch):
    """A cleanup-only run must not say "Installed the CA root certificate" -- those are different things."""
    _certutil_installed(monkeypatch)
    platform = _StalePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    ca = _write_ca(paths)
    _trust_both(platform, ca)

    result = CliRunner().invoke(app, ["trust", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "Removed the stale CA root certificate" in result.stdout
    assert "Installed the CA root certificate" not in result.stdout


def test_cli_trust_without_ca_is_a_note(tmp_path, monkeypatch):
    platform = FakePlatform()
    _patch_cli(monkeypatch, tmp_path, platform)
    result = CliRunner().invoke(app, ["trust"])
    assert result.exit_code == 0
    assert "No CA root certificate yet" in result.stdout


def test_cli_trust_refuses_privileged_without_yes(tmp_path, monkeypatch):
    """Non-interactive environment + root needed -> exit code 4; must not hang on a password prompt."""
    platform = FakePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    _write_ca(paths)
    _patch_cert_component(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["trust"])
    assert result.exit_code == 4
    assert platform.commands == []


def test_cli_trust_installs_into_injected_targets(tmp_path, monkeypatch):
    _certutil_installed(monkeypatch)
    platform = FakePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    _write_ca(paths)
    _patch_cert_component(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["trust", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "Installed the CA root certificate" in result.stdout
    assert (tmp_path / "ca" / "devproxy-local.crt").exists()
    for written in platform.written:
        assert tmp_path in written.parents


def test_cli_trust_failure_exits_1_without_traceback(tmp_path, monkeypatch):
    _certutil_missing(monkeypatch)
    platform = FakePlatform()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    _write_ca(paths)
    _patch_cert_component(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["trust", "--yes"])
    assert result.exit_code == 1
    assert "certutil" in result.stderr
    assert "Traceback" not in result.stdout


def test_cli_trust_exits_4_when_sudo_denied(tmp_path, monkeypatch):
    """A denied sudo is an elevation problem (exit code 4), not an ordinary runtime failure."""
    from devproxy.platform.fake import FakeElevated

    class SudoDenied(FakePlatform):
        def privilege(self):
            if self._elevated is None:

                class Elev(FakeElevated):
                    def copy(self, src, dest):
                        raise PermissionError("sudo: wrong password")

                self._elevated = Elev(self)
            return self._elevated

    _certutil_installed(monkeypatch)
    platform = SudoDenied()
    paths, cfg = _patch_cli(monkeypatch, tmp_path, platform)
    _write_ca(paths)
    _patch_cert_component(monkeypatch, tmp_path)
    result = CliRunner().invoke(app, ["trust", "--yes"])
    assert result.exit_code == 4
    assert "sudo: wrong password" in result.stderr


def test_cli_logs_rejects_unknown_service(tmp_path, monkeypatch):
    platform = FakePlatform()
    _patch_cli(monkeypatch, tmp_path, platform)
    result = CliRunner().invoke(app, ["logs", "nginx"])
    # Argument errors follow the global convention and exit 2 (section 9.1), not runtime failure 1
    assert result.exit_code == 2


def test_cli_logs_tails_service_log(tmp_path, monkeypatch):
    class RecordingPlatform(FakePlatform):
        def __init__(self):
            super().__init__()
            self.tailed = []

        def tail_service_log(self, name):
            self.tailed.append(name)

    platform = RecordingPlatform()
    _patch_cli(monkeypatch, tmp_path, platform)
    result = CliRunner().invoke(app, ["logs", "caddy"])
    assert result.exit_code == 0, result.stdout
    assert platform.tailed == ["caddy"]


def test_cli_logs_missing_journalctl_is_clean_error(tmp_path, monkeypatch):
    """A missing journalctl must not leak FileNotFoundError as a traceback."""

    class MissingJournal(FakePlatform):
        def tail_service_log(self, name):
            raise FileNotFoundError("journalctl")

    platform = MissingJournal()
    _patch_cli(monkeypatch, tmp_path, platform)
    result = CliRunner().invoke(app, ["logs", "caddy"])
    assert result.exit_code == 1
    assert "journalctl" in result.stderr
    assert "Traceback" not in result.output


def test_cli_logs_does_not_create_files_in_home(tmp_path, monkeypatch):
    """logs is a read-only command; it must not create files in the user's home directory out of nowhere."""
    platform = FakePlatform()
    _patch_cli(monkeypatch, tmp_path, platform)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    result = CliRunner().invoke(app, ["logs", "caddy"])
    assert result.exit_code == 0, result.stdout
    assert list(home.iterdir()) == []


# -- macOS: the CA goes into the system keychain; there is no NSS step --------


class _MacPlatform(FakePlatform):
    """macOS platform: no NSS DB; the CA is only installed into the system keychain."""

    name = "mac"
    dns_port = 5300

    def nss_trusted(self, ca):
        return True


def test_macos_plan_installs_ca_into_system_keychain(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    _write_ca(paths)
    actions = comp.plan()
    assert len(actions) == 1, "macOS has no NSS DB -- only install into the system keychain"
    assert actions[0].command[0] == "install-macos-ca"
    assert actions[0].privileged is True


def test_macos_plan_skips_nss_step(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    _write_ca(paths)
    actions = comp.plan()
    assert not any(a.command[:1] == ["install-nss-ca"] for a in actions)


def test_macos_plan_is_empty_when_keychain_trusts(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    ca = _write_ca(paths)
    platform.trusted.add(ca)
    assert comp.plan() == []


def test_macos_apply_adds_trusted_cert_to_system_keychain(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    ca = _write_ca(paths)
    comp.apply(comp.plan())
    assert [
        "security",
        "add-trusted-cert",
        "-d",
        "-r",
        "trustRoot",
        "-k",
        certs_module.SYSTEM_KEYCHAIN,
        str(ca),
    ] in platform.commands


def test_macos_apply_reports_keychain_failure(tmp_path):
    """A keychain write failure must error -- a silent failure would make the user think it is trusted."""

    class Failing(_MacPlatform):
        def privilege(self):
            class _S:
                def run(self, argv):
                    return 1, "boom"

                def write(self, path, content):
                    pass

                def read(self, path):
                    return ""

                def copy(self, src, dest):
                    pass

            return _S()

    comp, platform, paths = _component(tmp_path, platform=Failing())
    _write_ca(paths)
    with pytest.raises(RuntimeError, match="keychain"):
        comp.apply(comp.plan())


def test_macos_check_reports_keychain(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    _write_ca(paths)
    status = comp.check()
    assert status.ok is False
    assert "keychain" in status.detail


def test_macos_check_ok_when_keychain_trusts(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    ca = _write_ca(paths)
    platform.trusted.add(ca)
    status = comp.check()
    assert status.ok is True
    assert "keychain" in status.detail


def test_macos_verify_ok_when_keychain_trusts(tmp_path):
    comp, platform, paths = _component(tmp_path, platform=_MacPlatform())
    ca = _write_ca(paths)
    platform.trusted.add(ca)
    assert comp.verify().ok is True


def test_linux_plan_still_has_two_steps(tmp_path):
    """The Linux path is unchanged: both the system trust store and NSS steps are present."""
    comp, platform, paths = _component(tmp_path)
    _write_ca(paths)
    actions = comp.plan()
    assert len(actions) == 2
    assert {a.command[0] for a in actions} == {"install-nss-ca", "install-system-ca"}


class _DeniedKeychainPlatform(_MacPlatform):
    """An elevated session can be established, but `security add-trusted-cert` is denied by the system.

    The exact text measured on a real machine (macOS 26, SSH session):
    `SecTrustSettingsSetTrustSettings: The authorization was denied since no
    user interaction was possible.`
    """

    def privilege(self):
        platform = self

        class _Denied:
            def run(self, argv):
                platform.commands.append(list(argv))
                return 1, (
                    "SecTrustSettingsSetTrustSettings: The authorization was "
                    "denied since no user interaction was possible."
                )

            def write(self, path, content):  # pragma: no cover - not reached by this case
                raise AssertionError("must not get here")

            def read(self, path):  # pragma: no cover
                raise AssertionError("must not get here")

            def copy(self, src, dest):  # pragma: no cover
                raise AssertionError("must not get here")

        return _Denied()


def test_macos_ca_denied_without_gui_raises_permission_error(tmp_path):
    """In an SSH session macOS refuses to modify system trust settings -- this must be an actionable message.

    Previously this raised RuntimeError (with only the system text), which the CLI mapped
    to exit code 1; the user knew neither the cause nor where to do it. The essence of
    this failure is "a person must click in that Mac's GUI", so it should raise
    PermissionError (the CLI maps it to exit 4) and state directly where to run what.
    """
    comp, _platform, paths = _component(tmp_path, platform=_DeniedKeychainPlatform())
    _write_ca(paths)
    actions = comp.plan()
    assert actions and actions[0].command[0] == "install-macos-ca"

    with pytest.raises(PermissionError) as cm:
        comp.apply(actions)
    message = str(cm.value)
    assert "Terminal window" in message, "must state where to do it"
    assert "devproxy trust" in message, "must give the concrete command to run"
    assert "no user interaction" in message, (
        "the original system text must be kept for verification"
    )


def test_cli_trust_exits_4_when_macos_denies_without_gui(tmp_path, monkeypatch):
    """CLI layer: this kind of failure must be exit code 4 (human interaction needed), not 1.

    Exit code 4 is a signal for scripts/CI: "your command is not wrong; someone must
    click in the GUI on that machine". Using 1 would be taken as a program bug.
    """
    platform = _DeniedKeychainPlatform()
    paths, _cfg = _patch_cli(monkeypatch, tmp_path, platform)
    _write_ca(paths)

    result = CliRunner().invoke(app, ["trust", "--yes"])
    assert result.exit_code == 4, result.stdout
    # The hint goes through err_console (stderr) -- only success messages go to stdout
    shown = (result.stdout or "") + (result.stderr or "")
    assert "Terminal window" in shown
    assert "devproxy trust" in shown


# -- Firefox: it has its own certificate store; NSS/system trust does not apply --


def _make_firefox_profile(home: Path, name: str = "abc.default-release") -> Path:
    """Create a used Firefox profile (with a cert9.db store)."""
    profile = home / ".mozilla" / "firefox" / name
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "cert9.db").write_bytes(b"")
    return profile


def test_firefox_profile_dirs_empty_without_firefox(tmp_path):
    assert certs_module.firefox_profile_dirs(tmp_path) == []


def test_firefox_profile_dirs_finds_profile(tmp_path):
    profile = _make_firefox_profile(tmp_path)
    assert certs_module.firefox_profile_dirs(tmp_path) == [profile]


def test_firefox_profile_dirs_ignores_non_profile_dirs(tmp_path):
    """Miscellaneous directories under ~/.mozilla/firefox (e.g. Crash Reports) are not profiles."""
    (tmp_path / ".mozilla" / "firefox" / "Crash Reports").mkdir(parents=True)
    assert certs_module.firefox_profile_dirs(tmp_path) == []


def test_firefox_profile_dirs_probe_failure_is_not_an_error(tmp_path, monkeypatch):
    """When the probe fails, treat it as no Firefox -- must not raise, and must not be taken as "Firefox present"."""

    def boom(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert certs_module.firefox_profile_dirs(tmp_path) == []


def test_check_hints_firefox_when_installed(tmp_path, isolated_home):
    """Firefox is present on this machine: the hint must give actionable manual-trust guidance."""
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    _make_firefox_profile(isolated_home)
    status = comp.check()
    assert status.ok is True, (
        "not installing the CA into Firefox is not a defect; must not fail because of it"
    )
    assert "Firefox" in status.hint
    assert "security.enterprise_roots.enabled" in status.hint


def test_check_does_not_mention_firefox_when_absent(tmp_path):
    """Do not mention Firefox when it is not installed -- no noise."""
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    status = comp.check()
    assert status.ok is True
    assert "Firefox" not in status.hint


def test_check_firefox_hint_survives_missing_trust(tmp_path, isolated_home):
    """When the CA is not trusted yet, the hint must give both the trust command and the Firefox workaround."""
    comp, platform, paths = _component(tmp_path)
    _write_ca(paths)
    _make_firefox_profile(isolated_home)
    status = comp.check()
    assert status.ok is False
    assert "devproxy trust" in status.hint
    assert "Firefox" in status.hint


def test_firefox_note_none_without_ca(tmp_path, isolated_home):
    """With no CA there is nothing to import -- do not hint."""
    comp, _platform, _paths = _component(tmp_path)
    _make_firefox_profile(isolated_home)
    assert comp.firefox_note() is None


def test_firefox_note_points_at_ca_path(tmp_path, isolated_home):
    """The note must state which file to import."""
    comp, _platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    _make_firefox_profile(isolated_home)
    note = comp.firefox_note()
    assert note is not None
    assert str(ca) in note
    assert "security.enterprise_roots.enabled" in note


def test_check_never_raises_when_firefox_probe_breaks(tmp_path, isolated_home, monkeypatch):
    """A broken probe must not make check() raise -- doctor relies on it to list everything at once."""

    def boom(*args, **kwargs):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(certs_module, "firefox_profile_dirs", boom)
    comp, platform, paths = _component(tmp_path)
    ca = _write_ca(paths)
    _trust_both(platform, ca)
    status = comp.check()
    assert status.ok is True
    assert "Firefox" not in status.hint
