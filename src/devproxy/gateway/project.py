"""Project-level config file devproxy.toml: discovery, parsing, validation.

The file lives in the **project repository root**, is committed with the
code, and is shared by the whole team -- one declaration, and `devproxy up`
on everyone's machine produces the same set of domains.

Format (see the README for full details):

    [[routes]]
    name = "web"
    command = ["npm", "run", "dev"]

    [[routes]]
    name = "console"
    mode = "expose"
    port = 3000

Parsing must **fail loudly**: missing file, syntax error, unknown key,
invalid field -- each one must include the file name and error location,
otherwise users are left guessing at "a pile of TOML".
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from devproxy.core.config import READY_TIMEOUT_MAX
from devproxy.core.config import READY_TIMEOUT_MIN
from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import validate_name

PROJECT_FILE_NAME = "devproxy.toml"

MODE_UP = "up"
MODE_EXPOSE = "expose"
MODES = (MODE_UP, MODE_EXPOSE)

# Only routes is allowed at the top level; only the keys below are allowed
# in each route entry. Unknown keys always error out -- silently ignoring a
# typo (prot instead of port) would make people think the config took effect
# when it did not.
_TOP_LEVEL_KEYS = ("routes",)
_ROUTE_KEYS = ("name", "command", "mode", "port", "ready_timeout")


class ProjectFileError(RuntimeError):
    """devproxy.toml is missing, has a syntax error, or contains an invalid
    field."""


@dataclass(frozen=True)
class ProjectRoute:
    """One route declared in the file."""

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
    """A project declaration. `path` is always absolute."""

    path: Path
    routes: list[ProjectRoute] = field(default_factory=list)

    @property
    def project_dir(self) -> Path:
        """The command's working directory -- the directory containing the
        project file (not the user's current directory)."""
        return self.path.parent

    def get(self, name: str) -> ProjectRoute | None:
        return next((r for r in self.routes if r.name == name), None)


def find_project_file(start: Path | None = None) -> Path | None:
    """Search upward from start for devproxy.toml and return the first match.

    Return None when not found (the caller decides how to report it). `start`
    defaults to the current directory -- running sync from any subdirectory
    of the project should find the same file.
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
    """Locate the project file: explicit `--file`, or search upward from the
    current directory.

    The two paths are mutually exclusive: with `--file` there is no upward
    search (explicit wins). Raises ProjectFileError when not found (the CLI
    maps it to exit code 2).
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.is_file():
            raise ProjectFileError(f"specified config file does not exist: {path}")
        return path
    found = find_project_file()
    if found is None:
        raise ProjectFileError(
            f"{PROJECT_FILE_NAME} not found"
            f" (searched upward from the current directory {Path.cwd()});"
            f" create {PROJECT_FILE_NAME} in the project root"
            " or specify it explicitly with --file <path>"
        )
    return found


def load_project_file(path: Path) -> ProjectFile:
    """Read and validate one project file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ProjectFileError(f"config file does not exist: {path}") from e
    except UnicodeDecodeError as e:
        raise ProjectFileError(f"{path}: not a UTF-8 text file") from e
    except OSError as e:
        raise ProjectFileError(f"cannot read config file {path}: {e}") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        # tomllib's error includes line/column (at line X, column Y); keep it
        raise ProjectFileError(f"{path}: TOML syntax error -- {e}") from e
    return _parse(path, data)


def _parse(path: Path, data: dict) -> ProjectFile:
    unknown = [k for k in data if k not in _TOP_LEVEL_KEYS]
    if unknown:
        raise ProjectFileError(
            f"{path}: unknown top-level key(s) {', '.join(sorted(unknown))}"
            f" (available keys: {', '.join(_TOP_LEVEL_KEYS)})"
        )
    raw_routes = data.get("routes", [])
    if not isinstance(raw_routes, list):
        raise ProjectFileError(f"{path}: routes must be an array in [[routes]] form")
    routes: list[ProjectRoute] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(raw_routes):
        route = _parse_route(path, index, item)
        if route.name in seen:
            raise ProjectFileError(
                f"{path}: route name {route.name!r} declared more than once"
                f" (routes[{seen[route.name]}] and routes[{index}])"
            )
        seen[route.name] = index
        routes.append(route)
    return ProjectFile(path=Path(path), routes=routes)


def _parse_route(path: Path, index: int, item: object) -> ProjectRoute:
    where = f"{path}: routes[{index}]"
    if not isinstance(item, dict):
        raise ProjectFileError(f"{where} must be a table in [[routes]] form")
    unknown = [k for k in item if k not in _ROUTE_KEYS]
    if unknown:
        raise ProjectFileError(
            f"{where}: unknown key(s) {', '.join(sorted(unknown))}"
            f" (available keys: {', '.join(_ROUTE_KEYS)})"
        )
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise ProjectFileError(f"{where}: missing name (route name)")
    try:
        validate_name(name)
    except InvalidNameError as e:
        raise ProjectFileError(f"{where}: {e}") from e

    mode = item.get("mode", MODE_UP)
    if mode not in MODES:
        raise ProjectFileError(f"{where} ({name}): mode must be 'up' or 'expose', got {mode!r}")
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


def _parse_command(where: str, name: str, mode: str, item: dict) -> list[str] | None:
    raw = item.get("command")
    if mode == MODE_EXPOSE:
        if raw is not None:
            raise ProjectFileError(
                f"{where} ({name}): expose mode does not start a process, so command cannot be declared"
            )
        return None
    if raw is None:
        raise ProjectFileError(
            f'{where} ({name}): up mode must declare command (e.g. command = ["npm", "run", "dev"])'
        )
    if not isinstance(raw, list) or not raw:
        raise ProjectFileError(
            f"{where} ({name}): command must be a non-empty array of strings"
            f' (e.g. command = ["npm", "run", "dev"])'
        )
    if not all(isinstance(part, str) for part in raw):
        raise ProjectFileError(f"{where} ({name}): every command item must be a string")
    if any(not part for part in raw):
        raise ProjectFileError(f"{where} ({name}): command must not contain empty string arguments")
    return list(raw)


def _parse_port(where: str, name: str, mode: str, item: dict) -> int | None:
    raw = item.get("port")
    if raw is None:
        if mode == MODE_EXPOSE:
            raise ProjectFileError(
                f"{where} ({name}): expose mode must declare port "
                "(the port the upstream already listens on)"
            )
        return None
    # bool is a subclass of int -- True would slip through as port 1
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProjectFileError(f"{where} ({name}): port must be an integer")
    if not (1 <= raw <= 65535):
        raise ProjectFileError(f"{where} ({name}): port {raw} is outside the 1-65535 range")
    return raw


def _parse_ready_timeout(where: str, name: str, item: dict) -> int | None:
    raw = item.get("ready_timeout")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProjectFileError(f"{where} ({name}): ready_timeout must be an integer (seconds)")
    if not (READY_TIMEOUT_MIN <= raw <= READY_TIMEOUT_MAX):
        raise ProjectFileError(
            f"{where} ({name}): ready_timeout {raw} must be between "
            f"{READY_TIMEOUT_MIN}..{READY_TIMEOUT_MAX} seconds"
        )
    return raw


# -- Scaffolding: generate a usable devproxy.toml ---------------------------


def detect_project_root(start: Path | None = None) -> Path:
    """Where the project file belongs: the git repository root, or the current
    directory when there is no git.

    Search upward for `.git` (either a file or a directory -- in a worktree
    `.git` is a file); the directory where it is found is the repository
    root, and the project file belongs next to `pyproject.toml`.
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
    """Guess a start command based on the project type. Returns (command, reason).

    A wrong guess is worse than none -- but not guessing at all leaves users
    writing TOML from scratch. So only suggest when there is **clear
    evidence** (a dev script in package.json, a serve script in composer.json,
    an existing artisan, ...); otherwise return None and let the user write it.

    **Hard requirement**: a guessed command must make the app listen on the
    `$PORT` assigned by devproxy. devproxy's readiness check is "the app is
    connectable on $PORT" -- if the command starts the app on a fixed port,
    sync waits for readiness forever, reports "ready timeout", and the cause
    is hard to guess. So only port-controllable guesses (`--port` /
    `-S ...:$PORT`) are offered; non-port-controllable ones (like
    `composer serve`) get the command plus an explicit warning.
    """
    package_json = root / "package.json"
    if package_json.is_file():
        scripts = _read_json_field(package_json, "scripts")
        if isinstance(scripts, dict):
            for name in ("dev", "start"):
                if name in scripts:
                    # --port is passed through to the dev server in the
                    # script (vite/next both honor it)
                    return (
                        ["sh", "-c", f"npm run {name} -- --port $PORT"],
                        f"package.json has a {name} script",
                    )

    composer_json = root / "composer.json"
    if composer_json.is_file():
        ports = _port_env_names(root)
        php = _suggest_php_command(root, composer_json, ports)
        if php is not None:
            return php
        return None, (
            "detected composer.json (PHP project) -- fill in the actual start command"
            f"; the app must listen on $PORT{_ports_hint(ports)}"
        )

    if (root / "pyproject.toml").is_file():
        return None, (
            "detected pyproject.toml -- fill in the actual start command "
            "(the app must listen on $PORT)"
        )
    if (root / "Makefile").is_file():
        if _makefile_has_target(root / "Makefile", "dev"):
            return ["make", "dev"], "Makefile has a dev target"
        return None, "detected a Makefile but no dev target -- fill in the actual start command"
    return None, (
        "no common project type detected -- fill in the actual start command "
        "(the app must listen on $PORT)"
    )


def _ports_hint(ports: list[str]) -> str:
    """Port variables defined in .env -- name them in the hint so users know
    which to change."""
    if not ports:
        return ""
    return (
        f" (.env defines ports: {', '.join(ports)}"
        " -- change them to $PORT or switch to expose mode)"
    )


def _suggest_php_command(
    root: Path, composer_json: Path, ports: list[str]
) -> tuple[list[str] | None, str] | None:
    """Start command for a PHP project, by strength of evidence (strongest
    first):

    1. `artisan` -> Laravel's official start command;
    2. `public/index.php` -> the front controller of most frameworks;
    3. root-level `index.php` -> likewise a front controller;
    4. a serve/start/dev script in composer.json -> port not controllable, so
       only the command plus a warning.

    Return None when nothing is recognized (the caller provides the hint).
    """
    if (root / "artisan").is_file():
        return ["sh", "-c", "php artisan serve --port=$PORT"], "found artisan (Laravel)"
    if (root / "public" / "index.php").is_file():
        return (
            ["sh", "-c", "php -S 127.0.0.1:$PORT -t public"],
            "found public/index.php",
        )
    if (root / "index.php").is_file():
        return ["sh", "-c", "php -S 127.0.0.1:$PORT"], "found index.php in the project root"
    scripts = _read_json_field(composer_json, "scripts")
    if isinstance(scripts, dict):
        for name in ("serve", "start", "dev"):
            if name in scripts:
                return (
                    ["sh", "-c", f"composer {name}"],
                    f"composer.json has a {name} script; the port is not controllable"
                    f" -- the app must listen on $PORT to be taken over{_ports_hint(ports)}",
                )
    return None


# These ports are unrelated to devproxy (the app is not taken over because of
# them)
_NON_WEB_PORT_WORDS = (
    "DB",
    "DATABASE",
    "MYSQL",
    "POSTGRES",
    "REDIS",
    "MEMCACHE",
    "LDAP",
    "MONGO",
    "KAFKA",
    "RABBIT",
    "DEFAULT",
    "ADMIN",
    "DEBUG",
)


def _port_env_names(root: Path) -> list[str]:
    """Names of port variables defined in .env (for hints)."""
    env_file = root / ".env"
    if not env_file.is_file():
        return []
    names: list[str] = []
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key != "PORT" and not key.endswith("_PORT"):
                continue
            # Keep only likely web listening ports: database/cache/LDAP ones
            # are unrelated to devproxy
            upper = key.upper()
            if any(word in upper for word in _NON_WEB_PORT_WORDS):
                continue
            names.append(key)
    except OSError:
        return []
    return names


def _makefile_has_target(path: Path, target: str) -> bool:
    """Does the Makefile really have this target (only lines starting with
    `name:`)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith(("\t", " ", "	")) or line.startswith("#"):
            continue
        name = line.split(":", 1)[0].strip()
        if name == target:
            return True
    return False


def _read_json_field(path: Path, field_name: str) -> object:
    """Read one top-level JSON field; return None if it cannot be parsed
    (never raises)."""
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get(field_name)


def route_name_from_dir(root: Path) -> str:
    """Derive a valid route name from the directory name (`[a-z0-9-]`,
    letters/digits at both ends)."""
    raw = root.name.lower()
    cleaned = "".join(c if c.isalnum() or c == "-" else "-" for c in raw)
    cleaned = cleaned.strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"app-{cleaned}".strip("-")
    if not cleaned:
        return "app"
    # The directory name may exceed 63 characters -- after truncation the ends
    # must still be letters/digits
    cleaned = cleaned[:63].strip("-")
    return cleaned or "app"


def render_project_file(routes: list[ProjectRoute], *, notes: list[str] | None = None) -> str:
    """Render as devproxy.toml text.

    Comments only explain **why** and **which pitfalls exist**, not restate
    field names (that is the documentation's job).
    """
    lines: list[str] = [
        "# devproxy.toml -- local development route declarations for this project",
        "#",
        "# Commit this to the repository so the whole team shares it: everyone runs",
        "# `devproxy up` once and gets the same set of domains.",
        "# See docs/project-config.md in the devproxy repository for full details.",
        "#",
        "# command is an argv array and does not go through a shell -- $PORT is not",
        "# expanded automatically. Apps that need a port argument must wrap it in",
        "# sh -c so the shell expands it after port allocation:",
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
    """Render as a TOML basic string (including quotes and escapes)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
