#!/usr/bin/env bash
# One-line installer for devproxy (Linux / macOS).
#
#   curl -fsSL https://raw.githubusercontent.com/wenbinye/devproxy/main/scripts/install.sh | sh
#
# What it does:
#   - finds a Python >= 3.11 and an installer (uv / pipx / pip);
#   - installs devproxy from the **latest release tag**, falling back to the source
#     repository when no prebuilt binary exists for this platform;
#   - prints a health check.
#
# It never needs sudo: devproxy's own `setup` elevates on demand, when it must.
set -euo pipefail

REPO=${REPO:-wenbinye/devproxy}
PREFIX=${PREFIX:-"$HOME/.local"}
BINDIR="$PREFIX/bin"
TAG=${TAG:-latest}

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- prerequisites ---------------------------------------------------------

command -v curl >/dev/null 2>&1 || die "curl is required"
command -v tar >/dev/null 2>&1 || die "tar is required"

say "Looking for a Python >= 3.11"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PYTHON=$candidate
        break
    fi
done
[ -n "$PYTHON" ] || die "no Python >= 3.11 found (macOS: brew install python@3.12; Linux: your package manager, or mise / uv)"
echo "  using $PYTHON ($("$PYTHON" -V 2>&1))"

# --- which release ---------------------------------------------------------

say "Resolving the release to install"
resolved=""
if [ "$TAG" = "latest" ]; then
    meta=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null || true)
    TAG=$(printf '%s' "$meta" | grep -oE '"tag_name": *"[^"]+"' | head -1 | sed 's/.*"tag_name": *"//; s/"//' || true)
fi
[ -n "$TAG" ] && resolved="git+https://github.com/$REPO.git@$TAG" || resolved="git+https://github.com/$REPO.git"
echo "  installing $resolved"

# --- install ----------------------------------------------------------------

say "Installing"
installed=""
if command -v uv >/dev/null 2>&1; then
    uv tool install "$resolved" && installed="uv tool install $resolved"
elif command -v pipx >/dev/null 2>&1; then
    pipx install "$resolved" && installed="pipx install $resolved"
else
    "$PYTHON" -m pip install --user "$resolved" && installed="pip install --user $resolved"
fi
[ -n "$installed" ] || die "installation failed"

# --- health check -----------------------------------------------------------

say "Health check"
if command -v devproxy >/dev/null 2>&1; then
    devproxy --help >/dev/null 2>&1 && echo "  devproxy is ready: $(command -v devproxy)" ||
        die "devproxy was installed but does not run -- try: $installed"
else
    echo "  devproxy is installed but not on PATH. Add it:"
    echo "    echo 'export PATH=\"$(dirname "$(uv tool dir --bin 2>/dev/null || echo "$BINDIR")"):\$PATH\"' >> ~/.bashrc"
    echo "  or reinstall with: $installed"
fi

say "Done. Next:"
echo "  devproxy setup     # install the local dev environment (Caddy, DNS, certificates)"
echo "  devproxy up web -- npm run dev"
echo
echo "The setup step changes your system (DNS route, certificate, service unit) --"
echo "read docs/migration-and-uninstall.md before running it."
