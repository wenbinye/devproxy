# Project-level configuration `devproxy.toml`

## Project-level configuration `devproxy.toml` (`devproxy up` / `devproxy down`)

`config.toml` is **your machine's** configuration (stored in `~/.config/devproxy/`, not committed).
`devproxy.toml` is the **project's** configuration -- it lives in the project repository root
and **is committed to the repository**; everyone on the team clones it, runs `devproxy up` once,
and gets the same set of routes.
### Quick start: `devproxy init`

```bash
cd ~/work/tasks/my-project
devproxy init                       # generate devproxy.toml
devproxy init --name web --command "npm run dev"
devproxy init --from my-running-svc # import the start command from an already-running route
```

It will:

1. find the **project root** (search upward for `.git`; fall back to the current directory), and write `devproxy.toml` there;
2. default the route name to the **directory name** (`My App` → `my-app`), changeable with `--name`;
3. choose the start command in this order:
   - the one given explicitly with `--command` (split with shell rules; quotes and spaces are both honoured);
   - `--from <route name>` -- import the start command from an **already-registered route of the same name**
     (the route table is global, so the name must be given; you cannot "import all current routes");
   - otherwise, **guess from the project type** (table below). **Do not guess without clear evidence** --
     a wrong guess is worse than none;
4. the written file **must be parseable by itself** (after generating, it is read back with the same parser to verify).

```bash
$ devproxy init
✔ Generated /home/me/work/my-app/devproxy.toml
  Route my-app → https://my-app.test
    command: sh -c npm run dev -- --port $PORT
  Route my-app-web → https://my-app-web.test
    command: sh -c cd frontend && BACKEND_PORT=$DEVPROXY_MY_APP_PORT npm run dev
  package.json has a dev script -- confirm this is the start command you want
  the frontend's config reads BACKEND_PORT -- the API's dynamic port is injected into it

Next steps:
  1. Check the commands above (placeholders must be replaced)
  2. devproxy setup    # once: installs Caddy, the DNS route, and CA trust
  3. devproxy up       # start everything this file declares
```

A repository with a frontend next to the API gets **two routes**: the API route and a
`<name>-web` route wired to it. The frontend is looked for in `frontend/`, `web/`,
`client/` or `ui/` (each with its own `package.json`); the port variable is injected only
when the frontend's config really reads one (a name equal to `PORT` or ending in `_PORT`,
e.g. `env.BACKEND_PORT`) -- otherwise the generated command carries no port and the file
tells you to wire `<PORT_VAR>=$DEVPROXY_<API>_PORT` in by hand.

If the file already exists it refuses (exit code 2); add `--force` to overwrite.

#### Recognized project types

| Evidence | Command guessed |
|---|---|
| `package.json` has a `dev` / `start` script (not vite) | `sh -c "npm run dev -- --port $PORT"` |
| `package.json` has a `dev` / `start` script running vite | `sh -c "./node_modules/.bin/vite --host 127.0.0.1 --port $PORT"` (vite is called directly: only the CLI flag reliably overrides a port written in the config) |
| `composer.json` + `artisan` (Laravel) | `sh -c "php artisan serve --port=$PORT"` |
| `composer.json` + `public/index.php` (Symfony etc.) | `sh -c "php -S 127.0.0.1:$PORT -t public"` |
| `composer.json` + root-level `index.php` | `sh -c "php -S 127.0.0.1:$PORT"` |
| `composer.json` has a `serve` / `start` / `dev` script | `sh -c "composer serve"` ⚠ **port not controllable** |
| `Makefile` really has a `dev` target | `make dev` |
| `pyproject.toml` (Python) | no guess -- the entry point is not visible; the file gets a **placeholder** command (`python3 -m http.server $PORT`) |

> **A guessed command must make the app listen on `$PORT`** -- devproxy's readiness check is
> "the app is connectable on `$PORT`". If the command starts the app on a fixed port, `devproxy up`
> waits until "ready timeout", and the cause is very hard to guess. So only port-controllable
> commands are offered; non-port-controllable ones (`composer serve` etc.) get only the command
> plus an explicit warning, and it will name the port variables defined in `.env`
> (`APP_WEB_PORT` etc.) so you know which one to change -- or switch to `mode = "expose"`
> and let the app start itself, with devproxy only opening the domain.
### By hand

```toml
# devproxy.toml -- project root, commit it to the repository

[[routes]]
name = "web"
command = ["npm", "run", "dev"]

[[routes]]
name = "api"
command = ["sh", "-c", "uv run uvicorn app:app --port $PORT"]

[[routes]]
name = "console"
mode = "expose"     # start no process; open a domain for an already-running service
port = 3000
```
### Fields

| Key | Required | Description |
|---|---|---|
| `name` | yes | Route name; must be a single label `[a-z0-9-]` (the same rule as `devproxy up <name>`) |
| `command` | required when `mode="up"` | **argv array**, not a shell string |
| `mode` | no | `"up"` (default, starts a process) or `"expose"` (registers the route only) |
| `port` | required when `mode="expose"` | `expose` mode: the port the upstream is already listening on. `up` mode: if set, the port is fixed; if omitted, it is allocated automatically |
| `ready_timeout` | no | Overrides the readiness wait in seconds for this route |
### ⚠️ `command` does not go through a shell, `$PORT` is not expanded

`command` is an argv array handed directly to `subprocess.Popen`, **with no shell in between**.
So the `$PORT` in `["npx", "vite", "--port", "$PORT"]` is a **literal**, not a port number --
the app receives an argument called `$PORT` and errors out.

For apps that need a port argument, wrap it in `sh -c` (the same point as the "Two kinds of apps, two forms" section above):

```toml
command = ["sh", "-c", "npx vite --port $PORT --host 127.0.0.1"]
```

`sh` expands `$PORT` only after the child process has started, by which time devproxy has already
written the port into the environment.

### One route's port is available to the others

`devproxy up` chooses **all** the ports of a project **before** starting anything -- a route
that pins `port = 5173` keeps it, the others get a free one -- so one app can point at another
without a hardcoded port. Every app receives the port of every route **of the same project
file** as an environment variable (expose-mode routes included -- their port is exported
too):

| Route | Variable |
|---|---|
| `api` | `DEVPROXY_API_PORT` |
| `pp-api` | `DEVPROXY_PP_API_PORT` |

The name is uppercased and dashes become underscores. An app's own port is available under the
same name: inside the `api` route, `DEVPROXY_API_PORT` is the same value as `$PORT`.

```toml
[[routes]]
name = "api"
command = ["sh", "-c", "uv run uvicorn app:app --port $PORT"]

[[routes]]
name = "web"
command = ["sh", "-c", "cd frontend && BACKEND_PORT=$DEVPROXY_API_PORT npm run dev"]
```

This is what makes dynamic ports usable for a frontend/backend pair: the frontend proxies to the
port the backend actually got, and two checkouts of the same project can run side by side.

**A reference to a route this file does not declare is an error** -- the variable would be
empty, so the file is rejected while it is read:

```
/home/me/work/my-app/devproxy.toml: route 'web' uses $DEVPROXY_API_PORT, but no route 'api' is declared in this file (devproxy only exports the ports of this project's routes)
```

(The path in the message is the absolute path of your `devproxy.toml`.)

**When a sibling's port changes, the app that uses it is restarted.** The frontend holds the
port the API had when it started; if the API is restarted on another port, the frontend would
silently proxy to a dead port. devproxy remembers what each app was started with, notices the
drift, and restarts that app with the new port:

```
↻ restarted web → https://web.test -- sibling port changed (DEVPROXY_API_PORT: 49435 -> 51717); port 49436
```

Such an app is **broken**, not "different from the declaration" -- keeping it running serves no
purpose, and `up`'s job is to make the machine match the declarations. The restart keeps the
route's port (the old process is stopped first), so the domain keeps its upstream.

Only the routes whose command actually mentions the variable are checked -- a route that never
reads `$DEVPROXY_API_PORT` does not care that the API moved. Apps started by an older
devproxy have no recorded environment and are never checked: after an upgrade, restart them
once.

A **declaration** change (a different command, or a pinned port in the file) is still only
reported, never acted on: there the question is "should the running process be replaced by
this new definition?", and that is your call. devproxy does not kill anything in that case,
and `devproxy up` exits with code 1:

```
⚠️  conflict web → https://web.test -- command differs (current [...], expected [...])
   Fix: run devproxy down web and then devproxy up again
```

### How to use it

```bash
devproxy up                # search upward from the current directory for devproxy.toml and sync
devproxy up --file <path>  # specify it explicitly
devproxy up --prune        # also remove routes that were deleted from this file
devproxy stop                # stop every app in this project (restart with: devproxy up)
```

**File discovery**: search upward from the current working directory for `devproxy.toml` and use the first one found
(all the way to the filesystem root). So running it from a subdirectory still finds the project-root copy.

**The child process's working directory** is the directory containing `devproxy.toml`, not the subdirectory
you are currently in -- that is what makes relative-path commands like `npm run dev` meaningful.
### Sync semantics (`devproxy up`)

| Situation | Action |
|---|---|
| Not in the route table | **Create**: `up` mode starts a process and registers it; `expose` mode only registers the route |
| In the route table and the declaration matches the current state | **Skip** (idempotent; running it twice changes nothing the second time) |
| In the route table, but the command/port changed | **Report a conflict, take no action**. Print the current state, the expected state and the fix command; **never kill and restart processes automatically** |
| In the route table, but deleted from the file | **Do nothing** by default; only `--prune` removes it |

**`--prune` removes only routes "declared by this file"** -- routes started manually with `devproxy up`
(those without a `source` marker in the route table) are never deleted by it. `--prune` also stops
child processes following `down`'s semantics, so in a non-interactive environment you must add
`--yes` (otherwise exit code 4).

**On conflict the exit code is 1** -- config drift must be visible to CI. When some routes fail, the remaining
routes are still processed, the final exit code is 1, and the output lists clearly which succeeded and which failed.
### Background processes and logs

Apps started by `up` in sync are **background processes** (they keep running after sync exits, in an independent
session), and stdout/stderr go to `~/.local/state/devproxy/logs/routes/<name>.log`.

**Bare `devproxy logs` behaves depending on where you are**:

- **Inside a project directory** (with `devproxy.toml`) → follow the logs of **every app in this project**;
  with a single app that is `tail -f`; with several, each line is prefixed with a coloured tag:

  ```
  $ devproxy logs
  Following (Ctrl-C to exit):
    [codeup-api]
    [codeup-web]
  [codeup-api] INFO:     Uvicorn running on http://127.0.0.1:39743
  [codeup-web] VITE v6.4.4  ready in 329 ms
  ```

  The tags are the **app names** in `devproxy.toml` (each with its own colour, padded for alignment).
- **Outside a project** → follow caddy (the old behaviour).

To see one app's output:

```bash
devproxy logs web              # follow (Ctrl-C to exit)
tail -f ~/.local/state/devproxy/logs/routes/web.log   # equivalent
```

`devproxy logs` takes:

| Argument | What it shows |
|---|---|
| `caddy` / `dnsmasq` | the **service's own** logs (journalctl on Linux, files on macOS) |
| `<route name>` | the logs of the **app started by sync** |

An unknown argument exits 2 and lists the route log names currently available. To see what is available:

```bash
devproxy logs --list
```

**Inside a project directory only the routes this project declares are listed** (filtered by `devproxy.toml`;
other projects' logs do not mix in); outside a project, all are listed. Routes that are declared but have
never run are also mentioned:

```
Service logs:
  caddy
  dnsmasq
This project (devproxy.toml: /home/me/work/xxl-job/devproxy.toml):
  xxl-job-admin            8 MB
    /home/me/.local/state/devproxy/logs/routes/xxl-job-admin.log
  xxl-job-executor        13 KB
    /home/me/.local/state/devproxy/logs/routes/xxl-job-executor.log
  declared but no logs yet: xxl-job-executor (run devproxy up)
```

The line below each entry is the **full path** -- you do not have to assemble
`~/.local/state/devproxy/logs/routes/<name>.log` yourself.

To see information for a **single** log (path, size, follow command):

```bash
devproxy logs codeup-api --list
```

To open a log directly in your editor (`$EDITOR`, falling back to `$VISUAL`):

```bash
devproxy logs codeup-api --edit
```

**Logs rotate**: once a single file exceeds **10 MB** it is renamed to `.log.1` (overwriting the previous one),
and the current file starts over from the beginning. So each route's logs take at most 20 MB and never grow
without bound -- an app like Spring Boot can write several MB per hour during development, and without
rotation it would eat hundreds of MB in a few days.

Rotation happens **on the write side** (devproxy collects the app's output and writes the file itself), not
via logrotate -- these logs are written by processes that devproxy started, and only the writer can guarantee
the file never grows without bound.
### Stop every app in this project

```bash
devproxy stop
```

One command stops **all** apps declared by `devproxy.toml` (processes and routes are cleaned up together).
It touches only the routes this file declares -- manually started routes and other projects' routes are
unaffected. Once everything is stopped, one `devproxy up` starts it all again.

Single-route operations are still `devproxy down <name>`.
### Relationship to `up` / `expose`

`up` is the batch version of single-app `up` + `expose`. Both share the same route table and transactional
writes -- routes created by `up` are no different from routes created by a manual single `up`; `devproxy ls`
lists them just the same, and `devproxy down <name>` unregisters them just the same.
### Real-world case: codeup-manager

`codeup-manager` (`git@codeup.aliyun.com:msy/tools/codeup-manager.git`, an asset inventory management tool,
FastAPI + Vite with separate frontend and backend) originally served dynamic domains through a now-deleted
legacy script `devp`. Its `devproxy.toml` now looks like this:

```toml
[[routes]]
name = "codeup-api"
command = ["sh", "-c", "uv run uvicorn codeup_manager.web.app:create_app --factory --reload --host 127.0.0.1 --port $PORT"]

[[routes]]
name = "codeup-web"
command = ["sh", "-c", "cd frontend && API_TARGET=https://codeup-api.test NODE_EXTRA_CA_CERTS=$HOME/.local/share/caddy/pki/authorities/local/root.crt __VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=codeup-web.test ./node_modules/.bin/vite --host 127.0.0.1 --port $PORT"]
```

One `devproxy up` gives you `https://codeup-web.test` and `https://codeup-api.test/docs`,
**with both ports allocated automatically**. Three things here are worth copying:

1. **When the frontend needs to proxy to the backend, proxy to the backend's "domain" rather than its port**
   (`API_TARGET`) -- this lets the backend use a dynamic port too, without sacrificing dynamic allocation
   for a static port;
2. **node does not read the system trust store**, so `NODE_EXTRA_CA_CERTS` must feed devproxy's root certificate
   to it (browsers and curl use the system trust store and do not need this step);
3. **Vite 6 has Host validation**; to access it through a custom domain you must pass
   `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS`.

For the repository's full instructions see its `docs/devproxy-quickstart.md`.

---
