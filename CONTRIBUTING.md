# Contributing

Thanks for spending time on devproxy. This tool **modifies the user's machine** (it
installs a CA into the system trust store, changes the machine-wide DNS resolution path,
and writes service units), so changes call for extra restraint: **rather do less than
half a thing**.

中文版：[CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)

## Development environment

Dependencies are managed with [uv](https://docs.astral.sh/uv/) (the `uv.lock` file must
be committed).

```bash
uv sync --locked --all-groups   # install everything per the lock file (incl. dev deps)
uv run pytest -q                # unit tests (no root, never touches the real system)
uv run ruff check src/ tests/   # lint
uv run ruff format src/ tests/  # formatting
uv run mypy                     # type checking
```

Install locally in editable mode (runs the current source, not an installed copy):

```bash
uv tool install -e .
```

## Before committing

CI runs these four; run them locally to save a round trip:

| Check | Command |
|---|---|
| Unit tests | `uv run pytest -q` |
| Lint | `uv run ruff check src/ tests/` |
| Formatting | `uv run ruff format --check src/ tests/` |
| Types | `uv run mypy` |

When you change behaviour, also update:

- the matching prose under `docs/` (commands, configuration, the exit-code table);
- the "Unreleased" section of `CHANGELOG.md`;
- if you touch the CLI, **do not change exit-code semantics** — the exit-code table in
  `docs/commands.md` is a public contract that external scripts branch on.

## Code style

- One command per file (`src/devproxy/cli/*.py`), with shared logic in `_shared.py`.
  Command modules look dependencies up **at call time** via
  `from devproxy.cli import _shared as s` (`s.build_context(...)`), so patching
  `_shared.<name>` in tests takes effect.
- Comments and docstrings are in English; they explain **why** (constraints, traps,
  boundaries) rather than restating the code.
- Domain exceptions must be mapped to a readable message plus an exit code — **never let
  a traceback reach the terminal**.
- Before adding a dependency, ask whether the standard library can do it.

## Before opening a PR

- [ ] All four checks above pass
- [ ] Documentation is updated
- [ ] New behaviour is covered by tests; for a bug fix, **write the failing test first**
- [ ] If you touched the platform layer (`src/devproxy/platform/`), say how you verified
      it on Linux and on macOS
- [ ] If you touched real-machine behaviour (installing services, editing `/etc`,
      certificate handling), add an entry to `docs/verification-checklist.md`

## Changes that need elevation

`devproxy setup` / `trust` / `uninstall` touch the system. Pay extra attention:

- elevation always goes through `sudo -n` (never pop a password prompt); fail with a
  clear message when non-interactive;
- print what is about to change before writing anything, so the user can decline;
- failures must be recoverable (`devproxy setup --rollback <dir>`).

## Reporting bugs

Use the repository's **bug template** (it appears when you open an issue). The most
useful parts are the **full command, the exit code, and `devproxy doctor` output** — most
errors in this tool are diagnosed through exit-code semantics (see the exit-code table in
`docs/commands.md`).

For security issues see [SECURITY.md](SECURITY.md); **do not** open a public issue.
