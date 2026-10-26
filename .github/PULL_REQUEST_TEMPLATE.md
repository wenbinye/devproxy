## What this PR does

<!-- One or two sentences. Link the issue: Fixes #123 -->

## How it was verified

<!--
Not just "it works". Be specific:
- which commands / tests you ran;
- on which platforms (Linux / macOS);
- what was **not** verified (real-machine behaviour? needs elevation? needs a real Caddy?)
-->

## Checklist

- [ ] `uv run pytest -q` passes
- [ ] `uv run ruff check src/ tests/` passes
- [ ] `uv run ruff format --check src/ tests/` passes
- [ ] `uv run mypy` passes
- [ ] Behaviour changed? The matching prose under `docs/` is updated
- [ ] CLI changed? Exit-code semantics are unchanged (see the exit-code table in `docs/commands.md`)
