# devproxy 核心网关 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个可用 `uv tool install` 安装的 `devproxy` CLI，把本地服务以 `https://<name>.test` 暴露出来——`up` 包装启动并动态分配端口，`expose` 给已在跑的服务开域名，`down`/`ls` 管理路由表。

**Architecture:** 路由表是唯一真相，存于 `routes.json`；每次变更渲染成 `routes.caddy`（Caddyfile 片段），先 `caddy validate` 再原子落盘再 `caddy reload`。一个路由 = 一个精确站点块 + `tls internal`，因此未注册域名拿不到证书。CLI 用 Typer，路由/渲染/事务三层各自独立可测。

**Tech Stack:** Python ≥ 3.11（`tomllib`）、Typer ≥ 0.12、Rich ≥ 13、pytest ≥ 8、Caddy 2.11.7

**Spec:** `docs/superpowers/specs/2026-10-22-devproxy-local-dev-env-tool-design.md` —— 实施前必读，尤其是 §2.1（S1–S5 实测结论）、§5（路由模型）、§9.1（退出码）

**本计划的范围：** 只做核心网关。基础设施自举（`setup`/`doctor`/组件层/平台层）属于 Plan 2；环境迁移与 macOS 属于 Plan 3。本计划假定 **Caddy 已经在跑**，且其主 Caddyfile 由本工具的 `devproxy config init` 生成（Task 3 提供该能力）。

---

## Global Constraints

以下约束适用于本计划每一个 Task，不再逐条重复：

- **Python 版本下限 3.11**（`tomllib` 进标准库的版本）
- **依赖仅 `typer>=0.12` 与 `rich>=13`**，不引入其他三方库；测试依赖仅 `pytest>=8`
- **包名 `devproxy`，CLI 命令 `devproxy`**，两者一致
- **目录布局**：配置 `~/.config/devproxy/`，运行时状态 `~/.local/state/devproxy/`，Caddy 存储 `~/.local/share/caddy/`（复用现有位置，迁移时 CA 连续）
- **退出码**：0 成功 / 1 运行失败 / 2 参数错误 / 3 前置条件不满足 / 4 需要提权但非交互
- **路由名正则** `\A[a-z0-9][a-z0-9-]*\Z`，长度 ≤ 63，单标签（不含点）
- **TLS 策略**：精确站点块 + `tls internal`，**禁止** `*.test` 通配块、**禁止** on-demand
- **渲染必须确定性**：`render(routes.json)` 对同一输入产出逐字节相同的文本，输出中**不得**含时间戳
- **写文件必须原子**：先写 `.tmp` 再 `os.replace`
- **路由写操作必须持 flock**，锁范围覆盖"改表 → 渲染 → 校验 → 落盘 → reload"整段
- **语言**：代码注释与文档用简体中文，代码标识符用英文

---

## File Structure

```
pyproject.toml                      # 包定义、entry point、pytest 配置
src/devproxy/
├── __init__.py                     # __version__
├── paths.py                        # Paths 数据类 + 目录约定
├── config.py                       # Config 数据类 + TOML 加载
├── cli.py                          # Typer 应用与全部子命令
└── proxy/
    ├── __init__.py
    ├── names.py                    # 路由名校验、域名拼装
    ├── routes.py                   # Route/RouteTable、读写、渲染
    ├── caddy.py                    # Admin API 客户端、validate/reload、主 Caddyfile 生成
    ├── store.py                    # RouteStore：事务性增删改
    ├── runner.py                   # up 的子进程与信号管理
    └── selftest.py                 # 隔离 Caddy 端到端自检
tests/
├── conftest.py                     # paths / store 等 fixture
├── test_paths.py
├── test_config.py
├── test_names.py
├── test_render.py
├── test_caddy.py
├── test_store.py
├── test_runner.py
├── test_cli.py
└── test_integration.py
```

**分工边界**：`routes.py` 只管数据与文本（无 IO 副作用，纯函数 + 两个文件读写）；`store.py` 只管事务（加锁、校验、落盘、reload、回滚）；`caddy.py` 只管与 Caddy 进程交互；`runner.py` 只管子进程生命周期。三者互不直接依赖对方的内部状态。

---

### Task 1: 包骨架、路径与配置

**Files:**
- Create: `pyproject.toml`
- Create: `src/devproxy/__init__.py`
- Create: `src/devproxy/paths.py`
- Create: `src/devproxy/config.py`
- Create: `tests/conftest.py`
- Create: `tests/test_paths.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces:
  - `devproxy.paths.Paths` — frozen dataclass，字段 `config_dir: Path`、`state_dir: Path`、`data_dir: Path`；派生属性 `config_toml` / `caddyfile` / `sites_caddy` / `routes_json` / `routes_caddy` / `lock_path` / `backup_dir` / `log_dir`；方法 `ensure()`、`ensure_files()`
  - `devproxy.paths.default_paths() -> Paths` — 读环境变量 `DEVPROXY_CONFIG_DIR` / `DEVPROXY_STATE_DIR` / `DEVPROXY_DATA_DIR`，缺省走 XDG 约定
  - `devproxy.config.Config` — frozen dataclass，字段 `domain_suffix: str = "test"`、`dns_backend: str = "auto"`、`admin_addr: str = "127.0.0.1:2019"`、`http_port: int = 80`、`https_port: int = 443`、`storage: str = "~/.local/share/caddy"`、`ready_timeout: int = 60`、`caddy_bin: str = "~/.local/bin/caddy"`；派生属性 `admin_base -> str`、`storage_path -> Path`
  - `devproxy.config.ConfigError(Exception)`
  - `devproxy.config.load_config(path: Path | None) -> Config`

- [ ] **Step 1: 写 pyproject.toml**

```toml
[project]
name = "devproxy"
version = "0.1.0"
description = "本地开发环境一键部署与域名网关"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "typer>=0.12",
    "rich>=13",
]

[project.scripts]
devproxy = "devproxy.cli:main"

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/devproxy"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

- [ ] **Step 2: 写失败的测试**

`tests/conftest.py`：

```python
import pytest

from devproxy.paths import Paths


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
```

`tests/test_paths.py`：

```python
from devproxy.paths import Paths, default_paths


def test_derived_paths(tmp_path):
    p = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    assert p.config_toml == tmp_path / "c" / "config.toml"
    assert p.caddyfile == tmp_path / "c" / "Caddyfile"
    assert p.sites_caddy == tmp_path / "c" / "sites.caddy"
    assert p.routes_json == tmp_path / "s" / "routes.json"
    assert p.routes_caddy == tmp_path / "s" / "routes.caddy"
    assert p.lock_path == tmp_path / "s" / "routes.lock"
    assert p.backup_dir == tmp_path / "s" / "backups"
    assert p.log_dir == tmp_path / "s" / "logs"


def test_ensure_creates_config_and_state_only(tmp_path):
    p = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    p.ensure()
    assert p.config_dir.is_dir()
    assert p.state_dir.is_dir()
    assert not p.data_dir.exists(), "data_dir 由 Caddy 自行创建，工具不预建"


def test_ensure_files_creates_import_targets(tmp_path):
    p = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "d")
    p.ensure_files()
    assert p.sites_caddy.is_file()
    assert p.routes_caddy.is_file()
    assert p.sites_caddy.read_text() == ""


def test_default_paths_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVPROXY_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("DEVPROXY_STATE_DIR", str(tmp_path / "st"))
    monkeypatch.setenv("DEVPROXY_DATA_DIR", str(tmp_path / "data"))
    p = default_paths()
    assert p.config_dir == tmp_path / "cfg"
    assert p.state_dir == tmp_path / "st"
    assert p.data_dir == tmp_path / "data"
```

`tests/test_config.py`：

```python
from pathlib import Path

import pytest

from devproxy.config import Config, ConfigError, load_config


def test_defaults_when_file_missing(tmp_path):
    cfg = load_config(tmp_path / "absent.toml")
    assert cfg.domain_suffix == "test"
    assert cfg.dns_backend == "auto"
    assert cfg.admin_addr == "127.0.0.1:2019"
    assert cfg.http_port == 80
    assert cfg.https_port == 443
    assert cfg.ready_timeout == 60
    assert cfg.admin_base == "http://127.0.0.1:2019"


def test_file_overrides_only_named_keys(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('domain_suffix = "dev"\nready_timeout = 5\n', encoding="utf-8")
    cfg = load_config(f)
    assert cfg.domain_suffix == "dev"
    assert cfg.ready_timeout == 5
    assert cfg.http_port == 80, "未在文件里出现的键保持默认值"


def test_unknown_key_raises_config_error(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("nope = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as cm:
        load_config(f)
    assert "nope" in str(cm.value)


def test_storage_path_expands_user():
    assert Config(storage="~/x").storage_path == Path.home() / "x"
```

- [ ] **Step 3: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_paths.py tests/test_config.py -v
```
Expected: 收集阶段报错 —— `ModuleNotFoundError: No module named 'devproxy'`

- [ ] **Step 4: 实现**

`src/devproxy/__init__.py`：

```python
"""devproxy — 本地开发环境一键部署与域名网关。"""

__version__ = "0.1.0"
```

`src/devproxy/paths.py`：

```python
"""目录约定。所有路径集中在此，其他模块不得自行拼路径。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """devproxy 使用的三棵目录树。"""

    config_dir: Path
    state_dir: Path
    data_dir: Path

    @property
    def config_toml(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def caddyfile(self) -> Path:
        return self.config_dir / "Caddyfile"

    @property
    def sites_caddy(self) -> Path:
        return self.config_dir / "sites.caddy"

    @property
    def routes_json(self) -> Path:
        return self.state_dir / "routes.json"

    @property
    def routes_caddy(self) -> Path:
        return self.state_dir / "routes.caddy"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "routes.lock"

    @property
    def backup_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    def ensure(self) -> None:
        """创建配置与状态目录。data_dir 交给 Caddy 自行创建。"""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def ensure_files(self) -> None:
        """创建 Caddyfile import 目标必须存在的空文件。"""
        self.ensure()
        for f in (self.sites_caddy, self.routes_caddy):
            f.touch(exist_ok=True)


def default_paths() -> Paths:
    """默认路径；环境变量可整体覆盖（测试与多实例场景用）。"""
    home = Path.home()
    config_dir = Path(
        os.environ.get("DEVPROXY_CONFIG_DIR", home / ".config" / "devproxy")
    )
    state_dir = Path(
        os.environ.get("DEVPROXY_STATE_DIR", home / ".local" / "state" / "devproxy")
    )
    data_dir = Path(
        os.environ.get("DEVPROXY_DATA_DIR", home / ".local" / "share" / "caddy")
    )
    return Paths(config_dir=config_dir, state_dir=state_dir, data_dir=data_dir)
```

`src/devproxy/config.py`：

```python
"""配置加载。优先级：CLI 参数 > config.toml > 内置默认值。"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """配置文件非法。"""


@dataclass(frozen=True)
class Config:
    domain_suffix: str = "test"
    dns_backend: str = "auto"
    admin_addr: str = "127.0.0.1:2019"
    http_port: int = 80
    https_port: int = 443
    storage: str = "~/.local/share/caddy"
    ready_timeout: int = 60
    caddy_bin: str = "~/.local/bin/caddy"

    @property
    def admin_base(self) -> str:
        return f"http://{self.admin_addr}"

    @property
    def storage_path(self) -> Path:
        return Path(self.storage).expanduser()

    @property
    def caddy_bin_path(self) -> Path:
        return Path(self.caddy_bin).expanduser()


def load_config(path: Path | None = None) -> Config:
    """读取 TOML 配置；文件不存在时返回全默认值。未知键直接报错。"""
    if path is None or not path.exists():
        return Config()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in dataclasses.fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"未知配置项: {', '.join(unknown)}")
    return Config(**{k: v for k, v in data.items() if k in known})
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_paths.py tests/test_config.py -v
```
Expected: PASS，8 passed

- [ ] **Step 6: 提交**

```bash
git add pyproject.toml src/devproxy/__init__.py src/devproxy/paths.py src/devproxy/config.py tests/conftest.py tests/test_paths.py tests/test_config.py
git commit -m "✨ feat(devproxy): 包骨架、路径约定与配置加载"
```

---

### Task 2: 路由模型（数据与渲染）

**Files:**
- Create: `src/devproxy/proxy/__init__.py`
- Create: `src/devproxy/proxy/names.py`
- Create: `src/devproxy/proxy/routes.py`
- Create: `tests/test_names.py`
- Create: `tests/test_render.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `devproxy.proxy.names.NAME_RE`、`MAX_NAME_LEN = 63`
  - `devproxy.proxy.names.InvalidNameError(ValueError)` — 属性 `name`、`reason`
  - `devproxy.proxy.names.validate_name(name: str) -> str` — 合法则原样返回，非法抛 `InvalidNameError`
  - `devproxy.proxy.names.domain_for(name: str, suffix: str) -> str` — 返回 `f"{name}.{suffix.lstrip('.')}"`
  - `devproxy.proxy.routes.Route` — dataclass，字段 `name: str`、`domain: str`、`port: int`、`mode: str`、`upstream: str`、`command: list[str] | None = None`、`pid: int | None = None`、`created_at: str = ""`；方法 `to_dict() -> dict`、`from_dict(d: dict) -> Route`（classmethod）
  - `devproxy.proxy.routes.RouteTable` — dataclass，字段 `version: int = 1`、`domain_suffix: str = "test"`、`routes: list[Route]`；方法 `get(name) -> Route | None`、`upsert(route: Route) -> None`、`remove(name: str) -> bool`、`to_dict() -> dict`、`from_dict(d: dict) -> RouteTable`（classmethod）
  - `devproxy.proxy.routes.RENDER_HEADER` — `str`
  - `devproxy.proxy.routes.load_table(path: Path) -> RouteTable` — 文件不存在时返回空表
  - `devproxy.proxy.routes.save_table(path: Path, table: RouteTable) -> None` — 原子写
  - `devproxy.proxy.routes.atomic_write(path: Path, content: str) -> None` — 原子写文本
  - `devproxy.proxy.routes.render_caddyfile(table: RouteTable) -> str` — 纯函数，确定性

- [ ] **Step 1: 写失败的测试**

`tests/test_names.py`：

```python
import pytest

from devproxy.proxy.names import InvalidNameError, domain_for, validate_name


@pytest.mark.parametrize("name", ["codeup", "codeup-web", "a1-b2-c3", "x", "x" * 63])
def test_valid_names_pass(name):
    assert validate_name(name) == name


@pytest.mark.parametrize(
    "name,reason",
    [
        ("Codeup", "大写"),
        ("a.b", "含点"),
        ("-x", "首字符连字符"),
        ("", "空"),
        ("a_b", "下划线"),
        ("a b", "空格"),
        ("x" * 64, "超长"),
        ("codeup\n", "尾随换行"),
    ],
)
def test_invalid_names_raise(name, reason):
    with pytest.raises(InvalidNameError) as cm:
        validate_name(name)
    assert cm.value.name == name
    assert cm.value.reason


def test_domain_for_strips_leading_dot():
    assert domain_for("codeup", ".test") == "codeup.test"
    assert domain_for("codeup", "test") == "codeup.test"


def test_domain_for_validates_name():
    with pytest.raises(InvalidNameError):
        domain_for("bad.name", "test")
```

`tests/test_render.py`：

```python
import json

from devproxy.proxy.routes import (
    Route,
    RouteTable,
    atomic_write,
    load_table,
    render_caddyfile,
    save_table,
)


def _route(name="codeup", port=42133, mode="up"):
    return Route(
        name=name,
        domain=f"{name}.test",
        port=port,
        mode=mode,
        upstream=f"127.0.0.1:{port}",
    )


def test_render_empty_table_is_header_only():
    out = render_caddyfile(RouteTable())
    assert out == "# 由 devproxy 自动生成 —— 请勿手工编辑\n"


def test_render_single_route():
    out = render_caddyfile(RouteTable(routes=[_route()]))
    assert out == (
        "# 由 devproxy 自动生成 —— 请勿手工编辑\n"
        "\n"
        "codeup.test {\n"
        "\ttls internal\n"
        "\treverse_proxy 127.0.0.1:42133\n"
        "}\n"
    )


def test_render_two_routes_separated_by_blank_line():
    out = render_caddyfile(RouteTable(routes=[_route(), _route("legacy", 3000, "expose")]))
    assert out.count("tls internal") == 2
    assert "\n}\n\nlegacy.test {" in out


def test_render_is_deterministic():
    table = RouteTable(routes=[_route()])
    assert render_caddyfile(table) == render_caddyfile(table)


def test_render_contains_no_timestamp():
    out = render_caddyfile(RouteTable(routes=[_route()]))
    assert "生成于" not in out, "渲染必须确定性，不能含时间戳"


def test_route_roundtrip():
    r = _route()
    r.command = ["sh", "-c", "npx vite"]
    r.pid = 12345
    assert Route.from_dict(r.to_dict()) == r


def test_table_roundtrip():
    t = RouteTable(routes=[_route()], domain_suffix="test")
    assert RouteTable.from_dict(t.to_dict()) == t


def test_table_upsert_replaces_same_name():
    t = RouteTable(routes=[_route(port=1)])
    t.upsert(_route(port=2))
    assert len(t.routes) == 1
    assert t.routes[0].port == 2


def test_table_remove_returns_whether_removed():
    t = RouteTable(routes=[_route()])
    assert t.remove("codeup") is True
    assert t.remove("codeup") is False


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "routes.json"
    t = RouteTable(routes=[_route()])
    save_table(p, t)
    assert load_table(p) == t


def test_load_missing_file_returns_empty_table(tmp_path):
    t = load_table(tmp_path / "absent.json")
    assert t.routes == []
    assert t.version == 1


def test_atomic_write_leaves_no_tmp_file(tmp_path):
    p = tmp_path / "x.txt"
    atomic_write(p, "hello")
    assert p.read_text() == "hello"
    assert list(tmp_path.iterdir()) == [p]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_names.py tests/test_render.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'devproxy.proxy'`

- [ ] **Step 3: 实现**

`src/devproxy/proxy/__init__.py`：空文件。

`src/devproxy/proxy/names.py`：

```python
"""路由名校验与域名拼装。"""

from __future__ import annotations

import re

# 用 \A / \Z 而非 ^ / $ —— $ 也会匹配末尾换行之前，"codeup\n" 会被误放行
NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")
MAX_NAME_LEN = 63


class InvalidNameError(ValueError):
    def __init__(self, name: str, reason: str):
        super().__init__(f"非法路由名 {name!r}: {reason}")
        self.name = name
        self.reason = reason


def validate_name(name: str) -> str:
    """合法则原样返回；非法抛 InvalidNameError。"""
    if not name:
        raise InvalidNameError(name, "不得为空")
    if len(name) > MAX_NAME_LEN:
        raise InvalidNameError(name, f"长度不得超过 {MAX_NAME_LEN}")
    if not NAME_RE.match(name):
        raise InvalidNameError(name, "限单标签 [a-z0-9-]，首字符为字母或数字，不得含点")
    return name


def domain_for(name: str, suffix: str) -> str:
    """<name>.<suffix>；suffix 前导点会被去掉。"""
    validate_name(name)
    return f"{name}.{suffix.lstrip('.')}"
```

`src/devproxy/proxy/routes.py`：

```python
"""路由表：数据模型、持久化与 Caddyfile 渲染。

渲染是纯函数且确定性 —— 同一张表永远产出逐字节相同的文本。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

TABLE_VERSION = 1
RENDER_HEADER = "# 由 devproxy 自动生成 —— 请勿手工编辑\n"


@dataclass
class Route:
    """一条路由 = 一个精确站点块。"""

    name: str
    domain: str
    port: int
    mode: str  # "up" | "expose"
    upstream: str
    command: list[str] | None = None
    pid: int | None = None
    created_at: str = ""

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "domain": self.domain,
            "port": self.port,
            "mode": self.mode,
            "upstream": self.upstream,
        }
        if self.command is not None:
            d["command"] = self.command
        if self.pid is not None:
            d["pid"] = self.pid
        if self.created_at:
            d["created_at"] = self.created_at
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Route":
        return cls(
            name=d["name"],
            domain=d["domain"],
            port=int(d["port"]),
            mode=d["mode"],
            upstream=d["upstream"],
            command=list(d["command"]) if d.get("command") is not None else None,
            pid=int(d["pid"]) if d.get("pid") is not None else None,
            created_at=d.get("created_at", ""),
        )


@dataclass
class RouteTable:
    version: int = TABLE_VERSION
    domain_suffix: str = "test"
    routes: list[Route] = field(default_factory=list)

    def get(self, name: str) -> Route | None:
        return next((r for r in self.routes if r.name == name), None)

    def upsert(self, route: Route) -> None:
        """同名就地替换。"""
        for i, r in enumerate(self.routes):
            if r.name == route.name:
                self.routes[i] = route
                return
        self.routes.append(route)

    def remove(self, name: str) -> bool:
        before = len(self.routes)
        self.routes = [r for r in self.routes if r.name != name]
        return len(self.routes) < before

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "domain_suffix": self.domain_suffix,
            "routes": [r.to_dict() for r in self.routes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RouteTable":
        return cls(
            version=int(d.get("version", TABLE_VERSION)),
            domain_suffix=d.get("domain_suffix", "test"),
            routes=[Route.from_dict(r) for r in d.get("routes", [])],
        )


def atomic_write(path: Path, content: str) -> None:
    """先写临时文件再 os.replace —— 避免读到半截内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def load_table(path: Path) -> RouteTable:
    if not path.exists():
        return RouteTable()
    return RouteTable.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_table(path: Path, table: RouteTable) -> None:
    atomic_write(path, json.dumps(table.to_dict(), ensure_ascii=False, indent=2) + "\n")


def render_caddyfile(table: RouteTable) -> str:
    """渲染成 Caddyfile 片段：一个路由 = 一个精确站点块。

    刻意不用通配块与 on-demand —— 精确块让未注册域名拿不到证书。
    """
    blocks = [
        f"{r.domain} {{\n\ttls internal\n\treverse_proxy {r.upstream}\n}}"
        for r in table.routes
    ]
    if not blocks:
        return RENDER_HEADER
    return RENDER_HEADER + "\n" + "\n\n".join(blocks) + "\n"
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_names.py tests/test_render.py -v
```
Expected: PASS，全部通过

- [ ] **Step 5: 提交**

```bash
git add src/devproxy/proxy/ tests/test_names.py tests/test_render.py
git commit -m "✨ feat(devproxy): 路由数据模型与确定性 Caddyfile 渲染"
```

---

### Task 3: Caddy 集成层

**Files:**
- Create: `src/devproxy/proxy/caddy.py`
- Create: `tests/test_caddy.py`

**Interfaces:**
- Consumes: `devproxy.config.Config`（`admin_addr`、`storage_path`）
- Produces:
  - `devproxy.proxy.caddy.CaddyError(RuntimeError)`
  - `devproxy.proxy.caddy.CaddyAdmin` — dataclass，字段 `base: str = "http://127.0.0.1:2019"`、`timeout: float = 5.0`；方法 `api(method, path, body=None) -> tuple[int, object]`（HTTP 错误返回 `(code, None)`，连接失败返回 `(0, None)`）、`is_alive() -> bool`
  - `devproxy.proxy.caddy.CaddyConfig` — dataclass，字段 `caddy_bin: Path`、`caddyfile: Path`、`admin_addr: str = "127.0.0.1:2019"`；方法 `validate(caddyfile: Path | None = None) -> tuple[bool, str]`、`reload(caddyfile: Path | None = None) -> tuple[bool, str]`
  - `devproxy.proxy.caddy.build_main_caddyfile(*, admin_addr: str, storage: Path, routes_caddy: Path, sites_caddy: Path) -> str`

- [ ] **Step 1: 写失败的测试**

`tests/test_caddy.py`：

```python
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from devproxy.proxy.caddy import (
    CaddyAdmin,
    CaddyConfig,
    build_main_caddyfile,
)


class _Quiet(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _stub_admin():
    srv = _Quiet(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_api_get_returns_200_and_body():
    srv, base = _stub_admin()
    try:
        code, body = CaddyAdmin(base=base).api("GET", "/config/")
        assert code == 200
        assert body == {"ok": True}
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_returns_zero_when_unreachable():
    code, body = CaddyAdmin(base="http://127.0.0.1:1", timeout=0.5).api("GET", "/config/")
    assert code == 0
    assert body is None


def test_is_alive_true_and_false():
    srv, base = _stub_admin()
    try:
        assert CaddyAdmin(base=base).is_alive() is True
    finally:
        srv.shutdown()
        srv.server_close()
    assert CaddyAdmin(base="http://127.0.0.1:1", timeout=0.5).is_alive() is False


def test_validate_uses_caddy_binary(tmp_path):
    """用假 caddy 脚本验证参数拼装与返回码处理。"""
    fake = tmp_path / "caddy"
    fake.write_text('#!/bin/sh\necho "ARGS: $@" >&2\nexit 0\n')
    fake.chmod(0o755)
    cfg = CaddyConfig(caddy_bin=fake, caddyfile=tmp_path / "Caddyfile")
    ok, msg = cfg.validate(tmp_path / "Caddyfile")
    assert ok is True
    assert "validate" in msg and "--config" in msg


def test_validate_reports_failure(tmp_path):
    fake = tmp_path / "caddy"
    fake.write_text('#!/bin/sh\necho "boom" >&2\nexit 1\n')
    fake.chmod(0o755)
    cfg = CaddyConfig(caddy_bin=fake, caddyfile=tmp_path / "Caddyfile")
    ok, msg = cfg.validate(tmp_path / "Caddyfile")
    assert ok is False
    assert "boom" in msg


def test_reload_passes_address(tmp_path):
    fake = tmp_path / "caddy"
    fake.write_text('#!/bin/sh\necho "ARGS: $@" >&2\nexit 0\n')
    fake.chmod(0o755)
    cfg = CaddyConfig(caddy_bin=fake, caddyfile=tmp_path / "Caddyfile", admin_addr="127.0.0.1:2019")
    ok, msg = cfg.reload(tmp_path / "Caddyfile")
    assert ok is True
    assert "reload" in msg
    assert "--address" in msg and "127.0.0.1:2019" in msg


def test_build_main_caddyfile_shape(tmp_path):
    out = build_main_caddyfile(
        admin_addr="127.0.0.1:2019",
        storage=Path("/data/caddy"),
        routes_caddy=Path("/state/routes.caddy"),
        sites_caddy=Path("/config/sites.caddy"),
    )
    assert "admin 127.0.0.1:2019" in out
    assert "storage file_system /data/caddy" in out
    assert "import /state/routes.caddy" in out
    assert "import /config/sites.caddy" in out
    assert out.count("{") == 1, "全局块只能有一个（被 import 的文件不能再有全局块）"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_caddy.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'devproxy.proxy.caddy'`

- [ ] **Step 3: 实现**

`src/devproxy/proxy/caddy.py`：

```python
"""与 Caddy 交互：Admin API 客户端、配置校验、热加载、主 Caddyfile 生成。"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class CaddyError(RuntimeError):
    """Caddy 交互失败：不可达、配置非法或 reload 失败。"""


@dataclass
class CaddyAdmin:
    """Caddy Admin API 客户端。"""

    base: str = "http://127.0.0.1:2019"
    timeout: float = 5.0

    def api(self, method: str, path: str, body: object | None = None) -> tuple[int, object]:
        """HTTP 错误返回 (code, None)；连接失败返回 (0, None)。"""
        url = self.base.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, OSError):
            return 0, None

    def is_alive(self) -> bool:
        code, _ = self.api("GET", "/config/")
        return code == 200


@dataclass
class CaddyConfig:
    """主 Caddyfile 的校验与热加载。"""

    caddy_bin: Path
    caddyfile: Path
    admin_addr: str = "127.0.0.1:2019"

    def validate(self, caddyfile: Path | None = None) -> tuple[bool, str]:
        path = caddyfile or self.caddyfile
        proc = subprocess.run(
            [str(self.caddy_bin), "validate", "--config", str(path)],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()

    def reload(self, caddyfile: Path | None = None) -> tuple[bool, str]:
        path = caddyfile or self.caddyfile
        proc = subprocess.run(
            [
                str(self.caddy_bin),
                "reload",
                "--config",
                str(path),
                "--address",
                self.admin_addr,
            ],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def build_main_caddyfile(
    *,
    admin_addr: str,
    storage: Path,
    routes_caddy: Path,
    sites_caddy: Path,
) -> str:
    """生成主 Caddyfile：唯一全局块 + 两个 import。

    被 import 的文件不能含全局块 —— 全局块只能有一个。
    """
    return (
        "{\n"
        f"\tadmin {admin_addr}\n"
        f"\tstorage file_system {storage}\n"
        "}\n"
        "\n"
        f"import {routes_caddy}\n"
        f"import {sites_caddy}\n"
    )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_caddy.py -v
```
Expected: PASS，7 passed

- [ ] **Step 5: 提交**

```bash
git add src/devproxy/proxy/caddy.py tests/test_caddy.py
git commit -m "✨ feat(devproxy): Caddy Admin 客户端与配置校验/热加载"
```

---

### Task 4: 事务性路由仓库

**Files:**
- Create: `src/devproxy/proxy/store.py`
- Create: `tests/test_store.py`

**Interfaces:**
- Consumes: `devproxy.paths.Paths`、`devproxy.config.Config`、`devproxy.proxy.caddy.CaddyConfig` / `build_main_caddyfile`、`devproxy.proxy.routes.{Route, RouteTable, load_table, render_caddyfile, save_table, atomic_write}`
- Produces:
  - `devproxy.proxy.store.file_lock(path: Path)` — contextmanager，`flock(LOCK_EX)`
  - `devproxy.proxy.store.RouteStore` — 构造 `(paths: Paths, config: Config, caddy: CaddyConfig)`；方法 `list() -> list[Route]`、`get(name: str) -> Route | None`、`mutate(fn: Callable[[RouteTable], None]) -> list[Route]`
  - `devproxy.proxy.store.RouteStoreError(RuntimeError)` — 事务失败（校验或 reload 失败）时抛出，消息含失败原因

- [ ] **Step 1: 写失败的测试**

`tests/test_store.py`：

```python
import json

import pytest

from devproxy.config import Config
from devproxy.proxy.caddy import CaddyConfig
from devproxy.proxy.routes import Route
from devproxy.proxy.store import RouteStore, RouteStoreError


class FakeCaddy:
    """假 Caddy：记录调用，validate/reload 可编排成功或失败。"""

    def __init__(self, validate_ok=True, reload_ok=True):
        self.validate_ok = validate_ok
        self.reload_ok = reload_ok
        self.validated = []
        self.reloaded = []

    def validate(self, caddyfile=None):
        self.validated.append(caddyfile)
        return (self.validate_ok, "ok" if self.validate_ok else "语法错误")

    def reload(self, caddyfile=None):
        self.reloaded.append(caddyfile)
        return (self.reload_ok, "ok" if self.reload_ok else "reload 失败")


def _store(paths, caddy=None):
    cfg = Config(storage=str(paths.data_dir))
    return RouteStore(paths, cfg, caddy or FakeCaddy())


def _route(name="codeup", port=42133):
    return Route(
        name=name,
        domain=f"{name}.test",
        port=port,
        mode="up",
        upstream=f"127.0.0.1:{port}",
    )


def test_mutate_creates_table_and_caddyfile(paths):
    paths.ensure_files()
    store = _store(paths)
    store.mutate(lambda t: t.upsert(_route()))
    assert paths.routes_json.exists()
    assert paths.routes_caddy.exists()
    assert "codeup.test" in paths.routes_caddy.read_text()
    assert json.loads(paths.routes_json.read_text())["routes"][0]["name"] == "codeup"


def test_list_returns_routes(paths):
    paths.ensure_files()
    store = _store(paths)
    store.mutate(lambda t: t.upsert(_route()))
    assert [r.name for r in store.list()] == ["codeup"]


def test_get_returns_none_for_unknown(paths):
    paths.ensure_files()
    assert _store(paths).get("nope") is None


def test_mutate_validates_before_writing(paths):
    """校验失败时不得落盘。"""
    paths.ensure_files()
    store = _store(paths, FakeCaddy(validate_ok=False))
    with pytest.raises(RouteStoreError) as cm:
        store.mutate(lambda t: t.upsert(_route()))
    assert "校验" in str(cm.value)
    assert not paths.routes_json.exists()
    assert paths.routes_caddy.read_text() == ""


def test_mutate_rolls_back_on_reload_failure(paths):
    paths.ensure_files()
    store = _store(paths, FakeCaddy(reload_ok=False))
    with pytest.raises(RouteStoreError) as cm:
        store.mutate(lambda t: t.upsert(_route()))
    assert "reload" in str(cm.value)
    assert not paths.routes_json.exists(), "reload 失败必须回滚路由表"
    assert paths.routes_caddy.read_text() == ""


def test_mutate_rolls_back_to_previous_state(paths):
    paths.ensure_files()
    good = _store(paths)
    good.mutate(lambda t: t.upsert(_route("first", 1111)))
    before = paths.routes_json.read_text()

    bad = _store(paths, FakeCaddy(reload_ok=False))
    with pytest.raises(RouteStoreError):
        bad.mutate(lambda t: t.upsert(_route("second", 2222)))
    assert paths.routes_json.read_text() == before
    assert "second.test" not in paths.routes_caddy.read_text()


def test_mutate_creates_import_targets_if_missing(paths):
    """sites.caddy 不存在时不能让 Caddy 的 import 失败。"""
    store = _store(paths)
    store.mutate(lambda t: t.upsert(_route()))
    assert paths.sites_caddy.exists()


def test_validate_uses_temp_caddyfile_not_the_live_one(paths):
    """校验必须针对新内容，不能直接用线上主 Caddyfile。"""
    paths.ensure_files()
    caddy = FakeCaddy()
    store = _store(paths, caddy)
    store.mutate(lambda t: t.upsert(_route()))
    validated = caddy.validated[0]
    assert validated != paths.caddyfile
    assert validated.name == "Caddyfile.new"


def test_lock_file_created(paths):
    paths.ensure_files()
    _store(paths).mutate(lambda t: None)
    assert paths.lock_path.exists()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_store.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'devproxy.proxy.store'`

- [ ] **Step 3: 实现**

`src/devproxy/proxy/store.py`：

```python
"""路由表的事务性写入口。

事务顺序固定为：加锁 → 改表 → 渲染 → 校验 → 落盘 → reload。
校验在落盘之前，reload 失败则回滚落盘内容 —— 保证 Caddy 永远不因本工具而挂掉。
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.proxy.caddy import CaddyConfig, build_main_caddyfile
from devproxy.proxy.routes import (
    Route,
    RouteTable,
    atomic_write,
    load_table,
    render_caddyfile,
    save_table,
)


class RouteStoreError(RuntimeError):
    """事务失败。"""


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """flock 互斥。持锁范围必须覆盖整个事务。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fh.close()


class RouteStore:
    def __init__(self, paths: Paths, config: Config, caddy: CaddyConfig):
        self.paths = paths
        self.config = config
        self.caddy = caddy

    def list(self) -> list[Route]:
        return load_table(self.paths.routes_json).routes

    def get(self, name: str) -> Route | None:
        return next((r for r in self.list() if r.name == name), None)

    def mutate(self, fn: Callable[[RouteTable], None]) -> list[Route]:
        paths = self.paths
        paths.ensure_files()
        with file_lock(paths.lock_path):
            old_json = (
                paths.routes_json.read_text(encoding="utf-8")
                if paths.routes_json.exists()
                else None
            )
            old_caddy = (
                paths.routes_caddy.read_text(encoding="utf-8")
                if paths.routes_caddy.exists()
                else None
            )

            table = load_table(paths.routes_json)
            fn(table)
            rendered = render_caddyfile(table)

            ok, msg = self._validate(rendered)
            if not ok:
                raise RouteStoreError(f"Caddy 配置校验失败，未做任何修改：\n{msg}")

            save_table(paths.routes_json, table)
            atomic_write(paths.routes_caddy, rendered)

            ok, msg = self.caddy.reload()
            if not ok:
                self._restore(old_json, old_caddy)
                raise RouteStoreError(f"Caddy reload 失败，已回滚路由表：\n{msg}")
            return table.routes

    def _validate(self, rendered: str) -> tuple[bool, str]:
        """用临时主 Caddyfile 校验新内容，不动线上文件。"""
        tmp_routes = self.paths.state_dir / "routes.caddy.new"
        tmp_main = self.paths.state_dir / "Caddyfile.new"
        atomic_write(tmp_routes, rendered)
        atomic_write(
            tmp_main,
            build_main_caddyfile(
                admin_addr=self.config.admin_addr,
                storage=self.config.storage_path,
                routes_caddy=tmp_routes,
                sites_caddy=self.paths.sites_caddy,
            ),
        )
        return self.caddy.validate(tmp_main)

    def _restore(self, old_json: str | None, old_caddy: str | None) -> None:
        if old_json is None:
            self.paths.routes_json.unlink(missing_ok=True)
        else:
            atomic_write(self.paths.routes_json, old_json)
        if old_caddy is None:
            self.paths.routes_caddy.unlink(missing_ok=True)
        else:
            atomic_write(self.paths.routes_caddy, old_caddy)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_store.py -v
```
Expected: PASS，9 passed

- [ ] **Step 5: 提交**

```bash
git add src/devproxy/proxy/store.py tests/test_store.py
git commit -m "✨ feat(devproxy): 事务性路由仓库（校验→落盘→reload→回滚）"
```

---

### Task 5: up —— 子进程管理与信号

**Files:**
- Create: `src/devproxy/proxy/runner.py`
- Create: `tests/test_runner.py`

**Interfaces:**
- Consumes: `devproxy.config.Config`（`ready_timeout`）、`devproxy.proxy.names.{validate_name, domain_for}`、`devproxy.proxy.routes.Route`、`devproxy.proxy.store.RouteStore`、`devproxy.proxy.caddy.CaddyAdmin`
- Produces:
  - `devproxy.proxy.runner.alloc_port() -> int`
  - `devproxy.proxy.runner.reachable(port: int, timeout: float = 0.5) -> bool`
  - `devproxy.proxy.runner.stop_process_group(proc, pgid: int | None = None, sig: int = signal.SIGTERM) -> None`
  - `devproxy.proxy.runner.kill_pid_group(pid: int) -> None` — 按进程组杀掉残留子进程（pid == pgid）
  - `devproxy.proxy.runner.HEARTBEAT_INTERVAL` — `float`，默认 `30.0`；`UpRunner.heartbeat_interval` 类属性可覆盖（测试用）
  - `devproxy.proxy.runner.UpError(RuntimeError)`
  - `devproxy.proxy.runner.UpRunner` — 构造 `(store: RouteStore, config: Config, admin: CaddyAdmin)`；类属性 `heartbeat_interval: float = HEARTBEAT_INTERVAL`；方法 `run(name, cmd, port=None, ready_timeout=None, on_ready=None) -> int`（返回子进程退出码）、`_watch(route, stop) -> None`

- [ ] **Step 1: 写失败的测试**

`tests/test_runner.py`：

```python
import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from devproxy.config import Config
from devproxy.proxy.caddy import CaddyAdmin
from devproxy.proxy.runner import (
    UpError,
    UpRunner,
    alloc_port,
    reachable,
    stop_process_group,
)
from devproxy.proxy.store import RouteStore


class FakeCaddy:
    """假 Caddy：validate/reload 恒成功，并记录 reload 次数。"""

    def __init__(self):
        self.reloads = 0

    def validate(self, caddyfile=None):
        return True, "ok"

    def reload(self, caddyfile=None):
        self.reloads += 1
        return True, "ok"


class FakeAdmin:
    """假 Admin API 客户端。config_payload 模拟 Caddy 当前配置。"""

    def __init__(self, alive=True):
        self.alive = alive
        self.config_payload = {}

    def is_alive(self):
        return self.alive

    def api(self, method, path, body=None):
        return 200, self.config_payload


def _runner(paths, alive=True):
    cfg = Config(storage=str(paths.data_dir))
    store = RouteStore(paths, cfg, FakeCaddy())
    return UpRunner(store, cfg, FakeAdmin(alive=alive))


# 起一个真正监听 $PORT 的子进程，并在 0.5s 后退出 —— 同时验证 PORT 注入
_LISTEN_AND_EXIT = (
    "import http.server,os,threading,time;"
    "s=http.server.HTTPServer(('127.0.0.1',int(os.environ['PORT'])),"
    "http.server.SimpleHTTPRequestHandler);"
    "threading.Thread(target=s.serve_forever,daemon=True).start();"
    "time.sleep(0.5)"
)


def _process_running(pid: int) -> bool:
    """僵尸进程不算活着。/proc 不存在时退回 kill(pid,0) 探测。"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False


def test_alloc_port_returns_bindable_port():
    port = alloc_port()
    assert 1024 < port < 65536
    s = socket.socket()
    s.bind(("127.0.0.1", port))
    s.close()


def test_reachable_true_and_false():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert reachable(port, timeout=1.0) is True
    finally:
        s.close()
    assert reachable(port, timeout=0.2) is False


def test_stop_process_group_kills_grandchildren(tmp_path):
    """子进程 fork 出孙进程后，stop_process_group 必须按组清理干净。"""
    marker = tmp_path / "grandchild.pid"
    script = tmp_path / "spawn.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"(sleep 60 & echo $! > {marker})\n"
        "sleep 60\n"
    )
    script.chmod(0o755)
    proc = subprocess.Popen(
        [str(script)], start_new_session=True, stdout=subprocess.DEVNULL
    )
    deadline = time.time() + 5
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), "孙进程 pid 未写出"
    gpid = int(marker.read_text().strip())
    stop_process_group(proc, proc.pid)
    time.sleep(0.5)
    assert not _process_running(gpid), "孙进程应已被按组清理"


def test_run_requires_caddy_alive(paths):
    paths.ensure_files()
    runner = _runner(paths, alive=False)
    with pytest.raises(UpError) as cm:
        runner.run("codeup", [sys.executable, "-c", "import time; time.sleep(1)"])
    assert "Admin API" in str(cm.value)


def test_run_rejects_occupied_explicit_port(paths):
    paths.ensure_files()
    runner = _runner(paths)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        with pytest.raises(UpError) as cm:
            runner.run("codeup", [sys.executable, "-c", "pass"], port=port)
        assert "已被占用" in str(cm.value)
    finally:
        s.close()


def test_run_rejects_invalid_name(paths):
    paths.ensure_files()
    runner = _runner(paths)
    with pytest.raises(UpError):
        runner.run("bad.name", [sys.executable, "-c", "pass"])


def test_run_rejects_invalid_port(paths):
    paths.ensure_files()
    runner = _runner(paths)
    with pytest.raises(UpError):
        runner.run("codeup", [sys.executable, "-c", "pass"], port=0)


def test_run_injects_port_and_registers_route(paths):
    """子进程应拿到 PORT 环境变量；就绪后注册路由。"""
    paths.ensure_files()
    runner = _runner(paths)
    ready = []
    rc = runner.run(
        "codeup",
        [sys.executable, "-c", _LISTEN_AND_EXIT],
        on_ready=lambda domain, port: ready.append((domain, port)),
    )
    assert rc == 0
    assert ready and ready[0][0] == "codeup.test"
    table = json.loads(paths.routes_json.read_text())
    assert table["routes"][0]["name"] == "codeup"
    assert table["routes"][0]["mode"] == "up"
    assert table["routes"][0]["port"] == ready[0][1]
    assert table["routes"][0]["command"][0] == sys.executable


def test_run_cleans_up_route_when_child_exits(paths):
    """子进程退出后路由必须被注销。"""
    paths.ensure_files()
    runner = _runner(paths)
    runner.run("codeup", [sys.executable, "-c", _LISTEN_AND_EXIT])
    table = json.loads(paths.routes_json.read_text())
    assert table["routes"] == [], "子进程退出后路由必须注销"


def test_run_kills_process_on_ready_timeout(paths):
    paths.ensure_files()
    runner = _runner(paths)
    with pytest.raises(UpError) as cm:
        runner.run(
            "slow",
            [sys.executable, "-c", "import time; time.sleep(30)"],
            ready_timeout=1,
        )
    assert "就绪超时" in str(cm.value)
    assert not paths.routes_json.exists(), "超时不得留下路由"


def test_watch_re_registers_when_route_lost_from_caddy(paths):
    """Caddy 里路由丢了、但路由表里还在 → 自检应重新注册。"""
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.1

    route = Route(
        name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1"
    )
    store.mutate(lambda t: t.upsert(route))
    before = caddy.reloads

    admin.config_payload = {}  # Caddy 当前配置里没有它
    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    time.sleep(0.5)
    stop.set()
    t.join(timeout=2)
    assert caddy.reloads > before, "路由丢失时应触发重新注册"


def test_watch_does_not_revive_removed_route(paths):
    """路由表里已经没有它（用户主动 down 过）→ 自检不得复活。"""
    import threading

    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    caddy = FakeCaddy()
    store = RouteStore(paths, cfg, caddy)
    admin = FakeAdmin()
    runner = UpRunner(store, cfg, admin)
    runner.heartbeat_interval = 0.1

    route = Route(
        name="x", domain="x.test", port=1, mode="up", upstream="127.0.0.1:1"
    )
    store.mutate(lambda t: t.upsert(route))
    store.mutate(lambda t: t.remove("x"))
    before = caddy.reloads

    admin.config_payload = {}
    stop = threading.Event()
    t = threading.Thread(target=runner._watch, args=(route, stop), daemon=True)
    t.start()
    time.sleep(0.4)
    stop.set()
    t.join(timeout=2)
    assert caddy.reloads == before, "用户已注销的路由不应被复活"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_runner.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'devproxy.proxy.runner'`

- [ ] **Step 3: 实现**

`src/devproxy/proxy/runner.py`：

```python
"""up 的子进程生命周期：端口分配、启动、就绪等待、信号转发、按组清理。

沿用 devp 的成熟做法：子进程以 start_new_session=True 启动（独立进程组），
信号按组转发，退出时按组 SIGTERM→SIGKILL，确保孙进程不残留。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
from typing import Callable

from devproxy.config import Config
from devproxy.proxy.caddy import CaddyAdmin
from devproxy.proxy.names import domain_for, validate_name
from devproxy.proxy.routes import Route
from devproxy.proxy.store import RouteStore

HEARTBEAT_INTERVAL = 30.0


class UpError(RuntimeError):
    """up 失败。"""


def alloc_port() -> int:
    """绑定 :0 取一个随机空闲端口后立即释放。存在微小时隙竞争。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def reachable(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _route_live(admin: CaddyAdmin, domain: str) -> bool:
    """路由是否真的在 Caddy 当前配置里。

    整个 /config/ 序列化后做子串匹配 —— 免去猜测 Caddy 内部的 server 编号
    （srv0/srv1 取决于 listen 地址的字典序）。
    """
    code, body = admin.api("GET", "/config/")
    if code != 200 or body is None:
        return False
    return domain in json.dumps(body)


def stop_process_group(
    proc: subprocess.Popen | None, pgid: int | None = None, sig: int = signal.SIGTERM
) -> None:
    """向子进程组发信号 → 有限等待 → SIGKILL 进程组，确保孙进程也不残留。

    pgid 在启动时捕获：组长先退出时（launcher 场景）进程组里仍留着孙进程，
    依然要按组发信号 —— 不能因为 proc.poll() 已非 None 就跳过。
    """
    if proc is None:
        return
    if pgid is None:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = proc.pid
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # 组长可能已退出但组内仍有孙进程：再按组补一刀 SIGKILL
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class UpRunner:
    heartbeat_interval = HEARTBEAT_INTERVAL

    def __init__(self, store: RouteStore, config: Config, admin: CaddyAdmin):
        self.store = store
        self.config = config
        self.admin = admin

    def run(
        self,
        name: str,
        cmd: list[str],
        port: int | None = None,
        ready_timeout: int | None = None,
        on_ready: Callable[[str, int], None] | None = None,
    ) -> int:
        try:
            validate_name(name)
        except ValueError as e:
            raise UpError(str(e)) from e

        timeout = ready_timeout if ready_timeout is not None else self.config.ready_timeout
        if port is not None and not (1 <= port <= 65535):
            raise UpError(f"非法端口 {port}：必须在 1..65535 之间")

        if not self.admin.is_alive():
            raise UpError("Caddy Admin API 不可达（caddy 是否在运行？）")

        # 同名路由先清理（旧子进程一并收掉）
        existing = self.store.get(name)
        if existing is not None:
            self.store.mutate(lambda t: t.remove(name))
            if existing.pid:
                kill_pid_group(existing.pid)

        if port is None:
            port = alloc_port()
        elif reachable(port, timeout=0.2):
            raise UpError(f"端口 {port} 已被占用")

        env = dict(os.environ, PORT=str(port))
        stop = threading.Event()
        proc: subprocess.Popen | None = None
        pgid: int | None = None
        watcher: threading.Thread | None = None
        registered = False

        def on_signal(signum, frame):
            # 清理期间收到第二个信号时直接忽略，避免中断 finally 留下残留
            if stop.is_set():
                return
            stop.set()
            if pgid is not None:
                child_sig = signal.SIGINT if signum == signal.SIGINT else signal.SIGTERM
                try:
                    os.killpg(pgid, child_sig)
                except (ProcessLookupError, PermissionError):
                    pass
            raise SystemExit(128 + signum)

        old_handlers = {
            s: signal.signal(s, on_signal)
            for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }
        try:
            try:
                proc = subprocess.Popen(cmd, env=env, start_new_session=True)
                pgid = proc.pid  # start_new_session=True → 子进程是进程组 leader
            except FileNotFoundError:
                raise UpError(f"命令不存在: {cmd[0]}") from None

            deadline = time.time() + timeout
            while time.time() < deadline:
                if proc.poll() is not None:
                    raise UpError(f"子进程先退出（code={proc.returncode}），未注册")
                if reachable(port, timeout=1):
                    break
                time.sleep(0.3)
            else:
                raise UpError(
                    f"就绪超时（{timeout}s）——应用可能未使用 $PORT，可用 --port 显式指定"
                )

            domain = domain_for(name, self.config.domain_suffix)
            route = Route(
                name=name,
                domain=domain,
                port=port,
                mode="up",
                upstream=f"127.0.0.1:{port}",
                command=list(cmd),
                pid=proc.pid,
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            self.store.mutate(lambda t: t.upsert(route))
            registered = True
            watcher = threading.Thread(
                target=self._watch, args=(route, stop), daemon=True
            )
            watcher.start()
            if on_ready is not None:
                on_ready(domain, port)
            rc = proc.wait()
        finally:
            stop.set()
            if watcher is not None:
                watcher.join(timeout=10)
            if registered:
                try:
                    self.store.mutate(lambda t: t.remove(name))
                except Exception:
                    pass
            stop_process_group(proc, pgid)
            for s, handler in old_handlers.items():
                signal.signal(s, handler)
        # 被信号杀死时 wait() 返回 -n；映射成 shell 约定的 128+n
        return 128 - rc if rc < 0 else rc

    def _watch(self, route: Route, stop: threading.Event) -> None:
        """每 heartbeat_interval 秒确认路由还在 Caddy 里。

        路由是文件化的，正常情况下 Caddy 重启会自动加载；这个自检兜住
        "Caddy 是用别的配置启动的" 与 "有人手改了渲染产物" 两种情况。
        路由表里已经没有它 —— 说明用户主动 down 过 —— 就不再复活。
        """
        while not stop.wait(self.heartbeat_interval):
            try:
                if stop.is_set():
                    return
                if _route_live(self.admin, route.domain):
                    continue
                if self.store.get(route.name) is None:
                    return
                self.store.mutate(lambda t: t.upsert(route))
            except Exception:
                # Admin 返回畸形响应等异常不能杀死守护线程
                pass


def kill_pid_group(pid: int) -> None:
    """按进程组杀掉残留的上一次 up 子进程（pid == pgid）。"""
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_runner.py -v
```
Expected: PASS，12 passed

- [ ] **Step 5: 提交**

```bash
git add src/devproxy/proxy/runner.py tests/test_runner.py
git commit -m "✨ feat(devproxy): up 的子进程生命周期与信号处理"
```

---

### Task 6: CLI —— up / expose / down / ls / config

**Files:**
- Create: `src/devproxy/cli.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: `devproxy.paths.default_paths`、`devproxy.config.load_config`、`devproxy.proxy.store.RouteStore`、`devproxy.proxy.caddy.{CaddyAdmin, CaddyConfig, build_main_caddyfile}`、`devproxy.proxy.runner.UpRunner`、`devproxy.proxy.names.{validate_name, domain_for}`、`devproxy.proxy.routes.Route`
- Produces:
  - `devproxy.cli.DevProxyGroup(typer.core.TyperGroup)` — 覆写 `parse_args`，把 `--` 之后的参数原样切给子命令并存入 `ctx.ensure_object(dict)["trailing"]`；同时写入 `["has_sep"]: bool`
  - `devproxy.cli.app` — `typer.Typer` 实例，`cls=DevProxyGroup`
  - `devproxy.cli.build_context(paths=None, config=None) -> tuple[Paths, Config, RouteStore, CaddyAdmin]` — 组装依赖，测试可注入
  - `devproxy.cli.main() -> None` — entry point
  - 子命令：`up`、`expose`、`down`、`ls`、`config`

- [ ] **Step 1: 写失败的测试**

`tests/test_cli.py`：

```python
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import DevProxyGroup, app
from devproxy.config import Config
from devproxy.paths import Paths
from devproxy.proxy.caddy import CaddyAdmin
from devproxy.proxy.routes import Route
from devproxy.proxy.store import RouteStore


class FakeCaddy:
    """假 Caddy：validate/reload 恒成功，避免测试真的去调 caddy 二进制。"""

    def validate(self, caddyfile=None):
        return True, "ok"

    def reload(self, caddyfile=None):
        return True, "ok"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把 devproxy 的三棵目录指向 tmp_path。"""
    monkeypatch.setenv("DEVPROXY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DEVPROXY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DEVPROXY_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def _patch_ctx(monkeypatch, tmp_path):
    """替换 build_context，注入假 Caddy。"""
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    paths.ensure_files()
    cfg = Config(storage=str(paths.data_dir))
    store = RouteStore(paths, cfg, FakeCaddy())
    admin = CaddyAdmin(base="http://127.0.0.1:1")
    monkeypatch.setattr(
        "devproxy.cli.build_context", lambda *a, **k: (paths, cfg, store, admin)
    )
    return paths, cfg, store


def _seed_route(state_dir: Path, name="codeup", port=42133, mode="up"):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "routes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "domain_suffix": "test",
                "routes": [
                    {
                        "name": name,
                        "domain": f"{name}.test",
                        "port": port,
                        "mode": mode,
                        "upstream": f"127.0.0.1:{port}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_separator_is_split_and_passed_through(env, monkeypatch):
    """-- 之后的参数原样交给子命令，不被 devproxy 的选项解析器吞掉。"""
    _patch_ctx(monkeypatch, env)
    captured = {}

    def fake_run(self, name, cmd, port=None, ready_timeout=None, on_ready=None):
        captured["name"] = name
        captured["cmd"] = cmd
        captured["port"] = port
        return 0

    monkeypatch.setattr("devproxy.cli.UpRunner.run", fake_run)
    res = CliRunner().invoke(
        app, ["up", "codeup", "--port", "8080", "--", "npx", "vite", "--port", "3000"]
    )
    assert res.exit_code == 0, res.output
    assert captured["name"] == "codeup"
    assert captured["port"] == 8080
    assert captured["cmd"] == ["npx", "vite", "--port", "3000"]


def test_up_without_separator_is_rejected(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "codeup", "npx", "vite"])
    assert res.exit_code != 0
    assert "--" in res.output


def test_up_without_command_is_rejected(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["up", "codeup", "--"])
    assert res.exit_code != 0


def test_expose_registers_route(env, monkeypatch):
    """expose 是一次性的：注册完立即返回，路由常驻。"""
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["expose", "legacy", "--port", "3000"])
    assert res.exit_code == 0, res.output
    table = json.loads((env / "state" / "routes.json").read_text())
    assert table["routes"][0]["name"] == "legacy"
    assert table["routes"][0]["mode"] == "expose"
    assert table["routes"][0]["domain"] == "legacy.test"


def test_down_removes_route(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    _seed_route(env / "state")
    res = CliRunner().invoke(app, ["down", "codeup"])
    assert res.exit_code == 0, res.output
    table = json.loads((env / "state" / "routes.json").read_text())
    assert table["routes"] == []


def test_down_unknown_name_is_ok(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["down", "nope"])
    assert res.exit_code == 0


def test_down_kills_leftover_process_group(env, monkeypatch):
    """down 要顺带杀掉 mode=up 路由的残留子进程组。"""
    _patch_ctx(monkeypatch, env)
    killed = []
    monkeypatch.setattr("devproxy.cli.kill_pid_group", lambda pid: killed.append(pid))
    _seed_route(env / "state", "codeup", 42133, mode="up")
    p = env / "state" / "routes.json"
    data = json.loads(p.read_text())
    data["routes"][0]["pid"] = 4242
    p.write_text(json.dumps(data))
    res = CliRunner().invoke(app, ["down", "codeup"])
    assert res.exit_code == 0, res.output
    assert killed == [4242]


def test_ls_lists_routes(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    _seed_route(env / "state", "codeup", 42133)
    res = CliRunner().invoke(app, ["ls"])
    assert res.exit_code == 0, res.output
    assert "codeup.test" in res.output


def test_config_shows_effective_config(env, monkeypatch):
    _patch_ctx(monkeypatch, env)
    res = CliRunner().invoke(app, ["config"])
    assert res.exit_code == 0, res.output
    assert "domain_suffix" in res.output
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_cli.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'devproxy.cli'`

- [ ] **Step 3: 实现**

`src/devproxy/cli.py`：

```python
"""devproxy 命令行入口。"""

from __future__ import annotations

import sys
import time

import typer
from rich.console import Console
from rich.table import Table
from typer.core import TyperGroup

from devproxy.config import Config, ConfigError, load_config
from devproxy.paths import Paths, default_paths
from devproxy.proxy.caddy import CaddyAdmin, CaddyConfig
from devproxy.proxy.names import InvalidNameError, domain_for, validate_name
from devproxy.proxy.routes import Route, load_table, render_caddyfile
from devproxy.proxy.runner import UpError, UpRunner, kill_pid_group, reachable
from devproxy.proxy.store import RouteStore, RouteStoreError

console = Console()
err_console = Console(stderr=True)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PRECONDITION = 3


class DevProxyGroup(TyperGroup):
    """把 '--' 之后的参数原样切给子命令。

    Typer 默认会用自己的解析器处理子命令的所有参数，导致子命令的
    --port 之类被 devproxy 抢走。这里在解析前先切分，尾部参数以
    "trailing" 原样放进 ctx.obj，子命令自行取用。
    """

    def parse_args(self, ctx, args):
        has_sep = "--" in args
        if has_sep:
            i = args.index("--")
            head, tail = args[:i], args[i + 1:]
        else:
            head, tail = args, []
        data = ctx.ensure_object(dict)
        data["trailing"] = tail
        data["has_sep"] = has_sep
        return super().parse_args(ctx, head)


app = typer.Typer(
    cls=DevProxyGroup,
    help="本地开发环境一键部署与域名网关",
    no_args_is_help=True,
)


def build_context(
    paths: Paths | None = None, config: Config | None = None
) -> tuple[Paths, Config, RouteStore, CaddyAdmin]:
    """组装依赖。测试可传入自定义 paths/config。"""
    paths = paths or default_paths()
    config = config or load_config(paths.config_toml)
    paths.ensure_files()
    caddy = CaddyConfig(
        caddy_bin=config.caddy_bin_path,
        caddyfile=paths.caddyfile,
        admin_addr=config.admin_addr,
    )
    store = RouteStore(paths, config, caddy)
    admin = CaddyAdmin(base=config.admin_base)
    return paths, config, store, admin


@app.command(context_settings={"ignore_unknown_options": True})
def up(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="路由名（[a-z0-9-]，单标签）"),
    port: int = typer.Option(None, "--port", help="显式端口（默认自动分配）"),
    ready_timeout: int = typer.Option(None, "--ready-timeout", help="就绪超时秒数"),
) -> None:
    """分配端口、启动命令并注册 https://<name>.test。"""
    data = ctx.ensure_object(dict)
    if not data.get("has_sep"):
        err_console.print("缺少 '--'：devproxy up <name> [选项] -- CMD...")
        raise typer.Exit(EXIT_FAIL)
    cmd = list(data.get("trailing") or [])
    if not cmd:
        err_console.print("缺少启动命令：-- 之后需要 CMD...")
        raise typer.Exit(EXIT_FAIL)

    _, config, store, admin = build_context()
    runner = UpRunner(store, config, admin)

    def on_ready(domain: str, allocated: int) -> None:
        console.print(f"✅ https://{domain}  (端口 {allocated})")

    try:
        rc = runner.run(
            name,
            cmd,
            port=port,
            ready_timeout=ready_timeout,
            on_ready=on_ready,
        )
    except (UpError, RouteStoreError, InvalidNameError) as e:
        err_console.print(f"devproxy: {e}")
        raise typer.Exit(EXIT_FAIL) from e
    raise typer.Exit(rc)


@app.command()
def expose(
    name: str = typer.Argument(..., help="路由名"),
    port: int = typer.Option(..., "--port", help="已在上游监听的端口"),
) -> None:
    """给已在运行的服务开一个域名。注册后立即返回，路由常驻。"""
    try:
        validate_name(name)
    except InvalidNameError as e:
        err_console.print(f"devproxy: {e}")
        raise typer.Exit(EXIT_FAIL) from e
    if not (1 <= port <= 65535):
        err_console.print(f"devproxy: 非法端口 {port}：必须在 1..65535 之间")
        raise typer.Exit(EXIT_FAIL)

    _, config, store, _ = build_context()
    domain = domain_for(name, config.domain_suffix)
    route = Route(
        name=name,
        domain=domain,
        port=port,
        mode="expose",
        upstream=f"127.0.0.1:{port}",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    try:
        store.mutate(lambda t: t.upsert(route))
    except RouteStoreError as e:
        err_console.print(f"devproxy: {e}")
        raise typer.Exit(EXIT_FAIL) from e
    if not reachable(port):
        err_console.print(f"⚠️  上游 127.0.0.1:{port} 当前无响应（路由已注册，稍后可访问）")
    console.print(f"✅ https://{domain}  (上游 127.0.0.1:{port})")
    console.print(f"   移除: devproxy down {name}")


@app.command()
def down(name: str = typer.Argument(..., help="路由名")) -> None:
    """注销路由；若该路由的子进程还在，一并收掉。"""
    try:
        validate_name(name)
    except InvalidNameError as e:
        err_console.print(f"devproxy: {e}")
        raise typer.Exit(EXIT_FAIL) from e
    _, _, store, _ = build_context()
    route = store.get(name)
    if route is None:
        console.print(f"（{name} 未注册）")
        raise typer.Exit(EXIT_OK)
    try:
        store.mutate(lambda t: t.remove(name))
    except RouteStoreError as e:
        err_console.print(f"devproxy: {e}")
        raise typer.Exit(EXIT_FAIL) from e
    if route.pid:
        kill_pid_group(route.pid)
    console.print(f"已注销 {name}")


@app.command()
def ls() -> None:
    """列出所有已注册路由。"""
    _, _, store, _ = build_context()
    routes = store.list()
    if not routes:
        console.print("（无路由）")
        raise typer.Exit(EXIT_OK)
    table = Table(show_header=True, header_style="bold")
    table.add_column("NAME")
    table.add_column("DOMAIN")
    table.add_column("PORT")
    table.add_column("MODE")
    table.add_column("UPSTREAM")
    table.add_column("STATUS")
    for r in routes:
        ok = reachable(r.port)
        table.add_row(
            r.name,
            r.domain,
            str(r.port),
            r.mode,
            r.upstream,
            "● reachable" if ok else "⛔ 不可达",
        )
    console.print(table)


@app.command()
def config() -> None:
    """显示当前生效配置。"""
    paths, cfg, store, _ = build_context()
    console.print_json(
        data={
            "domain_suffix": cfg.domain_suffix,
            "dns_backend": cfg.dns_backend,
            "admin_addr": cfg.admin_addr,
            "http_port": cfg.http_port,
            "https_port": cfg.https_port,
            "storage": cfg.storage,
            "ready_timeout": cfg.ready_timeout,
            "paths": {
                "config_dir": str(paths.config_dir),
                "state_dir": str(paths.state_dir),
                "data_dir": str(paths.data_dir),
            },
        }
    )
    console.print("[dim]--- 渲染出的路由片段 ---[/dim]")
    console.print(render_caddyfile(load_table(paths.routes_json)))


def main() -> None:
    try:
        app()
    except ConfigError as e:
        err_console.print(f"devproxy: 配置错误：{e}")
        sys.exit(EXIT_FAIL)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_cli.py -v
```
Expected: PASS，9 passed

- [ ] **Step 5: 提交**

```bash
git add src/devproxy/cli.py tests/test_cli.py
git commit -m "✨ feat(devproxy): CLI —— up/expose/down/ls/config 与 '--' 切分"
```

---

### Task 7: selftest 端到端自检

**Files:**
- Create: `src/devproxy/proxy/selftest.py`
- Create: `tests/test_integration.py`
- Modify: `src/devproxy/cli.py`（加 `selftest` 子命令）

**Interfaces:**
- Consumes: `devproxy.proxy.routes.{Route, RouteTable, render_caddyfile}`、`devproxy.proxy.caddy.build_main_caddyfile`
- Produces:
  - `devproxy.proxy.selftest.SelftestResult` — dataclass，字段 `checks: list[tuple[str, bool]]`、`failed: list[str]`
  - `devproxy.proxy.selftest.run_selftest(caddy_bin: Path) -> SelftestResult` — 起隔离 Caddy 实例，跑完整流程后清理

- [ ] **Step 1: 写失败的测试**

`tests/test_integration.py`：

```python
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import unittest
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from devproxy.proxy.routes import Route, RouteTable, render_caddyfile

CADDY = Path(os.environ.get("DEVPROXY_TEST_CADDY", Path.home() / ".local" / "bin" / "caddy"))

pytestmark = pytest.mark.skipif(
    not CADDY.exists(), reason=f"需要 caddy 二进制：{CADDY}"
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello-from-upstream"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _fetch(host, port, ctx):
    conn = HTTPSConnection(host, port, context=ctx, timeout=5)
    conn.request("GET", "/")
    r = conn.getresponse()
    body = r.read().decode()
    conn.close()
    return r.status, body


def test_exact_block_serves_route_and_withholds_cert_for_unknown():
    """精确块：已注册域名可用；未注册域名拿不到证书。"""
    from devproxy.proxy.selftest import run_selftest

    result = run_selftest(CADDY)
    for desc, ok in result.checks:
        assert ok, f"检查失败：{desc}"
    assert result.failed == []
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_integration.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'devproxy.proxy.selftest'`

- [ ] **Step 3: 实现**

`src/devproxy/proxy/selftest.py`：

```python
"""隔离 Caddy 实例的端到端自检 —— 不碰生产 443/2019。

沿用 devp selftest 的成熟做法：独立 admin 端口、独立 https/http 端口、
独立 storage，用沙箱自己的 CA 做真实主机名校验的 HTTPS 请求。
"""

from __future__ import annotations

import http.client
import http.server
import os
import shutil
import ssl
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from devproxy.proxy.routes import Route, RouteTable, render_caddyfile

ADMIN_PORT = 20191
HTTPS_PORT = 14443
HTTP_PORT = 14480


@dataclass
class SelftestResult:
    checks: list[tuple[str, bool]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def record(self, desc: str, ok: bool) -> None:
        self.checks.append((desc, ok))
        if not ok:
            self.failed.append(desc)


def _wait_admin(base: str, timeout: float = 15.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/config/", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    return False


def run_selftest(caddy_bin: Path) -> SelftestResult:
    """起隔离 Caddy，跑注册→转发→注销→证书策略的完整流程。"""
    result = SelftestResult()
    if not caddy_bin.exists():
        result.record(f"找到 caddy 二进制 {caddy_bin}", False)
        return result

    tmp = Path(tempfile.mkdtemp(prefix="devproxy-selftest-"))
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    import threading

    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    uport = upstream.server_address[1]

    routes_file = tmp / "routes.caddy"
    sites_file = tmp / "sites.caddy"
    sites_file.write_text("", encoding="utf-8")

    def write_routes(routes):
        routes_file.write_text(render_caddyfile(RouteTable(routes=routes)), encoding="utf-8")

    write_routes([])

    cfg_path = tmp / "Caddyfile"
    cfg_path.write_text(
        "{\n"
        f"\tadmin 127.0.0.1:{ADMIN_PORT}\n"
        f"\tstorage file_system {tmp / 'data'}\n"
        f"\thttp_port {HTTP_PORT}\n"
        f"\thttps_port {HTTPS_PORT}\n"
        "}\n"
        "\n"
        f"import {routes_file}\n"
        f"import {sites_file}\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [str(caddy_bin), "run", "--config", str(cfg_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cafile = tmp / "data" / "pki" / "authorities" / "local" / "root.crt"
    try:
        base = f"http://127.0.0.1:{ADMIN_PORT}"
        if not _wait_admin(base):
            result.record("沙箱 caddy 启动", False)
            return result
        result.record("沙箱 caddy 启动", True)

        deadline = time.time() + 10
        ctx = None
        while time.time() < deadline:
            if cafile.exists():
                try:
                    ctx = ssl.create_default_context(cafile=str(cafile))
                    break
                except ssl.SSLError:
                    pass
            time.sleep(0.2)
        result.record("沙箱 CA 根证书已生成并可加载", ctx is not None)
        if ctx is None:
            return result

        # 未注册域名：不应拿到证书
        try:
            _fetch("unregistered.test", HTTPS_PORT, ctx)
            result.record("未注册域名不签发证书", False)
        except ssl.SSLError:
            result.record("未注册域名不签发证书", True)
        except OSError:
            result.record("未注册域名不签发证书", True)

        # 已注册域名：精确块 + tls internal
        write_routes(
            [
                Route(
                    name="foo",
                    domain="foo.test",
                    port=uport,
                    mode="expose",
                    upstream=f"127.0.0.1:{uport}",
                )
            ]
        )
        reload_proc = subprocess.run(
            [str(caddy_bin), "reload", "--config", str(cfg_path), "--address", f"127.0.0.1:{ADMIN_PORT}"],
            capture_output=True,
            text=True,
        )
        result.record("reload 生效", reload_proc.returncode == 0)

        try:
            status, body = _fetch("foo.test", HTTPS_PORT, ctx)
            result.record(
                "已注册域名命中上游",
                status == 200 and body == "hello-from-upstream",
            )
        except (ssl.SSLError, OSError) as e:
            result.record(f"已注册域名命中上游（{e}）", False)

        # 80 → 443 跳转（必须在注销之前测：注销后 Caddy 不再管理该域名的跳转）
        conn = http.client.HTTPConnection("127.0.0.1", HTTP_PORT, timeout=5)
        conn.request("GET", "/", headers={"Host": "foo.test"})
        r = conn.getresponse()
        location = r.getheader("Location")
        r.read()
        conn.close()
        result.record(
            "http 跳转 https", location is not None and location.startswith("https://")
        )

        # 注销后拿不到证书
        write_routes([])
        subprocess.run(
            [str(caddy_bin), "reload", "--config", str(cfg_path), "--address", f"127.0.0.1:{ADMIN_PORT}"],
            capture_output=True,
            text=True,
        )
        try:
            _fetch("foo.test", HTTPS_PORT, ctx)
            result.record("注销后不再有路由", False)
        except (ssl.SSLError, OSError):
            result.record("注销后不再有路由", True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.shutdown()
        upstream.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
    return result
```

在 `src/devproxy/cli.py` 的 `app` 上追加子命令（放在 `config` 之后）：

```python
@app.command()
def selftest() -> None:
    """隔离 Caddy 实例端到端自检（不碰生产 443/2019）。"""
    from devproxy.proxy.selftest import run_selftest

    _, config, _, _ = build_context()
    result = run_selftest(config.caddy_bin_path)
    for desc, ok in result.checks:
        console.print(f"  {'✅' if ok else '❌'} {desc}")
    if result.failed:
        err_console.print(f"devproxy: selftest 失败 {len(result.failed)} 项")
        raise typer.Exit(EXIT_FAIL)
    console.print(f"selftest 全部通过 ✅（{len(result.checks)} 项）")
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest tests/test_integration.py -v
```
Expected: PASS，1 passed

- [ ] **Step 5: 跑全量测试并提交**

```bash
cd /home/yewenbin/work/tasks/local-https
python3 -m pytest -v
```
Expected: 全部 PASS，无 error

```bash
git add src/devproxy/proxy/selftest.py src/devproxy/cli.py tests/test_integration.py
git commit -m "✨ feat(devproxy): 隔离 Caddy 端到端 selftest"
```

---

## Plan 1 完成标准

- [ ] `python3 -m pytest` 全绿
- [ ] `uv tool install -e .` 后 `devproxy --help` 可用
- [ ] `devproxy selftest` 通过（在本机 Caddy 上）
- [ ] `devproxy ls` 在无 Caddy 时不崩溃，输出「（无路由）」
- [ ] `devproxy up` 在 Caddy 未运行时给出退出码 3 语义的清晰报错

## 交给 Plan 2 的接口

- `devproxy.paths.Paths` / `default_paths()` —— 平台层与组件层都用它定位目录
- `devproxy.config.Config` —— 组件层读 `storage`、`admin_addr`、`caddy_bin`
- `devproxy.proxy.caddy.CaddyConfig` —— 组件层用它 validate/reload
- `devproxy.proxy.caddy.build_main_caddyfile()` —— Plan 2 的 Caddy 组件生成主 Caddyfile 时直接复用
- `devproxy.proxy.store.RouteStore` —— Plan 2 的 `setup` 在迁移后要写入初始路由表
