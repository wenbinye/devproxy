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

import re
import tomllib
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from devproxy.core.config import READY_TIMEOUT_MAX
from devproxy.core.config import READY_TIMEOUT_MIN
from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import port_env_name
from devproxy.gateway.names import route_name_from_port_env
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
    _check_port_references(path, routes)
    return ProjectFile(path=Path(path), routes=routes)


# A command may use a sibling route's port: $DEVPROXY_API_PORT (or
# ${DEVPROXY_API_PORT}). Only the shell form is recognized -- that is the
# form that gets a number substituted at start time.
_PORT_ENV_RE = re.compile(r"\$\{?(DEVPROXY_[A-Z0-9_]*_PORT)\b")


def referenced_port_vars(command: list[str] | None) -> list[str]:
    """The DEVPROXY_<NAME>_PORT variables a command actually uses."""
    if not command:
        return []
    return sorted(set(_PORT_ENV_RE.findall(" ".join(command))))


def _check_port_references(path: Path, routes: list[ProjectRoute]) -> None:
    """Reject a command that asks for a port no route of this file provides.

    devproxy exports the ports of the routes declared in the same file, so a
    command referencing a route that is not declared here would silently get
    an empty value. Better to say so while the file is being read.
    """
    declared = {r.name for r in routes}
    for route in routes:
        for var in referenced_port_vars(route.command):
            target = route_name_from_port_env(var)
            if target is None or target in declared:
                continue
            raise ProjectFileError(
                f"{path}: route {route.name!r} uses ${var}, but no route "
                f"{target!r} is declared in this file (devproxy only exports "
                f"the ports of this project's routes)"
            )


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
                    script = scripts[name]
                    if isinstance(script, str) and "vite" in script:
                        # A script that hardcodes a port ("vite --port 5174")
                        # cannot be overridden reliably by appending --port,
                        # so call vite directly: the CLI flag wins over the
                        # config file.
                        return (
                            [
                                "sh",
                                "-c",
                                "./node_modules/.bin/vite --host 127.0.0.1 --port $PORT",
                            ],
                            f"package.json has a {name} script running vite",
                        )
                    # --port is passed through to the dev server in the
                    # script (next honors it)
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


# -- Scaffolding: the routes of a project -------------------------------------

# A command that certainly listens on $PORT, so the generated file is
# **loadable and startable** even before the user edits it. A file with an
# empty command is rejected by devproxy ("command must be a non-empty array"),
# which would leave the user with a config that cannot even be parsed.
PLACEHOLDER_COMMAND = [
    "sh",
    "-c",
    "echo 'devproxy: this is a placeholder -- replace it with the real start command'; "
    "python3 -m http.server $PORT",
]

# Where a frontend usually lives inside a repository that also holds the API.
_FRONTEND_DIRS = ("frontend", "web", "client", "ui")

_VITE_CONFIG_FILES = (
    "vite.config.ts",
    "vite.config.js",
    "vite.config.mts",
    "vite.config.mjs",
)


def suggest_routes(
    root: Path, *, api_name: str | None = None, suffix: str = "test"
) -> tuple[list[ProjectRoute], list[str]]:
    """Guess the **routes** of a project -- not just one command.

    A repository with a frontend next to the API needs two routes, and the
    frontend has to know the API's port. devproxy exports the port of every
    route of the same file as DEVPROXY_<NAME>_PORT, so the generated file can
    wire them together without a hardcoded port.

    `api_name` overrides the API route's name (the `--name` of `devproxy init`)
    and `suffix` is the configured domain suffix -- both must be decided
    **before** the commands are built: the generated frontend command embeds
    the route names (its domain in the allowed-hosts variable) and the API's
    port variable name, so renaming the routes afterwards would leave the file
    pointing at names that no longer exist.

    Returns (routes, notes). The notes explain every guess, so the user can
    see what was assumed (and the file carries the same notes as comments).
    """
    api_name = api_name or route_name_from_dir(root)
    frontend = _detect_frontend(root)
    if frontend is None:
        command, reason = suggest_command(root)
        notes: list[str] = []
        if command is None:
            notes.append(f"{reason} -- using a placeholder command; edit it before starting")
            command = list(PLACEHOLDER_COMMAND)
        else:
            notes.append(f"{reason} -- confirm this is the start command you want")
        return [ProjectRoute(name=api_name, mode=MODE_UP, command=command)], notes

    web_name = f"{api_name}-web"
    api_command, api_reason = suggest_command(root)
    notes = []
    if api_command is None:
        notes.append(f"{api_reason} -- using a placeholder command; edit it before starting")
        api_command = list(PLACEHOLDER_COMMAND)
    else:
        notes.append(f"{api_reason} -- confirm this is the start command you want")
    web_command, web_notes = _frontend_command(frontend, web_name, api_name, suffix)
    notes.extend(web_notes)
    return [
        ProjectRoute(name=api_name, mode=MODE_UP, command=api_command),
        ProjectRoute(name=web_name, mode=MODE_UP, command=web_command),
    ], notes


def _detect_frontend(root: Path) -> Path | None:
    """A frontend subproject next to the API (frontend/ with its own package.json)."""
    for name in _FRONTEND_DIRS:
        candidate = root / name
        if (candidate / "package.json").is_file():
            return candidate
    return None


def _frontend_command(
    frontend: Path, web_name: str, api_name: str, suffix: str
) -> tuple[list[str], list[str]]:
    """The frontend's start command, wired to the API's dynamic port.

    Two things have to be right for the frontend to reach the API:

    - the API's port, injected as DEVPROXY_<API>_PORT into the variable the
      project's own config reads (found in its vite config);
    - the Host header, which Vite rejects unless the route's domain is
      allowed (Vite 6 answers 403 otherwise).
    """
    notes: list[str] = []
    script, script_name = _frontend_dev_script(frontend)
    is_vite = "vite" in (script or "")

    env_prefix = ""
    if is_vite:
        env_prefix += f"__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS={web_name}.{suffix} "

    api_var = _api_port_env_name(frontend)
    if api_var:
        env_prefix += f"{api_var}=${port_env_name(api_name)} "
        notes.append(
            f"the frontend's config reads {api_var} -- the API's dynamic port is injected into it"
        )
        if _vite_config_hardcodes_target(frontend):
            notes.append(
                "warning: the vite config hardcodes the API address, so the injected port is "
                "ignored -- change the config to read the variable, or pin the API's port"
            )
    else:
        notes.append(
            "the frontend's config does not read a port from the environment -- add "
            f"<PORT_VAR>=${port_env_name(api_name)} to the command by hand "
            "(and make the config read that variable)"
        )
        if _vite_config_hardcodes_target(frontend):
            notes.append(
                "warning: the vite config hardcodes the API address, so no injected port can "
                "reach it -- change the config to read a port env var, or pin the API's port"
            )

    rel = frontend.name
    if is_vite:
        inner = "./node_modules/.bin/vite --host 127.0.0.1 --port $PORT"
        notes.append(
            "vite is called directly so that --port $PORT overrides the port in the config"
        )
    else:
        inner = f"npm run {script_name or 'dev'} -- --port $PORT"
    return ["sh", "-c", f"cd {rel} && {env_prefix}{inner}"], notes


def _frontend_dev_script(frontend: Path) -> tuple[str | None, str | None]:
    """The dev script of the frontend's package.json."""
    scripts = _read_json_field(frontend / "package.json", "scripts")
    if not isinstance(scripts, dict):
        return None, None
    for name in ("dev", "start", "serve"):
        value = scripts.get(name)
        if isinstance(value, str) and value.strip():
            return value, name
    return None, None


def _api_port_env_name(frontend: Path) -> str | None:
    """The env var the frontend's config uses for the API's port.

    Looked for in the vite config ("env.BACKEND_PORT", "process.env.API_PORT") --
    that is where the proxy target is built. Only names that look like a port
    are accepted, so an unrelated variable is never picked up.
    """
    for filename in _VITE_CONFIG_FILES:
        path = frontend / filename
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        names = set(re.findall(r"(?:process\.env|env)\.([A-Z][A-Z0-9_]*)", text))
        ports = sorted(n for n in names if n == "PORT" or n.endswith("_PORT"))
        if ports:
            return ports[0]
    return None


def _vite_config_hardcodes_target(frontend: Path) -> bool:
    """Does the vite config build the proxy target from a literal address?"""
    for filename in _VITE_CONFIG_FILES:
        path = frontend / filename
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # "target: 'http://localhost:8012'" -- a literal host:port in the
        # target line means no env var can influence it
        return bool(re.search(r"target\s*:\s*[\"'`][^\"'`]*:\d{2,5}", text))
    return False


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
        "#",
        "# Each app also gets the port of every other route as",
        "# DEVPROXY_<NAME>_PORT (name uppercased, dashes to underscores), so a",
        "# frontend can point at its API without hardcoding a port:",
        '#   command = ["sh", "-c", "BACKEND_PORT=$DEVPROXY_API_PORT npm run dev"]',
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
