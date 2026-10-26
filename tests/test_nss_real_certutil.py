"""Behavior against a real NSS database: the **real certutil** + a temporary DB.

The second defect exposed by the first real-machine run on 2026-10-23: NSS
deduplicates by (issuer, serial) -- adding the same certificate again under a different
nickname with `certutil -A` **silently does nothing** (returns 0, no new entry).
So "is this CA in the NSS DB?" can only be judged by **certificate content**, not by nickname.

This file reproduces that behavior with the real certutil -- a fake platform can never
test it: `FakePlatform` does not implement certutil's deduplication rules.

The whole group is skipped when certutil is not available on this machine
(`sudo apt install libnss3-tools`).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from devproxy.components.certs import CertComponent
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.platform.base import Action
from devproxy.platform.linux import LinuxPlatform

pytestmark = pytest.mark.skipif(
    shutil.which("certutil") is None, reason="no certutil on this machine (libnss3-tools)"
)


def _need_openssl() -> None:
    if shutil.which("openssl") is None:
        pytest.skip("no openssl on this machine")


def _certutil(db: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["certutil", "-d", f"sql:{db}", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _new_db(path: Path) -> Path:
    """Create an empty NSS database."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["certutil", "-N", "-d", f"sql:{path}", "--empty-password"],
        capture_output=True,
        text=True,
        check=True,
    )
    return path


def _make_ca(tmp_path: Path, cn: str = "devproxy fixwave test CA") -> Path:
    """Create a self-signed CA (content can be compared; no fake PEM needed)."""
    _need_openssl()
    stem = cn.replace(" ", "-")
    ca = tmp_path / f"{stem}.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-keyout",
            str(tmp_path / f"{stem}.key"),
            "-out",
            str(ca),
            "-days",
            "3650",
            "-nodes",
            "-subj",
            f"/CN={cn}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return ca


def _rows(db: Path) -> list[tuple[str, str]]:
    """List the entries in the DB: [(nickname, trust flags)].

    The nickname column is fixed at width 55 (test nicknames are short) and flags start at
    column 57 -- this deliberately **does not reuse** the platform-layer parser, so a
    wrong implementation cannot make the test wrong too.
    """
    out = _certutil(db, "-L").stdout
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        if "Trust Attributes" in line or line.strip().startswith("SSL,"):
            continue
        name = line[:55].strip()
        flags = line[56:].strip()
        if name:
            rows.append((name, flags))
    return rows


# -- behavior reproduction --------------------------------------------------


def test_certutil_add_dedups_by_issuer_and_serial(tmp_path):
    """Reproduce the real-machine behavior: adding the same certificate under another nickname with -A does **not** create an entry.

    The return code is still 0 -- this is "silent failure": the caller thinks it
    installed it, but the DB has no entry under its own nickname.
    """
    ca = _make_ca(tmp_path)
    db = _new_db(tmp_path / "nssdb")
    assert (
        _certutil(db, "-A", "-n", "devproxy Local CA", "-t", "C,,", "-i", str(ca)).returncode == 0
    )
    assert [name for name, _ in _rows(db)] == ["devproxy Local CA"]

    again = _certutil(db, "-A", "-n", "another nickname", "-t", "C,,", "-i", str(ca))
    assert again.returncode == 0, "certutil returns 0 for a duplicate certificate"
    assert [name for name, _ in _rows(db)] == ["devproxy Local CA"], (
        "a duplicate certificate does not add an entry -- NSS deduplicates by (issuer, serial)"
    )


# -- platform layer: judge by content --------------------------------------


def test_nss_trusted_when_cert_stored_under_other_nickname(tmp_path):
    """The same CA is stored under another nickname -- judged as trusted.

    On a real machine this entry is the "Caddy Local Authority" installed by the old devp.
    """
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", "C,,", "-i", str(ca))

    platform = LinuxPlatform(user_home=home)
    assert platform.nss_trusted(ca) is True
    assert platform.nss_entries(ca) == [("Caddy Local Authority", "C,,")]


def test_nss_trusted_false_when_entry_has_no_trust_flag(tmp_path):
    """The DB has this CA but the flags are ",," (no trust) -- not counted as trusted."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", ",,", "-i", str(ca))

    platform = LinuxPlatform(user_home=home)
    assert platform.nss_trusted(ca) is False
    assert platform.nss_entries(ca) == [("Caddy Local Authority", ",,")]


def test_nss_trusted_false_when_other_cert_stored(tmp_path):
    """The DB holds a **different** certificate -- this CA is not counted as trusted."""
    ca = _make_ca(tmp_path)
    other = tmp_path / "other.crt"
    _need_openssl()
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-keyout",
            str(tmp_path / "other.pem"),
            "-out",
            str(other),
            "-days",
            "3650",
            "-nodes",
            "-subj",
            "/CN=some other CA",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "other CA", "-t", "C,,", "-i", str(other))

    platform = LinuxPlatform(user_home=home)
    assert platform.nss_trusted(ca) is False
    assert platform.nss_entries(ca) == []


def test_nss_entries_finds_entry_whose_nickname_has_spaces(tmp_path):
    """Nicknames may contain spaces -- parsing must not split on the first space."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "my local ca", "-t", "C,,", "-i", str(ca))

    platform = LinuxPlatform(user_home=home)
    assert platform.nss_trusted(ca) is True
    assert platform.nss_entries(ca) == [("my local ca", "C,,")]


# -- component layer: plan / apply -----------------------------------------


class _SystemTrusted(LinuxPlatform):
    """Pretend the CA already went into the system trust store -- this group only cares about the NSS half."""

    def ca_trusted(self, ca: Path) -> bool:
        return True


def _component(tmp_path, home: Path, db: Path):
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    platform = _SystemTrusted(user_home=home)
    comp = CertComponent(platform, paths, cfg, nss_dir=db)
    return comp, platform, paths, cfg


def _install_action(ca: Path, db: Path) -> Action:
    return Action(
        description="install the CA root certificate into the NSS DB (Chrome/Chromium)",
        kind="run",
        command=["install-nss-ca", str(ca), str(db)],
        privileged=False,
    )


def _remove_action(ca: Path, db: Path) -> Action:
    return Action(
        description="remove the stale CA root under the tool's nickname in the NSS DB",
        kind="run",
        command=["remove-stale-nss-ca", str(ca), str(db)],
        privileged=False,
    )


def test_plan_is_empty_when_ca_trusted_under_other_nickname(tmp_path):
    """The DB already has this CA with a trust flag -> do not ask the user to do it again."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", "C,,", "-i", str(ca))

    comp, _platform, paths, cfg = _component(tmp_path, home, db)
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    assert comp.check().ok is True
    assert comp.plan() == []


def test_apply_does_not_touch_existing_entry(tmp_path):
    """Running the install action when already trusted: must not error and must not touch the user's existing entry."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", "C,,", "-i", str(ca))

    comp, _platform, _paths, _cfg = _component(tmp_path, home, db)
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    comp.apply([_install_action(comp.ca_path, db)])

    rows = _rows(db)
    assert rows == [("Caddy Local Authority", "C,,")], (
        "the user's existing entry must not be deleted or renamed -- the tool must not add a second nickname to the user's DB"
    )


def test_apply_repairs_missing_trust_flag(tmp_path):
    """The DB has this CA but lacks the SSL trust flag -> it must have the trust flag after installation."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", ",,", "-i", str(ca))

    comp, _platform, _paths, _cfg = _component(tmp_path, home, db)
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    assert comp.check().ok is False
    comp.apply([_install_action(comp.ca_path, db)])
    assert comp.check().ok is True
    assert _rows(db) == [("Caddy Local Authority", "C,,")]


def test_apply_installs_into_empty_database(tmp_path):
    """Empty DB -> install normally; the nickname is the tool's own."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")

    comp, _platform, _paths, _cfg = _component(tmp_path, home, db)
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    comp.apply([_install_action(comp.ca_path, db)])
    assert _rows(db) == [("devproxy Local CA", "C,,")]
    assert comp.check().ok is True


# -- fix round 1: only the SSL column's "C" counts, plus three-column flags --


def _trusted(ca: Path, home: Path, db: Path) -> tuple[bool, list]:
    platform = LinuxPlatform(user_home=home)
    return platform.nss_trusted(ca), platform.nss_entries(ca)


def test_nss_trusted_false_for_trusted_peer_flag(tmp_path):
    """`P,,` is "trusted peer", **not a CA anchor** -- must not count as trusted.

    Measured during evaluation (real NSS): after storing the CA as `P,,`, a leaf it
    signed reports "issuer has been marked as not trusted" under `certutil -V -u V`.
    """
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", "P,,", "-i", str(ca))

    trusted, entries = _trusted(ca, home, db)
    assert trusted is False
    assert entries == [("Caddy Local Authority", "P,,")]


def test_nss_trusted_false_for_client_ca_flag(tmp_path):
    """`T,,` is "CA that can issue client certificates", **not** a server anchor -- must not count as trusted."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", "T,,", "-i", str(ca))

    trusted, entries = _trusted(ca, home, db)
    assert trusted is False
    assert entries == [("Caddy Local Authority", "T,,")]


def test_nss_trusted_false_when_only_smime_column_trusted(tmp_path):
    """`,C,` trusts only S/MIME usage -- the SSL column is empty, so HTTPS does not accept it."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", ",C,", "-i", str(ca))

    platform = LinuxPlatform(user_home=home)
    # First confirm certutil really stored ",C," (not rewritten by NSS)
    assert _rows(db) == [("Caddy Local Authority", ",C,")]
    assert platform.nss_trusted(ca) is False
    assert platform.nss_entries(ca) == [("Caddy Local Authority", ",C,")]


def test_nss_trusted_true_for_three_column_flags(tmp_path):
    """`C,C,C` (all three columns trusted) must be judged trusted.

    On a real machine `Caddy Local Authority` has exactly this shape. The old "at least
    two consecutive commas" regex cannot parse it -> judged untrusted -> adds an install
    step to the plan -> `-A -t C,,` rewrites the row as `C,,`, silently wiping the trust
    in the other two columns.
    """
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", "C,C,C", "-i", str(ca))
    assert _rows(db) == [("Caddy Local Authority", "C,C,C")]

    trusted, entries = _trusted(ca, home, db)
    assert trusted is True
    assert entries == [("Caddy Local Authority", "C,C,C")]


def test_plan_is_empty_for_three_column_flags(tmp_path):
    """`C,C,C` already trusted -> plan() must be empty (B-5)."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", "C,C,C", "-i", str(ca))

    comp, _platform, _paths, _cfg = _component(tmp_path, home, db)
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    assert comp.check().ok is True
    assert comp.plan() == []


def test_apply_keeps_three_column_flags(tmp_path):
    """After apply the entry must still be `C,C,C` -- it must not be wiped to `C,,` by `-A -t C,,`."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", "C,C,C", "-i", str(ca))

    comp, _platform, _paths, _cfg = _component(tmp_path, home, db)
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    comp.apply([_install_action(comp.ca_path, db)])

    assert _rows(db) == [("Caddy Local Authority", "C,C,C")], (
        "the other two trust columns of the user entry must not be wiped"
    )


def test_apply_repairs_only_ssl_column(tmp_path):
    """Repairing `,C,` patches only the SSL column -> `C,C,`; S/MIME trust must be preserved."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", ",C,", "-i", str(ca))

    comp, _platform, _paths, _cfg = _component(tmp_path, home, db)
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    assert comp.check().ok is False
    comp.apply([_install_action(comp.ca_path, db)])
    assert comp.check().ok is True
    assert _rows(db) == [("Caddy Local Authority", "C,C,")], (
        "adding the SSL column must not wipe S/MIME trust"
    )


# -- fix round 1: stale entries under the tool's own nickname ---------------


def test_plan_offers_stale_cleanup_when_ca_trusted_elsewhere(tmp_path):
    """An old CA is left under the tool's own nickname while the current CA is trusted under another nickname -> plan() must produce a cleanup action.

    In this scenario there is **no install action**: if cleanup only lived in the install
    path, `apply()` would never be called (CLI and setup skip when plan is empty), so that
    code would never run in production.
    """
    old_ca = _make_ca(tmp_path, "old devproxy CA")
    ca = _make_ca(tmp_path, "current devproxy CA")
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "devproxy Local CA", "-t", "C,,", "-i", str(old_ca))
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", "C,,", "-i", str(ca))

    comp, _platform, _paths, _cfg = _component(tmp_path, home, db)
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    status = comp.check()
    assert status.ok is True, (
        "the CA really is trusted -- the stale root is a hygiene issue, not unreadiness"
    )
    assert "stale" in status.detail, "detail must not falsely claim everything is fine"

    actions = comp.plan()
    assert [a.command[0] for a in actions] == ["remove-stale-nss-ca"], (
        "when already trusted, no install action should be planned, but a cleanup action must be"
    )
    assert actions[0].privileged is False

    comp.apply(actions)

    assert _rows(db) == [
        (
            "Caddy Local Authority",
            "C,,",
        )
    ], (
        "the stale CA under the tool's own nickname must be deleted; the user's entry must not be touched"
    )
    assert comp.plan() == [], "after cleanup there should be absolutely nothing left to do"


def test_apply_removes_stale_entry_while_repairing_flags(tmp_path):
    """The trust-flag repair path must also clean up stale entries -- nobody looks back at the old root after the fix."""
    old_ca = _make_ca(tmp_path, "old devproxy CA")
    ca = _make_ca(tmp_path, "current devproxy CA")
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "devproxy Local CA", "-t", "C,,", "-i", str(old_ca))
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", ",,", "-i", str(ca))

    comp, _platform, _paths, _cfg = _component(tmp_path, home, db)
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    comp.apply([_install_action(comp.ca_path, db)])

    assert _rows(db) == [("Caddy Local Authority", "C,,")]


def test_production_construction_detects_stale_entry(tmp_path, isolated_home):
    """**Production construction** (without injecting nss_dir) must also see the stale entry.

    `NSS_DIR = Path("~/.pki/nssdb")` -- pathlib does not expand `~`. If the component does
    not expand it at construction, `(nss_dir / "cert9.db").exists()` checks "a directory
    named `~` in the current working directory", which is always False. All three
    production constructions in cli.py do not pass nss_dir, so this whole cleanup path is
    dead for every real user.

    This case deliberately **does not inject** nss_dir: the DB lives in
    `$HOME/.pki/nssdb` (isolated_home), and the platform also uses the default `Path.home()`.
    """
    old_ca = _make_ca(tmp_path, "old devproxy CA")
    ca = _make_ca(tmp_path, "current devproxy CA")
    db = _new_db(isolated_home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "devproxy Local CA", "-t", "C,,", "-i", str(old_ca))
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", "C,,", "-i", str(ca))

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    platform = _SystemTrusted()  # user_home defaults to Path.home() = isolated_home
    comp = CertComponent(platform, paths, cfg)  # <- production construction: no nss_dir

    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    assert comp.nss_dir == isolated_home / ".pki" / "nssdb", "`~` must be expanded"
    assert (comp.nss_dir / "cert9.db").exists()

    status = comp.check()
    assert status.ok is True
    assert "stale" in status.detail, (
        "under production construction check() must report the stale entry"
    )
    assert "devproxy trust" in status.hint

    actions = comp.plan()
    assert [a.command[0] for a in actions] == ["remove-stale-nss-ca"], (
        "under production construction plan() must produce a cleanup action, otherwise apply() is never called"
    )

    comp.apply(actions)
    assert _rows(db) == [
        (
            "Caddy Local Authority",
            "C,,",
        )
    ]
    assert comp.plan() == []


def test_apply_keeps_own_entry_when_it_is_the_current_ca(tmp_path):
    """The tool's own nickname holds the **current** CA (already trusted) -> delete nothing."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "devproxy Local CA", "-t", "C,,", "-i", str(ca))

    comp, _platform, _paths, _cfg = _component(tmp_path, home, db)
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    comp.apply([_install_action(comp.ca_path, db)])
    assert _rows(db) == [("devproxy Local CA", "C,,")]


# -- fix round 1: the re-check DB must match the write DB ------------------


def test_apply_fails_loud_when_platform_db_differs(tmp_path):
    """The DB the component writes and the DB the platform re-checks differ -> explicit error; never silently check another DB."""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    platform_db = _new_db(home / ".pki" / "nssdb")
    other_db = _new_db(tmp_path / "injected" / "nssdb")

    comp, _platform, _paths, _cfg = _component(tmp_path, home, platform_db)
    comp.nss_dir = other_db  # deliberately make the two sides disagree
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(RuntimeError) as ei:
        comp.apply([_install_action(comp.ca_path, other_db)])
    message = str(ei.value)
    assert "mismatch" in message
    assert str(other_db) in message and str(platform_db) in message
    assert _rows(other_db) == [], "must not write to either DB before erroring"


# -- fix round 3: re-verification after deletion ---------------------------


def test_apply_reports_when_delete_silently_does_nothing(tmp_path):
    """When the DB **directory is read-only**, `certutil -D` returns 0 but deletes nothing -- must error.

    Measured (NSS 3.120): after `chmod 500 <db>`, `certutil -D` returns 0 while
    `certutil -L` still lists the entry. Trusting the return code alone would report "cleaned up".
    """
    if os.geteuid() == 0:
        pytest.skip(
            "root is not restricted by directory write permissions, so this scenario cannot be reproduced"
        )
    old_ca = _make_ca(tmp_path, "old devproxy CA")
    ca = _make_ca(tmp_path, "current devproxy CA")
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "devproxy Local CA", "-t", "C,,", "-i", str(old_ca))
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", "C,,", "-i", str(ca))

    comp, _platform, _paths, _cfg = _component(tmp_path, home, db)
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    db.chmod(0o500)
    try:
        with pytest.raises(RuntimeError) as ei:
            comp.apply([_remove_action(comp.ca_path, db)])
    finally:
        db.chmod(0o700)
    assert "had no effect" in str(ei.value)
    assert [n for n, _ in _rows(db)] == ["devproxy Local CA", "Caddy Local Authority"], (
        "the entries cannot be deleted in this scenario anyway -- the test's job is to assert that we **error**"
    )
