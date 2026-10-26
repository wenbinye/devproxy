# devproxy

<p align="center">
  <img src="assets/icon-256.png" width="128" alt="devproxy icon: several local services converging into one HTTPS padlock that points at a .test domain">
</p>

<p align="center">
  <a href="https://github.com/wenbinye/devproxy/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/wenbinye/devproxy/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://pypi.org/project/devproxy/"><img alt="PyPI" src="https://img.shields.io/pypi/v/devproxy.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-blue.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS-lightgrey.svg">
</p>

<p align="center">
  <img src="assets/demo.gif" width="820" alt="devproxy demo: the route table, starting an app under a domain, and curl over HTTPS without -k">
</p>

## Local services under real HTTPS domains

`devproxy` puts whatever runs on your machine behind `https://<name>.test` — developers
remember domains, not ports.

```bash
devproxy up api -- npm run dev
# ✅ https://api.test
```

| What you get | Why it matters |
|---|---|
| **One command for the whole stack** | `devproxy setup` installs Caddy + dnsmasq, points `*.test` at your machine, trusts the CA in the system store and enables autostart. No config files to write. |
| **A domain per app, not a port** | `https://api.test` instead of `https://localhost:3000` — shareable with the team, and the browser stops warning about the certificate. |
| **The route table is a file** | `routes.json` — routes survive a Caddy restart, with no resident daemon to babysit. |
| **Shareable with the team** | Commit a `devproxy.toml` and everyone runs the same domains. |

---

## ⚠️ This tool changes your system

`devproxy setup` is not "install a Python package and you're done". It writes into system
directories, installs service units, changes the DNS resolution path, and puts a
self-signed CA into your system trust store. **Read this section before running it.**

What it touches:

| What | Where (Linux) | Where (macOS) |
|---|---|---|
| Caddy binary | `~/.local/bin/caddy` (downloaded, sha256-verified) | same |
| Main Caddy config | `~/.config/devproxy/Caddyfile` | same |
| Your personal sites | `~/.config/devproxy/sites.caddy` (imported only, never rewritten) | same |
| Caddy service unit | `~/.config/systemd/user/caddy.service` (user scope) | `/Library/LaunchDaemons/com.devproxy.caddy.plist` (**root** LaunchDaemon) |
| dnsmasq service unit | `~/.config/systemd/user/dnsmasq.service` (user scope) | `~/Library/LaunchAgents/com.devproxy.dnsmasq.plist` |
| dnsmasq config | merged incrementally into `~/.config/dnsmasq/dnsmasq.conf` | same |
| DNS route | `/etc/systemd/resolved.conf.d/devproxy.conf` (or `/etc/dnsmasq.d/devproxy.conf`) | `/etc/resolver/test` |
| CA root certificate | `/usr/local/share/ca-certificates/devproxy-local.crt` + NSS DB `~/.pki/nssdb` | system keychain `/Library/Keychains/System.keychain` |
| Autostart | `loginctl enable-linger <user>` | launchd's `RunAtLoad` |

**On the Linux `resolved` backend the DNS change is machine-wide** — this deserves its
own explanation:

- The `DNS=127.0.0.1:5353` written into the drop-in lands in resolved's **global**
  section, so it decides that **every domain on this machine** asks the user-scope
  dnsmasq first — **not just `*.test`**;
- The same drop-in carries `FallbackDNS=1.1.1.1 8.8.8.8`, which is a **machine-wide
  fallback**: if dnsmasq is not up, all resolution degrades to those two public resolvers;
- `Domains=~.<suffix>` is the line that hands `<suffix>` to dnsmasq — but given that
  `DNS=` already points all resolution at dnsmasq, it only decides how dnsmasq splits
  traffic internally.

`devproxy setup` prints this machine-wide scope **again on its own** when showing the
change plan (it neither fits nor reads well in the table), so you see it before you
confirm. macOS's per-domain resolver file and the Linux `system` backend's dnsmasq
snippet affect **only this suffix**, never the whole machine.

**Steps that need sudo:**

- Linux
  - `sudo setcap cap_net_bind_service=+ep ~/.local/bin/caddy` (lets caddy bind 80/443)
  - `sudo` writes `/etc/systemd/resolved.conf.d/devproxy.conf`, then `sudo systemctl restart systemd-resolved`
  - `sudo loginctl enable-linger <user>` (autostart even when not logged in)
  - `sudo` installs the CA into the system trust store and runs `update-ca-certificates`
- macOS
  - `sudo` writes `/Library/LaunchDaemons/com.devproxy.caddy.plist` (macOS has no
    `setcap`; binding 80/443 requires a root LaunchDaemon)
  - `sudo` writes `/etc/resolver/test`
  - `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain <ca>`

**Steps that need no sudo**: downloading caddy; writing `~/.config/devproxy/` and
`~/.local/state/devproxy/`; merging the dnsmasq config; installing the systemd user unit /
LaunchAgent; installing into the NSS DB (needs `certutil`).

In a non-interactive environment (CI, scripts), `setup` / `trust` need an explicit
`--yes` whenever a step requires elevation; otherwise the tool refuses with exit code 4
(it neither skips silently nor elevates silently).

**Want to undo it?** See [Uninstall](docs/migration-and-uninstall.md#uninstall):
`devproxy uninstall --dry-run` shows what it would do.

---

## Install

**macOS / Linux, one line:**

```bash
curl -fsSL https://raw.githubusercontent.com/wenbinye/devproxy/main/scripts/install.sh | sh
```

The script picks the latest release, verifies its sha256 and drops the CLI into
`~/.local/bin` — it never needs sudo (`devproxy setup` elevates on demand, when it
must).

**Once it is on PyPI**, the usual suspects work too:

```bash
uv tool install devproxy     # or: pipx install devproxy
```

**From the source repository** (any version, no release needed):

```bash
uv tool install 'git+https://github.com/wenbinye/devproxy.git'
```

To pin a branch / tag / commit:

```bash
uv tool install 'git+https://github.com/wenbinye/devproxy.git@main'
uv tool install 'git+https://github.com/wenbinye/devproxy.git@<tag or commit>'
```

Without `@...` you get the repository's default branch (currently `main`). Upgrade to
the latest upstream:

```bash
uv tool upgrade devproxy
```

No `uv`? macOS: `brew install uv`; Linux: the official install script.

If you would rather not use uv, pipx works with the same git URL:

```bash
pipx install 'git+https://github.com/wenbinye/devproxy.git'
```

**Local development** uses an editable install — source changes take effect immediately:

```bash
git clone https://github.com/wenbinye/devproxy.git
cd devproxy
uv tool install -e .
```

`uv tool install devproxy` only works once the package is on PyPI — until then that
command fails (there is no such package on PyPI, and the name may collide later).

Requirements: Python ≥ 3.11 (uv picks a suitable interpreter), with only `typer` and
`rich` at runtime.

After installing, `devproxy --help` should list every subcommand. The command lands in
`~/.local/bin/devproxy`; if `command -v devproxy` cannot find it, run `uv tool update-shell`
once, or add `~/.local/bin` to `PATH`.

---

## Quick start

```bash
devproxy setup     # deploy Caddy + dnsmasq + the DNS route + autostart (idempotent)
devproxy doctor    # health check (static, no DNS lookups): missing pieces come with a fix
```

After `setup`:

```bash
# Start a service and register https://codeup.test
devproxy up codeup -- sh -c 'npx vite --port $PORT --host 127.0.0.1'

# Give an already-running service a domain (without starting it)
devproxy expose console --port 3000

devproxy ls                 # route list (with reachability)
devproxy down codeup        # unregister the route (its child process goes too)
devproxy logs caddy         # follow logs (Ctrl-C exits; the command never ends on its own)

# When the project has a devproxy.toml (committed, shared by the team):
devproxy up               # reconcile the routes with the declarations
```

### The CA root certificate only exists after the first route is registered

Caddy's internal CA is generated **the first time it loads a `tls internal` site** — that
is, **when the first route is registered** (`devproxy up` and `devproxy expose` both
count). So on a clean machine:

1. `devproxy setup` succeeds, but there is **no CA root certificate** in the system yet
   (`setup` prints a note about this at the end);
2. Caddy generates the CA when the first route is registered
   (`devproxy up <name> -- ...` or `devproxy expose <name> --port N`);
3. **You still need to run `devproxy trust` once** before curl / Chrome will trust it.

Until the CA exists, `devproxy doctor` reports "no CA root certificate yet (Caddy
creates it when the first route is registered)" — that is a normal state, not a fault.

---

## Commands

| Command | What it does |
|---|---|
| `devproxy setup` | Deploy the local dev environment (idempotent) |
| `devproxy setup --rollback <backup dir>` | Roll back from a backup directory (see "Migration and rollback") |
| `devproxy doctor` | Check every precondition (static: files present, services running, certificate installed, unit pointing at this tool); missing pieces come with a fix |
| `devproxy up <name> [--port N] [--ready-timeout S] -- CMD...` | Allocate a port, start the command, register `https://<name>.test` |
| `devproxy expose <name> --port N` | Give an already-running service a domain; returns immediately, the route persists (an unresponsive upstream only warns, the route is still registered) |
| `devproxy down <name>` | Unregister a route; if its child process is still alive it is stopped too |
| `devproxy ls` / `devproxy ps` | List every registered route (with reachability), sorted by app name |
| `devproxy logs [caddy\|dnsmasq\|<route name>]` | Follow logs: a service (default `caddy`) or an app started by up |
| `devproxy logs <route name> --edit` | Open the route's log file with `$EDITOR` (falls back to `vim` / `vi` / `nano`; `--list` shows what exists plus full paths) |
| `devproxy trust` | Install the CA root certificate on its own (system store + NSS DB / system keychain) |
| `devproxy config` | Show the effective configuration and the rendered route snippet |
| `devproxy selftest` | End-to-end self-check against an isolated Caddy instance |
| `devproxy uninstall` | Uninstall: stop services, remove units, the DNS route, and (optionally) the CA. Conservative by default: your config, personal sites, Caddy storage, and backups stay |
| `devproxy up` | In a project: apply **every** declaration in `devproxy.toml` (compose's `up -d`) |
| `devproxy down` | In a project: stop **every** app it declares; with a route name, only that one |
| `devproxy run` | Supervise Caddy in the **foreground** (installs no service, keeps no state; Ctrl-C exits). For environments without systemd (containers / WSL) |
| `devproxy init` | Generate a `devproxy.toml` in the project (route name from the directory, start command guessed from the project type or given with `--command` / `--from`) |
| `devproxy --install-completion` | Install shell completion: commands, options, and the **route names** in `logs` / `down` (bash / zsh / fish / powershell) |

Every option of every command: see [docs/commands.md](docs/commands.md).

---

## Documentation

The details live under [docs/](docs/), pick what you need:

| Document | Contents |
|---|---|
| [docs/commands.md](docs/commands.md) | Every command, option, and exit code |
| [docs/apps.md](docs/apps.md) | Wiring apps up: `expose`, the two ways to use `$PORT`, caveats |
| [docs/dns-and-certs.md](docs/dns-and-certs.md) | The DNS route, certificate trust (including Firefox), `doctor` vs `setup` |
| [docs/configuration.md](docs/configuration.md) | Every `config.toml` setting, the domain-suffix allowlist, the storage archive |
| [docs/project-config.md](docs/project-config.md) | `devproxy.toml`: the declarative `up` / `down` workflow |
| [docs/platforms.md](docs/platforms.md) | Directories and files, platform differences, `devproxy run` without systemd |
| [docs/migration-and-uninstall.md](docs/migration-and-uninstall.md) | Migration and rollback, uninstall |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Troubleshooting |
| [docs/development.md](docs/development.md) | Development and CI, code structure |

Find documents by scenario: [docs/README.md](docs/README.md).
Real-machine verification checklist: [docs/verification-checklist.md](docs/verification-checklist.md).

**Docs in Chinese:** [README.zh-CN.md](README.zh-CN.md) and [docs/zh-CN/](docs/zh-CN/).

---

## Contributing

| File | When to read it |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | Before a PR: dev environment, checklist, code style |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | The community's code of conduct |
| [SECURITY.md](SECURITY.md) | Reporting security issues (**do not** open a public issue); also the boundaries of what this tool changes |
| [CHANGELOG.md](CHANGELOG.md) | User-visible changes in every release |

## License

[MIT](LICENSE) © 2026 Ye Wenbin
