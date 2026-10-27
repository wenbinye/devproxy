# Changelog

User-visible changes to devproxy. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version numbers follow
[Semantic Versioning](https://semver.org/).

> There was no formal release before 0.1.0; this file starts at 0.1.0.

中文版：[CHANGELOG.zh-CN.md](CHANGELOG.zh-CN.md)

## [Unreleased]

### Added

- `devproxy init` generates **two routes** for a repository that has a frontend next to
  the API, and wires them together: the frontend's command receives the API's dynamic
  port (`DEVPROXY_<API>_PORT`) in the variable its own config reads, plus its domain in
  `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS` (Vite 6 answers 403 for unknown Host
  headers).

- **Sibling route ports**: every app of a project receives the port of every other
  route of the same `devproxy.toml` as `DEVPROXY_<NAME>_PORT` (e.g. `api` →
  `DEVPROXY_API_PORT`). The ports of a project are now allocated **before** any app
  starts, so a frontend can point at its API without a hardcoded port:

  ```toml
  [[routes]]
  name = "api"
  command = ["sh", "-c", "uv run uvicorn app:app --port $PORT"]

  [[routes]]
  name = "web"
  command = ["sh", "-c", "cd frontend && BACKEND_PORT=$DEVPROXY_API_PORT npm run dev"]
  ```

- A command that references a route the file does not declare is **rejected** while the
  file is read (`route 'web' uses $DEVPROXY_API_PORT, but no route 'api' is declared`).
- When a referenced sibling's port changed, the app holding the stale pointer is
  reported as a **conflict** (`sibling port changed (DEVPROXY_API_PORT: 49435 -> 51717)`)
  instead of silently proxying to a dead port.

### Fixed

- `devproxy init` no longer writes a file that devproxy itself refuses to load: a
  project with no guessable command got `command = []`, and `devproxy up` rejected it
  with "command must be a non-empty array". The generated file now contains a
  placeholder command that listens on `$PORT`, so it is always loadable and startable.
- `devproxy init` calls vite directly when the project's dev script runs vite
  (`"dev": "vite --port 5174"`): appending `--port $PORT` to such a script does not
  reliably override the hardcoded port, while the CLI flag always does.

## [0.1.0] - 2026-10-27

### Added

- `devproxy logs <name> --edit` (short option `-e`): open the log file with `$EDITOR`.
  When neither `EDITOR` nor `VISUAL` is set it probes `vim` / `vi` / `nano` in turn.
- `devproxy logs --list` prints the **full path** of every log file (no more assembling
  it by hand).
- `devproxy ls` / `ps` sort by **app name** — the route table is a set, so the output is
  now stable and predictable.
- Shell completion: `devproxy --install-completion` (bash / zsh / fish / powershell), with
  value completion for `logs` / `down` (route names and service names).
- `devproxy uninstall`: stop services, remove units, delete the DNS route, optionally the
  CA too (`--remove-ca` / `--purge`); conservative by default, keeping your config and
  personal sites.
- `devproxy init`: generate a `devproxy.toml` in the project (works for PHP / Node /
  Makefile projects).

### Changed

- **The default language is now English**: CLI output, error messages, comments, and
  documentation are all English. Chinese documentation is still maintained as a full
  mirror under `docs/zh-CN/` (and `README.zh-CN.md`).
- The command surface now mirrors **docker compose**: project-level operations are
  `up` / `down` / `ps` / `logs`; the old `sync` / `stop` were folded into `up` / `down`.
- Bare `devproxy logs` inside a project follows **every app in the project** (with a
  coloured tag per line when there are several).

### Fixed

- **macOS**: `sync` no longer tears down and rebuilds apps that are still running --
  the liveness check read `/proc`, which macOS does not have, so every live app looked
  dead.
- **macOS**: grandchildren of a backgrounded app are cleaned up again. The group
  cleanup was skipped entirely there (no `WNOWAIT`, so the leader was reaped before
  the group was signaled), leaving stray processes behind.
- **Verified on a real Mac** (2026-10-26 / 10-27): the whole real-machine checklist for
  macOS was run against a macOS 26.1 machine, and every item now has a conclusion. Two
  behaviour notes came out of it: `security add-trusted-cert` cannot set the trust flag
  without a graphical session -- so `devproxy trust` has to be run in the Mac's own
  terminal -- and once it has been, the keychain is the **single** trust store: the
  bundled `curl` trusts the CA from it too (no need to touch `/etc/ssl/cert.pem`).
- Site blocks now carry `X-Forwarded-Port`, so an app's absolute URLs no longer include
  the dynamic port.
- Subprocess calls have a timeout: something like `certutil` stuck on a password prompt
  can no longer write to disk without bound (it once filled `/home` to 100%).
- Domain-suffix allowlist: prevents hijacking all machine-wide resolution onto an
  unexpected suffix (such as `.com`).
