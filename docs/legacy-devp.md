# What happened to the old `devp` script

`devp` is the **pre-1.0 predecessor** of `devproxy`: a single-file Python script that did
exactly one thing — put services running locally behind `https://<name>.test`.

It was deleted from the repository during this round's cleanup (along with its tests
`tests/test_devp.py`). This note stays in the repository so that "why it is gone" is
traceable, rather than it disappearing silently.

## What `devp` did

- `devp up <name> -- CMD...`: allocate a free port, start the child process (in its own
  process group), and insert the route into
  `/config/apps/http/servers/srv0/routes` through the Caddy Admin API
- `devp down <name>`: delete the route and tear down the child process group with it
- `devp ls`: list the registered routes
- `devp selftest`: start an isolated Caddy instance for an end-to-end self-check
- Route state lived in a JSON file under `~/.local/state/devp/`, serialized with flock

The design document is at `docs/superpowers/specs/2026-10-21-devp-dynamic-domain-gateway-design.md`.

## Why `devproxy` replaced it

`devp` PATCHed the Admin API configuration of the **live Caddy** directly:

```
PATCH /config/apps/http/servers/srv0/routes
```

This caused two structural problems:

1. **Other tools cannot see or delete what it registered.** Routes hide in Caddy's
   in-memory config, with no independent state file; `devproxy` can only see the routes
   it wrote to `routes.json`. When both tools use the same machine's Caddy, routes split
   into two worlds.
2. **It does not touch the system.** No deployment, no DNS route, no certificate trust,
   no autostart — all of that was manual. The new tool does these once and for all.

`devproxy` replaces it with **the file as the source of truth**: routes are written to
`~/.local/state/devproxy/routes.json`, rendered into
`~/.local/state/devproxy/routes.caddy`, and `import`ed by the main Caddyfile; changes go
through a "lock → mutate table → render → validate → write to disk → reload" transaction,
and a failed reload rolls back.

## After migrating to `devproxy`

| Old command | New command |
|---|---|
| `devp up <name> -- CMD...` | `devproxy up <name> -- CMD...` |
| `devp down <name>` | `devproxy down <name>` |
| `devp ls` | `devproxy ls` |
| `devp selftest` | `devproxy selftest` |

Command names and argument semantics stay the same; the main behavioral differences:

- `devproxy up` routes **persist** in `routes.json` (`devp`'s heartbeat mechanism
  automatically unregistered a route when the upstream died);
- on first route registration, `devproxy` makes Caddy generate an internal CA; run
  `devproxy trust` once more so curl / Chrome trust it;
- `devproxy` needs `devproxy setup` to run once first (deploy dnsmasq, DNS route,
  certificate trust, autostart).

The old `devp` state directory `~/.local/state/devp/` can be deleted directly — it is no
longer read.

## Environment-side cleanup (outside the repository)

The script is gone from the repository, but it may still remain **on the machine**:

- `~/.local/bin/devp` (the old copy on PATH)
- routes the old `devp` inserted into Caddy (they must be removed manually from the Caddy
  Admin API, or just run `devproxy setup` so the new tool takes over the whole config)

These two things are for the machine's owner to handle.
