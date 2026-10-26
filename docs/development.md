# Development and CI


The unit tests are essentially **hermetic**: they do not touch the real system, do not
need the caddy binary, do not need the network, and do not write into your home
directory (`HOME` points at a temporary directory in tests).

The one exception is the NSS test group: it needs the **real `certutil`**
(`libnss3-tools`) to create a real NSS DB in a temporary directory, verifying behavior
such as "decide trust by certificate content" that only the real tool can verify. When
`certutil` is not on the machine the whole group is skipped automatically and the rest
are unaffected; CI therefore installs `libnss3-tools` specifically.

Set up the development environment from the lockfile (`uv.lock` must be committed):

```bash
uv sync --locked --all-groups          # install everything (including dev dependencies)
uv run pytest -q                       # unit suite (= just test)
uv run pytest -q tests/test_integration.py   # real-machine integration (needs a real caddy)
uv run ruff check src/ tests/          # lint
uv run ruff format --check src/ tests/ # format
uv run mypy                            # types (61 source files, 0 issues)
```

`just test` / `just lint` / `just fix` / `just sync` are shorthand for the commands above.

CI lives in `.github/workflows/ci.yml` and has two jobs:

| Job | Contents |
|---|---|
| `lint` | `ruff check` + `ruff format --check` + `mypy`, **blocking** |
| `test` | unit suite on Linux and **macOS** × Python 3.11 / 3.12 / 3.13 |

Dependencies are installed with `uv sync --locked`: if the lockfile and `pyproject.toml`
disagree it fails immediately (typer 0.27 no longer depends on click; such changes must
be caught by the lockfile).

Real-machine integration tests **do not run in CI** — they depend on a real caddy binary
and a real network.

## Historical files in the repository

The root directory **no longer contains** `Caddyfile` or `caddy.service`: they were the
hand-written config and hand-written unit from **before the migration**. After the
migration ran, the live unit points at `~/.config/devproxy/Caddyfile`, so they have only
historical value. They now live in [superpowers/legacy/](superpowers/legacy/) (together with the
build-time specs and decision records under [superpowers/](superpowers/)) — that is **the state
of this machine at the time**, not a project asset.

The `devp` script at the repository root **has been deleted** — it is the pre-1.0
predecessor of `devproxy`: it PATCHed the live Caddy Admin API directly to register
routes, and the new tool could neither see nor delete what it registered. Its story is
recorded in [legacy-devp.md](legacy-devp.md). The part of the `Justfile` that forwards to
`uv run devproxy` is a compatibility shell (it runs **the current source**, not the
installed copy); recipes such as `test` / `lint` are the repository's own development
actions.

## Code structure

```
src/devproxy/
├── cli/          # command layer: one command per file (up / expose / down / ls / logs / trust /
│   │             #   config / selftest / setup / doctor / uninstall / sync / run)
│   ├── _shared.py   # shared seams for command modules (console, exit codes, build_context, component wiring);
│   │                #   commands look up dependencies **at call time** through it, so test doubles take effect
│   ├── _runtime.py  # exit-code constants and the app entry point
│   ├── _context.py  # context assembly (build_context: config, route table, Caddy client)
│   ├── _render.py   # rendering of tables and hints
│   ├── _typer.py    # Typer argument splitting group (passes arguments after `--` verbatim to the subcommand)
│   ├── _migrate.py  # command-layer orchestration for migration and rollback
│   └── _worker.py   # background up worker process (sync uses it to supervise child processes when starting apps)
├── core/         # infrastructure: config (config loading and validation), paths (directories and state files),
│                 #   backup (migration backups), caddy_env (archive of unit environment variables)
├── gateway/      # runtime gateway: routes (route model and deterministic rendering), store (transactional writes:
│                 #   flock → render → validate → write to disk → reload → rollback on failure),
│                 #   runner (supervision of up's child processes), caddy (Admin API), names (domains and route names),
│                 #   selftest (isolated self-check), project / sync (project-level declarations),
│                 #   foreground (run's foreground supervision)
├── lifecycle/    # environment lifecycle: setup, doctor, uninstall,
│                 #   migrate/{caddyfile, legacy, install, rollback} (migration and rollback)
├── components/   # four deployable components: caddy / certs / dns / dnsmasq;
│                 #   base defines the component protocol — check() -> Status (never raises),
│                 #   plan() -> list[Action] (empty = no change), apply(actions)
└── platform/     # platform seams: base (protocol: Platform / Action / Status / ElevatedSession),
                  #   linux (systemd + resolved), macos (launchd + /etc/resolver),
                  #   fake (fake platform for tests; all writes go into a sandbox directory)
```

Dependency direction (each layer depends only on the layers below it; there are no
reverse dependencies):

```
cli → lifecycle / gateway / components → platform / core
```

- `core` depends on no internal module — it is the bottom layer;
- `platform` depends only on `core` (using its path utilities);
- `components` depend on `core` + `platform`, plus one **function-local** delayed import
  of `gateway.caddy` (the template that renders the main Caddyfile, avoiding a circular
  dependency);
- `lifecycle` depends on `core` / `gateway` / `components` / `platform`;
- `cli` depends on all of them.

Tests have a **mirrored structure**: `tests/test_component_certs.py` corresponds to
`components/certs.py`, and so on; cross-layer end-to-end behavior is covered by
`tests/test_integration.py` and `devproxy selftest`.

---
