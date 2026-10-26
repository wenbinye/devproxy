#!/usr/bin/env bash
# Record the demo session: every step's real output (ANSI included) into demo/step-NN.txt,
# then render assets/demo.gif from it.
#
# What it does on the machine it runs on:
#   - shows the route table, starts a throwaway app under a domain (demo.test),
#     shows the route table again, and curls it over HTTPS;
#   - removes the demo route again at the end (the app goes with it).
#
# The only state it touches is that one demo route. Run it on a machine whose Caddy
# you do not mind reloading.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-$ROOT/demo}
GIF=${2:-$ROOT/assets/demo.gif}
DEVPROXY=${DEVPROXY:-$ROOT/.venv/bin/devproxy}
NAME=${NAME:-demo}
COLUMNS=${COLUMNS:-120}

mkdir -p "$OUT"
rm -f "$OUT"/step-*.txt

capture() { # capture <file> <display-command> <command...>
    local file="$1" shown="$2"; shift 2
    script -qec "COLUMNS=$COLUMNS $*" /dev/null > "$file" 2>&1 || true
    _with_prompt "$file" "$shown"
}

# Put the command in front of its output, the way a terminal shows it.
_with_prompt() { # _with_prompt <file> <shown-command>
    local file="$1" command="$2" tmp
    tmp=$(mktemp)
    {
        printf '\033[32m$\033[0m %s\n' "$command"
        # Drop the noise script(1) leaves behind, and the app's own log line
        # (the demo is about devproxy, not about what the app prints).
        grep -v -e "^Script started" -e "^Script done" -e "Session terminated" \
            -e "^Serving HTTP on" "$file" || true
    } > "$tmp"
    mv "$tmp" "$file"
}

cleanup() {
    "$DEVPROXY" down "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> recording into $OUT"

# 1) the routes that exist before
capture "$OUT/step-01.txt" "devproxy ls" "$DEVPROXY ls"

# 2) start the app -- `devproxy up` stays in the foreground with it, so the session
#    runs in the background and the recording moves on
script -qec "COLUMNS=$COLUMNS \
    $DEVPROXY up $NAME -- sh -c 'python3 -m http.server \$PORT'" /dev/null \
    > "$OUT/step-02.raw" 2>&1 &
UP_SESSION=$!
sleep 6
_with_prompt "$OUT/step-02.raw" \
    "devproxy up $NAME -- python3 -m http.server \$PORT"
mv "$OUT/step-02.raw" "$OUT/step-02.txt"

# 3) the routes now
capture "$OUT/step-03.txt" "devproxy ls" "$DEVPROXY ls"

# 4) the payoff: HTTPS works, no -k, no port to remember
capture "$OUT/step-04.txt" "curl -sI https://$NAME.test | head -1" \
    "curl -sI https://$NAME.test | head -1"

kill "$UP_SESSION" 2>/dev/null || true
cleanup
trap - EXIT

echo "==> rendering $GIF"
python3 "$ROOT/scripts/make-demo-gif.py" "$OUT" "$GIF" 1800
echo "==> done: $GIF"
