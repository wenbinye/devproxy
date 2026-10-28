# Troubleshooting

## Common symptoms


- **mise (or other tools) warn "Error loading CA root certificate: failed to read PEM ... Permission denied" at `/etc/ssl/certs/caddy-local.pem`**:
  a leftover of the predecessor `devp` script: it installed the CA as
  `/usr/local/share/ca-certificates/caddy-local.crt` with `cp`, which carried the source
  file's 0600 over -- the copy is root-only, and every tool that walks the trust store
  directory fails on it. Run `devproxy trust`: it recognizes the leftover by name, checks
  its fingerprint, and removes it (update-ca-certificates drops the stale symlink with
  it). Verify: `ls /usr/local/share/ca-certificates/` no longer lists `caddy-local.crt`.
- **`devproxy up` reports "conflict web ... sibling port changed (DEVPROXY_API_PORT: 49435 -> 51717)"**:
  a sibling route was restarted on another port, and the app still holds the old one. Run
  `devproxy down <name>` and then `devproxy up` again -- or pin the sibling's `port` in
  `devproxy.toml` so it never moves.
- **`devproxy up` reports "route 'web' uses $DEVPROXY_API_PORT, but no route 'api' is declared in this file"**:
  the command references a route that this `devproxy.toml` does not declare -- devproxy only
  exports the ports of this project's own routes. Declare the route in the same file.
- **`devproxy up` reports "ready timeout"**: the app most likely does not read `PORT`. Change it to
  `sh -c '... $PORT ...'`, or pass `--port N` explicitly (in that case the app must bind that port itself).
- **`up` reports "Caddy is not running (Admin API unreachable)"** (exit code 3): run `devproxy setup` first,
  or `systemctl --user start caddy` / `launchctl kickstart -k system/com.devproxy.caddy`.
- **`up` reports "main Caddyfile not found"** (exit code 3): setup has not run yet; run `devproxy setup` first.
- **curl reports a certificate error / Chrome reports `net::ERR_CERT_AUTHORITY_INVALID`**:
  `devproxy trust` has not been run yet, or the CA was generated afterwards. The order is: run `up` first
  (generates the CA) → then `trust`.
  Chrome only reads the NSS DB, so after installing you must **fully quit and restart Chrome**.
- **`doctor` says "CA root certificate is not trusted (missing: system trust store, NSS DB)"**: run `devproxy trust`.
  On Linux, if it reports `certutil` missing, first run `sudo apt install libnss3-tools`.
- **macOS: `devproxy trust` fails with `SecTrustSettingsSetTrustSettings: The authorization was denied since no user interaction was possible`**:
  the trust flag can only be set in a **graphical session**, so `sudo` is not enough -- run
  `devproxy trust` in the Mac's own terminal (over SSH it fails). The certificate does land
  in `/Library/Keychains/System.keychain` either way, but without the trust flag nothing
  trusts it -- `curl` then still fails with exit 60 and `doctor` still reports it missing.
- **Chrome works but Firefox still reports a certificate error**: Firefox has its own certificate store
  (`~/.mozilla/firefox/<profile>/cert9.db`), which **does not read** the NSS DB or the system trust store.
  Import the CA manually, or enable `security.enterprise_roots.enabled` in `about:config`;
  both require **fully quitting and restarting Firefox**. See the "Firefox needs separate trust" section above.
- **After upgrading, every command reports "invalid config option domain_suffix"**: the newly added top-level
  domain whitelist rejects your original `domain_suffix`. See the "Upgrade note: existing users may suddenly
  get errors" section above.
- **`doctor` says "caddy version mismatch"**: setup will replace it with the pinned version; rerun `devproxy setup`.
- **Changed `storage` in `config.toml` but it has no effect**: see the "storage archive" section above.
- **The domain resolves to another IP**: `doctor` does not check resolution (it only does static checks); check
  once yourself with `resolvectl query <name>.test` (on macOS use `dscacheutil -q host -a name <name>.test`);
  after confirming the route file is in place, on Linux restart systemd-resolved if needed with
  `sudo systemctl restart systemd-resolved`.
- **`devproxy logs caddy` prints a lot but does not exit**: this is expected -- it streams/follows;
  Ctrl-C exits.
- **Route residue** (devproxy was `kill -9`ed): `devproxy ls` marks unreachable routes;
  `devproxy down <name>` clears them.
- **`devproxy logs dnsmasq`** shows dnsmasq's logs; on Linux it is journalctl, on macOS
  `~/.local/state/devproxy/logs/dnsmasq.log`.
- **`devproxy run` says "the caddy service is still running and holds 80/443"**: first stop the service as
  instructed (Linux `systemctl --user stop caddy`, macOS `launchctl bootout system/com.devproxy.caddy`),
  or use `--config` to point at a Caddyfile that uses other ports.

For the real-machine verification checklist see [verification-checklist.md](verification-checklist.md) --
it lists behaviours automated tests cannot cover that must be run item by item on a real machine.

---
