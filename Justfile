# Two kinds of recipes, two different jobs:
#
# **1. Developer recipes** (test / lint / fix / sync) -- the repository's own engineering
#    tasks, not devproxy commands. These are exactly what CI runs.
#
# **2. A compatibility shell that forwards to devproxy** -- all the logic lives in the
#    devproxy command; nothing is implemented here. It only keeps the old `just` habits:
#         just setup   ->  devproxy setup
#         just doctor  ->  devproxy doctor
#
# Note: the forwarding shell deliberately has no `up` target. just's variadic `{{args}}`
# does pass `--` through, but **quotes are lost in the forward** ('sh -c "..."' is split
# into separate words, so the child gets different semantics); and a fixed-arity recipe
# treats `--` as a target name and errors out (Justfile does not contain recipe '--').
# Either approach would change what `devproxy up` means, so run
# `devproxy up <name> -- CMD...` directly.

# -- Developer recipes ----------------------------------------------------

# Unit tests (no root needed; never touches the real system)
test:
    uv run pytest -q

# Lint (ruff check + format check + type check)
lint:
    uv run ruff check src/ tests/
    uv run ruff format --check src/ tests/
    uv run mypy

# Auto-fix (formatting plus the lint issues that can be fixed automatically)
fix:
    uv run ruff check --fix src/ tests/
    uv run ruff format src/ tests/

# Install the development environment from the lock file
sync:
    uv sync --locked --all-groups

# Default: show devproxy's help
default:
    @uv run devproxy --help

# Deploy the local dev environment (Caddy + dnsmasq + DNS route + CA trust + autostart)
setup:
    uv run devproxy setup

# Check every precondition
doctor:
    uv run devproxy doctor

# Follow a service log (caddy by default)
logs service="caddy":
    uv run devproxy logs {{service}}

# Health check (the same as doctor)
status:
    uv run devproxy doctor

# Unregister a route
down name:
    uv run devproxy down {{name}}

# List the registered routes
ls:
    uv run devproxy ls

# Install the CA root certificate
trust:
    uv run devproxy trust

# Show the effective configuration
config:
    uv run devproxy config

# End-to-end self-check against an isolated Caddy instance
selftest:
    uv run devproxy selftest

# Local development install (editable)
install:
    uv tool install -e .

# Old name kept: the `devp` script used to be installed; now it is the devproxy package
install-devp: install

# End-to-end verification in a clean ubuntu container (checklist F10; needs docker and network)
verify-clean:
    bash scripts/verify-clean-machine.sh
