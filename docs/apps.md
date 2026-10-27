# Wiring apps up

## Service already running? Just give it a domain

Not every service is started by devproxy -- database admin consoles, mock services,
things running in Docker are often already up. For those, use `expose`:

```bash
devproxy expose datahub --port 9002
# ✅ https://datahub.test  (upstream 127.0.0.1:9002)
#    Remove with: devproxy down datahub
```

Key points:

- **Write `127.0.0.1` for the upstream**: devproxy proxies to a port on **this machine**.
  `localhost` inside a container is the container itself -- in that case use the host IP
  or `host.docker.internal`;
- **You can register even when the upstream is not running**: it prints "upstream ... is
  not responding right now", but the route is still written to `routes.json` (routes are
  persistent; they work once the service is up);
- **Routes are persistent**: they survive a Caddy restart and a machine reboot
  ("the file is the source of truth");
- To put it in the repository and share it, write it into `devproxy.toml` (see
  [Project-level configuration](project-config.md)):

  ```toml
  [[routes]]
  name = "datahub"
  mode = "expose"     # do not start a process; only give an already-running service a domain
  port = 9002
  ```
## Two kinds of app, two ways to write them

**1. Apps that natively read the `PORT` environment variable** (Next.js / CRA, etc.) --
just write the command:

```bash
devproxy up next-app -- npx next dev
```

devproxy puts the allocated port into the child process's `PORT` environment variable.

**2. Apps that need an explicit port argument** (Vite / uvicorn / gunicorn, etc.) -- wrap
them in `sh -c` so the shell expands `PORT` **after** devproxy injects it:

```bash
devproxy up codeup-web -- sh -c 'npx vite --port $PORT --host 127.0.0.1'
devproxy up codeup-api -- sh -c 'uv run uvicorn app:app --host 127.0.0.1 --port $PORT'
devproxy up codeup-api -- sh -c 'gunicorn app:app --bind 127.0.0.1:$PORT'
```

**Do not write `--port $PORT` directly**: devproxy executes the command directly with
`subprocess.Popen(argv)` and **does not go through a shell**, so the `$PORT` in the command
is expanded by your own shell first (at this point devproxy has not allocated a port yet,
so the value is an empty string), and the app receives an argument with a missing value.
Wrapping the `sh -c` script in single quotes defers expansion until after the child
process starts.

**`--` is mandatory**: everything before `--` is devproxy's own arguments (`name`, `--port`,
`--ready-timeout`); everything after `--` is passed to the child process **verbatim**, and
devproxy does not parse it. This keeps the app's own `--port` from being stolen by devproxy.
Without `--`, the tool exits with an error (code 2).

## One app pointing at another

In a `devproxy.toml` the ports of the whole project are allocated **before** any app starts,
and every app receives the port of the project's other routes as an environment variable --
`DEVPROXY_<NAME>_PORT` (route `api` → `DEVPROXY_API_PORT`, dashes become underscores). A
frontend therefore never has to hardcode the API's port:

```toml
[[routes]]
name = "api"
command = ["sh", "-c", "uv run uvicorn app:app --port $PORT"]

[[routes]]
name = "web"
command = ["sh", "-c", "cd frontend && BACKEND_PORT=$DEVPROXY_API_PORT npm run dev"]
```

`BACKEND_PORT` is the *project's* variable -- read the frontend's proxy config to find which
one it builds the API target from. The referenced route must be declared in the same file,
otherwise the variable would be empty and devproxy rejects the file.

When a referenced sibling is restarted on another port, the app still holding the old one is
reported as a conflict (`sibling port changed (DEVPROXY_API_PORT: 49435 -> 51717)`) --
restart it with `devproxy down <name>` and `devproxy up`.

## Other caveats on the app side

- **Allow the Host header**: the request's Host header is `<name>.test`. Vite needs
  `server.allowedHosts`, and Django needs the domain added to `ALLOWED_HOSTS`.
- **The child process detaches from the controlling terminal**: devproxy starts the child with
  `start_new_session=True` (its own process group, so descendant processes can be cleaned
  up by group on exit). The cost is that the child has **no controlling terminal**, so
  interactive startup semantics that rely on stdin change; stdout/stderr still go straight
  to the current terminal.
- **Readiness check**: devproxy polls "is the port connectable", waiting at most
  `ready_timeout` seconds (default 60). On timeout it reports
  `ready timeout (<seconds>s) -- the app may not be using $PORT; specify one explicitly with --port`.
- **A route with the same name is replaced**: running `up` again with the same name first
  unregisters the old route and then stops the old child process.

---
