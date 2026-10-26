# devproxy documentation

The [README](../README.md) is the quick start (install → start a service → everyday
commands); this directory holds the detailed write-ups per topic.

中文文档：[docs/zh-CN/](zh-CN/README.md)

## Find by scenario

| What you want to do | Read |
|---|---|
| Install and run it for the first time | [README](../README.md) |
| Look up a command, its options, or exit codes | [commands.md](commands.md) |
| Put my own service behind a domain | [apps.md](apps.md) |
| DNS does not resolve / the certificate errors out | [dns-and-certs.md](dns-and-certs.md) |
| Change configuration (suffix, ports, storage path) | [configuration.md](configuration.md) |
| Share one set of domains with the whole team (`devproxy.toml`) | [project-config.md](project-config.md) |
| Migrate a legacy caddy setup / roll back / uninstall | [migration-and-uninstall.md](migration-and-uninstall.md) |
| Containers / WSL, where there is no systemd | [platforms.md](platforms.md) |
| Something is broken | [troubleshooting.md](troubleshooting.md) |
| Change the code, run the tests | [development.md](development.md) |

## All documents

- [commands.md](commands.md) — every command, option, and exit code
- [apps.md](apps.md) — wiring apps up: `expose`, the two ways to use `$PORT`, caveats
- [dns-and-certs.md](dns-and-certs.md) — the DNS route, certificate trust (including Firefox), `doctor` vs `setup`
- [configuration.md](configuration.md) — every `config.toml` setting, the domain-suffix allowlist, the storage archive
- [project-config.md](project-config.md) — `devproxy.toml`: the declarative `up` / `down` workflow
- [platforms.md](platforms.md) — directories and files, platform differences, `devproxy run` without systemd
- [migration-and-uninstall.md](migration-and-uninstall.md) — migration and rollback, uninstall
- [troubleshooting.md](troubleshooting.md) — troubleshooting
- [development.md](development.md) — development and CI, code structure

## Other

- [verification-checklist.md](verification-checklist.md) — real-machine verification checklist
  (behaviour automated tests cannot cover and that must be run on a real machine)
- [legacy-devp.md](legacy-devp.md) — why the old `devp` script is gone
- `superpowers/` — design specs and decision records from the build phase (a historical
  archive, kept in the language it was written in; not the current design)
