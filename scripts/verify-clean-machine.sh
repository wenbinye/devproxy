#!/usr/bin/env bash
# End-to-end verification of devproxy in a clean ubuntu container (the reproducible
# form of checklist item F10).
#
# Usage (from the devproxy repository root):
#   bash scripts/verify-clean-machine.sh
#
# What it does:
#   1. packages the source of the **current commit** with `git archive HEAD`
#      (no .git / .venv);
#   2. starts a clean ubuntu container (non-root user with passwordless sudo, no systemd);
#   3. pip-installs it and verifies layer by layer: CLI basics -> root rejection ->
#      graceful failure of setup on a bare machine -> standing in for systemd by hand ->
#      `run` supervision -> `up` -> `trust` -> curl -> `down`.
#
# Prerequisites: docker available and network access (pull the image, pip-install
# typer/rich, download caddy). The container is removed on exit; the host is untouched.

set -euo pipefail

CONTAINER=${CONTAINER:-dp-clean}
IMAGE=${IMAGE:-ubuntu:24.04}
DEVPROXY='~/.venv/bin/devproxy'   # absolute path for the dev user inside the container
                                  # (the quotes must stay: the tilde has to expand
                                  # **inside the container**, not on the host)

PASS=0
FAIL=0

check() { # check <description> <command...>
    local desc="$1"; shift
    local out
    if out=$("$@" 2>&1); then
        echo "  ✔ $desc"; PASS=$((PASS+1)); return 0
    fi
    echo "  ✘ $desc"
    echo "$out" | head -3 | sed 's/^/      /'
    FAIL=$((FAIL+1))
    return 0   # always return 0: one failing item must not abort the whole run (set -e)
}

expect_absent() { # expect_absent <description> <text that must not appear> <command...>
    local desc="$1" banned="$2"; shift 2
    local out rc=0
    out=$("$@" 2>&1) || rc=$?   # under set -e the exit status must be caught with ||;
                                # the assignment itself would propagate it
    if [[ $rc -ne 0 && "$out" != *"$banned"* ]]; then
        echo "  ✔ $desc"; PASS=$((PASS+1)); return 0
    fi
    echo "  ✘ $desc (exit code $rc; output contains '$banned' but must not)"
    echo "    actual output: $(echo "$out" | head -3 | tr '\n' ' ')"
    FAIL=$((FAIL+1))
    return 0
}

expect_fail() { # expect_fail <description> <text that must appear> <command...>
    local desc="$1" want="$2"; shift 2
    local out rc=0
    out=$("$@" 2>&1) || rc=$?   # same set -e note as above
    if [[ $rc -ne 0 && "$out" == *"$want"* ]]; then
        echo "  ✔ $desc"; PASS=$((PASS+1)); return 0
    fi
    echo "  ✘ $desc (exit code $rc; '$want' not found in the output)"
    echo "    actual output: $(echo "$out" | head -3 | tr '\n' ' ')"
    FAIL=$((FAIL+1))
    return 0
}

runc() { # run a command inside the container as the dev user (login shell, clean PATH)
    docker exec -u dev "$CONTAINER" bash -lc "$*"
}

cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -f /tmp/dp-src.tar
}
trap cleanup EXIT

echo "==> 1) Packaging the source of the current commit ($(git rev-parse --short HEAD))"
git archive HEAD > /tmp/dp-src.tar

echo "==> 2) Starting a clean container ($IMAGE)"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" --hostname cleanbox "$IMAGE" sleep infinity >/dev/null
docker exec "$CONTAINER" bash -c "
    apt-get -qq update >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get -qq install -y \
        python3-venv python3-pip sudo curl ca-certificates dnsutils >/dev/null
    useradd -m -s /bin/bash dev
    echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev
    chmod 440 /etc/sudoers.d/dev
"

echo "==> 3) Installing devproxy (clean machine; the only prerequisite is python3-venv)"
docker cp /tmp/dp-src.tar "$CONTAINER":/tmp/src.tar >/dev/null
docker exec "$CONTAINER" bash -c "mkdir -p /opt/src && tar -xf /tmp/src.tar -C /opt/src && chown -R dev:dev /opt/src"
runc "python3 -m venv ~/.venv && ~/.venv/bin/pip install -q /opt/src"

echo "==> 4) Tier 1: CLI basics"
runc "cd ~ && mkdir -p work/demo && cd work/demo && git init -q . 2>/dev/null; true"
check "config reads the default configuration"  runc "$DEVPROXY config"
check "ls on an empty route table"              runc "$DEVPROXY ls"
check "init generates a declaration"            runc "cd ~/work/demo && $DEVPROXY init --name demo --command 'npm run dev'"
check "what init generated can be parsed"       runc "cd ~/work/demo && grep -q 'name = \"demo\"' devproxy.toml"

echo "==> 5) Tier 2: root rejection + graceful failure of setup on a bare machine"
expect_fail "running under sudo is rejected (exit 3)" "run devproxy as a regular user" \
    runc "cd ~/work/demo && sudo $DEVPROXY setup --yes"
expect_fail "bare-machine setup: the missing-setcap hint is actionable" "libcap2-bin" \
    runc "cd ~/work/demo && timeout 120 $DEVPROXY setup --yes"
expect_absent "bare-machine setup: no traceback" "Traceback" \
    runc "cd ~/work/demo && timeout 120 $DEVPROXY setup --yes"

echo "==> 6) Tier 3 preparation: install the packages + stand in for systemd by hand"
docker exec "$CONTAINER" bash -c "
    DEBIAN_FRONTEND=noninteractive apt-get -qq install -y libcap2-bin dnsmasq >/dev/null
    # dnsmasq on port 53 acts as the system resolver (the container has no resolved):
    # .test is taken over by the tool's config, everything else is forwarded to docker's DNS
    dnsmasq --port=53 --listen-address=127.0.0.1 --no-resolv \
        --address=/.test/127.0.0.1 --server=192.168.50.1 2>/dev/null || \
    dnsmasq --port=53 --listen-address=127.0.0.1 --no-resolv \
        --address=/.test/127.0.0.1 --server=127.0.0.11
    printf 'nameserver 127.0.0.1\n' > /etc/resolv.conf
    sleep 1
" || true
check "demo.test resolves to 127.0.0.1 through the system resolver" \
    docker exec "$CONTAINER" bash -c "dig +short demo.test 2>/dev/null | grep -q 127.0.0.1"

echo "==> 7) Tier 3: setup produces everything -> run supervises -> up -> trust -> curl"
runc "cd ~/work/demo && timeout 180 $DEVPROXY setup --yes" || true
check "the caddy binary was downloaded"          docker exec "$CONTAINER" test -x /home/dev/.local/bin/caddy
check "setcap took effect"                       docker exec "$CONTAINER" bash -c "getcap /home/dev/.local/bin/caddy | grep -q cap_net_bind_service"
check "the main Caddyfile was generated"         docker exec "$CONTAINER" test -f /home/dev/.config/devproxy/Caddyfile
check "the DNS route file was written"           docker exec "$CONTAINER" bash -c "grep -q address=/.test/ /etc/dnsmasq.d/devproxy.conf"

docker exec -u dev -d "$CONTAINER" bash -c "nohup $DEVPROXY run > /tmp/run.log 2>&1"
sleep 4
check "run: Caddy is up (Admin API)" \
    docker exec "$CONTAINER" bash -c "curl -s http://127.0.0.1:2019/config/ >/dev/null"
# The declaration is written into the container with a heredoc -- this avoids the
# quoting hell of passing --command through two shells.
# Note: without any site block Caddy does **not** listen on 80/443 -- the port-listening
# check has to happen after `up`.
docker exec -i -u dev "$CONTAINER" bash -c 'cat > /home/dev/work/demo/devproxy.toml' <<'TOML'
[[routes]]
name = "demo"
command = ["sh", "-c", "python3 -m http.server $PORT"]
TOML
check "up: the app starts and the route is registered"  runc "cd ~/work/demo && timeout 60 $DEVPROXY up --yes"
# Known behaviour: there is a short window between `up` returning and Caddy finishing the
# rebind (~2s measured), so retry here -- and take the opportunity to record it as a known
# race on the product side
check "run: 80/443 are listening (setcap works; tolerating the rebind window)" \
    docker exec "$CONTAINER" bash -c "for i in 1 2 3 4 5 6 7 8 9 10; do (echo > /dev/tcp/127.0.0.1/443) 2>/dev/null && exit 0; sleep 1; done; exit 1"
check "the first route triggers CA generation"  docker exec "$CONTAINER" test -f /home/dev/.local/share/caddy/pki/authorities/local/root.crt

# trust: the NSS step fails without certutil (expected).
# Note that the system-trust step **may be skipped** -- caddy also borrows sudo to install
# its own root CA at startup, and devproxy sees "already trusted" and does not install it
# twice. So do not assert that devproxy-local.crt exists; assert the property: **the system
# trust store can verify this CA**.
runc "cd ~/work/demo && timeout 60 $DEVPROXY trust --yes" || true
check "trust: the system trust store verifies this CA" \
    runc "openssl verify -CApath /etc/ssl/certs ~/.local/share/caddy/pki/authorities/local/root.crt"

check "curl https://demo.test -> 200 (no -k)" \
    docker exec "$CONTAINER" bash -c "curl -s -o /dev/null -w '%{http_code}' https://demo.test/ | grep -q 200"

check "down: the handshake fails after unregistering" \
    runc "cd ~/work/demo && $DEVPROXY down demo >/dev/null 2>&1; ! curl -s -o /dev/null https://demo.test/"

echo
echo "==> Result: $PASS passed / $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
    echo "There are failures. The container is kept ($CONTAINER) so you can investigate:"
    echo "  docker exec -u dev $CONTAINER bash -l"
    trap - EXIT
    exit 1
fi
echo "All passed. The container was cleaned up."
