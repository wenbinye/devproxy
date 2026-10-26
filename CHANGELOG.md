# Changelog

User-visible changes to devproxy. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version numbers follow
[Semantic Versioning](https://semver.org/).

> There was no formal release before 0.1.0; this file starts at 0.1.0.

中文版：[CHANGELOG.zh-CN.md](CHANGELOG.zh-CN.md)

## [Unreleased]

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
- Site blocks now carry `X-Forwarded-Port`, so an app's absolute URLs no longer include
  the dynamic port.
- Subprocess calls have a timeout: something like `certutil` stuck on a password prompt
  can no longer write to disk without bound (it once filled `/home` to 100%).
- Domain-suffix allowlist: prevents hijacking all machine-wide resolution onto an
  unexpected suffix (such as `.com`).

## [0.1.0]

- First version: `setup` / `up` / `expose` / `down` / `ls` / `logs` / `trust` / `doctor` /
  `config` / `selftest`.
