# Legacy files from before the migration

This directory holds the hand-maintained configuration that lived on the machine
**before the devproxy migration ran on 2026-10-22**:

- `Caddyfile` — the manually maintained Caddy config. The migration split it into
  `~/.config/devproxy/Caddyfile` + `sites.caddy`, and backed up and rewrote the
  `caddy.service` `ExecStart`.

It is kept only so history can be looked up (for example, "what did that `MINIKUBE_IP`
site block look like?"). **No service reads these files any more** — the live config is
under `~/.config/devproxy/`.

Likewise, the `caddy.service` and `site/` (a test page) that once sat in the repository
root have been deleted; both were temporary artifacts of the migration.
