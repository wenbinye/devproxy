# devproxy 基础设施自举 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `devproxy setup` 一条命令把本地开发环境拉起来——装 Caddy、部署用户级 dnsmasq、配 DNS 路由、装 CA 根证书信任、注册 systemd 自启——并且 `devproxy doctor` 能体检出任何缺失。

**Architecture:** 平台层暴露"能力"（提权会话、服务管理、证书信任、DNS 路由），组件层用这些能力拼出五个组件（Caddy / Dnsmasq / Dns / Cert / Routes）。每个组件实现 `check() / plan() / apply()` 三段式：`check()` 永不抛异常（doctor 靠它一次列全问题），`plan()` 算出待执行动作（setup 靠它生成计划表），`apply()` 执行。提权由平台层缓存的 `ElevatedSession` 承载——一次 `sudo -v` 预认证，后续所有特权命令复用时间戳，用户只被问一次密码。

**Tech Stack:** Python ≥ 3.11、Typer ≥ 0.12、Rich ≥ 13、pytest ≥ 8、Caddy 2.11.7、dnsmasq、systemd（user scope）

**Spec:** `docs/design/specs/2026-10-22-devproxy-local-dev-env-tool-design.md` —— 重点看 §6（setup 流程）、§2 D1/D2/D12/D13/D16/D19、§9（错误处理）

**前置条件：** Plan 1（`docs/design/plans/2026-10-22-devproxy-core-gateway.md`）已完成并通过测试。本计划在其上叠加基础设施层，不改动 Plan 1 的路由模型。

---

## Global Constraints

沿用 Plan 1 的全部约束（Python ≥ 3.11、依赖仅 typer + rich、退出码 0/1/2/3/4、路由名正则、精确站点块 + `tls internal`、原子写、flock），并追加：

- **组件三段式接口固定为** `check() -> Status`、`plan() -> list[Action]`、`apply(actions) -> None`；`check()` 永不抛异常
- **`Status` 三字段**：`ok: bool`、`detail: str`、`hint: str`（hint 是可执行的修复命令）
- **提权只经由 `platform.privilege()`**，组件不得自己拼 `sudo` 命令
- **`ElevatedSession` 由平台缓存**，多次 `privilege()` 只产生一次密码提示
- **Caddy 版本固定 2.11.7**，sha256 用源码内置值（上游 `checksums.txt` 与实际资产不一致，见规格 §2.2）
- **dnsmasq 配置只做增量修改**，保留用户既有的 `address=` 条目与上游设定
- **服务单元沿用 `caddy.service` / `dnsmasq.service` 名字**（user scope）
- **修改任何用户既有配置文件前必须先备份**（备份机制在 Plan 3 实现，本计划只在写自己新建的文件时无需备份）

---

## File Structure

```
src/devproxy/
├── platform/
│   ├── __init__.py
│   ├── base.py         # Status / Action / ElevatedSession / Platform 协议
│   ├── linux.py        # Linux 实现（systemd / setcap / update-ca / NSS / resolved）
│   └── fake.py         # 假平台：测试用，可脚本化预置系统状态
├── components/
│   ├── __init__.py
│   ├── base.py         # Component 协议
│   ├── caddy.py        # Caddy 二进制下载校验、setcap、单元、主 Caddyfile
│   ├── dnsmasq.py      # 用户级 dnsmasq 部署 + 配置增量合并
│   ├── dns.py          # DNS 路由（resolved / system 双后端）
│   ├── certs.py        # CA 根证书信任（Linux 系统库 + NSS）
│   └── routes.py       # 路由表组件（保证 routes.caddy 存在且与 json 一致）
├── setup.py            # plan / apply / verify 编排
└── doctor.py           # 体检报告
tests/
├── test_platform_linux.py
├── test_component_caddy.py
├── test_component_dnsmasq.py
├── test_component_dns.py
├── test_component_certs.py
├── test_setup.py
└── test_doctor.py
```

**分工边界**：平台层**只做系统调用封装**，不含任何业务判断；组件层**不含 subprocess 调用**（除 Caddy 下载外），只通过平台能力操作系统；`setup.py` **不含任何具体组件的知识**，只按统一协议编排。

---

### Task 8: 平台层协议与 Linux 实现

**Files:**
- Create: `src/devproxy/platform/__init__.py`
- Create: `src/devproxy/platform/base.py`
- Create: `src/devproxy/platform/linux.py`
- Create: `src/devproxy/platform/fake.py`
- Create: `tests/test_platform_linux.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `devproxy.platform.base.Status` — dataclass，字段 `ok: bool`、`detail: str = ""`、`hint: str = ""`
  - `devproxy.platform.base.Action` — dataclass，字段 `description: str`、`kind: str = "run"`（`"run"` / `"write"`）、`command: list[str] = []`、`path: Path | None = None`、`content: str = ""`、`privileged: bool = False`
  - `devproxy.platform.base.ElevatedSession` — Protocol：`run(argv) -> tuple[int, str]`、`write(path, content) -> None`、`read(path) -> str`、`copy(src, dest) -> None`
  - `devproxy.platform.base.Platform` — Protocol：`name: str`、`dns_port: int`；方法 `privilege() -> ElevatedSession`、`run(argv) -> tuple[int, str]`、`binary_version(binary) -> str`、`service_active(name) -> bool`、`install_service(name, unit) -> None`、`enable_service(name) -> None`、`restart_service(name) -> None`、`service_unit_path(name) -> Path`、`tail_service_log(name) -> None`、`ca_trusted(ca) -> bool`、`dns_route_installed(backend) -> bool`
  - `devproxy.platform.linux.LinuxPlatform` — 实现 `Platform`；构造 `LinuxPlatform(user_home: Path | None = None)`
  - `devproxy.platform.linux.LinuxElevated` — 实现 `ElevatedSession`
  - `devproxy.platform.fake.FakePlatform` — 实现 `Platform`，字段 `services: dict[str, str]`（单元内容）、`active: set[str]`、`trusted: set[Path]`、`dns_backends: set[str]`、`commands: list[list[str]]`、`written: dict[Path, str]`；方法 `privilege()` 返回 `FakeElevated`

- [ ] **Step 1: 写失败的测试**

`tests/test_platform_linux.py`：

```python
import os
import subprocess
from pathlib import Path

import pytest

from devproxy.platform.base import Action, Status
from devproxy.platform.fake import FakePlatform
from devproxy.platform.linux import LinuxPlatform


def test_status_defaults():
    s = Status(ok=True)
    assert s.detail == ""
    assert s.hint == ""


def test_action_defaults():
    a = Action(description="装 caddy")
    assert a.kind == "run"
    assert a.command == []
    assert a.path is None
    assert a.privileged is False


def test_linux_platform_name_and_dns_port():
    p = LinuxPlatform()
    assert p.name == "linux"
    assert p.dns_port == 5353


def test_linux_service_unit_path_is_user_scope(tmp_path):
    p = LinuxPlatform(user_home=tmp_path)
    assert p.service_unit_path("caddy") == tmp_path / ".config" / "systemd" / "user" / "caddy.service"


def test_linux_binary_version_parses_output(tmp_path):
    fake = tmp_path / "caddy"
    fake.write_text('#!/bin/sh\necho "v2.11.7 h1:abc="\n')
    fake.chmod(0o755)
    assert LinuxPlatform().binary_version(fake) == "v2.11.7"


def test_linux_binary_version_empty_when_missing(tmp_path):
    assert LinuxPlatform().binary_version(tmp_path / "absent") == ""


def test_fake_platform_records_writes(tmp_path):
    p = FakePlatform()
    sess = p.privilege()
    sess.write(tmp_path / "f.txt", "hello")
    assert (tmp_path / "f.txt").read_text() == "hello"
    assert p.written[tmp_path / "f.txt"] == "hello"


def test_fake_platform_service_lifecycle():
    p = FakePlatform()
    p.install_service("caddy", "[Service]\n")
    assert p.service_active("caddy") is False
    p.active.add("caddy")
    assert p.service_active("caddy") is True
    assert "caddy.service" in p.service_unit_path("caddy").name


def test_fake_platform_privilege_session_is_cached():
    p = FakePlatform()
    assert p.privilege() is p.privilege(), "提权会话必须复用，否则会重复提示密码"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_platform_linux.py -v
```
Expected: 收集阶段报错 —— `ModuleNotFoundError: No module named 'devproxy.platform'`

- [ ] **Step 3: 实现协议与 Linux 实现**

`src/devproxy/platform/base.py`：

```python
"""平台能力协议。组件层只依赖这些能力，不直接调用系统命令。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class Status:
    """组件检查结果。ok=False 时 detail 说明现状，hint 给出可执行的修复命令。"""

    ok: bool
    detail: str = ""
    hint: str = ""


@dataclass
class Action:
    """一条待执行动作。"""

    description: str
    kind: str = "run"  # "run" | "write"
    command: list[str] = field(default_factory=list)
    path: Path | None = None
    content: str = ""
    privileged: bool = False


@runtime_checkable
class ElevatedSession(Protocol):
    """一次提权会话：一次密码验证，后续操作复用同一会话。"""

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

    def install_service(self, name: str, unit: str) -> None: ...

    def enable_service(self, name: str) -> None: ...

    def restart_service(self, name: str) -> None: ...

    def service_unit_path(self, name: str) -> Path: ...

    def tail_service_log(self, name: str) -> None: ...

    def ca_trusted(self, ca: Path) -> bool: ...

    def dns_route_installed(self, backend: str) -> bool: ...
```

`src/devproxy/platform/linux.py`：

```python
"""Linux 平台实现：systemd(user) / setcap / update-ca-certificates / NSS / resolved。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from devproxy.platform.base import ElevatedSession


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError:
        return 127, f"命令不存在: {argv[0]}"
    return proc.returncode, (proc.stdout or proc.stderr).strip()


class LinuxElevated:
    """提权会话。构造时执行一次 `sudo -v` 完成预认证。"""

    def __init__(self) -> None:
        self._authenticated = False
        self._authenticate()

    def _authenticate(self) -> None:
        code, msg = _run(["sudo", "-v"])
        if code != 0:
            raise PermissionError(f"提权失败：{msg}")
        self._authenticated = True

    def run(self, argv: list[str]) -> tuple[int, str]:
        return _run(["sudo", *argv])

    def write(self, path: Path, content: str) -> None:
        """通过临时文件 + sudo cp 写入，避免 shell 引号问题。"""
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


class LinuxPlatform:
    name = "linux"
    dns_port = 5353

    def __init__(self, user_home: Path | None = None) -> None:
        self.user_home = user_home or Path.home()
        self._elevated: LinuxElevated | None = None

    def privilege(self) -> ElevatedSession:
        if self._elevated is None:
            self._elevated = LinuxElevated()
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
        return self.user_home / ".config" / "systemd" / "user" / f"{name}.service"

    def service_active(self, name: str) -> bool:
        code, out = _run(["systemctl", "--user", "is-active", name])
        return code == 0 and out.strip() == "active"

    def install_service(self, name: str, unit: str) -> None:
        path = self.service_unit_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unit, encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])

    def enable_service(self, name: str) -> None:
        _run(["systemctl", "--user", "enable", "--now", name])

    def restart_service(self, name: str) -> None:
        _run(["systemctl", "--user", "restart", name])

    def tail_service_log(self, name: str) -> None:
        _run(["journalctl", "--user", "-u", name, "-f"])

    def ca_trusted(self, ca: Path) -> bool:
        dest = Path("/usr/local/share/ca-certificates/devproxy-local.crt")
        if not dest.exists():
            return False
        try:
            return dest.read_text(encoding="utf-8") == ca.read_text(encoding="utf-8")
        except OSError:
            return False

    def dns_route_installed(self, backend: str) -> bool:
        if backend == "resolved":
            return Path("/etc/systemd/resolved.conf.d/devproxy.conf").exists()
        if backend == "system":
            return Path("/etc/dnsmasq.d/devproxy.conf").exists()
        return False
```

`src/devproxy/platform/fake.py`：

```python
"""假平台：测试与 dry-run 用。不碰真实系统。"""

from __future__ import annotations

from pathlib import Path

from devproxy.platform.base import ElevatedSession


class FakeElevated:
    def __init__(self, platform: "FakePlatform") -> None:
        self.platform = platform

    def run(self, argv: list[str]) -> tuple[int, str]:
        self.platform.commands.append(list(argv))
        return 0, ""

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.platform.written[path] = content

    def read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def copy(self, src: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


class FakePlatform:
    name = "fake"
    dns_port = 5353

    def __init__(self) -> None:
        self.services: dict[str, str] = {}
        self.active: set[str] = set()
        self.trusted: set[Path] = set()
        self.dns_backends: set[str] = set()
        self.commands: list[list[str]] = []
        self.written: dict[Path, str] = {}
        self._elevated: FakeElevated | None = None

    def privilege(self) -> ElevatedSession:
        if self._elevated is None:
            self._elevated = FakeElevated(self)
        return self._elevated

    def run(self, argv: list[str]) -> tuple[int, str]:
        self.commands.append(list(argv))
        return 0, ""

    def binary_version(self, binary: Path) -> str:
        return "v2.11.7"

    def service_unit_path(self, name: str) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"

    def service_active(self, name: str) -> bool:
        return name in self.active

    def install_service(self, name: str, unit: str) -> None:
        self.services[name] = unit

    def enable_service(self, name: str) -> None:
        self.active.add(name)

    def restart_service(self, name: str) -> None:
        self.active.add(name)

    def tail_service_log(self, name: str) -> None:
        pass

    def ca_trusted(self, ca: Path) -> bool:
        return ca in self.trusted

    def dns_route_installed(self, backend: str) -> bool:
        return backend in self.dns_backends
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_platform_linux.py -v
```
Expected: PASS，9 passed

- [ ] **Step 5: 提交**

```bash
git add src/devproxy/platform/ tests/test_platform_linux.py
git commit -m "✨ feat(devproxy): 平台层协议、Linux 实现与假平台"
```

---

### Task 9: Caddy 组件

**Files:**
- Create: `src/devproxy/components/__init__.py`
- Create: `src/devproxy/components/base.py`
- Create: `src/devproxy/components/caddy.py`
- Create: `tests/test_component_caddy.py`

**Interfaces:**
- Consumes: `devproxy.platform.base.{Status, Action, Platform}`、`devproxy.paths.Paths`、`devproxy.config.Config`
- Produces:
  - `devproxy.components.base.Component` — Protocol：`name: str`；`check() -> Status`；`plan() -> list[Action]`；`apply(actions: list[Action]) -> None`
  - `devproxy.components.caddy.CADDY_VERSION` — `"2.11.7"`
  - `devproxy.components.caddy.CADDY_SHA256` — `dict[tuple[str, str], str]`，键为 `(os, arch)`，`os ∈ {"linux", "mac"}`
  - `devproxy.components.caddy.caddy_asset_name(os: str, arch: str, version: str = CADDY_VERSION) -> str`
  - `devproxy.components.caddy.CaddyComponent` — 实现 `Component`；构造 `(platform: Platform, paths: Paths, config: Config)`

- [ ] **Step 1: 写失败的测试**

`tests/test_component_caddy.py`：

```python
import hashlib
import tarfile
import io
from pathlib import Path

import pytest

from devproxy.components.caddy import (
    CADDY_SHA256,
    CADDY_VERSION,
    CaddyComponent,
    caddy_asset_name,
)
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.fake import FakePlatform


def _paths(tmp_path):
    return Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")


def _component(tmp_path, platform=None):
    paths = _paths(tmp_path)
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir), caddy_bin=str(tmp_path / "bin" / "caddy"))
    return CaddyComponent(platform or FakePlatform(), paths, cfg), paths, cfg


def test_asset_name_uses_mac_not_darwin():
    assert caddy_asset_name("mac", "arm64") == f"caddy_{CADDY_VERSION}_mac_arm64.tar.gz"
    assert caddy_asset_name("linux", "amd64") == f"caddy_{CADDY_VERSION}_linux_amd64.tar.gz"


def test_sha256_covers_four_platforms():
    for key in [
        ("linux", "amd64"),
        ("linux", "arm64"),
        ("mac", "arm64"),
        ("mac", "amd64"),
    ]:
        assert key in CADDY_SHA256
        assert len(CADDY_SHA256[key]) == 64


def test_check_ok_when_binary_and_service_ready(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg)
    binary = Path(cfg.caddy_bin)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    platform.active.add("caddy")
    status = comp.check()
    assert status.ok is True, status.detail


def test_check_missing_binary_reports_hint(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    status = comp.check()
    assert status.ok is False
    assert "caddy" in status.detail.lower()
    assert status.hint, "缺失时必须给出修复提示"


def test_plan_includes_download_when_binary_missing(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    actions = comp.plan()
    kinds = [a.description for a in actions]
    assert any("下载" in d for d in kinds)
    assert any(a.privileged for a in actions), "setcap 需要提权"


def test_plan_is_empty_when_everything_ready(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg)
    binary = Path(cfg.caddy_bin)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    platform.active.add("caddy")
    assert comp.plan() == []


def test_apply_writes_unit_and_enables_service(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg)
    comp.apply(comp.plan())
    assert "caddy" in platform.services
    unit = platform.services["caddy"]
    assert "ExecStart=" in unit
    assert str(paths.caddyfile) in unit
    assert "caddy" in platform.active


def test_apply_creates_main_caddyfile(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    comp.apply(comp.plan())
    content = paths.caddyfile.read_text()
    assert "admin 127.0.0.1:2019" in content
    assert f"import {paths.routes_caddy}" in content
    assert f"import {paths.sites_caddy}" in content


def test_sites_caddy_is_not_overwritten(tmp_path):
    """sites.caddy 是用户的，工具只 import 不覆盖。"""
    comp, paths, cfg = _component(tmp_path)
    paths.sites_caddy.write_text("localhost { respond \"hi\" }\n")
    comp.apply(comp.plan())
    assert paths.sites_caddy.read_text() == "localhost { respond \"hi\" }\n"


def test_verify_fails_when_service_inactive(tmp_path):
    comp, paths, cfg = _component(tmp_path)
    platform = FakePlatform()
    comp = CaddyComponent(platform, paths, cfg)
    comp.apply(comp.plan())
    status = comp.verify()
    assert status.ok is False
    assert "systemctl" in status.hint
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_component_caddy.py -v
```
Expected: 收集阶段报错 —— `ModuleNotFoundError: No module named 'devproxy.components'`

- [ ] **Step 3: 实现**

`src/devproxy/components/base.py`：

```python
"""组件协议。check 永不抛异常 —— doctor 依赖它一次列出全部问题。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from devproxy.platform.base import Action, Status


@runtime_checkable
class Component(Protocol):
    name: str

    def check(self) -> Status:
        """检查现状。永不抛异常。"""

    def plan(self) -> list[Action]:
        """根据当前状态算出要执行的动作。空列表表示无需变更。"""

    def apply(self, actions: list[Action]) -> None:
        """执行动作。"""
```

`src/devproxy/components/caddy.py`：

```python
"""Caddy 组件：二进制下载与校验、setcap 授权、systemd 单元、主 Caddyfile。

Caddy 版本固定，sha256 内置在源码里 —— 上游发布的 checksums.txt 与实际
托管的资产不一致（实测 tar.gz 与 deb 均对不上），不能信任该文件。
"""

from __future__ import annotations

import hashlib
import platform as _platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Action, Platform, Status

CADDY_VERSION = "2.11.7"

CADDY_SHA256 = {
    ("linux", "amd64"): "727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b",
    ("linux", "arm64"): "d8fc6d179a5d283028a472a5618564f6ad8a86fed513e64f032b3b0b7cc45e42",
    ("mac", "arm64"): "cda3030e5d5b13eb9f0b6fb541037f633bddd958c070c5f561d7a5cccb581665",
    ("mac", "amd64"): "110c53e5c093e4cfce6eda787f94f23bef913c309f620fab26b4f678031841ad",
}

RELEASE_BASE = "https://github.com/caddyserver/caddy/releases/download/v{version}"


def caddy_asset_name(os_name: str, arch: str, version: str = CADDY_VERSION) -> str:
    """资产名。注意 macOS 用 mac 而非 darwin。"""
    return f"caddy_{version}_{os_name}_{arch}.tar.gz"


def current_platform_key() -> tuple[str, str]:
    system = _platform.system().lower()
    os_name = "mac" if system == "darwin" else "linux"
    machine = _platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    return os_name, arch


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_caddy(dest: Path, version: str = CADDY_VERSION) -> None:
    """下载并校验后解包出 caddy 二进制。校验失败必须抛异常，不留半个文件。"""
    os_name, arch = current_platform_key()
    key = (os_name, arch)
    if key not in CADDY_SHA256:
        raise RuntimeError(f"不支持的平台: {os_name}/{arch}")
    asset = caddy_asset_name(os_name, arch, version)
    url = RELEASE_BASE.format(version=version) + "/" + asset
    with tempfile.TemporaryDirectory(prefix="devproxy-caddy-") as tmp:
        tgz = Path(tmp) / asset
        with urllib.request.urlopen(url, timeout=120) as r, open(tgz, "wb") as f:
            shutil.copyfileobj(r, f)
        actual = _sha256_file(tgz)
        expected = CADDY_SHA256[key]
        if actual != expected:
            raise RuntimeError(
                f"caddy 包校验失败（{asset}）\n  期望 sha256: {expected}\n  实际 sha256: {actual}"
            )
        with tarfile.open(tgz) as tf:
            member = tf.getmember("caddy")
            with tf.extractfile(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    dest.chmod(0o755)


def render_unit(
    *,
    caddy_bin: Path,
    caddyfile: Path,
    admin_addr: str,
    env: dict[str, str] | None = None,
) -> str:
    """生成 systemd user 单元。形态与现有的 caddy.service 保持一致。"""
    lines = [
        "[Unit]",
        "Description=Caddy local HTTP/HTTPS server (managed by devproxy)",
        "Documentation=https://caddyserver.com/docs/",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=notify",
    ]
    for k, v in (env or {}).items():
        lines.append(f"Environment={k}={v}")
    lines += [
        f"ExecStart={caddy_bin} run --config {caddyfile}",
        f"ExecReload={caddy_bin} reload --config {caddyfile} --address {admin_addr}",
        f"ExecStop={caddy_bin} stop",
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


class CaddyComponent:
    name = "caddy"

    def __init__(self, platform: Platform, paths: Paths, config: Config):
        self.platform = platform
        self.paths = paths
        self.config = config

    # -- check ---------------------------------------------------------
    def check(self) -> Status:
        binary = self.config.caddy_bin_path
        if not binary.exists():
            return Status(
                ok=False,
                detail=f"caddy 二进制不存在: {binary}",
                hint=f"devproxy setup 会自动下载 {CADDY_VERSION}",
            )
        version = self.platform.binary_version(binary)
        if version and version != f"v{CADDY_VERSION}":
            return Status(
                ok=False,
                detail=f"caddy 版本不符: 期望 v{CADDY_VERSION}，实际 {version}",
                hint="devproxy setup 会替换为固定版本",
            )
        if not self.platform.service_active("caddy"):
            return Status(
                ok=False,
                detail="caddy 服务未运行",
                hint="systemctl --user start caddy",
            )
        if not self.paths.caddyfile.exists():
            return Status(
                ok=False,
                detail=f"主 Caddyfile 不存在: {self.paths.caddyfile}",
                hint="devproxy setup 会生成",
            )
        return Status(ok=True, detail=f"caddy {CADDY_VERSION} 已就绪")

    # -- plan ----------------------------------------------------------
    def plan(self) -> list[Action]:
        actions: list[Action] = []
        binary = self.config.caddy_bin_path
        status = self.check()
        if "二进制不存在" in status.detail or "版本不符" in status.detail:
            actions.append(
                Action(
                    description=f"下载 caddy {CADDY_VERSION} 并校验 sha256",
                    kind="run",
                    command=["download-caddy", str(binary)],
                )
            )
        actions.append(
            Action(
                description="授权 caddy 绑定 80/443（setcap）",
                kind="run",
                command=["setcap", "cap_net_bind_service=+ep", str(binary)],
                privileged=True,
            )
        )
        actions.append(
            Action(
                description="生成主 Caddyfile",
                kind="write",
                path=self.paths.caddyfile,
                content=self._main_caddyfile(),
            )
        )
        actions.append(
            Action(
                description="安装并启动 caddy.service（systemd user）",
                kind="run",
                command=["install-service", "caddy"],
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
                binary.parent.mkdir(parents=True, exist_ok=True)
                download_caddy(binary)
                continue
            if action.command[:1] == ["setcap"]:
                self.platform.privilege().run(action.command)
                continue
            if action.command[:1] == ["install-service"]:
                unit = render_unit(
                    caddy_bin=binary,
                    caddyfile=self.paths.caddyfile,
                    admin_addr=self.config.admin_addr,
                )
                self.platform.install_service("caddy", unit)
                self.platform.enable_service("caddy")
                continue
            raise RuntimeError(f"未知动作: {action.command}")

    # -- verify --------------------------------------------------------
    def verify(self) -> Status:
        if not self.platform.service_active("caddy"):
            return Status(
                ok=False,
                detail="caddy 服务未处于 active",
                hint="systemctl --user status caddy",
            )
        return Status(ok=True, detail="caddy 服务运行中")

    def _main_caddyfile(self) -> str:
        from devproxy.proxy.caddy import build_main_caddyfile

        return build_main_caddyfile(
            admin_addr=self.config.admin_addr,
            storage=self.config.storage_path,
            routes_caddy=self.paths.routes_caddy,
            sites_caddy=self.paths.sites_caddy,
        )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_component_caddy.py -v
```
Expected: PASS，10 passed

- [ ] **Step 5: 提交**

```bash
git add src/devproxy/components/ tests/test_component_caddy.py
git commit -m "✨ feat(devproxy): Caddy 组件——下载校验/setcap/单元/主 Caddyfile"
```

---

### Task 10: Dnsmasq 与 DNS 组件

**Files:**
- Create: `src/devproxy/components/dnsmasq.py`
- Create: `src/devproxy/components/dns.py`
- Create: `tests/test_component_dnsmasq.py`
- Create: `tests/test_component_dns.py`

**Interfaces:**
- Consumes: `devproxy.platform.base.{Status, Action, Platform}`、`devproxy.paths.Paths`、`devproxy.config.Config`
- Produces:
  - `devproxy.components.dnsmasq.merge_dnsmasq_config(existing: str, *, port: int, suffix: str) -> str` — 纯函数：确保 `port=<port>` 与 `address=/.<suffix>/127.0.0.1`、`address=/.<suffix>/::1` 存在，其余原样保留
  - `devproxy.components.dnsmasq.DnsmasqComponent` — 实现 `Component`；构造 `(platform, paths, config)`
  - `devproxy.components.dns.render_resolved_conf(*, port: int, domains: list[str]) -> str`
  - `devproxy.components.dns.render_system_dnsmasq_conf(*, suffix: str) -> str`
  - `devproxy.components.dns.DnsComponent` — 实现 `Component`；构造 `(platform, paths, config, backend: str = "auto")`；属性 `resolved_backend: bool`

- [ ] **Step 1: 写失败的测试**

`tests/test_component_dnsmasq.py`：

```python
import pytest

from devproxy.components.dnsmasq import merge_dnsmasq_config
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.fake import FakePlatform


def test_merge_adds_port_and_addresses_to_empty():
    out = merge_dnsmasq_config("", port=5353, suffix="test")
    assert "port=5353" in out
    assert "address=/.test/127.0.0.1" in out
    assert "address=/.test/::1" in out


def test_merge_preserves_existing_address_entries():
    existing = "address=/.develop/127.0.0.1\naddress=/.develop/::1\nno-resolv\nserver=223.5.5.5\nfilter-AAAA\n"
    out = merge_dnsmasq_config(existing, port=5353, suffix="test")
    assert "address=/.develop/127.0.0.1" in out
    assert "address=/.develop/::1" in out
    assert "no-resolv" in out
    assert "server=223.5.5.5" in out
    assert "filter-AAAA" in out


def test_merge_replaces_existing_port():
    out = merge_dnsmasq_config("port=53\n", port=5353, suffix="test")
    assert "port=53" not in out
    assert "port=5353" in out
    assert out.count("port=") == 1


def test_merge_is_idempotent():
    once = merge_dnsmasq_config("", port=5353, suffix="test")
    twice = merge_dnsmasq_config(once, port=5353, suffix="test")
    assert once == twice


def test_merge_does_not_duplicate_address():
    existing = "address=/.test/127.0.0.1\naddress=/.test/::1\n"
    out = merge_dnsmasq_config(existing, port=5353, suffix="test")
    assert out.count("address=/.test/127.0.0.1") == 1
    assert out.count("address=/.test/::1") == 1
```

`tests/test_component_dns.py`：

```python
from devproxy.components.dns import (
    DnsComponent,
    render_resolved_conf,
    render_system_dnsmasq_conf,
)
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.fake import FakePlatform


def test_render_resolved_conf_shape():
    out = render_resolved_conf(port=5353, domains=["test"])
    assert "[Resolve]" in out
    assert "DNS=127.0.0.1:5353" in out
    assert "Domains=~.test" in out


def test_render_system_dnsmasq_conf_shape():
    out = render_system_dnsmasq_conf(suffix="test")
    assert "address=/.test/127.0.0.1" in out


def test_auto_backend_prefers_resolved(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    cfg = Config()
    comp = DnsComponent(FakePlatform(), paths, cfg, backend="auto")
    assert comp.resolved_backend is True


def test_explicit_system_backend(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    comp = DnsComponent(FakePlatform(), paths, Config(), backend="system")
    assert comp.resolved_backend is False


def test_check_reports_missing_route(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    comp = DnsComponent(FakePlatform(), paths, Config(), backend="resolved")
    status = comp.check()
    assert status.ok is False
    assert "DNS" in status.detail or "解析" in status.detail


def test_plan_writes_resolved_conf(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    comp = DnsComponent(FakePlatform(), paths, Config(), backend="resolved")
    actions = comp.plan()
    assert any(a.privileged for a in actions), "写 /etc 需要提权"
    assert any(a.kind == "write" for a in actions)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_component_dnsmasq.py tests/test_component_dns.py -v
```
Expected: 收集阶段报错 —— `ModuleNotFoundError: No module named 'devproxy.components.dnsmasq'`

- [ ] **Step 3: 实现**

`src/devproxy/components/dnsmasq.py`：

```python
"""用户级 dnsmasq 组件：配置增量合并 + systemd user 单元。"""

from __future__ import annotations

from pathlib import Path

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Action, Platform, Status

DNSMASQ_CONF = Path("~/.config/dnsmasq/dnsmasq.conf")


def merge_dnsmasq_config(existing: str, *, port: int, suffix: str) -> str:
    """增量修改 dnsmasq 配置：确保 port 与 address 条目存在，其余原样保留。

    绝不重写整个文件 —— 用户可能有 .develop 等自己的域名条目。
    """
    lines = existing.splitlines()
    out: list[str] = []
    seen_port = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("port="):
            if not seen_port:
                out.append(f"port={port}")
                seen_port = True
            continue
        out.append(line)
    if not seen_port:
        out.append(f"port={port}")
    suffix = suffix.lstrip(".")
    for addr in (f"address=/.{suffix}/127.0.0.1", f"address=/.{suffix}/::1"):
        if addr not in out:
            out.append(addr)
    return "\n".join(out) + "\n"


class DnsmasqComponent:
    name = "dnsmasq"

    def __init__(self, platform: Platform, paths: Paths, config: Config):
        self.platform = platform
        self.paths = paths
        self.config = config
        self.conf_path = Path.home() / ".config" / "dnsmasq" / "dnsmasq.conf"

    def check(self) -> Status:
        if not self.platform.service_active("dnsmasq"):
            return Status(
                ok=False,
                detail="dnsmasq 服务未运行",
                hint="systemctl --user start dnsmasq",
            )
        if not self.conf_path.exists():
            return Status(
                ok=False,
                detail=f"dnsmasq 配置不存在: {self.conf_path}",
                hint="devproxy setup 会生成",
            )
        return Status(ok=True, detail="dnsmasq 运行中")

    def plan(self) -> list[Action]:
        existing = ""
        if self.conf_path.exists():
            existing = self.conf_path.read_text(encoding="utf-8")
        merged = merge_dnsmasq_config(
            existing, port=self.platform.dns_port, suffix=self.config.domain_suffix
        )
        return [
            Action(
                description="合并 dnsmasq 配置（保留既有域名条目）",
                kind="write",
                path=self.conf_path,
                content=merged,
            ),
            Action(
                description="安装并启动 dnsmasq.service（systemd user）",
                kind="run",
                command=["install-service", "dnsmasq"],
            ),
        ]

    def apply(self, actions: list[Action]) -> None:
        for action in actions:
            if action.kind == "write" and action.path is not None:
                action.path.parent.mkdir(parents=True, exist_ok=True)
                action.path.write_text(action.content, encoding="utf-8")
                continue
            if action.command[:1] == ["install-service"]:
                unit = (
                    "[Unit]\n"
                    "Description=dnsmasq for devproxy (user scope)\n"
                    "After=network.target\n"
                    "\n"
                    "[Service]\n"
                    "Type=simple\n"
                    "ExecStart=/usr/sbin/dnsmasq --conf-dir=%h/.config/dnsmasq "
                    "--pid-file=%h/.cache/dnsmasq.pid\n"
                    "Restart=on-failure\n"
                    "RestartSec=2s\n"
                    "\n"
                    "[Install]\n"
                    "WantedBy=default.target\n"
                )
                self.platform.install_service("dnsmasq", unit)
                self.platform.enable_service("dnsmasq")
                continue
            raise RuntimeError(f"未知动作: {action.command}")

    def verify(self) -> Status:
        if not self.platform.service_active("dnsmasq"):
            return Status(
                ok=False,
                detail="dnsmasq 服务未处于 active",
                hint="systemctl --user status dnsmasq",
            )
        return Status(ok=True, detail="dnsmasq 运行中")
```

`src/devproxy/components/dns.py`：

```python
"""DNS 路由组件：把 *.test 指向本机。

两个后端：
- resolved（Linux 默认）：写 /etc/systemd/resolved.conf.d/devproxy.conf
- system：写 /etc/dnsmasq.d/devproxy.conf（需自行处理 53 端口冲突）
"""

from __future__ import annotations

from pathlib import Path

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Action, Platform, Status

RESOLVED_CONF = Path("/etc/systemd/resolved.conf.d/devproxy.conf")
SYSTEM_DNSMASQ_CONF = Path("/etc/dnsmasq.d/devproxy.conf")


def render_resolved_conf(*, port: int, domains: list[str]) -> str:
    suffix = ",".join(f"~.{d.lstrip('.')}" for d in domains)
    return (
        "[Resolve]\n"
        f"DNS=127.0.0.1:{port}\n"
        "FallbackDNS=1.1.1.1 8.8.8.8\n"
        f"Domains={suffix}\n"
    )


def render_system_dnsmasq_conf(*, suffix: str) -> str:
    s = suffix.lstrip(".")
    return f"address=/.{s}/127.0.0.1\naddress=/.{s}/::1\n"


class DnsComponent:
    name = "dns"

    def __init__(
        self, platform: Platform, paths: Paths, config: Config, backend: str = "auto"
    ):
        self.platform = platform
        self.paths = paths
        self.config = config
        self.backend = backend

    @property
    def resolved_backend(self) -> bool:
        """auto 时优先 resolved（不占 53，用户态守护）。"""
        if self.backend == "system":
            return False
        if self.backend == "resolved":
            return True
        return Path("/etc/systemd/resolved.conf.d").is_dir()

    @property
    def target_path(self) -> Path:
        return RESOLVED_CONF if self.resolved_backend else SYSTEM_DNSMASQ_CONF

    def check(self) -> Status:
        key = "resolved" if self.resolved_backend else "system"
        if self.platform.dns_route_installed(key):
            return Status(ok=True, detail=f"DNS 路由已配置（{key} 后端）")
        return Status(
            ok=False,
            detail=f"DNS 路由缺失（{key} 后端）：{self.target_path}",
            hint="devproxy setup 会写入（需要 sudo）",
        )

    def plan(self) -> list[Action]:
        if self.resolved_backend:
            content = render_resolved_conf(
                port=self.platform.dns_port, domains=[self.config.domain_suffix]
            )
            desc = "写入 systemd-resolved 路由（*.%s → 127.0.0.1）" % self.config.domain_suffix
            reload_action = Action(
                description="重载 systemd-resolved 使路由生效",
                kind="run",
                command=["systemctl", "restart", "systemd-resolved"],
                privileged=True,
            )
        else:
            content = render_system_dnsmasq_conf(suffix=self.config.domain_suffix)
            desc = "写入系统 dnsmasq 路由（*.%s → 127.0.0.1）" % self.config.domain_suffix
            reload_action = Action(
                description="重载系统 dnsmasq 使路由生效",
                kind="run",
                command=["systemctl", "restart", "dnsmasq"],
                privileged=True,
            )
        return [
            Action(description=desc, kind="write", path=self.target_path, content=content, privileged=True),
            reload_action,
        ]

    def apply(self, actions: list[Action]) -> None:
        elevated = self.platform.privilege()
        for action in actions:
            if action.kind == "write" and action.path is not None:
                elevated.write(action.path, action.content)
                continue
            if action.privileged:
                elevated.run(action.command)
                continue
            raise RuntimeError(f"未知动作: {action.command}")

    def verify(self) -> Status:
        key = "resolved" if self.resolved_backend else "system"
        if not self.platform.dns_route_installed(key):
            return Status(
                ok=False,
                detail="DNS 路由未生效",
                hint="systemctl restart systemd-resolved",
            )
        return Status(ok=True, detail="DNS 路由已配置")
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_component_dnsmasq.py tests/test_component_dns.py -v
```
Expected: PASS，10 passed

- [ ] **Step 5: 提交**

```bash
git add src/devproxy/components/dnsmasq.py src/devproxy/components/dns.py tests/test_component_dnsmasq.py tests/test_component_dns.py
git commit -m "✨ feat(devproxy): dnsmasq 与 DNS 路由组件（配置增量合并 + 双后端）"
```

---

### Task 11: Cert 组件与 setup 编排

**Files:**
- Create: `src/devproxy/components/certs.py`
- Create: `src/devproxy/setup.py`
- Create: `src/devproxy/doctor.py`
- Create: `tests/test_component_certs.py`
- Create: `tests/test_setup.py`
- Create: `tests/test_doctor.py`
- Modify: `src/devproxy/cli.py`（加 `setup` / `doctor` / `trust` / `logs` 子命令）

**Interfaces:**
- Consumes: Task 8 的 `Platform` / `Status` / `Action`，Task 9/10 的组件，Plan 1 的 `Paths` / `Config`
- Produces:
  - `devproxy.components.certs.CertComponent` — 实现 `Component`；构造 `(platform, paths, config)`
  - `devproxy.setup.ComponentReport` — dataclass，字段 `name: str`、`status: Status`、`actions: list[Action]`
  - `devproxy.setup.SetupPlan` — dataclass，字段 `reports: list[ComponentReport]`；属性 `needs_sudo -> list[Action]`、`is_noop -> bool`
  - `devproxy.setup.build_plan(components: list[Component]) -> SetupPlan`
  - `devproxy.setup.apply_plan(plan: SetupPlan) -> list[tuple[str, Status]]`
  - `devproxy.doctor.build_report(components: list[Component]) -> list[ComponentReport]`
  - `devproxy.doctor.report_is_healthy(reports) -> bool`

- [ ] **Step 1: 写失败的测试**

`tests/test_component_certs.py`：

```python
from pathlib import Path

from devproxy.components.certs import CertComponent
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.fake import FakePlatform


def _component(tmp_path, platform=None):
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    platform = platform or FakePlatform()
    return CertComponent(platform, paths, cfg), platform, paths


def test_check_ok_when_trusted(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = paths.data_dir / "pki" / "authorities" / "local" / "root.crt"
    platform.trusted.add(ca)
    assert comp.check().ok is True


def test_check_missing_ca_reports_hint(tmp_path):
    comp, platform, paths = _component(tmp_path)
    status = comp.check()
    assert status.ok is False
    assert "root.crt" in status.detail
    assert status.hint


def test_plan_is_empty_when_trusted(tmp_path):
    comp, platform, paths = _component(tmp_path)
    ca = paths.data_dir / "pki" / "authorities" / "local" / "root.crt"
    platform.trusted.add(ca)
    assert comp.plan() == []


def test_plan_includes_privileged_steps(tmp_path):
    comp, platform, paths = _component(tmp_path)
    actions = comp.plan()
    assert actions
    assert all(a.privileged for a in actions), "装信任库需要提权"
```

`tests/test_setup.py`：

```python
from devproxy.components.base import Component
from devproxy.platform.base import Action, Status
from devproxy.setup import apply_plan, build_plan


class FakeComponent:
    def __init__(self, name, status, actions=()):
        self.name = name
        self._status = status
        self._actions = list(actions)
        self.applied = None

    def check(self):
        return self._status

    def plan(self):
        return list(self._actions)

    def apply(self, actions):
        self.applied = actions


def test_build_plan_collects_reports():
    c = FakeComponent("caddy", Status(ok=True))
    plan = build_plan([c])
    assert len(plan.reports) == 1
    assert plan.reports[0].name == "caddy"
    assert plan.is_noop is True


def test_plan_needs_sudo_lists_privileged_actions():
    c = FakeComponent(
        "caddy",
        Status(ok=False, detail="缺"),
        [Action(description="setcap", privileged=True), Action(description="写文件")],
    )
    plan = build_plan([c])
    assert len(plan.needs_sudo) == 1
    assert plan.needs_sudo[0].description == "setcap"
    assert plan.is_noop is False


def test_apply_plan_applies_each_component():
    c1 = FakeComponent("a", Status(ok=True), [Action(description="x")])
    c2 = FakeComponent("b", Status(ok=True), [Action(description="y")])
    plan = build_plan([c1, c2])
    results = apply_plan(plan)
    assert c1.applied is not None and c2.applied is not None
    assert {name for name, _ in results} == {"a", "b"}


def test_apply_plan_continues_after_failure():
    class Boom(FakeComponent):
        def apply(self, actions):
            raise RuntimeError("炸了")

    good = FakeComponent("good", Status(ok=True), [Action(description="x")])
    bad = Boom("bad", Status(ok=True), [Action(description="y")])
    results = apply_plan(build_plan([good, bad]))
    statuses = dict(results)
    assert statuses["bad"].ok is False
    assert "炸了" in statuses["bad"].detail
    assert statuses["good"].ok is True
```

`tests/test_doctor.py`：

```python
from devproxy.doctor import build_report, report_is_healthy
from devproxy.platform.base import Status


class C:
    def __init__(self, name, ok):
        self.name = name
        self._ok = ok

    def check(self):
        return Status(ok=self._ok, detail="d", hint="h")


def test_report_is_healthy_when_all_ok():
    assert report_is_healthy(build_report([C("a", True), C("b", True)])) is True


def test_report_is_unhealthy_when_any_fails():
    assert report_is_healthy(build_report([C("a", True), C("b", False)])) is False
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_component_certs.py tests/test_setup.py tests/test_doctor.py -v
```
Expected: 收集阶段报错 —— `ModuleNotFoundError: No module named 'devproxy.components.certs'`

- [ ] **Step 3: 实现**

`src/devproxy/components/certs.py`：

```python
"""CA 根证书信任组件。

Linux 需要两处：系统信任库（curl / Go / Java 等）与 NSS 库（Chrome/Chromium）。
macOS 只需要系统钥匙串（由 Plan 3 的 macOS 平台实现提供）。
"""

from __future__ import annotations

from pathlib import Path

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.platform.base import Action, Platform, Status

CA_NAME = "devproxy Local CA"
SYSTEM_CA_DEST = Path("/usr/local/share/ca-certificates/devproxy-local.crt")
NSS_DIR = Path("~/.pki/nssdb")


class CertComponent:
    name = "cert"

    def __init__(self, platform: Platform, paths: Paths, config: Config):
        self.platform = platform
        self.paths = paths
        self.config = config

    @property
    def ca_path(self) -> Path:
        return self.config.storage_path / "pki" / "authorities" / "local" / "root.crt"

    def check(self) -> Status:
        if not self.ca_path.exists():
            return Status(
                ok=False,
                detail=f"CA 根证书不存在: {self.ca_path}",
                hint="先启动 caddy 生成内部 CA（devproxy setup 会做）",
            )
        if not self.platform.ca_trusted(self.ca_path):
            return Status(
                ok=False,
                detail="CA 根证书未被系统信任",
                hint="devproxy trust",
            )
        return Status(ok=True, detail="CA 根证书已信任")

    def plan(self) -> list[Action]:
        if self.platform.ca_trusted(self.ca_path):
            return []
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

    def apply(self, actions: list[Action]) -> None:
        for action in actions:
            if action.command[:1] == ["install-system-ca"]:
                elevated = self.platform.privilege()
                src, dest = Path(action.command[1]), Path(action.command[2])
                elevated.copy(src, dest)
                elevated.run(["update-ca-certificates"])
                continue
            if action.command[:1] == ["install-nss-ca"]:
                src = Path(action.command[1])
                nss = Path(action.command[2]).expanduser()
                nss.mkdir(parents=True, exist_ok=True)
                code, _ = self.platform.run(["certutil", "-V"])
                if code != 0:
                    raise RuntimeError(
                        "缺少 certutil（Chrome 信任需 NSS 库）：sudo apt install libnss3-tools"
                    )
                # 先删同名再装，保证幂等
                self.platform.run(
                    ["certutil", "-d", f"sql:{nss}", "-D", "-n", CA_NAME]
                )
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
                    raise RuntimeError(f"安装 NSS 证书失败: {msg}")
                continue
            raise RuntimeError(f"未知动作: {action.command}")

    def verify(self) -> Status:
        if not self.platform.ca_trusted(self.ca_path):
            return Status(ok=False, detail="CA 仍未被信任", hint="devproxy trust")
        return Status(ok=True, detail="CA 已信任")
```

`src/devproxy/setup.py`：

```python
"""setup 编排：check → plan → apply → verify。

组件层不含任何编排逻辑；这里也不含任何具体组件的知识。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from devproxy.components.base import Component
from devproxy.platform.base import Action, Status


@dataclass
class ComponentReport:
    name: str
    status: Status
    actions: list[Action] = field(default_factory=list)
    component: Component | None = None


@dataclass
class SetupPlan:
    reports: list[ComponentReport] = field(default_factory=list)

    @property
    def needs_sudo(self) -> list[Action]:
        return [a for r in self.reports for a in r.actions if a.privileged]

    @property
    def is_noop(self) -> bool:
        return not any(r.actions for r in self.reports)


def build_plan(components: list[Component]) -> SetupPlan:
    """对每个组件取 check() 与 plan()。plan 为空表示该组件无需变更。"""
    reports = []
    for c in components:
        reports.append(
            ComponentReport(
                name=c.name,
                status=c.check(),
                actions=c.plan(),
                component=c,
            )
        )
    return SetupPlan(reports=reports)


def apply_plan(plan: SetupPlan) -> list[tuple[str, Status]]:
    """逐组件执行；单个组件失败不阻断其他组件。"""
    results: list[tuple[str, Status]] = []
    for report in plan.reports:
        component = report.component
        try:
            if report.actions and component is not None:
                component.apply(report.actions)
            results.append((report.name, component.verify()))
        except Exception as e:  # noqa: BLE001 —— 组件失败不应中断 setup
            results.append(
                (report.name, Status(ok=False, detail=str(e), hint="查看上面的报错"))
            )
    return results
```

`src/devproxy/doctor.py`：

```python
"""doctor：体检报告。只读，不改任何东西。"""

from __future__ import annotations

from devproxy.components.base import Component
from devproxy.setup import ComponentReport


def build_report(components: list[Component]) -> list[ComponentReport]:
    reports = []
    for c in components:
        reports.append(ComponentReport(name=c.name, status=c.check(), component=c))
    return reports


def report_is_healthy(reports: list[ComponentReport]) -> bool:
    return all(r.status.ok for r in reports)
```

- [ ] **Step 4: 在 cli.py 加 setup / doctor / trust / logs**

在 `src/devproxy/cli.py` 追加：

```python
from devproxy.components.caddy import CaddyComponent
from devproxy.components.certs import CertComponent
from devproxy.components.dns import DnsComponent
from devproxy.components.dnsmasq import DnsmasqComponent
from devproxy.doctor import build_report, report_is_healthy
from devproxy.setup import apply_plan, build_plan


def _platform():
    from devproxy.platform.linux import LinuxPlatform

    return LinuxPlatform()


def _components(paths, config, dns_backend="auto"):
    platform = _platform()
    return [
        CaddyComponent(platform, paths, config),
        DnsmasqComponent(platform, paths, config),
        DnsComponent(platform, paths, config, backend=dns_backend),
        CertComponent(platform, paths, config),
    ]


@app.command()
def setup(
    dns_backend: str = typer.Option("auto", "--dns-backend", help="auto|resolved|system"),
    domain_suffix: str = typer.Option(None, "--domain-suffix"),
    no_trust: bool = typer.Option(False, "--no-trust", help="跳过证书信任"),
    yes: bool = typer.Option(False, "--yes", help="非交互模式，不询问"),
) -> None:
    """部署本地开发环境（Caddy + dnsmasq + DNS 路由 + 证书信任 + 自启）。"""
    paths, config, _, _ = build_context()
    if domain_suffix:
        config = replace(config, domain_suffix=domain_suffix)
    components = _components(paths, config, dns_backend)
    if no_trust:
        components = [c for c in components if c.name != "cert"]

    plan = build_plan(components)

    table = Table(show_header=True, header_style="bold")
    table.add_column("组件")
    table.add_column("现状")
    table.add_column("将要执行")
    for r in plan.reports:
        table.add_row(
            r.name,
            "✔ 就绪" if r.status.ok else "✗ " + r.status.detail,
            "\n".join(a.description for a in r.actions) or "—",
        )
    console.print(table)

    if plan.is_noop:
        console.print("所有组件已就绪，无需变更。")
        raise typer.Exit(EXIT_OK)

    if plan.needs_sudo:
        console.print(f"[yellow]以下步骤需要 sudo（共 {len(plan.needs_sudo)} 项）：[/yellow]")
        for a in plan.needs_sudo:
            console.print(f"  · {a.description}")
        if not yes and not sys.stdin.isatty():
            err_console.print("非交互环境且未指定 --yes，拒绝执行需要提权的步骤。")
            raise typer.Exit(EXIT_SUDO)

    results = apply_plan(plan)
    ok = True
    for name, status in results:
        mark = "✔" if status.ok else "✗"
        console.print(f"{mark} {name}: {status.detail}")
        if not status.ok:
            ok = False
            if status.hint:
                console.print(f"    修复: {status.hint}")
    if not ok:
        raise typer.Exit(EXIT_FAIL)
    console.print("完成。试试: devproxy up demo -- python3 -m http.server")


@app.command()
def doctor() -> None:
    """体检：检查所有前置条件。"""
    paths, config, _, _ = build_context()
    reports = build_report(_components(paths, config))
    table = Table(show_header=True, header_style="bold")
    table.add_column("组件")
    table.add_column("状态")
    table.add_column("详情")
    table.add_column("修复")
    for r in reports:
        table.add_row(
            r.name,
            "✔ 就绪" if r.status.ok else "✗ 缺失",
            r.status.detail,
            r.status.hint,
        )
    console.print(table)
    if not report_is_healthy(reports):
        raise typer.Exit(EXIT_PRECONDITION)


@app.command()
def trust() -> None:
    """把 CA 根证书装进系统信任库与 NSS 库。"""
    paths, config, _, _ = build_context()
    comp = CertComponent(_platform(), paths, config)
    actions = comp.plan()
    if not actions:
        console.print("CA 根证书已信任，无需变更。")
        raise typer.Exit(EXIT_OK)
    comp.apply(actions)
    console.print("已安装 CA 根证书（Chrome 需完全退出重启后生效）")


@app.command()
def logs(
    service: str = typer.Argument("caddy", help="caddy|dnsmasq"),
) -> None:
    """跟踪服务日志。"""
    paths, config, _, _ = build_context()
    if service not in ("caddy", "dnsmasq"):
        err_console.print(f"devproxy: 未知服务 {service}（可选 caddy / dnsmasq）")
        raise typer.Exit(EXIT_FAIL)
    _platform().tail_service_log(service)
```

同时在 `cli.py` 顶部补充导入：

```python
from dataclasses import replace
```
并新增退出码常量：
```python
EXIT_SUDO = 4
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_component_certs.py tests/test_setup.py tests/test_doctor.py -v
```
Expected: PASS，10 passed

- [ ] **Step 6: 跑全量测试并提交**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest -v
```
Expected: 全部 PASS

```bash
git add src/devproxy/components/certs.py src/devproxy/setup.py src/devproxy/doctor.py src/devproxy/cli.py tests/
git commit -m "✨ feat(devproxy): 证书信任组件与 setup/doctor/trust/logs 编排"
```

---

## Plan 2 完成标准

- [ ] `python3 -m pytest` 全绿
- [ ] `devproxy doctor` 在未装 Caddy 的干净环境上列出全部缺失项，退出码 3
- [ ] `devproxy setup` 在本机跑通后：`https://localhost` 可访问、`*.test` 解析到 127.0.0.1、CA 已被 curl 与 Chrome 信任
- [ ] `devproxy setup` 幂等：连跑两次，第二次显示"所有组件已就绪"
- [ ] `devproxy setup` 只提示一次密码

## 交给 Plan 3 的接口

- `devproxy.platform.base.Platform` / `ElevatedSession` —— Plan 3 的 macOS 实现要满足同一协议
- `devproxy.components.caddy.CaddyComponent` —— Plan 3 的迁移流程要改写它的单元
- `devproxy.setup.build_plan` / `apply_plan` —— Plan 3 的迁移要插在 plan 与 apply 之间
- `devproxy.paths.Paths.backup_dir` —— Plan 3 的备份机制落在那里
