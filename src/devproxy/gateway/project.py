"""项目级配置文件 devproxy.toml：发现、解析、校验。

文件放在**项目仓库根目录**，随代码提交，全组共享 —— 一份声明，
每个人的机器上跑 `devproxy sync` 就得到同一套域名。

格式（完整说明见 README）：

    [[routes]]
    name = "web"
    command = ["npm", "run", "dev"]

    [[routes]]
    name = "console"
    mode = "expose"
    port = 3000

解析要**明确报错**：文件缺失、语法错、未知键、字段不合法 —— 每一条
都要带上文件名与出错位置，否则用户只能对着「一堆 TOML」猜。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from devproxy.core.config import READY_TIMEOUT_MAX
from devproxy.core.config import READY_TIMEOUT_MIN
from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import validate_name

PROJECT_FILE_NAME = "devproxy.toml"

MODE_UP = "up"
MODE_EXPOSE = "expose"
MODES = (MODE_UP, MODE_EXPOSE)

# 顶层只允许 routes；路由表里只允许下面这几个键。
# 未知键一律报错 —— 拼错的键名（prot 而不是 port）静默忽略会让人
# 以为配置生效了，实际没有。
_TOP_LEVEL_KEYS = ("routes",)
_ROUTE_KEYS = ("name", "command", "mode", "port", "ready_timeout")


class ProjectFileError(RuntimeError):
    """devproxy.toml 缺失、语法错误或字段不合法。"""


@dataclass(frozen=True)
class ProjectRoute:
    """文件里声明的一条路由。"""

    name: str
    mode: str = MODE_UP
    command: list[str] | None = None
    port: int | None = None
    ready_timeout: int | None = None

    @property
    def is_up(self) -> bool:
        return self.mode == MODE_UP


@dataclass(frozen=True)
class ProjectFile:
    """一份项目声明。`path` 始终是绝对路径。"""

    path: Path
    routes: list[ProjectRoute] = field(default_factory=list)

    @property
    def project_dir(self) -> Path:
        """命令的工作目录 —— 项目文件所在目录（不是用户当前目录）。"""
        return self.path.parent

    def get(self, name: str) -> ProjectRoute | None:
        return next((r for r in self.routes if r.name == name), None)


def find_project_file(start: Path | None = None) -> Path | None:
    """从 start 向上逐级找 devproxy.toml，找到第一个就返回。

    找不到返回 None（由调用方决定怎么报错）。`start` 默认当前目录 ——
    用户在项目里的任意子目录执行 sync 都应该找到同一个文件。
    """
    try:
        current = (start or Path.cwd()).resolve()
    except OSError:
        return None
    for directory in (current, *current.parents):
        candidate = directory / PROJECT_FILE_NAME
        if candidate.is_file():
            return candidate
    return None


def resolve_project_file(explicit: str | Path | None) -> Path:
    """定位项目文件：`--file` 显式指定，或从当前目录向上查找。

    两条路径互斥：给了 `--file` 就不再向上找（显式指定优先）。
    找不到时抛 ProjectFileError（CLI 层映射成退出码 2）。
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.is_file():
            raise ProjectFileError(f"指定的配置文件不存在：{path}")
        return path
    found = find_project_file()
    if found is None:
        raise ProjectFileError(
            f"未找到 {PROJECT_FILE_NAME}"
            f"（已从当前目录 {Path.cwd()} 逐级向上查找）。"
            f"请在项目根目录创建 {PROJECT_FILE_NAME}，"
            f"或用 --file <路径> 显式指定。"
        )
    return found


def load_project_file(path: Path) -> ProjectFile:
    """读取并校验一个项目文件。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ProjectFileError(f"配置文件不存在：{path}") from e
    except UnicodeDecodeError as e:
        raise ProjectFileError(f"{path}：不是 UTF-8 编码的文本文件") from e
    except OSError as e:
        raise ProjectFileError(f"无法读取配置文件 {path}：{e}") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        # tomllib 的报错自带行列号（at line X, column Y），保留它
        raise ProjectFileError(f"{path}：TOML 语法错误 —— {e}") from e
    return _parse(path, data)


def _parse(path: Path, data: dict) -> ProjectFile:
    unknown = [k for k in data if k not in _TOP_LEVEL_KEYS]
    if unknown:
        raise ProjectFileError(
            f"{path}：未知顶层键 {'、'.join(sorted(unknown))}"
            f"（可用键：{', '.join(_TOP_LEVEL_KEYS)}）"
        )
    raw_routes = data.get("routes", [])
    if not isinstance(raw_routes, list):
        raise ProjectFileError(
            f"{path}：routes 必须是 [[routes]] 形式的数组"
        )
    routes: list[ProjectRoute] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(raw_routes):
        route = _parse_route(path, index, item)
        if route.name in seen:
            raise ProjectFileError(
                f"{path}：路由名 {route.name!r} 重复声明"
                f"（routes[{seen[route.name]}] 与 routes[{index}]）"
            )
        seen[route.name] = index
        routes.append(route)
    return ProjectFile(path=Path(path), routes=routes)


def _parse_route(path: Path, index: int, item: object) -> ProjectRoute:
    where = f"{path}：routes[{index}]"
    if not isinstance(item, dict):
        raise ProjectFileError(f"{where} 必须是 [[routes]] 形式的表")
    unknown = [k for k in item if k not in _ROUTE_KEYS]
    if unknown:
        raise ProjectFileError(
            f"{where}：未知键 {'、'.join(sorted(unknown))}"
            f"（可用键：{', '.join(_ROUTE_KEYS)}）"
        )
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise ProjectFileError(f"{where}：缺少 name（路由名）")
    try:
        validate_name(name)
    except InvalidNameError as e:
        raise ProjectFileError(f"{where}：{e}") from e

    mode = item.get("mode", MODE_UP)
    if mode not in MODES:
        raise ProjectFileError(
            f"{where}（{name}）：mode 只能是 'up' 或 'expose'，当前 {mode!r}"
        )
    command = _parse_command(where, name, mode, item)
    port = _parse_port(where, name, mode, item)
    ready_timeout = _parse_ready_timeout(where, name, item)
    return ProjectRoute(
        name=name,
        mode=mode,
        command=command,
        port=port,
        ready_timeout=ready_timeout,
    )


def _parse_command(
    where: str, name: str, mode: str, item: dict
) -> list[str] | None:
    raw = item.get("command")
    if mode == MODE_EXPOSE:
        if raw is not None:
            raise ProjectFileError(
                f"{where}（{name}）：expose 模式不启动进程，不能声明 command"
            )
        return None
    if raw is None:
        raise ProjectFileError(
            f"{where}（{name}）：up 模式必须声明 command"
            f'（例如 command = ["npm", "run", "dev"]）'
        )
    if not isinstance(raw, list) or not raw:
        raise ProjectFileError(
            f"{where}（{name}）：command 必须是非空字符串数组"
            f'（例如 command = ["npm", "run", "dev"]）'
        )
    if not all(isinstance(part, str) for part in raw):
        raise ProjectFileError(f"{where}（{name}）：command 的每一项都必须是字符串")
    if any(not part for part in raw):
        raise ProjectFileError(f"{where}（{name}）：command 里不能有空字符串参数")
    return list(raw)


def _parse_port(where: str, name: str, mode: str, item: dict) -> int | None:
    raw = item.get("port")
    if raw is None:
        if mode == MODE_EXPOSE:
            raise ProjectFileError(
                f"{where}（{name}）：expose 模式必须声明 port"
                f"（上游已在监听的端口）"
            )
        return None
    # bool 是 int 的子类 —— True 会当成端口 1 混过去
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProjectFileError(f"{where}（{name}）：port 必须是整数")
    if not (1 <= raw <= 65535):
        raise ProjectFileError(
            f"{where}（{name}）：port {raw} 超出 1..65535 范围"
        )
    return raw


def _parse_ready_timeout(where: str, name: str, item: dict) -> int | None:
    raw = item.get("ready_timeout")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProjectFileError(f"{where}（{name}）：ready_timeout 必须是整数（秒）")
    if not (READY_TIMEOUT_MIN <= raw <= READY_TIMEOUT_MAX):
        raise ProjectFileError(
            f"{where}（{name}）：ready_timeout {raw} 必须在 "
            f"{READY_TIMEOUT_MIN}..{READY_TIMEOUT_MAX} 秒之间"
        )
    return raw


# -- 脚手架：生成一份可用的 devproxy.toml --------------------------------


def detect_project_root(start: Path | None = None) -> Path:
    """项目文件该放在哪：git 仓库根，没有 git 就用当前目录。

    向上找 `.git`（文件或目录都行 —— worktree 里 `.git` 是文件），
    找到的那一层就是仓库根，项目文件该和 `pyproject.toml` 放一起。
    """
    try:
        current = (start or Path.cwd()).resolve()
    except OSError:
        return Path.cwd()
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            return directory
    return current


def suggest_command(root: Path) -> tuple[list[str] | None, str]:
    """按项目类型猜一条启动命令。返回 (命令, 判断依据)。

    猜错比不猜更糟 —— 但完全不猜等于让用户从零写 TOML。所以只在
    **有明确证据**时给建议（package.json 里有 dev 脚本、pyproject.toml
    存在且能推出入口点），否则返回 None 让用户自己写。
    """
    package_json = root / "package.json"
    if package_json.is_file():
        scripts = _read_json_field(package_json, "scripts")
        if isinstance(scripts, dict) and "dev" in scripts:
            return ["npm", "run", "dev"], "package.json 里有 dev 脚本"
        if isinstance(scripts, dict) and "start" in scripts:
            return ["npm", "run", "start"], "package.json 里有 start 脚本"
    if (root / "pyproject.toml").is_file():
        return None, "检测到 pyproject.toml —— 请填上实际启动命令"
    if (root / "Makefile").is_file():
        return ["make", "dev"], "检测到 Makefile（若有 dev 目标）"
    return None, "没有检测到常见的项目类型 —— 请填上实际启动命令"


def _read_json_field(path: Path, field_name: str) -> object:
    """读 JSON 的一个顶层字段；解析不了就返回 None（不抛）。"""
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get(field_name)


def route_name_from_dir(root: Path) -> str:
    """用目录名推一个合法的路由名（`[a-z0-9-]`，首尾字母数字）。"""
    raw = root.name.lower()
    cleaned = "".join(c if c.isalnum() or c == "-" else "-" for c in raw)
    cleaned = cleaned.strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"app-{cleaned}".strip("-")
    if not cleaned:
        return "app"
    # 目录名可能超过 63 字符 —— 截断后仍要保证首尾是字母数字
    cleaned = cleaned[:63].strip("-")
    return cleaned or "app"


def render_project_file(
    routes: list[ProjectRoute], *, notes: list[str] | None = None
) -> str:
    """渲染成 devproxy.toml 的文本。

    注释只说**为什么**与**有哪些坑**，不复述字段名（那是文档的事）。
    """
    lines: list[str] = [
        "# devproxy.toml —— 本项目的本地开发路由声明",
        "#",
        "# 提交进仓库，全组共享：每个人跑一次 `devproxy sync` 就得到同一套域名。",
        "# 完整说明见 devproxy 的 README「项目级配置」。",
        "#",
        "# command 是 argv 数组，不经过 shell —— $PORT 不会自动展开。",
        "# 需要端口参数的应用要自己包一层 sh -c，让 shell 在端口分配之后再展开：",
        '#   command = ["sh", "-c", "npx vite --port $PORT --host 127.0.0.1"]',
    ]
    for note in notes or []:
        lines.append(f"# {note}")
    lines.append("")
    for route in routes:
        lines.append("[[routes]]")
        lines.append(f'name = "{route.name}"')
        if route.mode == MODE_EXPOSE:
            lines.append('mode = "expose"')
            lines.append(f"port = {route.port}")
        else:
            command = route.command or []
            rendered = ", ".join(_toml_string(part) for part in command)
            lines.append(f"command = [{rendered}]")
        if route.ready_timeout is not None:
            lines.append(f"ready_timeout = {route.ready_timeout}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _toml_string(value: str) -> str:
    """渲染成 TOML 基本字符串（含引号与转义）。"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
