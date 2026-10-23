"""真 NSS 库上的行为：**真的 certutil** + 临时库。

2026-10-23 真机首跑暴露的缺陷之二：NSS 按 (issuer, serial) 去重 ——
同一张证书换个昵称再 `certutil -A`，会**静默无操作**（返回 0，条目并不新增）。
于是"这张 CA 装进 NSS 库了没"只能按**证书内容**判定，不能按昵称。

本文件用真的 certutil 复现这个现象 —— 假平台永远测不出来：
`FakePlatform` 不实现 certutil 的去重规则。

本机没有 certutil 就整组跳过（`sudo apt install libnss3-tools`）。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from devproxy.components.certs import CertComponent
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Action
from devproxy.platform.linux import LinuxPlatform

pytestmark = pytest.mark.skipif(
    shutil.which("certutil") is None, reason="本机没有 certutil（libnss3-tools）"
)


def _need_openssl() -> None:
    if shutil.which("openssl") is None:
        pytest.skip("本机没有 openssl")


def _certutil(db: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["certutil", "-d", f"sql:{db}", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _new_db(path: Path) -> Path:
    """建一个空的 NSS 库。"""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["certutil", "-N", "-d", f"sql:{path}", "--empty-password"],
        capture_output=True,
        text=True,
        check=True,
    )
    return path


def _make_ca(tmp_path: Path) -> Path:
    """造一张自签 CA（内容可比对，不用假 PEM）。"""
    _need_openssl()
    ca = tmp_path / "root.crt"
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
            str(tmp_path / "key.pem"),
            "-out",
            str(ca),
            "-days",
            "3650",
            "-nodes",
            "-subj",
            "/CN=devproxy fixwave test CA",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return ca


def _rows(db: Path) -> list[tuple[str, str]]:
    """列出库里的条目：[(昵称, 信任标志)]。

    昵称列宽固定 55（测试里的昵称都不长），标志在第 57 列 ——
    这里刻意**不复用**平台层的解析代码，免得实现错了测试也跟着错。
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


# -- 现象复现 ---------------------------------------------------------------


def test_certutil_add_dedups_by_issuer_and_serial(tmp_path):
    """复现真机现象：同一张证书换个昵称再 -A，条目**不会**新增。

    返回码仍是 0 —— 这就是"静默失败"：调用方以为装上了，库里却根本没有
    自己那个昵称的条目。
    """
    ca = _make_ca(tmp_path)
    db = _new_db(tmp_path / "nssdb")
    assert _certutil(db, "-A", "-n", "devproxy Local CA", "-t", "C,,", "-i", str(ca)).returncode == 0
    assert [name for name, _ in _rows(db)] == ["devproxy Local CA"]

    again = _certutil(db, "-A", "-n", "另一个昵称", "-t", "C,,", "-i", str(ca))
    assert again.returncode == 0, "certutil 对重复证书返回 0"
    assert [name for name, _ in _rows(db)] == ["devproxy Local CA"], (
        "重复的证书不会新增条目 —— NSS 按 (issuer, serial) 去重"
    )


# -- 平台层：按内容判定 ---------------------------------------------------


def test_nss_trusted_when_cert_stored_under_other_nickname(tmp_path):
    """同一张 CA 用别的昵称入库 —— 判定为已信任。

    真机上这条是老 devp 装的 "Caddy Local Authority"。
    """
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", "C,,", "-i", str(ca))

    platform = LinuxPlatform(user_home=home)
    assert platform.nss_trusted(ca) is True
    assert platform.nss_entries(ca) == [("Caddy Local Authority", "C,,")]


def test_nss_trusted_false_when_entry_has_no_trust_flag(tmp_path):
    """库里有这张 CA 但标志是 ",,"（无信任）—— 不算已信任。"""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "Caddy Local Authority", "-t", ",,", "-i", str(ca))

    platform = LinuxPlatform(user_home=home)
    assert platform.nss_trusted(ca) is False
    assert platform.nss_entries(ca) == [("Caddy Local Authority", ",,")]


def test_nss_trusted_false_when_other_cert_stored(tmp_path):
    """库里是**别的**证书 —— 不算这张 CA 已信任。"""
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
    _certutil(db, "-A", "-n", "别的 CA", "-t", "C,,", "-i", str(other))

    platform = LinuxPlatform(user_home=home)
    assert platform.nss_trusted(ca) is False
    assert platform.nss_entries(ca) == []


def test_nss_entries_finds_entry_whose_nickname_has_spaces(tmp_path):
    """昵称可以含空格 —— 解析不能按"第一个空格"切。"""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")
    _certutil(db, "-A", "-n", "my local ca", "-t", "C,,", "-i", str(ca))

    platform = LinuxPlatform(user_home=home)
    assert platform.nss_trusted(ca) is True
    assert platform.nss_entries(ca) == [("my local ca", "C,,")]


# -- 组件层：plan / apply --------------------------------------------------


class _SystemTrusted(LinuxPlatform):
    """假装 CA 已经进了系统信任库 —— 本组只关心 NSS 这一半。"""

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
        description="安装 CA 根证书到 NSS 库（Chrome/Chromium）",
        kind="run",
        command=["install-nss-ca", str(ca), str(db)],
        privileged=False,
    )


def test_plan_is_empty_when_ca_trusted_under_other_nickname(tmp_path):
    """库里已有这张 CA 且带信任标志 → 不该再要求用户做一遍。"""
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
    """已经信任时执行安装动作：不得报错，也不得动用户已有的条目。"""
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
        "用户已有的条目不能被删掉或改名 —— 工具不该往用户库里塞第二个昵称"
    )


def test_apply_repairs_missing_trust_flag(tmp_path):
    """库里有这张 CA 但缺 SSL 信任标志 → 装完必须有信任标志。"""
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
    """空库 → 正常装上，昵称就是工具自己的那个。"""
    ca = _make_ca(tmp_path)
    home = tmp_path / "home"
    db = _new_db(home / ".pki" / "nssdb")

    comp, _platform, _paths, _cfg = _component(tmp_path, home, db)
    comp.ca_path.parent.mkdir(parents=True, exist_ok=True)
    comp.ca_path.write_text(ca.read_text(encoding="utf-8"), encoding="utf-8")

    comp.apply([_install_action(comp.ca_path, db)])
    assert _rows(db) == [("devproxy Local CA", "C,,")]
    assert comp.check().ok is True
