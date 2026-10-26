# devproxy 迁移与跨平台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有的手工环境（Caddy systemd 单元 + 手写 Caddyfile + 手写 dnsmasq 配置）安全迁移到 devproxy 管理之下——**迁移前强制备份所有将被修改的配置文件**——并补齐 macOS 平台支持，最后产出可发布的 README。

**Architecture:** 备份是迁移的强制前置步骤：所有将被改动的文件先复制到 `~/.local/state/devproxy/backups/<UTC时间戳>/`，附 `manifest.json`（机器可读）与 `MANIFEST.txt`（人可读），备份失败则整个迁移中止。迁移本身是"检测遗留 → 备份 → 拆分 Caddyfile → 改写单元 → 验证"，任一步失败都能按备份还原。平台层的 `install_service` 签名在本计划中调整为"由平台自己渲染单元"，这样 systemd 与 launchd 各写各的格式，组件层保持平台无关。

**Tech Stack:** Python ≥ 3.11、Typer ≥ 0.12、Rich ≥ 13、pytest ≥ 8、systemd(user)、launchd(LaunchDaemon/LaunchAgent)、dnsmasq、macOS `security` / `/etc/resolver`

**Spec:** `docs/superpowers/specs/2026-10-22-devproxy-local-dev-env-tool-design.md` —— 重点看 §8（迁移）、§2 D7/D16/D19/D20、§10.3（真机验证清单）

**前置条件：** Plan 1 与 Plan 2 已完成并通过测试。

---

## Global Constraints

沿用 Plan 1、Plan 2 的全部约束，并追加：

- **迁移前必须备份**每一个将被修改的既有文件；备份目录 `~/.local/state/devproxy/backups/<UTC时间戳>/`，带 `manifest.json` + `MANIFEST.txt`；备份失败 → 中止迁移，不做任何修改
- **备份只增不删**，由用户自行清理
- **平台层负责渲染服务单元**（systemd unit / launchd plist），组件层只给 `exec_start` 与 `env`
- **macOS 默认 DNS 端口 5300**（避开 mDNSResponder 占用的 5353）
- **macOS 的 Caddy 服务是 system scope**（LaunchDaemon，root）——macOS 无 setcap 等价物，80/443 只能靠 root 守护进程
- **macOS 的 dnsmasq 服务是 user scope**（LaunchAgent），端口 5300，DNS 路由写 `/etc/resolver/<suffix>`
- **macOS 证书信任只做系统钥匙串一处**（Safari/Chrome 都读它），不做 NSS
- **Caddy 二进制在两个平台都装到 `~/.local/bin/caddy`**（绝对路径写进 plist，root 可读）
- **不改用户的 `sites.caddy` 内容**——工具只 import 它

---

## File Structure

```
src/devproxy/
├── backup.py            # BackupManager / Backup：备份与还原
├── migrate.py           # 遗留检测、Caddyfile 拆分、迁移与回滚
├── platform/
│   ├── base.py          # （改）Platform.install_service 签名调整
│   ├── linux.py         # （改）渲染 systemd unit
│   └── macos.py         # 新增：渲染 launchd plist、/etc/resolver、security 信任
├── components/
│   ├── caddy.py         # （改）不再自己渲染 unit，改为传 exec_start
│   ├── certs.py         # （改）按平台选择信任方式
│   └── dns.py           # （改）按平台选择后端与写法
tests/
├── test_backup.py
├── test_migrate.py
├── test_platform_macos.py
└── test_component_certs_platform.py
README.md                # （重写）
Justfile                 # （瘦身为转发壳）
```

---

### Task 14: 备份机制

**Files:**
- Create: `src/devproxy/backup.py`
- Create: `tests/test_backup.py`

**Interfaces:**
- Consumes: `devproxy.paths.Paths`
- Produces:
  - `devproxy.backup.BackupEntry` — dataclass，字段 `original: Path`、`backup: Path`（备份目录内的相对路径）、`existed: bool`、`sha256: str = ""`
  - `devproxy.backup.Backup` — dataclass，字段 `directory: Path`、`created_at: str`、`entries: list[BackupEntry]`；方法 `manifest_text() -> str`、`restore() -> list[Path]`（返回被还原/删除的原始路径）
  - `devproxy.backup.BackupManager` — 构造 `(base_dir: Path)`；方法 `create(targets: list[Path]) -> Backup`（备份不存在文件时记 `existed=False`）
  - `devproxy.backup.load_backup(directory: Path) -> Backup` — 从 `manifest.json` 读回

- [ ] **Step 1: 写失败的测试**

`tests/test_backup.py`：

```python
import hashlib
import json
from pathlib import Path

import pytest

from devproxy.backup import BackupManager, load_backup


def test_create_backs_up_existing_files(tmp_path):
    src = tmp_path / "unit.service"
    src.write_text("hello")
    b = BackupManager(tmp_path / "backups").create([src])
    assert len(b.entries) == 1
    e = b.entries[0]
    assert e.existed is True
    assert e.original == src
    assert (b.directory / e.backup).read_text() == "hello"
    assert e.sha256 == hashlib.sha256(b"hello").hexdigest()


def test_create_records_missing_file_as_not_existed(tmp_path):
    missing = tmp_path / "absent.conf"
    b = BackupManager(tmp_path / "backups").create([missing])
    assert b.entries[0].existed is False
    assert not (b.directory / b.entries[0].backup).exists()


def test_backup_directory_is_timestamped(tmp_path):
    b = BackupManager(tmp_path / "backups").create([tmp_path / "a"])
    assert b.directory.name.endswith("Z") or "T" in b.directory.name
    assert b.directory.parent == tmp_path / "backups"


def test_manifest_json_is_written(tmp_path):
    src = tmp_path / "f"
    src.write_text("x")
    b = BackupManager(tmp_path / "backups").create([src])
    manifest = json.loads((b.directory / "manifest.json").read_text())
    assert manifest["entries"][0]["original"] == str(src)
    assert manifest["entries"][0]["existed"] is True


def test_manifest_txt_is_human_readable(tmp_path):
    src = tmp_path / "caddy.service"
    src.write_text("x")
    b = BackupManager(tmp_path / "backups").create([src])
    text = (b.directory / "MANIFEST.txt").read_text()
    assert str(src) in text
    assert "caddy.service" in text


def test_two_backups_do_not_overwrite_each_other(tmp_path):
    src = tmp_path / "f"
    src.write_text("v1")
    m = BackupManager(tmp_path / "backups")
    b1 = m.create([src])
    src.write_text("v2")
    b2 = m.create([src])
    assert b1.directory != b2.directory
    assert (b1.directory / b1.entries[0].backup).read_text() == "v1"
    assert (b2.directory / b2.entries[0].backup).read_text() == "v2"


def test_restore_puts_files_back(tmp_path):
    src = tmp_path / "f"
    src.write_text("original")
    b = BackupManager(tmp_path / "backups").create([src])
    src.write_text("modified")
    b.restore()
    assert src.read_text() == "original"


def test_restore_removes_files_that_did_not_exist(tmp_path):
    target = tmp_path / "created-by-tool"
    b = BackupManager(tmp_path / "backups").create([target])
    target.write_text("new")
    b.restore()
    assert not target.exists(), "原本不存在的文件在还原时应被删除"


def test_load_backup_roundtrip(tmp_path):
    src = tmp_path / "f"
    src.write_text("x")
    b = BackupManager(tmp_path / "backups").create([src])
    loaded = load_backup(b.directory)
    assert loaded.entries[0].original == b.entries[0].original
    assert loaded.entries[0].sha256 == b.entries[0].sha256


def test_basename_collision_is_resolved(tmp_path):
    a = tmp_path / "a" / "conf"
    b = tmp_path / "b" / "conf"
    for p in (a, b):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(p.name)
    m = BackupManager(tmp_path / "backups")
    backup = m.create([a, b])
    names = {e.backup for e in backup.entries}
    assert len(names) == 2, "同名文件的备份名不能冲突"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_backup.py -v
```
Expected: 收集阶段报错 —— `ModuleNotFoundError: No module named 'devproxy.backup'`

- [ ] **Step 3: 实现**

`src/devproxy/backup.py`：

```python
"""迁移前的强制备份与回滚。

备份目录带 UTC 时间戳，多次迁移不互相覆盖；只增不删，由用户自行清理。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class BackupEntry:
    original: Path
    backup: str  # 相对备份目录的文件名
    existed: bool
    sha256: str = ""


@dataclass
class Backup:
    directory: Path
    created_at: str
    entries: list[BackupEntry] = field(default_factory=list)

    def manifest_text(self) -> str:
        lines = [
            "# devproxy 备份清单",
            f"# 备份时间: {self.created_at}",
            "# 还原方式: devproxy setup --rollback " + str(self.directory),
            "",
        ]
        for i, e in enumerate(self.entries, 1):
            state = "存在" if e.existed else "原本不存在（还原时将删除）"
            lines.append(f"[{i}] {e.original}")
            lines.append(f"    备份文件: {e.backup}")
            lines.append(f"    原始状态: {state}")
            if e.sha256:
                lines.append(f"    sha256: {e.sha256}")
            lines.append("")
        return "\n".join(lines)

    def restore(self) -> list[Path]:
        """还原所有条目。返回被还原或被删除的原始路径。"""
        touched: list[Path] = []
        for e in self.entries:
            src = self.directory / e.backup
            if e.existed:
                e.original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, e.original)
            else:
                e.original.unlink(missing_ok=True)
            touched.append(e.original)
        return touched


class BackupManager:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    def create(self, targets: list[Path]) -> Backup:
        """备份目标文件。目录创建或复制失败一律抛异常 —— 调用方必须中止迁移。"""
        stamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
        directory = self.base_dir / stamp
        n = 0
        while directory.exists():
            n += 1
            directory = self.base_dir / f"{stamp}-{n}"
        directory.mkdir(parents=True)

        entries: list[BackupEntry] = []
        used: set[str] = set()
        for target in targets:
            name = target.name or "root"
            backup_name = f"{name}.bak"
            i = 1
            while backup_name in used:
                i += 1
                backup_name = f"{name}.{i}.bak"
            used.add(backup_name)
            if target.exists() and target.is_file():
                shutil.copy2(target, directory / backup_name)
                entries.append(
                    BackupEntry(
                        original=target,
                        backup=backup_name,
                        existed=True,
                        sha256=_sha256(target),
                    )
                )
            else:
                entries.append(
                    BackupEntry(original=target, backup=backup_name, existed=False)
                )

        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        backup = Backup(directory=directory, created_at=created_at, entries=entries)
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "created_at": created_at,
                    "entries": [
                        {
                            "original": str(e.original),
                            "backup": e.backup,
                            "existed": e.existed,
                            "sha256": e.sha256,
                        }
                        for e in entries
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (directory / "MANIFEST.txt").write_text(backup.manifest_text(), encoding="utf-8")
        return backup


def load_backup(directory: Path) -> Backup:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    return Backup(
        directory=directory,
        created_at=manifest.get("created_at", ""),
        entries=[
            BackupEntry(
                original=Path(e["original"]),
                backup=e["backup"],
                existed=bool(e["existed"]),
                sha256=e.get("sha256", ""),
            )
            for e in manifest.get("entries", [])
        ],
    )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_backup.py -v
```
Expected: PASS，10 passed

- [ ] **Step 5: 提交**

```bash
git add src/devproxy/backup.py tests/test_backup.py
git commit -m "✨ feat(devproxy): 迁移前的强制备份与回滚机制"
```

---

### Task 15: 迁移流程

**Files:**
- Create: `src/devproxy/migrate.py`
- Create: `tests/test_migrate.py`
- Modify: `src/devproxy/platform/base.py`（`install_service` 签名调整）
- Modify: `src/devproxy/platform/linux.py`（渲染 systemd unit）
- Modify: `src/devproxy/platform/fake.py`（跟随新签名）
- Modify: `src/devproxy/components/caddy.py`（不再自己渲染 unit）
- Modify: `src/devproxy/cli.py`（`setup` 集成迁移 + `--rollback`）

**Interfaces:**
- Consumes: Task 14 的 `BackupManager` / `Backup`、Task 8 的 `Platform`、Plan 1 的 `Paths` / `Config`、Plan 2 的 `CaddyComponent`
- Produces:
  - `devproxy.migrate.split_caddyfile(text: str) -> tuple[str, list[str]]` — 返回 `(个人站点文本, 被丢弃的 .test 相关块列表)`
  - `devproxy.migrate.unit_config_path(unit_text: str, home: Path) -> Path | None` — 从 `ExecStart=` 解析 `--config` 路径（展开 `%h`）
  - `devproxy.migrate.unit_env(unit_text: str) -> dict[str, str]` — 提取 `Environment=` 键值
  - `devproxy.migrate.detect_legacy(platform, paths: Paths) -> LegacyState | None`
  - `devproxy.migrate.LegacyState` — dataclass，字段 `unit_path: Path`、`config_path: Path`、`env: dict[str, str]`
  - `devproxy.migrate.migrate(platform, paths: Paths, config: Config, state: LegacyState) -> Backup` — 执行迁移，返回备份供回滚
  - `devproxy.migrate.rollback(backup: Backup) -> list[Path]`
  - `devproxy.platform.base.Platform.install_service` 新签名：`install_service(name: str, *, exec_start: list[str], env: dict[str, str], description: str = "") -> None`

- [ ] **Step 1: 写失败的测试**

`tests/test_migrate.py`：

```python
from pathlib import Path

import pytest

from devproxy.config import Config
from devproxy.migrate import (
    detect_legacy,
    migrate,
    rollback,
    split_caddyfile,
    unit_config_path,
    unit_env,
)
from devproxy.paths import Paths
from devproxy.platform.fake import FakePlatform


def _paths(tmp_path):
    return Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")


def test_split_keeps_personal_sites_and_drops_test_blocks():
    legacy = (
        "{\n"
        "\tstorage file_system /data\n"
        "}\n"
        "\n"
        "http://localhost {\n"
        "\troot * /site\n"
        "\tfile_server\n"
        "}\n"
        "\n"
        "*.local.yikuaigao.com {\n"
        "\ttls internal\n"
        "\treverse_proxy 1.2.3.4:30800\n"
        "}\n"
        "\n"
        "*.test {\n"
        "\ttls {\n"
        "\t\ton_demand\n"
        "\t\tissuer internal\n"
        "\t}\n"
        "\trespond \"nope\" 404\n"
        "}\n"
        "\n"
        "http://*.test {\n"
        "\tredir https://{host}{uri} permanent\n"
        "}\n"
    )
    personal, dropped = split_caddyfile(legacy)
    assert "http://localhost" in personal
    assert "*.local.yikuaigao.com" in personal
    assert "*.test" not in personal
    assert len(dropped) == 2
    assert all(".test" in d for d in dropped)


def test_split_preserves_nested_braces():
    legacy = "*.test {\n\ttls {\n\t\ton_demand\n\t}\n}\n\nlocalhost {\n\ttls internal\n}\n"
    personal, dropped = split_caddyfile(legacy)
    assert "localhost" in personal
    assert len(dropped) == 1


def test_unit_config_path_parsed():
    unit = "[Service]\nExecStart=%h/.local/bin/caddy run --config %h/work/x/Caddyfile\n"
    p = unit_config_path(unit, Path("/home/u"))
    assert p == Path("/home/u/work/x/Caddyfile")


def test_unit_config_path_none_when_absent():
    assert unit_config_path("[Service]\nExecStart=/usr/bin/foo\n", Path("/home/u")) is None


def test_unit_env_extracted():
    unit = "[Service]\nEnvironment=MINIKUBE_IP=192.168.49.2\nEnvironment=FOO=bar\n"
    assert unit_env(unit) == {"MINIKUBE_IP": "192.168.49.2", "FOO": "bar"}


def test_detect_legacy_returns_none_without_unit(tmp_path):
    paths = _paths(tmp_path)
    platform = FakePlatform()
    assert detect_legacy(platform, paths) is None


def test_detect_legacy_finds_existing_unit(tmp_path):
    paths = _paths(tmp_path)
    platform = FakePlatform()
    unit_path = tmp_path / "caddy.service"
    unit_path.write_text(
        "[Service]\nExecStart=%h/.local/bin/caddy run --config %h/old/Caddyfile\n"
        "Environment=MINIKUBE_IP=1.2.3.4\n"
    )
    state = detect_legacy(platform, paths, unit_dir=tmp_path)
    assert state is not None
    assert state.unit_path == unit_path
    assert state.config_path.name == "Caddyfile"
    assert state.env == {"MINIKUBE_IP": "1.2.3.4"}


def test_migrate_backs_up_before_writing(tmp_path):
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    unit_path = tmp_path / "caddy.service"
    unit_path.write_text("[Service]\nExecStart=caddy run --config /old/Caddyfile\n")
    old_cfg = tmp_path / "old" / "Caddyfile"
    old_cfg.parent.mkdir(parents=True)
    old_cfg.write_text("localhost {\n\ttls internal\n}\n")

    from devproxy.migrate import LegacyState

    state = LegacyState(
        unit_path=unit_path, config_path=old_cfg, env={"MINIKUBE_IP": "1.2.3.4"}
    )
    backup = migrate(platform, paths, Config(), state)
    assert backup.entries, "迁移必须产生备份"
    assert (backup.directory / "MANIFEST.txt").exists()
    assert (backup.directory / "manifest.json").exists()
    # 原文件仍在备份里
    assert any(e.original == unit_path and e.existed for e in backup.entries)


def test_migrate_writes_sites_caddy_from_legacy(tmp_path):
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    old_cfg = tmp_path / "old" / "Caddyfile"
    old_cfg.parent.mkdir(parents=True)
    old_cfg.write_text("localhost {\n\ttls internal\n}\n\n*.test {\n\trespond 404\n}\n")
    from devproxy.migrate import LegacyState

    state = LegacyState(unit_path=tmp_path / "caddy.service", config_path=old_cfg, env={})
    migrate(platform, paths, Config(), state)
    assert "localhost" in paths.sites_caddy.read_text()
    assert "*.test" not in paths.sites_caddy.read_text()


def test_migrate_preserves_unit_env(tmp_path):
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    old_cfg = tmp_path / "old" / "Caddyfile"
    old_cfg.parent.mkdir(parents=True)
    old_cfg.write_text("localhost {\n}\n")
    from devproxy.migrate import LegacyState

    state = LegacyState(
        unit_path=tmp_path / "caddy.service", config_path=old_cfg, env={"MINIKUBE_IP": "9.9.9.9"}
    )
    migrate(platform, paths, Config(), state)
    unit = platform.services["caddy"]
    assert "MINIKUBE_IP=9.9.9.9" in unit


def test_rollback_restores_unit_and_caddyfile(tmp_path):
    paths = _paths(tmp_path)
    paths.ensure_files()
    platform = FakePlatform()
    unit_path = tmp_path / "caddy.service"
    unit_path.write_text("ORIGINAL UNIT\n")
    old_cfg = tmp_path / "old" / "Caddyfile"
    old_cfg.parent.mkdir(parents=True)
    old_cfg.write_text("ORIGINAL CADDYFILE\n")
    from devproxy.migrate import LegacyState

    state = LegacyState(unit_path=unit_path, config_path=old_cfg, env={})
    backup = migrate(platform, paths, Config(), state)
    unit_path.write_text("CLOBBERED\n")
    old_cfg.write_text("CLOBBERED\n")
    rollback(backup)
    assert unit_path.read_text() == "ORIGINAL UNIT\n"
    assert old_cfg.read_text() == "ORIGINAL CADDYFILE\n"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_migrate.py -v
```
Expected: 收集阶段报错 —— `ModuleNotFoundError: No module named 'devproxy.migrate'`

- [ ] **Step 3: 调整平台接口**

`src/devproxy/platform/base.py` 中把 `install_service` 签名改为：

```python
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
        """安装并注册一个服务。单元格式由平台自己决定（systemd / launchd）。"""
```

同时把 `dns_route_installed` 的签名改为带上域名后缀（macOS 的解析器文件按后缀命名，
Linux 的两个后端不关心后缀，但接口保持一致）：

```python
    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        """DNS 路由是否已配置。backend 取 "resolved" / "system" / "resolver"。"""
```

`src/devproxy/platform/linux.py`：

```python
    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        if backend == "resolved":
            return Path("/etc/systemd/resolved.conf.d/devproxy.conf").exists()
        if backend == "system":
            return Path("/etc/dnsmasq.d/devproxy.conf").exists()
        return False
```

`src/devproxy/platform/fake.py`：

```python
    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        return backend in self.dns_backends
```

`src/devproxy/components/dns.py` 的 `check()` 与 `verify()` 相应改为：

```python
    def _backend_key(self) -> str:
        if self.platform.name == "mac":
            return "resolver"
        return "resolved" if self.resolved_backend else "system"

    def check(self) -> Status:
        key = self._backend_key()
        if self.platform.dns_route_installed(key, self.config.domain_suffix):
            return Status(ok=True, detail=f"DNS 路由已配置（{key} 后端）")
        return Status(
            ok=False,
            detail=f"DNS 路由缺失（{key} 后端）：{self.target_path}",
            hint="devproxy setup 会写入（需要 sudo）",
        )
```

`verify()` 同样改用 `_backend_key()`。

`src/devproxy/platform/linux.py` 中把 `install_service` 改为：

```python
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
        path = self.service_unit_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_systemd_unit(name, exec_start, exec_reload, exec_stop, env, description),
            encoding="utf-8",
        )
        _run(["systemctl", "--user", "daemon-reload"])
```

并在同文件新增渲染函数：

```python
def render_systemd_unit(
    name: str,
    exec_start: list[str],
    exec_reload: list[str] | None,
    exec_stop: list[str] | None,
    env: dict[str, str],
    description: str = "",
) -> str:
    """生成 systemd user 单元。形态与现有的 caddy.service 保持一致。"""
    lines = [
        "[Unit]",
        f"Description={description or f'{name} (managed by devproxy)'}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=notify",
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
```

`src/devproxy/platform/fake.py` 中同步更新：

```python
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
        self.services[name] = "\n".join(
            [f"# {name}", f"ExecStart={' '.join(exec_start)}"]
            + [f"Environment={k}={v}" for k, v in env.items()]
        )
```

`src/devproxy/components/caddy.py` 中：

- 删除 `render_unit` 函数
- `apply()` 里 `install-service` 分支改为：

```python
            if action.command[:1] == ["install-service"]:
                self.platform.install_service(
                    "caddy",
                    exec_start=[
                        str(self.config.caddy_bin_path),
                        "run",
                        "--config",
                        str(self.paths.caddyfile),
                    ],
                    exec_reload=[
                        str(self.config.caddy_bin_path),
                        "reload",
                        "--config",
                        str(self.paths.caddyfile),
                        "--address",
                        self.config.admin_addr,
                    ],
                    exec_stop=[str(self.config.caddy_bin_path), "stop"],
                    env=self.extra_env,
                    description="Caddy local HTTP/HTTPS server (managed by devproxy)",
                )
                self.platform.enable_service("caddy")
                continue
```

- `__init__` 增加 `self.extra_env: dict[str, str] = {}`

`src/devproxy/components/dnsmasq.py` 的 `install-service` 分支同样改为新签名：

```python
            if action.command[:1] == ["install-service"]:
                self.platform.install_service(
                    "dnsmasq",
                    exec_start=[
                        "/usr/sbin/dnsmasq",
                        "--conf-dir=%h/.config/dnsmasq",
                        "--pid-file=%h/.cache/dnsmasq.pid",
                    ],
                    env={},
                    description="dnsmasq for devproxy (user scope)",
                )
                self.platform.enable_service("dnsmasq")
                continue
```

- [ ] **Step 4: 实现迁移模块**

`src/devproxy/migrate.py`：

```python
"""遗留环境检测与迁移。

迁移的铁律：先备份，再动手。任何一步失败都能按备份还原。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from devproxy.backup import Backup, BackupManager
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Platform

TEST_BLOCK_MARKERS = ("*.test", "http://*.test", "http://*.local.test")


@dataclass
class LegacyState:
    """检测到的遗留环境。"""

    unit_path: Path
    config_path: Path
    env: dict[str, str] = field(default_factory=dict)


def _split_blocks(text: str) -> tuple[list[str], list[str]]:
    """按大括号深度切分顶层块。返回 (全局块列表, 站点块列表)。"""
    global_blocks: list[str] = []
    site_blocks: list[str] = []
    current: list[str] = []
    depth = 0
    first = ""
    for line in text.splitlines():
        if not current:
            first = line.strip()
        current.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0 and current:
            block = "\n".join(current).rstrip() + "\n"
            current = []
            if first == "{":
                global_blocks.append(block)
            else:
                site_blocks.append(block)
    if current:
        site_blocks.append("\n".join(current).rstrip() + "\n")
    return global_blocks, site_blocks


def split_caddyfile(text: str) -> tuple[str, list[str]]:
    """拆出个人站点块。返回 (个人站点文本, 被丢弃的 .test 相关块)。

    只做块级切分，不解析 Caddyfile 语法 —— 站点块原样保留。
    """
    _, sites = _split_blocks(text)
    personal: list[str] = []
    dropped: list[str] = []
    for block in sites:
        head = block.strip().splitlines()[0].strip()
        if any(head.startswith(m) for m in TEST_BLOCK_MARKERS):
            dropped.append(block)
        else:
            personal.append(block)
    return "\n".join(personal), dropped


def unit_config_path(unit_text: str, home: Path) -> Path | None:
    """从 ExecStart= 里解析 --config 指向的路径（展开 %h）。"""
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line.startswith("ExecStart="):
            continue
        args = line[len("ExecStart="):].split()
        if "--config" in args:
            i = args.index("--config")
            if i + 1 < len(args):
                return Path(args[i + 1].replace("%h", str(home))).expanduser()
    return None


def unit_env(unit_text: str) -> dict[str, str]:
    """提取单元里的 Environment= 键值。"""
    env: dict[str, str] = {}
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        for item in line[len("Environment="):].split():
            if "=" in item:
                k, v = item.split("=", 1)
                env[k] = v
    return env


def detect_legacy(
    platform: Platform, paths: Paths, unit_dir: Path | None = None
) -> LegacyState | None:
    """检测是否存在本工具未管理的 caddy 服务。"""
    unit_path = unit_dir / "caddy.service" if unit_dir else platform.service_unit_path("caddy")
    if not unit_path.exists():
        return None
    unit_text = unit_path.read_text(encoding="utf-8")
    config_path = unit_config_path(unit_text, Path.home())
    if config_path is None:
        return None
    try:
        if config_path.resolve() == paths.caddyfile.resolve():
            return None  # 已经是本工具管理的
    except OSError:
        pass
    return LegacyState(
        unit_path=unit_path, config_path=config_path, env=unit_env(unit_text)
    )


def migrate(
    platform: Platform, paths: Paths, config: Config, state: LegacyState
) -> Backup:
    """执行迁移。返回备份对象，调用方可据此回滚。"""
    targets = [
        state.unit_path,
        state.config_path,
        Path.home() / ".config" / "dnsmasq" / "dnsmasq.conf",
        platform.service_unit_path("dnsmasq"),
    ]
    backup = BackupManager(paths.backup_dir).create(targets)

    # 1) 拆出个人站点，写入 sites.caddy（不覆盖已有内容）
    if state.config_path.exists():
        legacy_text = state.config_path.read_text(encoding="utf-8")
        personal, _ = split_caddyfile(legacy_text)
        if personal.strip() and not paths.sites_caddy.exists():
            paths.sites_caddy.parent.mkdir(parents=True, exist_ok=True)
            paths.sites_caddy.write_text(personal, encoding="utf-8")

    # 2) 改写单元：指向工具的 Caddyfile，保留原有 Environment=
    caddy_bin = config.caddy_bin_path
    platform.install_service(
        "caddy",
        exec_start=[str(caddy_bin), "run", "--config", str(paths.caddyfile)],
        exec_reload=[
            str(caddy_bin),
            "reload",
            "--config",
            str(paths.caddyfile),
            "--address",
            config.admin_addr,
        ],
        exec_stop=[str(caddy_bin), "stop"],
        env=state.env,
        description="Caddy local HTTP/HTTPS server (managed by devproxy)",
    )
    platform.enable_service("caddy")
    return backup


def rollback(backup: Backup) -> list[Path]:
    """按备份还原。"""
    return backup.restore()
```

- [ ] **Step 5: 在 cli.py 集成迁移**

在 `src/devproxy/cli.py` 的 `setup` 命令**最前面**插入迁移检测：

```python
@app.command()
def setup(
    dns_backend: str = typer.Option("auto", "--dns-backend", help="auto|resolved|system"),
    domain_suffix: str = typer.Option(None, "--domain-suffix"),
    no_trust: bool = typer.Option(False, "--no-trust", help="跳过证书信任"),
    yes: bool = typer.Option(False, "--yes", help="非交互模式，不询问"),
    rollback_dir: str = typer.Option(None, "--rollback", help="从指定备份目录回滚"),
) -> None:
    """部署本地开发环境（Caddy + dnsmasq + DNS 路由 + 证书信任 + 自启）。"""
    paths, config, _, _ = build_context()

    if rollback_dir:
        from devproxy.backup import load_backup
        from devproxy.migrate import rollback as do_rollback

        touched = do_rollback(load_backup(Path(rollback_dir)))
        console.print(f"已回滚 {len(touched)} 个文件：")
        for p in touched:
            console.print(f"  · {p}")
        raise typer.Exit(EXIT_OK)

    from devproxy.migrate import detect_legacy, migrate

    state = detect_legacy(_platform(), paths)
    if state is not None:
        console.print("[yellow]检测到遗留的 caddy 服务（非本工具管理）[/yellow]")
        console.print(f"  单元: {state.unit_path}")
        console.print(f"  配置: {state.config_path}")
        console.print(f"  环境变量: {state.env or '无'}")
        console.print("将执行：备份 → 拆出个人站点到 sites.caddy → 改写单元 → 重启")
        if not yes and not sys.stdin.isatty():
            err_console.print("非交互环境且未指定 --yes，拒绝迁移。")
            raise typer.Exit(EXIT_SUDO)
        backup = migrate(_platform(), paths, config, state)
        console.print(f"已备份到: {backup.directory}")
        console.print(f"回滚命令: devproxy setup --rollback {backup.directory}")
```

其余 setup 逻辑不变（plan → 打印表格 → apply → verify）。

- [ ] **Step 6: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_migrate.py -v
```
Expected: PASS，11 passed

- [ ] **Step 7: 跑全量测试并提交**

**先同步 Plan 2 的测试**（`install_service` 与 `dns_route_installed` 的签名变了）：

- `tests/test_platform_linux.py::test_fake_platform_service_lifecycle` —— 改为新签名：
  ```python
  def test_fake_platform_service_lifecycle():
      p = FakePlatform()
      p.install_service("caddy", exec_start=["/bin/caddy", "run"], env={})
      assert p.service_active("caddy") is False
      p.active.add("caddy")
      assert p.service_active("caddy") is True
      assert "caddy.service" in p.service_unit_path("caddy").name
  ```
- `tests/test_component_caddy.py::test_apply_writes_unit_and_enables_service` —— 单元内容改为按片段断言：
  ```python
  def test_apply_writes_unit_and_enables_service(tmp_path):
      comp, paths, cfg = _component(tmp_path)
      platform = FakePlatform()
      comp = CaddyComponent(platform, paths, cfg)
      comp.apply(comp.plan())
      assert "caddy" in platform.services
      assert "run" in platform.services["caddy"]
      assert "caddy" in platform.active
  ```

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest -v
```
Expected: 全部 PASS

```bash
git add src/devproxy/migrate.py src/devproxy/backup.py src/devproxy/platform/ src/devproxy/components/ src/devproxy/cli.py tests/
git commit -m "✨ feat(devproxy): 遗留环境迁移——先备份再改写单元与 Caddyfile"
```

---

### Task 16: macOS 平台实现

**Files:**
- Create: `src/devproxy/platform/macos.py`
- Create: `tests/test_platform_macos.py`
- Modify: `src/devproxy/components/dns.py`（按平台选后端与写法）
- Modify: `src/devproxy/components/certs.py`（按平台选信任方式）
- Modify: `src/devproxy/cli.py`（`_platform()` 按平台分派）

**Interfaces:**
- Consumes: Task 8 的 `Platform` / `ElevatedSession` 协议（`install_service` 为 Task 15 调整后的签名）
- Produces:
  - `devproxy.platform.macos.MacOSPlatform` — 实现 `Platform`；`name = "mac"`、`dns_port = 5300`；构造 `(user_home: Path | None = None)`
  - `devproxy.platform.macos.MacOSElevated` — 实现 `ElevatedSession`
  - `devproxy.platform.macos.render_plist(name, exec_start, env, log_path) -> str`
  - `devproxy.platform.macos.render_resolver_file(port: int) -> str` — `/etc/resolver/<suffix>` 的内容
  - `devproxy.platform.macos.get_platform() -> Platform` — 跨平台分派工厂（Linux 返 LinuxPlatform，Darwin 返 MacOSPlatform）

- [ ] **Step 1: 写失败的测试**

`tests/test_platform_macos.py`：

```python
import plistlib
from pathlib import Path

import pytest

from devproxy.platform.macos import (
    MacOSPlatform,
    render_plist,
    render_resolver_file,
)


def test_dns_port_avoids_mdns():
    assert MacOSPlatform().dns_port == 5300


def test_caddy_service_is_system_scope(tmp_path):
    p = MacOSPlatform(user_home=tmp_path)
    path = p.service_unit_path("caddy")
    assert path.parent == Path("/Library/LaunchDaemons")
    assert path.name.endswith(".plist")


def test_dnsmasq_service_is_user_scope(tmp_path):
    p = MacOSPlatform(user_home=tmp_path)
    path = p.service_unit_path("dnsmasq")
    assert str(path).startswith(str(tmp_path))
    assert "LaunchAgents" in str(path)


def test_render_plist_is_valid_plist():
    text = render_plist(
        "caddy",
        ["/opt/caddy", "run", "--config", "/Users/u/Caddyfile"],
        {"MINIKUBE_IP": "1.2.3.4"},
        Path("/Users/u/.local/state/devproxy/logs/caddy.log"),
    )
    data = plistlib.loads(text.encode())
    assert data["Label"] == "com.devproxy.caddy"
    assert data["ProgramArguments"][0] == "/opt/caddy"
    assert data["EnvironmentVariables"]["MINIKUBE_IP"] == "1.2.3.4"
    assert data["RunAtLoad"] is True
    assert data["StandardOutPath"].endswith("caddy.log")


def test_render_resolver_file_has_port():
    text = render_resolver_file(5300)
    assert "nameserver 127.0.0.1" in text
    assert "port 5300" in text


def test_macos_privilege_session_is_cached(tmp_path):
    p = MacOSPlatform(user_home=tmp_path)
    assert p.privilege() is p.privilege()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_platform_macos.py -v
```
Expected: 收集阶段报错 —— `ModuleNotFoundError: No module named 'devproxy.platform.macos'`

- [ ] **Step 3: 实现**

`src/devproxy/platform/macos.py`：

```python
"""macOS 平台实现：launchd、/etc/resolver、security 钥匙串。

与 Linux 的三处关键差异：
- 无 setcap → 80/443 只能靠 root 的 LaunchDaemon
- 53 被 mDNSResponder 占用 → 用户级 dnsmasq 走 5300
- DNS 路由写 /etc/resolver/<suffix>，不动 /etc/resolv.conf
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from devproxy.platform.base import ElevatedSession, Platform

LAUNCH_AGENTS = "Library/LaunchAgents"
LAUNCH_DAEMONS = "/Library/LaunchDaemons"
SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError:
        return 127, f"命令不存在: {argv[0]}"
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def render_plist(
    name: str,
    exec_start: list[str],
    env: dict[str, str],
    log_path: Path,
) -> str:
    """生成 launchd plist。路径必须是绝对路径 —— LaunchDaemon 以 root 运行，~ 是 root 的家目录。"""
    data = {
        "Label": f"com.devproxy.{name}",
        "ProgramArguments": list(exec_start),
        "EnvironmentVariables": dict(env),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    return plistlib.dumps(data).decode("utf-8")


def render_resolver_file(port: int) -> str:
    """/etc/resolver/<suffix> 的内容。macOS 原生支持每域解析器，不必改 resolv.conf。"""
    return f"nameserver 127.0.0.1\nport {port}\n"


class MacOSElevated:
    """提权会话。构造时执行一次 sudo -v。"""

    def __init__(self) -> None:
        code, msg = _run(["sudo", "-v"])
        if code != 0:
            raise PermissionError(f"提权失败：{msg}")

    def run(self, argv: list[str]) -> tuple[int, str]:
        return _run(["sudo", *argv])

    def write(self, path: Path, content: str) -> None:
        tmp = path.with_name(path.name + ".devproxy.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(content, encoding="utf-8")
        code, msg = self.run(["cp", str(tmp), str(path)])
        tmp.unlink(missing_ok=True)
        if code != 0:
            raise PermissionError(f"写入 {path} 失败：{msg}")

    def read(self, path: Path) -> str:
        code, out = self.run(["cat", str(path)])
        if code != 0:
            raise FileNotFoundError(path)
        return out

    def copy(self, src: Path, dest: Path) -> None:
        code, msg = self.run(["cp", str(src), str(dest)])
        if code != 0:
            raise PermissionError(f"复制 {src} → {dest} 失败：{msg}")


class MacOSPlatform:
    name = "mac"
    dns_port = 5300

    def __init__(self, user_home: Path | None = None) -> None:
        self.user_home = user_home or Path.home()
        self._elevated: MacOSElevated | None = None

    def privilege(self) -> ElevatedSession:
        if self._elevated is None:
            self._elevated = MacOSElevated()
        return self._elevated

    def run(self, argv: list[str]) -> tuple[int, str]:
        return _run(argv)

    def binary_version(self, binary: Path) -> str:
        if not binary.exists():
            return ""
        code, out = _run([str(binary), "version"])
        if code != 0:
            return ""
        return out.split()[0] if out else ""

    def service_unit_path(self, name: str) -> Path:
        # caddy 需要 root 才能绑 80/443 → system scope；其余用 user scope
        if name == "caddy":
            return Path(LAUNCH_DAEMONS) / f"com.devproxy.{name}.plist"
        return self.user_home / LAUNCH_AGENTS / f"com.devproxy.{name}.plist"

    def _is_system_scope(self, name: str) -> bool:
        return name == "caddy"

    def service_active(self, name: str) -> bool:
        if self._is_system_scope(name):
            code, _ = _run(["launchctl", "print", f"system/com.devproxy.{name}"])
        else:
            code, _ = _run(["launchctl", "print", f"gui/{_uid()}/com.devproxy.{name}"])
        return code == 0

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
        path = self.service_unit_path(name)
        log_path = self.user_home / ".local" / "state" / "devproxy" / "logs" / f"{name}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_plist(name, exec_start, env, log_path), encoding="utf-8"
        )
        if self._is_system_scope(name):
            self.privilege().run(["chown", "root:wheel", str(path)])
            self.privilege().run(["chmod", "644", str(path)])
            _run(["launchctl", "bootout", f"system/com.devproxy.{name}"])
            _run(["launchctl", "bootstrap", "system", str(path)])
        else:
            _run(["launchctl", "bootout", f"gui/{_uid()}/com.devproxy.{name}"])
            _run(["launchctl", "bootstrap", f"gui/{_uid()}", str(path)])

    def enable_service(self, name: str) -> None:
        """launchd 靠 RunAtLoad=True 自动拉起，无需额外 enable。

        刻意不做任何写操作 —— 重装 plist 会把 ProgramArguments 清空。
        """
        return

    def restart_service(self, name: str) -> None:
        if self._is_system_scope(name):
            self.privilege().run(["launchctl", "kickstart", "-k", f"system/com.devproxy.{name}"])
        else:
            _run(["launchctl", "kickstart", "-k", f"gui/{_uid()}/com.devproxy.{name}"])

    def tail_service_log(self, name: str) -> None:
        log = self.user_home / ".local" / "state" / "devproxy" / "logs" / f"{name}.log"
        _run(["tail", "-f", str(log)])

    def ca_trusted(self, ca: Path) -> bool:
        code, out = _run(
            ["security", "find-certificate", "-c", "devproxy Local CA",
             "-a", SYSTEM_KEYCHAIN]
        )
        return code == 0 and "devproxy" in out.lower()

    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        if backend == "resolver":
            return (Path("/etc/resolver") / suffix.lstrip(".")).exists()
        return False


def _uid() -> int:
    import os

    return os.getuid()


def get_platform() -> Platform:
    """按当前系统返回对应平台实现。"""
    import sys

    if sys.platform == "darwin":
        return MacOSPlatform()
    from devproxy.platform.linux import LinuxPlatform

    return LinuxPlatform()
```

- [ ] **Step 4: 按平台调整 Cert 与 DNS 组件**

`src/devproxy/components/certs.py`：

- `plan()` 中，Linux 平台返回两条动作（系统库 + NSS），macOS 只返回一条（系统钥匙串）：

```python
    def plan(self) -> list[Action]:
        if self.platform.ca_trusted(self.ca_path):
            return []
        if self.platform.name == "mac":
            return [
                Action(
                    description="把 CA 根证书加入系统钥匙串（Safari/Chrome 都会信任）",
                    kind="run",
                    command=["install-macos-ca", str(self.ca_path)],
                    privileged=True,
                )
            ]
        return [
            Action(
                description="安装 CA 根证书到系统信任库并 update-ca-certificates",
                kind="run",
                command=["install-system-ca", str(self.ca_path), str(SYSTEM_CA_DEST)],
                privileged=True,
            ),
            Action(
                description="安装 CA 根证书到 NSS 库（Chrome/Chromium）",
                kind="run",
                command=["install-nss-ca", str(self.ca_path), str(NSS_DIR)],
            ),
        ]
```

- `apply()` 增加 `install-macos-ca` 分支：

```python
            if action.command[:1] == ["install-macos-ca"]:
                src = Path(action.command[1])
                self.platform.privilege().run(
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
                continue
```

并新增常量 `SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"`。

`src/devproxy/components/dns.py`：

- `resolved_backend` 在 macOS 上恒为 False：

```python
    @property
    def resolved_backend(self) -> bool:
        if self.platform.name == "mac":
            return False
        if self.backend == "system":
            return False
        if self.backend == "resolved":
            return True
        return Path("/etc/systemd/resolved.conf.d").is_dir()
```

- `target_path` 在 macOS 上为 `/etc/resolver/<suffix>`：

```python
    @property
    def target_path(self) -> Path:
        if self.platform.name == "mac":
            return Path("/etc/resolver") / self.config.domain_suffix.lstrip(".")
        return RESOLVED_CONF if self.resolved_backend else SYSTEM_DNSMASQ_CONF
```

- `plan()` 的 macOS 分支：

```python
        if self.platform.name == "mac":
            from devproxy.platform.macos import render_resolver_file

            content = render_resolver_file(self.platform.dns_port)
            desc = "写入 /etc/resolver/%s（macOS 每域解析器）" % self.config.domain_suffix.lstrip(".")
            return [Action(description=desc, kind="write", path=self.target_path, content=content, privileged=True)]
```

- `apply()` 的 macOS 分支跳过 `systemctl restart systemd-resolved`（plan 里已不含该动作）。

- [ ] **Step 5: 在 cli.py 里按平台分派**

`src/devproxy/cli.py`：

```python
def _platform():
    from devproxy.platform.macos import get_platform

    return get_platform()
```

- [ ] **Step 6: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_platform_macos.py -v
```
Expected: PASS，6 passed

- [ ] **Step 7: 跑全量测试并提交**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest -v
```
Expected: 全部 PASS

```bash
git add src/devproxy/platform/macos.py src/devproxy/components/ src/devproxy/cli.py tests/
git commit -m "✨ feat(devproxy): macOS 平台实现——launchd / /etc/resolver / 系统钥匙串"
```

---

### Task 17: 文档、发布与真机验证清单

**Files:**
- Modify: `README.md`（重写）
- Modify: `Justfile`（瘦身为转发壳）
- Modify: `pyproject.toml`（补发布元数据）
- Create: `docs/verification-checklist.md`（真机验证清单）

**Interfaces:**
- Consumes: Plan 1/2/3 的全部子命令
- Produces: 无代码接口

- [ ] **Step 1: 重写 README.md**

```markdown
# devproxy

本地开发环境一键部署与域名网关。把本地服务用 `https://<name>.test` 暴露出来，
开发者只记域名，不记端口。

## 安装

```bash
uv tool install devproxy
```

本地开发：

```bash
uv tool install -e .
```

## 快速开始

```bash
devproxy setup    # 部署 Caddy + dnsmasq + DNS 路由 + 证书信任 + 开机自启
devproxy doctor   # 体检：缺什么给什么修复命令
```

setup 之后：

```bash
devproxy up codeup -- sh -c 'npx vite --port $PORT'   # → https://codeup.test
devproxy up api -- sh -c 'uv run uvicorn app:app --port $PORT'
devproxy expose legacy --port 3000                    # 给已在跑的服务开域名
devproxy ls                                           # 路由清单
devproxy down codeup                                  # 注销
devproxy logs caddy                                   # 跟踪日志
```

## 命令

| 命令 | 说明 |
|---|---|
| `devproxy setup` | 部署本地开发环境（幂等，可重复执行） |
| `devproxy doctor` | 体检所有前置条件 |
| `devproxy up <name> -- CMD...` | 分配端口、启动命令、注册 `https://<name>.test` |
| `devproxy expose <name> --port N` | 给已在运行的服务开域名 |
| `devproxy down <name>` | 注销路由 |
| `devproxy ls` | 列出路由 |
| `devproxy logs [caddy\|dnsmasq]` | 跟踪服务日志 |
| `devproxy trust` | 单独安装 CA 根证书 |
| `devproxy config` | 显示生效配置与渲染出的路由片段 |
| `devproxy selftest` | 隔离 Caddy 实例端到端自检 |

## 应用接入

原生读 `PORT` 环境变量的应用（Next.js / CRA）直接写命令：

```bash
devproxy up next-app -- npx next dev
```

需要显式端口参数的应用（Vite / uvicorn / gunicorn）用 `sh -c` 包一层，
让 shell 在 devproxy 注入 `PORT` 后再展开：

```bash
devproxy up codeup-web -- sh -c 'npx vite --port $PORT --host 127.0.0.1'
devproxy up codeup-api -- sh -c 'uv run uvicorn app:app --host 127.0.0.1 --port $PORT'
```

**`--` 是必须的**：`--` 之后的一切原样交给子进程，devproxy 不会解析它。

Host 校验放行：请求的 Host 头是 `<name>.test`，Vite 需 `server.allowedHosts`、
Django 需 `ALLOWED_HOSTS` 加上该域名。

## 域名解析

| 平台 | 机制 |
|---|---|
| Linux | 用户级 dnsmasq 监听 `127.0.0.1:5353` + systemd-resolved 路由 `~.test` |
| macOS | 用户级 dnsmasq 监听 `127.0.0.1:5300` + `/etc/resolver/test` |

`devproxy setup` 会自动检测并配置，必要时提示 sudo。

## 证书

Caddy 的内部 CA 逐域名签发。**只有已注册的域名能拿到证书**——未注册的域名
TLS 握手就会失败，不存在"本机任意进程都能拿证书"的问题。

信任需要装入不同的库：

| 平台 | 装入位置 |
|---|---|
| Linux | 系统信任库（curl / Go / Java）+ NSS 库（**Chrome/Chromium 只认 NSS**） |
| macOS | 系统钥匙串（Safari/Chrome 都读） |

Linux 上装入 NSS 库后需**完全退出并重启 Chrome**（关窗口不算，要从托盘退出进程）。

## 配置

`~/.config/devproxy/config.toml`（全部可选，不写就用默认值）：

```toml
domain_suffix = "test"
dns_backend = "auto"        # auto | resolved | system
admin_addr = "127.0.0.1:2019"
storage = "~/.local/share/caddy"
ready_timeout = 60
```

改 `domain_suffix` 不会自动重写已注册路由的域名（避免正在跑的服务突然失联）。
旧路由需要 `devproxy down` 后重新 `up`。

## 目录

```
~/.config/devproxy/     配置、主 Caddyfile、你的个人站点 sites.caddy
~/.local/state/devproxy/ 路由表、锁、备份、日志
~/.local/share/caddy/   Caddy 存储（CA 根证书与已签发证书）
```

`~/.config/devproxy/sites.caddy` 是你自己的：devproxy 只 import 它，不改它。

## 迁移

从旧的手工环境迁移时，`devproxy setup` 会检测到遗留的 caddy 服务，
**先把所有将被改动的文件备份到 `~/.local/state/devproxy/backups/<时间戳>/`**
（附 `manifest.json` 与 `MANIFEST.txt`），再拆出个人站点、改写服务单元。

回滚：

```bash
devproxy setup --rollback ~/.local/state/devproxy/backups/2026-10-22T12-00-00Z
```

## 平台支持

- Linux（systemd user scope、setcap、systemd-resolved）
- macOS（launchd、/etc/resolver、系统钥匙串）

真机验证清单见 [docs/verification-checklist.md](docs/verification-checklist.md)。
```

- [ ] **Step 2: 瘦身为转发壳的 Justfile**

```make
# 已废弃：请直接使用 devproxy
# 保留本文件只为兼容旧的 just 习惯，实际逻辑全部在 devproxy 里。

default:
    @devproxy --help

setup:
    devproxy setup

doctor:
    devproxy doctor

logs:
    devproxy logs

status:
    devproxy doctor

install-devp:
    uv tool install -e .
```

- [ ] **Step 3: 补发布元数据**

`pyproject.toml` 的 `[project]` 段补充：

```toml
license = "MIT"
authors = [{ name = "Ye Wenbin" }]
keywords = ["caddy", "dnsmasq", "localhost", "https", "reverse-proxy", "dev-server"]
classifiers = [
    "Environment :: Console",
    "Intended Audience :: Developers",
    "Operating System :: MacOS :: MacOS X",
    "Operating System :: POSIX :: Linux",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Topic :: Software Development :: Testing",
]
```

- [ ] **Step 4: 写真机验证清单**

`docs/verification-checklist.md`：

```markdown
# devproxy 真机验证清单

自动化测试覆盖不到的、必须在真实机器上跑一遍的项。发版前逐条打勾。

## Linux

- [ ] `devproxy setup` 在干净用户下跑通，`https://localhost` 可访问
- [ ] `devproxy setup` 跑第二次显示「所有组件已就绪」（幂等）
- [ ] `devproxy setup` 全程只提示一次密码
- [ ] `curl https://<name>.test` 证书校验通过
- [ ] Chrome 访问 `https://<name>.test` 无证书告警（NSS 库已装）
- [ ] `devproxy up` 起的进程被 `kill -9` 后，`devproxy down <name>` 能清掉残留路由与进程
- [ ] `devproxy setup --rollback <备份目录>` 能把旧配置还原回来

## macOS（**未在本机验证过，需在真实 Mac 上跑**）

- [ ] `/etc/resolver/test` 里的 `port 5300` 是否被系统正确解析
- [ ] mDNSResponder 是否占用 `127.0.0.1:5353`（决定默认端口是否要改）
- [ ] `security add-trusted-cert -d -r trustRoot` 是否需要交互式授权
- [ ] LaunchDaemon（system scope）能否成功绑定 80/443
- [ ] `launchctl print system/com.devproxy.caddy` 显示服务已加载
```

- [ ] **Step 5: 跑全量测试并提交**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest -v
```
Expected: 全部 PASS

```bash
git add README.md Justfile pyproject.toml docs/verification-checklist.md
git commit -m "📝 docs(devproxy): 重写 README、瘦身 Justfile、补真机验证清单"
```

---

## Plan 3 完成标准

- [ ] `python3 -m pytest` 全绿
- [ ] 在本机跑一次完整迁移演练：备份生成、迁移成功、`--rollback` 还原成功
- [ ] `README.md` 的每条命令都能照着跑通
- [ ] `docs/verification-checklist.md` 逐条可执行
- [ ] `uv tool install -e .` 后 `devproxy --help` 正常

## 全部三个计划完成后的收尾

- [x] **迁移旧测试资产**：`tests/test_devp.py`（967 行）逐条对照下表确认新测试已覆盖，确认后删除旧文件
      —— 已完成（2026-10-23）：旧文件与其覆盖的 68 个用例已删除，删除前的单元套件为
      609 + 18 subtests，删除后为 541 —— 18 个 subtests 全部写在该文件里，随它一并消失。

| 旧用例（`tests/test_devp.py`） | 新归属 |
|---|---|
| `TestValidateName`（合法名、非法名、尾随换行） | Plan 1 Task 2 `tests/test_names.py` |
| `TestAllocPort` | Plan 1 Task 5 `test_alloc_port_returns_bindable_port` |
| `TestHeartbeatEnv`（非法心跳值回落） | 不再需要 —— 心跳周期改为类属性 `heartbeat_interval`，不读环境变量 |
| `TestApi`（GET/PATCH/DELETE/连接拒绝） | Plan 1 Task 3 `test_api_*` |
| `TestBuildRoute` / `TestInsertRoute` / `TestIsWildcard` | Plan 1 Task 2 `tests/test_render.py`（内存 route 对象 → 渲染文本，语义已变） |
| `TestLocked`（flock、相对路径） | Plan 1 Task 4 `test_lock_file_created` + Plan 1 Task 4 `file_lock` |
| `TestRegisterLifecycle`（幂等、重试、stop 置位、退避间隔） | Plan 1 Task 4 `test_mutate_*`（事务语义替代逐条 API 调用） |
| 进程组信号转发 / 退出码 / 清理 | Plan 1 Task 5 `test_stop_process_group_kills_grandchildren`、`test_run_*` |
| `devp selftest` 端到端 | Plan 1 Task 7 `tests/test_integration.py` + `devproxy selftest` |

- [x] 删除旧的 `devp` 单文件脚本与 `tests/test_devp.py`（逻辑已并入 `src/devproxy/`）
      —— 已完成（2026-10-23）：删除脚本与旧测试，并新增 `docs/legacy-devp.md`
      记录它的来龙去脉，避免"无声消失"。机器上 `~/.local/bin/devp` 的清理
      不在仓库范围内，由机器主人处理。
- [ ] 旧 `Caddyfile` 保留在仓库作历史参考，文件头注明「已迁移，实际生效的是 `~/.config/devproxy/Caddyfile`」
- [ ] `git tag v0.1.0`
