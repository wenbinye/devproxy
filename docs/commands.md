# Command reference

All devproxy commands. The naming follows **docker compose**: project-level operations use `up` / `down` / `ps` / `logs`, while machine-level operations (installing the environment, health checks, trusting certificates) keep their own names.

| Command | One-liner |
|---|---|
| [`up`](#up) | Start services: a single app (foreground) or everything this project declares (background) |
| [`expose`](#expose) | Give an **already-running** service a domain |
| [`down`](#down) | Stop services: a single route or everything this project declares |
| [`ls` / `ps`](#ls--ps) | List routes |
| [`logs`](#logs) | Follow logs |
| [`init`](#init) | Generate `devproxy.toml` |
| [`setup`](#setup) | Deploy the local dev environment (Caddy, dnsmasq, DNS, certificates, autostart) |
| [`doctor`](#doctor) | Health check: report whatever is missing and the command that fixes it |
| [`trust`](#trust) | Install the CA root certificate into the system trust store and the NSS DB |
| [`config`](#config) | Show the configuration currently in effect |
| [`selftest`](#selftest) | End-to-end self-check against an isolated instance |
| [`run`](#run) | Supervise Caddy in the foreground (for environments without systemd) |
| [`uninstall`](#uninstall) | Uninstall: stop services, remove units, the DNS route, and (optionally) the CA |
| [`--install-completion`](#shell-completion) | Install shell completion (bash / zsh / fish / powershell) |

---

## up

Start services. **With a route name** = start a single app in the foreground (the command runs alongside the app, and Ctrl-C ends both); **without a route name** = apply every declaration in `devproxy.toml` in the project directory (runs in the background, compose's `up -d`).

```bash
devproxy up web -- sh -c 'npx vite --port $PORT --host 127.0.0.1'
devproxy up                       # project mode: apply every declaration in devproxy.toml
```

| Option | Description |
|---|---|
| `<name>` | Route name, a single label `[a-z0-9-]`. Omit it for project mode |
| `--port N` | Explicit port (allocated automatically by default); an out-of-range value is a usage error, e.g. `devproxy: invalid port 99999: must be between 1 and 65535`; fails outright if it is already in use |
| `--ready-timeout S` | Ready timeout in seconds (defaults to `ready_timeout` in `config.toml`) |
| `--file <path>` | *[project mode]* Explicit `devproxy.toml` (searched upwards from the current directory by default) |
| `--prune` | *[project mode]* Remove routes deleted from this file (needs `--yes` when non-interactive) |
| `--yes` | *[project mode]* Non-interactive mode; do not ask |

- Everything after `--` is passed to the child process **verbatim**; devproxy does not parse it (so the app's own `--port` is not stolen).
- Project mode is **idempotent**: run it again and apps that are already running are skipped, while **dead apps are recreated**.
- The port may differ each time (automatic allocation); the domain does not change.
- See [Project-level configuration](project-config.md).

## expose

Give an **already-running** service a domain (a database admin console, a mock service, something in Docker, ...).

```bash
devproxy expose datahub --port 9002
# ✅ https://datahub.test  (upstream 127.0.0.1:9002)
#    Remove with: devproxy down datahub
```

| Option | Description |
|---|---|
| `<name>` | Route name (required) |
| `--port N` | Port the upstream is already listening on (required) |

- The upstream must be **`127.0.0.1`**: devproxy proxies to a port on **this machine**. `localhost` inside a container is the container itself.
- You can register even when the upstream is not running: it prints `⚠️  upstream 127.0.0.1:9002 is not responding right now (the route is registered and will work once it is up)`, but the route is still written to the route table (persistent, and it works as soon as the service comes up).
- Routes are **persistent**: they survive Caddy restarts and machine reboots. To remove one: `devproxy down <name>`.

## down

Unregister a route (on success it prints `Unregistered <name>`), taking its child process with it if still running. **With a route name** = stop only that route; **without** = stop every app declared in this project (`devproxy.toml`).

```bash
devproxy down web            # stop one route
devproxy down                # stop everything this project declares
```

- Running it without a name outside a project exits 2 (it does not know what to stop) and tells you to use `devproxy down <name>`.
- Routes started manually with `up` are **not** touched by project-mode `down` (only routes declared in this file are stopped).
- After stopping, one `devproxy up` brings them back.

## ls / ps

List every registered route (including reachability). The two names are equivalent; `ps` follows docker compose.

```bash
devproxy ls
devproxy ps
```

- **Sorted ascending by app name** — the route table is a set, so the output is stable and predictable (independent of registration order). An empty route table prints `(no routes)`.

## logs

Follow logs: the service itself (`caddy` / `dnsmasq`) or **a route** (an app started by `up`).

```bash
devproxy logs              # follow caddy by default
devproxy logs caddy
devproxy logs dnsmasq
devproxy logs my-app       # an app started by up
devproxy logs --list       # list the targets that can be followed, without following
devproxy logs my-app -e    # open the log file with $EDITOR (= --edit)
```

| Option | Description |
|---|---|
| `--list` | List the log targets that can be followed (only this project's when inside one); do not follow. For route logs it gives the **full path of each one** (no need to assemble it yourself) |
| `--edit` / `-e` | Open that route's log file with `$EDITOR` (falling back to `$VISUAL`, then looking for `vim` / `vi` / `nano` in that order) |

- Read-only; changes nothing. **Ctrl-C exits** (the command does not end on its own).
- Running `devproxy logs` bare inside a project directory = follow **every app in this project**; with several apps every line carries a coloured tag (the tag is the app name).
- Route logs are followed **from the end** (no history replay); if the file is rotated it reattaches automatically.
- `--edit` only works on **route logs**: service logs (caddy / dnsmasq) are rolling logs in journalctl, not files, so an editor cannot open them — it reports this explicitly and tells you to follow them instead.
- `--edit` and `--list` are mutually exclusive; if neither `EDITOR` nor `VISUAL` is set and `vim` / `vi` / `nano` cannot be found on `PATH`, it errors and exits 2.
- See [Project-level configuration · Background processes and logs](project-config.md#background-processes-and-logs).

## init

Generate `devproxy.toml` in the project.

```bash
devproxy init
devproxy init --name web --command "npm run dev"
devproxy init --from my-running-svc   # import the start command from an already-registered route with this name
```

| Option | Description |
|---|---|
| `--name <name>` | Route name (defaults to the project directory name) |
| `--command <command>` | Start command (split with shell lexing; quotes and spaces are both honoured) |
| `--from <route name>` | Import the start command from an already-registered route with this name (the route table is global, so the name must be given) |
| `--force` | Overwrite an existing `devproxy.toml` |

The start command comes from, in priority order: `--command` > `--from` > a guess based on the project type (Node / PHP / Makefile). **No clear evidence means no guess** — guessing wrong is worse than not guessing. See [Project-level configuration · Quick start: `devproxy init`](project-config.md#quick-start-devproxy-init).

**A repository with a frontend next to the API gets two routes**, wired together: the
frontend's command receives the API's dynamic port (`DEVPROXY_<API>_PORT`) in whatever
variable its own config reads (found in the vite config), plus the route's domain in
`__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS` (Vite 6 answers 403 for unknown Host headers).
When nothing can be guessed, the file gets a **placeholder** command that listens on
`$PORT` — the generated file is always loadable and startable, never `command = []`.

## setup

Deploy the local dev environment: Caddy + dnsmasq + DNS route + certificate trust + autostart. It is **idempotent** and can be run repeatedly.

```bash
devproxy setup
devproxy setup --yes                 # non-interactive (required when elevation is needed)
```

| Option | Description |
|---|---|
| `--dns-backend auto\|resolved\|system` | Force the DNS backend (defaults to `config.toml`) |
| `--domain-suffix <suffix>` | Temporarily override the domain suffix (validated; an invalid value exits 2) |
| `--no-trust` | Skip CA trust |
| `--yes` | Non-interactive mode; do not ask (required in scenarios that need elevation) |
| `--rollback <directory>` | Roll back from the given backup directory (see [Migration and rollback](migration-and-uninstall.md)) |

It **writes to system directories, installs service units, changes the DNS resolution path, and installs the self-signed CA into the system trust store** — read [the warning in the README](../README.md#%EF%B8%8F-this-tool-changes-your-system) before you start.

## doctor

Health check: verify every precondition and report the command that fixes whatever is missing.

```bash
devproxy doctor
devproxy doctor --dns-backend system
```

**Static checks only** (whether the file exists, whether the service is running, whether the certificate is installed, what the unit points at) — **it does not resolve DNS** and issues no `resolvectl` / `dig` queries. When everything is ready (every row reads `✔ ready`) it exits 0; otherwise it exits 3.

## trust

Install the CA root certificate into the system trust store and the NSS DB (on macOS, the system keychain).

```bash
devproxy trust
devproxy trust --yes
```

| Option | Description |
|---|---|
| `--yes` | Non-interactive mode; do not ask (required when elevation is needed) |

**When you need to run it**: after the first route is registered (that is when Caddy creates the CA). **Firefox reads neither the NSS DB nor the system trust store** (by default) — you must import the certificate manually or enable `security.enterprise_roots.enabled`; see [DNS and certificates](dns-and-certs.md#certificates-exact-site-blocks-not-wildcards).

## config

Show the configuration currently in effect (including the result of resolving the storage archive).

```bash
devproxy config
```

## selftest

Run an end-to-end self-check against an isolated Caddy instance: register → forward → redirect → unregister → certificate policy. **It does not touch the production 443/2019 ports**, and it does not install the sandbox CA into the host trust store.

```bash
devproxy selftest
```

## run

Supervise Caddy **in the foreground** — for environments without systemd (containers, WSL, minimal distributions), where a user-scope service cannot be installed and nothing loads the routes.

```bash
devproxy run
devproxy run --config /path/to/Caddyfile
```

| Option | Description |
|---|---|
| `--config <path>` | Caddyfile to load (the main Caddyfile by default) |

- **stdio goes straight to the terminal** — Caddy logs are printed in real time; Ctrl-C exits.
- It installs no service and writes no state; after it exits, the routes are no longer served (the route table is a file, but a Caddy process has to load it).
- Mutually exclusive with `setup`: while the service is running, `run` exits 3 (port conflict) and tells you to stop the service first.
- See [Platform support and differences](platforms.md#environments-without-systemd-supervise-with-devproxy-run).

## uninstall

Uninstall: stop services, remove units, the DNS route, and (optionally) the CA.

```bash
devproxy uninstall --dry-run     # preview only; changes nothing and needs no sudo
devproxy uninstall               # execute after interactive confirmation
```

| Option | Description |
|---|---|
| `--dry-run` | Only print the steps that would run; change nothing and need no sudo |
| `--yes` | Non-interactive mode; do not ask |
| `--purge` | Also delete the Caddy storage, backups, and the downloaded caddy binary (asked interactively when omitted) |
| `--remove-ca` | Also remove the CA (asked interactively when omitted; a migrated CA is kept by default) |

**Conservative by default**: it deletes only the tool's own things (services, units, DNS route, state files), and keeps your `sites.caddy`, `config.toml`, Caddy storage, backup directory, and linger. See [Migration, rollback, and uninstall](migration-and-uninstall.md).

## Shell completion

```bash
devproxy --install-completion        # install completion for the current shell (bash / zsh / fish / powershell)
devproxy --show-completion bash      # only print the script; write nothing to disk
```

- Completion for **command names and options** is built into Typer/Click; no extra configuration is needed.
- On bash it installs to `~/.bash_completions/devproxy.sh` and adds a `source` line to `~/.bashrc`; **you must open a new terminal** for it to take effect.
- **Argument values** are completed by devproxy itself: `logs <name>` completes service names (`caddy` / `dnsmasq`) and **registered route names**; `down <name>` completes route names.
- Completion is **dynamic** — every `<TAB>` runs devproxy once to read the route table. If the route table cannot be read (not set up yet, corrupt file), it treats that as no candidates and **does not error**.
- The names for `up` / `expose` are **new** route names and are not completed (completing them would make people think old names can be reused).
- Everything after `--` is passed to the app verbatim; devproxy does not know the app's arguments, so it does not complete them.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Runtime failure (child process exited, reload failed, corrupt route table, migration rejected, `up` found a route that conflicts with a declaration, `run`'s Caddy config validation failed, ...) |
| 2 | Usage error (invalid route name / invalid port, missing `--`, unknown log service name, `logs --edit` with no target or no editor found, invalid `dns_backend`, invalid `--domain-suffix`, `up`'s config file missing or invalid, the file given to `run --config` does not exist) |
| 3 | Precondition unmet (Caddy is not running, the main Caddyfile does not exist, `doctor` health check fails) |
| 4 | Elevation is needed but the session is non-interactive (no `--yes`), **or the user answered "no" at a confirmation prompt** |

Two special cases: if `up` is missing `--`, or there is no command after `--`, it exits **2** as a usage error; when `up`'s command ends normally, its exit code **is the child process's exit code**.

Exit code 4 means **"a human needs to make the call, but no human did this time"** — either the environment is non-interactive and `--yes` was not passed, or the user answered "no" at a confirmation prompt. In both cases **the tool changed nothing**, and the terminal says so explicitly.

One more asymmetry that is easy to trip over: the same invalid value exits with a different code when written in `config.toml` than when passed on the command line — in `config.toml` it is a **configuration error** (exit code **1**), while passing it via `--domain-suffix` / `--dns-backend` is a **usage error** (exit code **2**).
