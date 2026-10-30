# Per-stack command patterns for devproxy.toml

Templates distilled from real projects (Python+uv, Node+Vite, PHP+Swoole). `command`
is an **argv array**: it does not go through a shell, so `$PORT` inside a bare string
would reach the app unexpanded — any command that needs shell expansion gets wrapped in
`["sh", "-c", "…"]`.

Every template makes the app listen on `$PORT`. That is the readiness contract: devproxy
polls the port it allocated, and an app on a fixed port just times out.

---

## Python (uv / pyproject.toml)

```toml
[[routes]]
name = "api"
command = ["sh", "-c", "uv run uvicorn <package>.main:app --host 127.0.0.1 --port $PORT"]
```

- Find the ASGI app path in the code (`uvicorn`/`fastapi`): `<module>:<app variable>`,
  `--factory` if it is a factory function.
- Django: `uv run python manage.py runserver 127.0.0.1:$PORT`.
- Werkzeug/flask dev server: `uv run flask run --host 127.0.0.1 --port $PORT`.
- If the app needs auth disabled locally (a common pattern when there is no OIDC proxy),
  put it in the same sh -c line: `AUTH_DISABLED=1 uv run …` — and comment why.

## Node (vite / next / npm scripts)

Dev script present:

```toml
[[routes]]
name = "web"
command = ["sh", "-c", "npm run dev -- --port $PORT"]
```

Vite frontend talking to a sibling API route (the full working pattern):

```toml
[[routes]]
name = "api"
command = ["sh", "-c", "uv run uvicorn app.main:app --host 127.0.0.1 --port $PORT"]

[[routes]]
name = "web"
command = ["sh", "-c", "cd frontend && BACKEND_PORT=$DEVPROXY_API_PORT NODE_EXTRA_CA_CERTS=$HOME/.local/share/caddy/pki/authorities/local/root.crt __VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=web.test ./node_modules/.bin/vite --host 127.0.0.1 --port $PORT"]
```

Three things to get right here:

- **`$DEVPROXY_API_PORT`** is the port devproxy gave the `api` route. Never hardcode it
  — that is the whole point of the dynamic port. The variable name comes from the route
  name (uppercased, dashes to underscores); the file must declare that route, or devproxy
  rejects it.
- **`BACKEND_PORT`** is the *project's* variable: read `vite.config.ts` to find which env
  var the proxy target is built from (`API_TARGET`, `BACKEND_PORT`, …) and pass that one.
  Vite's `loadEnv()` gives **process env priority over `.env` files**, so the injection
  wins even when `.env` pins the old port (that is exactly how
  `env.BACKEND_PORT ?? "8012"` gets overridden). If the config hardcodes the address
  instead of reading an env var, the injection cannot reach it — the project must make the
  one-line change to read an env var (ask first), or the port stays fixed.
- **`__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=web.test`** stops Vite from rejecting the
  proxied Host header; `NODE_EXTRA_CA_CERTS` makes Node trust the Caddy CA for
  server-to-server https calls (only needed when the frontend really calls the API over
  https; a same-origin proxy to `http://127.0.0.1:$DEVPROXY_API_PORT` does not).

**Prefer the same-origin proxy over cross-origin calls.** The pattern above proxies `/api`
inside the frontend dev server, so the browser only ever talks to `https://web.test` and
CORS never applies. Pointing the browser straight at `https://api.test` makes every call
cross-origin — the backend's CORS allowlist (usually `http://localhost:5174`-style entries)
will reject the new origin, and fixing that means editing the backend. Keep the browser on
one domain unless the project already lives that way.

Call `./node_modules/.bin/vite` directly instead of `npm run dev` when the project's
`package.json` hardcodes a port (`"dev": "vite --port 5174"`): the CLI flag `--port $PORT`
then reliably overrides the hardcoded value, and a second `--port` appended to `npm run dev`
does not always win.

Next.js: `npm run dev -- --port $PORT` is enough (next trusts the system store via
NODE_EXTRA_CA_CERTS the same way).

**Vite 6 rejects unknown Host headers with 403** ("Blocked request"), and that applies
to `npm run dev` too — not just explicit vite invocations. Any frontend that will be
reached as `https://<name>.test` needs
`__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=<name>.test` in its environment, wherever the
launch command comes from. Symptom: every proxied request returns 403 while
`curl http://127.0.0.1:5174/` works fine.

## PHP (composer)

Best case — composer.json has a `serve` script that honours an env port:

```toml
[[routes]]
name = "app"
command = ["sh", "-c", "APP_WEB_PORT=$PORT composer serve"]
ready_timeout = 180
```

- Check the project's `.env` for the port variable name (`APP_WEB_PORT`,
  `WEB_PORT`, …) — `grep -E "^(APP_)?WEB_?PORT" .env`. Pass **that** variable:
  immutable dotenv loaders (`Dotenv::createImmutable`) do not overwrite process env,
  so the override wins.
- PHP apps boot slowly (framework init); `ready_timeout = 180` avoids false timeouts.
- Laravel: `php artisan serve --host 127.0.0.1 --port $PORT`.
- No serve script: `php -S 127.0.0.1:$PORT -t public`.

## Java (spring boot)

```toml
[[routes]]
name = "api"
command = ["sh", "-c", "./mvnw spring-boot:run -Dspring-boot.run.arguments=--server.port=$PORT"]
ready_timeout = 300
```

Gradle: `./gradlew bootRun --args='--server.port=$PORT'`. JVM boot is slow —
`ready_timeout = 300`.

## Go

```toml
[[routes]]
name = "api"
command = ["sh", "-c", "go run . --port $PORT"]
```

Check how the app reads its port (flag vs `PORT` env) — adjust the flag form.

## The project hardcodes the port

Some projects cannot be told which port to use: the config says `http://localhost:8012`
and nothing reads an env var. Dynamic ports then do not apply to that app, and the
honest options are (in this order):

1. **Make the project read an env var** — one line in the project's config
   (`target: http://localhost:${process.env.BACKEND_PORT ?? 8012}`). This is a repo
   change: propose it, do not make it silently. When it is accepted, the app becomes
   dynamic and the sibling-port mechanism applies.
2. **Pin the port in devproxy.toml** and start the app on exactly that port:
   `port = 8012` plus a command that uses the literal port (not `$PORT`). devproxy then
   uses that port, and the app is a normal fixed-port app. Note in the file that the
   port is pinned on purpose.
3. **`devproxy expose <name> --port 8012`** when the app is already running outside
   devproxy — no managed process, no readiness check.

Never invent a port number to make a route start: a port that is free now is taken by
something else tomorrow, and the failure looks like "the app is flaky".

## Docker compose services

They are **not** routes in devproxy.toml — compose manages their lifecycle and ports.
Two options:

- compose maps a port to the host → `devproxy expose <name> --port <host-port>`;
- or leave them alone and only route the native dev servers.

## Already-running services

```bash
devproxy expose db-admin --port 8080
```

Persistent, no managed process, survives everything. Do not add them to the TOML.
