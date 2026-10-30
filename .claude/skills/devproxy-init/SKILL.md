---
name: devproxy-init
description: Set up devproxy for a project — detect the tech stack, generate devproxy.toml, start the services and verify the https://<name>.test domains work. Use this whenever the user wants to give local dev servers real HTTPS domains, mentions devproxy.toml / devproxy init / devproxy up / devproxy setup, asks "how do I get https locally", wants team-shared dev domains, or is starting work in a project that has a devproxy.toml but no running services — even if they don't say "devproxy" explicitly.
---

# devproxy project initialization

Wire a project up to devproxy: detect what it is, write its `devproxy.toml`, bring it up,
and prove the domains work. The end state is always **verified with curl**, not
"config written".

## Prerequisites (check first, in this order)

1. **Is devproxy installed?** `command -v devproxy`. If not:
   `uv tool install devproxy` (or the curl one-liner from the repo README), then re-check.
2. **Is the environment set up?** `devproxy doctor` — all four components ready means go.
   Exit code 3 or a "not ready" component means `devproxy setup --yes` is needed; that
   step **changes the system** (DNS route, certificate, service unit) — tell the user
   what it will do and get consent before running it. `devproxy trust` on macOS must run
   in the Mac's own graphical terminal (it cannot be done over SSH — the trust flag
   requires user interaction; everything else can run anywhere).
3. **Does this devproxy know about sibling ports?** The `DEVPROXY_<NAME>_PORT` mechanism
   (a frontend pointing at its API without a hardcoded port) is newer than the 0.1.0
   release on PyPI. Probe the installed copy instead of guessing — this asks the exact
   interpreter the `devproxy` command runs:

   ```bash
   "$(head -1 "$(command -v devproxy)" | sed 's/^#!//')" \
     -c "from devproxy.gateway.names import port_env_name; print(port_env_name('api'))"
   ```

   It prints `DEVPROXY_API_PORT` when the feature is there. An editable install of a
   devproxy checkout counts, too. If the import fails, the installed devproxy is too
   old: a `devproxy.toml` using `$DEVPROXY_API_PORT` would start the app with an
   **empty** variable and the frontend would silently proxy nowhere. Either install a
   newer one (`uv tool install --force devproxy`, or from a checkout:
   `uv tool install --force /path/to/devproxy`) or fall back to the fixed-port pattern
   below. Say which one you did.

## Step 1 — Detect the stack

Look at the project root for, in priority order:

| Evidence | Stack | Route name hint |
|---|---|---|
| `package.json` | Node (vite/next/…) | `"name"` field, or directory name |
| `composer.json` | PHP | directory name |
| `pyproject.toml` / `manage.py` | Python (uvicorn/django/…) | directory name |
| `go.mod` | Go | module name |
| `pom.xml` / `build.gradle` | Java (spring boot) | directory name |
| `docker-compose.yml` | services that are **already running elsewhere** | — → use `expose`, see below |
| `Makefile` with a `dev` target | whatever the target runs | — |

**Frontend + backend monorepos are the common case.** A `frontend/` (or `web/`) directory
next to an API is **two routes**, and the frontend usually has to reach the API — usually
through its own dev-server proxy, which needs the API's *port* (see Step 2). See
`references/stacks.md` for the exact patterns (API proxy env vars, Node CA trust, Vite
allowed-hosts).

For anything not in the table, read the files and reason: README run instructions,
`docker-compose.yml` port mappings, `.env` files with port variables. A wrong guess is
worse than asking the user — if the start command is genuinely unclear, ask.

## Step 2 — Plan the routes

For each service that needs a domain:

- **name**: `[a-z0-9-]`, single label. Derive it from the directory or the service's
  role (`api`, `web`); prefix with the project name when the same machine hosts several
  projects that could collide (`codeup-api`, not `api`).
- **command**: an argv array. The one hard requirement: **the app must listen on
  `$PORT`** — devproxy's readiness check is "connectable on $PORT". A command that
  starts the app on a fixed port will time out with a confusing "ready timeout".
- **a frontend pointing at its API**: never hardcode the API's port. devproxy allocates
  the ports of the whole project up front and exports each route's port to the other
  routes as `DEVPROXY_<NAME>_PORT` (route `pp-api` → `DEVPROXY_PP_API_PORT`). So the
  frontend's command is e.g.
  `sh -c "cd frontend && BACKEND_PORT=$DEVPROXY_PP_API_PORT npm run dev"`, where
  `BACKEND_PORT` is whatever variable the project's own proxy config reads (check
  `vite.config.ts` / the framework config). The file must declare the referenced route,
  or devproxy rejects the file.
- **but check how the frontend's config gets the API address first.** Three cases, in
  order of preference:
  1. *Config reads a port/host from an env var* (`target: http://localhost:${BACKEND_PORT}`)
     → inject the sibling var. No project change needed.
  2. *Config hardcodes the address* (`target: "http://localhost:8012"`) → the sibling var
     would never be read. Either make the one-line project change to read an env var
     (ask first — it is a repo edit), or pin the port in devproxy.toml
     (`port = 8012` on **both** routes is wrong; pin only what the config demands and let
     the other stay dynamic) — say which you chose and why.
  3. *The frontend calls the API cross-origin* (absolute URL, not a same-origin `/api`
     proxy) → the backend's CORS allowlist must include the devproxy domain
     (`https://<web>.test`), which it almost never does out of the box. Prefer routing
     through the frontend's own proxy (same origin, no CORS at all); only touch the
     CORS allowlist with the user's consent.
- **already running elsewhere** (a docker service, a colleague's machine, a database
  admin console): do not put it in `devproxy.toml`. Run
  `devproxy expose <name> --port <port>` for it instead.

Read `references/stacks.md` for per-stack command templates before writing anything —
it encodes the conventions that already work (port env overrides for PHP, API proxy
variables and CA trust for Vite frontends, shell wrapping for `$PORT`).

## Step 3 — Write devproxy.toml

Single route: `devproxy init --name <name> --command "<command>"` (it adds the header
comments). Multiple routes, or anything `init` cannot express: write the TOML directly —
`[[routes]]` blocks with `name` and `command`; it will be validated by `devproxy up`.

Comment the non-obvious decisions in the file (why an env var is overridden, which
`.env` port was remapped) — these files are committed and shared; the next person needs
the why, not just the what.

## Step 4 — Bring it up and verify

```bash
devproxy up
curl -sI https://<name>.test | head -1     # expect HTTP/1.1 200 (or a 3xx/401 that the app intends)
```

**Verifying the root path is not enough when there is a frontend/backend pair.** The
interesting path is the one that proves the frontend reached its API:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<web>.test/api/<some-real-endpoint>
curl -s -o /dev/null -w '%{http_code}\n' https://<api>.test/api/<some-real-endpoint>
```

The first goes browser → Caddy → frontend dev server → its proxy → the API's **dynamic**
port; the second hits the API directly. Both must return the app's real answer (a `404`
from the API is still proof the request arrived — a `502`/`000` means the chain is broken).
If the first one fails while the second works, the frontend is not pointing at the API:
check the env var name the project's proxy config actually reads.

If `devproxy up` reports a **conflict**, the route table already has that name with a
different *definition* — show the user both sides instead of overwriting; devproxy never
replaces a running app because its declaration changed, that is the user's call. A line of
the form `↻ restarted web -- sibling port changed (DEVPROXY_X_PORT: 1234 -> 5678)` is not
a conflict: devproxy already restarted that app with the new port (it was pointing at a
dead port), so there is nothing to do.
If it hits the **ready timeout**, the command almost certainly does not listen on
`$PORT` — re-check Step 2, do not just raise the timeout.
If it says **"port N is already in use"**, something else already holds that port (often
the app running from a previous `just dev`): find it (`ss -tlnp | grep :N`) and either stop
it or drop the fixed port from the declaration.

Finish by telling the user: the domains, the one command to start everything
(`devproxy up`), and that `devproxy.toml` is worth committing.

## Edge cases

- **devproxy.toml already exists**: read it first. The task is then "review and
  modernise" or "add the missing route", never blind overwrite (`init --force` is a
  last resort, with the user's consent).
- **Fixed-port app that cannot be changed** (a legacy binary, a config you cannot
  touch): see "The project hardcodes the port" in `references/stacks.md` — the honest
  options are a one-line project change, a pinned port, or
  `devproxy expose <name> --port <fixed-port>`. Say which one you chose; expose-mode
  routes have no managed process.
- **App reads its port from a `.env` variable** (common in PHP): pass the port env var
  in the command (`APP_WEB_PORT=$PORT composer serve`) — immutable dotenv loaders do not
  override existing process env, which is exactly why this works. Check `.env` for the
  variable names and mention them in the TOML comment.
