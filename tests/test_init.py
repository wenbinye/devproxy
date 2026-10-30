"""devproxy init: generate devproxy.toml.

Only tests "what the file is written as" -- the path that actually starts services is
covered by tests/test_sync.py.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devproxy.cli import app
from devproxy.gateway.project import ProjectRoute
from devproxy.gateway.project import detect_project_root
from devproxy.gateway.project import load_project_file
from devproxy.gateway.project import render_project_file
from devproxy.gateway.project import route_name_from_dir
from devproxy.gateway.project import suggest_command
from devproxy.gateway.project import suggest_routes


def _invoke(args: list[str]):
    return CliRunner().invoke(app, args)


# -- scaffolding logic --------------------------------------------------


def test_detect_project_root_finds_git_root(tmp_path):
    """The project file must live at the repository root -- alongside pyproject.toml."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert detect_project_root(nested) == root.resolve()


def test_detect_project_root_without_git_is_cwd(tmp_path):
    """Without git, use the current directory and do not search upwards."""
    assert detect_project_root(tmp_path) == tmp_path.resolve()


def test_route_name_from_dir_sanitizes():
    assert route_name_from_dir(Path("/x/My App")) == "my-app"
    assert route_name_from_dir(Path("/x/codeup_manager")) == "codeup-manager"
    assert route_name_from_dir(Path("/x/--weird--")) == "weird"


def test_route_name_from_dir_handles_empty():
    assert route_name_from_dir(Path("/")) == "app"


def test_suggest_command_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "next dev"}}), encoding="utf-8"
    )
    argv, reason = suggest_command(tmp_path)
    # The port must be controllable: if the app does not listen on $PORT, devproxy's
    # readiness check can never pass
    assert argv == ["sh", "-c", "npm run dev -- --port $PORT"]
    assert "dev script" in reason


def test_suggest_command_calls_vite_directly(tmp_path):
    """A dev script that runs vite must be replaced by a direct vite call: the
    script may hardcode a port ("vite --port 5174"), and an appended --port
    does not always win."""
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite --port 5174"}}), encoding="utf-8"
    )
    argv, reason = suggest_command(tmp_path)
    assert argv == [
        "sh",
        "-c",
        "./node_modules/.bin/vite --host 127.0.0.1 --port $PORT",
    ]
    assert "vite" in reason


def test_suggest_command_falls_back_to_start(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"start": "node ."}}), encoding="utf-8"
    )
    argv, _ = suggest_command(tmp_path)
    assert argv == ["sh", "-c", "npm run start -- --port $PORT"]


def test_suggest_command_without_evidence_returns_none(tmp_path):
    """Do not guess without evidence -- a wrong guess is worse than none."""
    argv, reason = suggest_command(tmp_path)
    assert argv is None
    assert reason


def test_suggest_command_survives_broken_package_json(tmp_path):
    """A broken package.json must not raise -- it is the user's file, not ours."""
    (tmp_path / "package.json").write_text("{ not json", encoding="utf-8")
    argv, _ = suggest_command(tmp_path)
    assert argv is None


def test_render_project_file_escapes_quotes():
    content = render_project_file([ProjectRoute(name="web", command=["sh", "-c", 'echo "hi"'])])
    assert r'"echo \"hi\""' in content


def test_render_project_file_marks_expose():
    content = render_project_file([ProjectRoute(name="legacy", mode="expose", port=3000)])
    assert 'mode = "expose"' in content
    assert "port = 3000" in content
    # No command line (do not search the whole text for "command" -- the header comment contains that word)
    assert not any(line.strip().startswith("command") for line in content.splitlines())


# -- command layer ------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A git repository root + change into it."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_init_creates_file_in_project_root(project):
    result = _invoke(["init", "--name", "web", "--command", "npm run dev"])
    assert result.exit_code == 0, result.output
    target = project / "devproxy.toml"
    assert target.exists()
    project_file = load_project_file(target)
    assert len(project_file.routes) == 1
    assert project_file.routes[0].name == "web"
    assert project_file.routes[0].command == ["npm", "run", "dev"]


def test_init_guesses_command_from_project_type(project):
    (project / "package.json").write_text(
        json.dumps({"scripts": {"dev": "next dev"}}), encoding="utf-8"
    )
    result = _invoke(["init", "--name", "web"])
    assert result.exit_code == 0, result.output
    project_file = load_project_file(project / "devproxy.toml")
    assert project_file.routes[0].command == ["sh", "-c", "npm run dev -- --port $PORT"]


def test_init_refuses_to_overwrite_without_force(project):
    (project / "devproxy.toml").write_text("# existing content\n", encoding="utf-8")
    result = _invoke(["init", "--name", "web", "--command", "npm run dev"])
    assert result.exit_code == 2, result.output
    assert (project / "devproxy.toml").read_text(encoding="utf-8") == "# existing content\n"


def test_init_force_overwrites(project):
    (project / "devproxy.toml").write_text("# old content\n", encoding="utf-8")
    result = _invoke(["init", "--name", "web", "--command", "npm run dev", "--force"])
    assert result.exit_code == 0, result.output
    assert "old content" not in (project / "devproxy.toml").read_text(encoding="utf-8")


def test_init_rejects_illegal_route_name(project):
    result = _invoke(["init", "--name", "Bad_Name", "--command", "npm run dev"])
    assert result.exit_code == 2, result.output
    assert not (project / "devproxy.toml").exists()


def test_init_splits_command_with_quotes(project):
    result = _invoke(["init", "--name", "web", "--command", 'sh -c "echo hi there"'])
    assert result.exit_code == 0, result.output
    project_file = load_project_file(project / "devproxy.toml")
    assert project_file.routes[0].command == ["sh", "-c", "echo hi there"]


def test_init_imports_command_from_existing_route(project, monkeypatch, tmp_path):
    """--from: copy the start command of an already running route into the declaration."""
    paths = tmp_path / "state"
    paths.mkdir()
    (paths / "routes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "domain_suffix": "test",
                "routes": [
                    {
                        "name": "legacy-svc",
                        "domain": "legacy-svc.test",
                        "port": 1234,
                        "mode": "up",
                        "upstream": "127.0.0.1:1234",
                        "command": ["npm", "run", "serve"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    from devproxy.core.paths import Paths

    fake = Paths(tmp_path / "cfg", paths, tmp_path / "data")
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (fake, None, None, None),
    )

    result = _invoke(["init", "--name", "web", "--from", "legacy-svc"])
    assert result.exit_code == 0, result.output
    project_file = load_project_file(project / "devproxy.toml")
    assert project_file.routes[0].command == ["npm", "run", "serve"]


def test_init_reports_unknown_route_for_from(project, monkeypatch, tmp_path):
    paths = tmp_path / "state"
    paths.mkdir()
    (paths / "routes.json").write_text(
        json.dumps({"version": 1, "domain_suffix": "test", "routes": []}),
        encoding="utf-8",
    )

    from devproxy.core.paths import Paths

    fake = Paths(tmp_path / "cfg", paths, tmp_path / "data")
    monkeypatch.setattr(
        "devproxy.cli._shared.build_context",
        lambda *a, **k: (fake, None, None, None),
    )

    result = _invoke(["init", "--name", "web", "--from", "nope"])
    assert result.exit_code == 2, result.output
    assert "nope" in result.output
    assert not (project / "devproxy.toml").exists()


def test_init_generated_file_parses_back(project):
    """The generated file must be **parseable by ourselves** -- otherwise the user's first sync explodes."""
    result = _invoke(["init", "--name", "web", "--command", "npm run dev"])
    assert result.exit_code == 0, result.output
    project_file = load_project_file(project / "devproxy.toml")
    assert project_file.routes[0].name == "web"


# -- PHP projects -------------------------------------------------------


def test_suggest_command_laravel(tmp_path):
    """An artisan file -> Laravel's official start command, with a controllable port."""
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "artisan").write_text("#!/usr/bin/env php\n", encoding="utf-8")
    argv, reason = suggest_command(tmp_path)
    assert argv == ["sh", "-c", "php artisan serve --port=$PORT"]
    assert "artisan" in reason


def test_suggest_command_php_public_docroot(tmp_path):
    """public/index.php -> the built-in server under public (Symfony etc.)."""
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "index.php").write_text("<?php\n", encoding="utf-8")
    argv, _ = suggest_command(tmp_path)
    assert argv == ["sh", "-c", "php -S 127.0.0.1:$PORT -t public"]


def test_suggest_command_php_root_index(tmp_path):
    """index.php at the root -> the built-in server at the project root."""
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "index.php").write_text("<?php\n", encoding="utf-8")
    argv, _ = suggest_command(tmp_path)
    assert argv == ["sh", "-c", "php -S 127.0.0.1:$PORT"]


def test_suggest_command_php_composer_script(tmp_path):
    """A serve script in composer.json -> offer it, but say plainly that the port is not controllable."""
    (tmp_path / "composer.json").write_text(
        json.dumps({"scripts": {"serve": "php src/index.php start"}}), encoding="utf-8"
    )
    argv, reason = suggest_command(tmp_path)
    assert argv == ["sh", "-c", "composer serve"]
    assert "the port is not controllable" in reason


def test_suggest_command_php_names_env_port_vars(tmp_path):
    """Port variables in .env must be named -- users need to know which one to change."""
    (tmp_path / "composer.json").write_text(
        json.dumps({"scripts": {"serve": "x"}}), encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "APP_WEB_PORT=18130\nAPP_JSONRPC_PORT=10130\nDB_PORT=3306\n", encoding="utf-8"
    )
    _, reason = suggest_command(tmp_path)
    assert "APP_WEB_PORT" in reason
    assert "DB_PORT" not in reason, (
        "database ports have nothing to do with devproxy; do not add noise"
    )


def test_suggest_command_php_unknown_does_not_guess(tmp_path):
    """An unrecognized PHP project is not guessed -- only a hint is given."""
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    argv, reason = suggest_command(tmp_path)
    assert argv is None
    assert "composer.json" in reason


def test_suggest_command_makefile_requires_real_target(tmp_path):
    """Only guess when the Makefile really has a dev target (it used to check only file existence -> wrong guesses)."""
    (tmp_path / "Makefile").write_text("build:\n\techo hi\n", encoding="utf-8")
    argv, reason = suggest_command(tmp_path)
    assert argv is None
    assert "Makefile" in reason

    (tmp_path / "Makefile").write_text("dev:\n\techo hi\n", encoding="utf-8")
    argv, _ = suggest_command(tmp_path)
    assert argv == ["make", "dev"]


def test_suggest_command_php_wins_over_makefile(tmp_path):
    """Both composer.json and a Makefile -- treat it as PHP."""
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "Makefile").write_text("dev:\n\techo hi\n", encoding="utf-8")
    argv, reason = suggest_command(tmp_path)
    assert argv is None
    assert "PHP" in reason


# -- multi-route scaffolding (frontend + backend) ------------------------


def _monorepo(tmp_path, *, vite_config: str | None, dev_script: str = "vite --port 5174"):
    root = tmp_path / "demo"
    root.mkdir()
    # .git makes detect_project_root stop here (and keeps the test independent
    # of where TMPDIR happens to live)
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    frontend = root / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps({"scripts": {"dev": dev_script}}), encoding="utf-8"
    )
    if vite_config is not None:
        (frontend / "vite.config.ts").write_text(vite_config, encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _isolated_devproxy_home(tmp_path, monkeypatch):
    """init reads the machine's config (the domain suffix) and state -- keep
    the developer's own files out of every test in this module."""
    monkeypatch.setenv("DEVPROXY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DEVPROXY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DEVPROXY_DATA_DIR", str(tmp_path / "data"))


def test_init_generates_loadable_file_for_python_project(tmp_path, monkeypatch):
    """A Python project has no guessable command -- but the file must still be
    **loadable**. An empty command used to make `devproxy up` reject the very
    file init had just written."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = _invoke(["init"])
    assert result.exit_code == 0, result.output
    project = load_project_file(tmp_path / "devproxy.toml")
    assert len(project.routes) == 1
    assert project.routes[0].command, "the command must not be empty"
    assert "placeholder" in " ".join(project.routes[0].command)


def test_init_generates_two_routes_for_a_monorepo(tmp_path, monkeypatch):
    root = _monorepo(
        tmp_path,
        vite_config=(
            "export default defineConfig(({ mode }) => ({\n"
            "  server: { proxy: { '/api': { target: `http://localhost:${env.BACKEND_PORT ?? '8012'}` } } },\n"
            "}));\n"
        ),
    )
    monkeypatch.chdir(root)
    result = _invoke(["init"])
    assert result.exit_code == 0, result.output
    project = load_project_file(root / "devproxy.toml")
    assert [r.name for r in project.routes] == ["demo", "demo-web"]
    api, web = project.routes
    assert "placeholder" in " ".join(api.command)
    web_cmd = " ".join(web.command)
    assert "BACKEND_PORT=$DEVPROXY_DEMO_PORT" in web_cmd, "the API port must be injected"
    assert "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=demo-web.test" in web_cmd
    assert "cd frontend" in web_cmd
    assert "vite --host 127.0.0.1 --port $PORT" in web_cmd


def test_init_monorepo_without_a_port_env_var_still_loads(tmp_path, monkeypatch):
    """No env var in the vite config -> no injection, and the note says what
    to add by hand; the file itself must stay valid."""
    root = _monorepo(
        tmp_path,
        vite_config="export default defineConfig({ server: { proxy: { '/api': { target: 'http://localhost:8012' } } } });",
    )
    monkeypatch.chdir(root)
    result = _invoke(["init"])
    assert result.exit_code == 0, result.output
    project = load_project_file(root / "devproxy.toml")
    web_cmd = " ".join(project.get("demo-web").command or [])
    assert "DEVPROXY" not in web_cmd
    assert "placeholder" not in web_cmd
    assert "hardcodes the API address" in result.output


def test_suggest_routes_without_a_frontend_is_one_route(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "next dev"}}), encoding="utf-8"
    )
    routes, notes = suggest_routes(tmp_path)
    assert len(routes) == 1
    assert routes[0].name == route_name_from_dir(tmp_path)
    assert any("confirm" in n for n in notes)


def test_init_with_explicit_name_wires_the_renamed_routes(tmp_path, monkeypatch):
    """--name renames the routes, so the generated command must refer to the
    **new** names -- otherwise the file points at routes that do not exist and
    devproxy refuses to load it."""
    root = _monorepo(
        tmp_path,
        vite_config=(
            "export default defineConfig(({ mode }) => ({\n"
            "  server: { proxy: { '/api': { target: `http://localhost:${env.BACKEND_PORT ?? '8012'}` } } },\n"
            "}));\n"
        ),
    )
    monkeypatch.chdir(root)
    result = _invoke(["init", "--name", "foo"])
    assert result.exit_code == 0, result.output
    project = load_project_file(root / "devproxy.toml")  # must be loadable
    assert [r.name for r in project.routes] == ["foo", "foo-web"]
    web_cmd = " ".join(project.get("foo-web").command or [])
    assert "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=foo-web.test" in web_cmd
    assert "BACKEND_PORT=$DEVPROXY_FOO_PORT" in web_cmd
    assert "DEMO" not in web_cmd.upper()


def test_init_uses_the_configured_domain_suffix(tmp_path, monkeypatch):
    """The domain in the generated command must use the suffix the machine
    really uses -- a hardcoded 'test' would give the frontend a Host header
    Vite rejects."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        'domain_suffix = "dev"\nallowed_domain_suffixes = ["dev"]\n', encoding="utf-8"
    )
    root = _monorepo(tmp_path, vite_config=None)
    monkeypatch.chdir(root)
    result = _invoke(["init"])
    assert result.exit_code == 0, result.output
    text = (root / "devproxy.toml").read_text(encoding="utf-8")
    assert "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=demo-web.dev" in text
    assert "demo-web.test" not in text


def test_init_non_vite_frontend_uses_npm_run(tmp_path, monkeypatch):
    """A frontend that is not vite (next, CRA) is started through its script."""
    root = _monorepo(tmp_path, vite_config=None, dev_script="next dev")
    monkeypatch.chdir(root)
    result = _invoke(["init"])
    assert result.exit_code == 0, result.output
    project = load_project_file(root / "devproxy.toml")
    web_cmd = " ".join(project.get("demo-web").command or [])
    assert "npm run dev -- --port $PORT" in web_cmd
    assert "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS" not in web_cmd


def _kill_group(proc) -> None:
    """Stop the child and everything it started (the shell's own child)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), 15)
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass


def _drain(proc) -> str:
    """The child's output, without blocking forever on a grandchild that
    still holds the pipe."""
    if proc.stdout is None:
        return ""
    try:
        proc.stdout.close()
    except OSError:
        pass
    return ""


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode().replace("\0", " ")
    except OSError:
        return "(no /proc on this platform)"


def test_placeholder_command_really_listens_on_port(tmp_path):
    """The placeholder must be startable: devproxy's readiness check is
    "connectable on $PORT", and the file is promised to be startable."""
    import shutil
    import subprocess
    import sys

    from devproxy.gateway.project import PLACEHOLDER_COMMAND

    # The placeholder resolves `python3` from PATH, and it must see the same
    # PATH a real `devproxy up` sees. A `uv run pytest` process has the venv's
    # bin directory **first in PATH** (so `python3` = the uv trampoline), and
    # on macOS runners that trampoline hangs the child before it ever binds --
    # observed repeatedly. A user's `devproxy up` never has this repo's .venv
    # in PATH, so drop it: the test then measures the real thing.
    venv_bin = Path(sys.executable).parent.resolve()
    kept = [
        part
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part and Path(part).resolve() != venv_bin
    ]
    env = {**os.environ, "PATH": os.pathsep.join(kept)}

    for _attempt in range(5):
        port = _pick_port()
        if port is None:
            continue
        child_env = {**env, "PORT": str(port)}
        out_file = tmp_path / "placeholder-out.txt"
        with out_file.open("w", encoding="utf-8") as handle:
            proc = subprocess.Popen(
                PLACEHOLDER_COMMAND,
                env=child_env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                if _wait_for_port(port, proc):
                    return
            finally:
                _kill_group(proc)
        output = out_file.read_text(encoding="utf-8")
        if "Address already in use" in output:
            continue  # the port was taken by somebody else -- try another one
        pytest.fail(
            "the placeholder command must listen on $PORT\n"
            f"  child output: {output!r}\n"
            f"  child returncode: {proc.returncode!r}\n"
            f"  child alive after wait: {proc.poll() is None}\n"
            f"  PORT in env: {child_env.get('PORT')!r}\n"
            f"  python3 resolves to: "
            f"{shutil.which('python3', path=child_env.get('PATH'))!r}\n"
            f"  sys.executable: {sys.executable!r}"
        )
    pytest.fail("the placeholder command never got a free port to bind")


def _pick_port() -> int | None:
    """A free port **below** the ephemeral range.

    Asking the kernel for a free port (bind to :0) hands out a number from
    the ephemeral range -- on macOS that range starts at 49152. Connecting to
    such a port from the same machine can fail because the outgoing socket's
    source port is drawn from the same range and collides with the
    destination. The test would then measure the kernel's port bookkeeping
    instead of the placeholder command.
    """
    import random
    import socket

    for _ in range(20):
        port = random.randint(18000, 28000)  # noqa: S311 -- a test port, not a secret
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    return None


def _wait_for_port(port: int, proc) -> bool:
    """Is the port connectable within a few seconds?

    Probe with `create_connection` (blocking, like devproxy's own readiness
    check) -- a hand-rolled non-blocking `connect_ex` returns EINPROGRESS
    forever against the dual-stack listener that Python 3.13+'s http.server
    creates on macOS, and the test would then measure its own probe instead
    of the placeholder.
    """
    import socket

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            pass
        if proc.poll() is not None:
            return False
        time.sleep(0.1)
    return False


def _kill_group(proc) -> None:
    """Stop the child and everything it started (the shell's own child)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), 15)
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass


def _pick_port() -> int | None:
    """A free port **below** the ephemeral range.

    Asking the kernel for a free port (bind to :0) hands out a number from
    the ephemeral range -- on macOS that range starts at 49152. Connecting to
    such a port from the same machine can fail because the outgoing socket's
    source port is drawn from the same range and collides with the
    destination. The test would then measure the kernel's port bookkeeping
    instead of the placeholder command.
    """
    import random
    import socket

    for _ in range(20):
        port = random.randint(18000, 28000)  # noqa: S311 -- a test port, not a secret
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    return None


def _wait_for_port(port: int, proc) -> bool:
    """Is the port connectable within a few seconds?"""
    import socket

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        if proc.poll() is not None:
            return False
        time.sleep(0.1)
    return False


def _kill_group(proc) -> None:
    """Stop the child and everything it started (the shell's own child)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), 15)
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass


def _stop_and_read(proc) -> str:
    """Kill the child, then read what it printed (the group kill closes the
    pipe, so this cannot block forever)."""
    _kill_group(proc)
    try:
        out, _ = proc.communicate(timeout=5)
    except Exception:  # pragma: no cover -- diagnostics only
        return ""
    return out or ""


def test_init_keeps_a_dev_script_that_runs_more_than_vite(tmp_path, monkeypatch):
    """A script like `concurrently 'vite …' 'node server.js'` must not be
    replaced by a plain vite call -- that would silently drop the second
    process."""
    root = _monorepo(
        tmp_path,
        vite_config=None,
        dev_script="concurrently 'vite --port 5174' 'node server.js'",
    )
    monkeypatch.chdir(root)
    result = _invoke(["init"])
    assert result.exit_code == 0, result.output
    project = load_project_file(root / "devproxy.toml")
    web_cmd = " ".join(project.get("demo-web").command or [])
    assert "npm run dev -- --port $PORT" in web_cmd
    assert "./node_modules/.bin/vite" not in web_cmd
    assert "several commands" in result.output


def test_init_warns_about_a_second_port_variable(tmp_path, monkeypatch):
    """Two port variables in the config: only one can be wired automatically,
    the other must be named instead of silently left empty."""
    root = _monorepo(
        tmp_path,
        vite_config=(
            "export default defineConfig(({ mode }) => ({\n"
            "  server: { proxy: { '/api': { target: `http://localhost:${env.BACKEND_PORT}` },\n"
            "                   '/ws': { target: `ws://localhost:${env.WS_PORT}` } } },\n"
            "}));\n"
        ),
    )
    monkeypatch.chdir(root)
    result = _invoke(["init"])
    assert result.exit_code == 0, result.output
    project = load_project_file(root / "devproxy.toml")
    web_cmd = " ".join(project.get("demo-web").command or [])
    assert "BACKEND_PORT=$DEVPROXY_DEMO_PORT" in web_cmd
    assert "WS_PORT" not in web_cmd, "only one variable can be wired"
    assert "WS_PORT" in result.output, "the other variable must be named"
