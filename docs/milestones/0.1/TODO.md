# TODO — milestone 0.1

Tracks the work items of this milestone. Format:

```
- [x] #<id>: **Subject**（YYYY-MM-DD registered by <name>; YYYY-MM-DD, done by <name>）：
- [ ] #<id>: **Subject**（YYYY-MM-DD registered by <name>）：
```

- `id` is unique across the file and increments from 1;
- state is expressed by the checkbox, with no status word;
- the dates in parentheses are the registration date (after the semicolon, the date it
  was done).

中文版：[TODO.zh-CN.md](TODO.zh-CN.md)

## Open

(none)

## Done

- [x] #1: **`ls` must sort by app name**（2026-10-26 registered by Ye Wenbin; 2026-10-26, done by Claude）：
  - The table order of `devproxy ls` / `devproxy ps` was the registration order in
    `routes.json` (`RouteStore.list()` returned `load_table(...).routes` unsorted).
  - Requirement: sort by app name (the `NAME` column, i.e. `Route.name`) ascending, so the
    output is stable and predictable.
  - Candidate spots: `_list_routes()` in `src/devproxy/cli/ls.py` (sort before rendering),
    or make `RouteStore.list()` return a sorted list (after checking the other callers).
  - **Decision**: done in `RouteStore.list()` (`src/devproxy/gateway/store.py`) — the route
    table is a **set**; the order in `routes.json` is merely write order, so sorting on read
    is what makes the output stable. Other callers were checked: `store.get()` looks up by
    name (order-irrelevant), and `build_sync_plan` in `cli/sync.py` collects `existing` into a
    `by_name` dict (order-irrelevant); only the name order of `plan.prune` changes (it becomes
    ascending, which is more predictable).
  - Verification: `tests/test_cli.py::test_ls_sorts_routes_by_name` (registered as
    zebra/alpha/mike, output ascending); full suite 1072 passed.

- [x] #2: **`logs --list` must show the log file name**（2026-10-26 registered by Ye Wenbin; 2026-10-26, done by Claude）：
  - Previously `devproxy logs --list` printed only the app name (`path.stem`) plus the size;
    the full path appeared only when querying a single target (`logs <name> --list`, whose
    `_print_one` printed "Full path: {path}").
  - Requirement: the list itself must show each log file's name/path, otherwise users still
    have to assemble `~/.local/state/devproxy/logs/routes/<name>.log` by hand.
  - Open question: file name only (`<name>.log`) or the full absolute path; long paths wrap
    at 80 columns, so maybe a table or a separate line per entry.
  - **Decision** (confirmed with the user, 2026-10-26): **keep the aligned list and put the
    full absolute path on its own indented line under each entry**. Not a table — a long path
    column would be squeezed; on its own line a wrap does not disturb the alignment above.
    The path line uses `soft_wrap=True`: Rich does not wrap it, so the path stays **one line**
    in the output stream (copyable as a whole); the terminal soft-wrapping is fine.
    In non-project mode the heading was simplified from "Route logs
    (~/.local/state/devproxy/logs/routes/):" to "Route logs:" — the directory is already
    spelled out by the full paths.
  - Verification: `tests/test_logs.py::test_logs_list_shows_full_path`,
    `::test_logs_list_in_project_shows_full_path` (assert the full path appears contiguously).

- [x] #3: **New `logs` option: open the log file with the default EDITOR**（2026-10-26 registered by Ye Wenbin; 2026-10-26, done by Claude）：
  - Requirement: add an option (e.g. `devproxy logs <name> --edit`) that opens the log file
    with the `EDITOR` environment variable (falling back to `VISUAL`) instead of only
    following it.
  - Previously `logs` had only `--list`; following went through `follow_log` / `follow_many`.
  - Open questions: behaviour when `EDITOR` is unset (exit 2 or fall back to some default
    editor); service logs (caddy / dnsmasq) have no file — should `--edit` support them?
  - **Decision** (confirmed with the user 2026-10-26; revised the same day on user feedback):
    - `EDITOR`, else `VISUAL`; with neither, **probe `vim` / `vi` / `nano` in turn**
      (`shutil.which`, using the first one on PATH); with none of them, **exit 2** with a hint
      to `export EDITOR=vim` (the candidates are probed, never forced — forcing an editor that
      is not installed just earns a "command not found").
    - The short option `-e` is equivalent to `--edit` (the user's first feedback was wanting
      to type `logs wrs-api -e`).
    - **Service logs do not support `--edit`**: caddy / dnsmasq are rolling journalctl logs,
      not files, so an editor cannot open them — fail with a clear message pointing at
      `devproxy logs caddy` (follow).
    - `--edit` requires a target name; it is mutually exclusive with `--list` (both → exit 2).
    - A non-zero editor exit code → exit 1 with the code reported; a missing editor → a clean
      error, no traceback.
  - Implementation:
    - `Platform.open_in_editor(path, editor) -> int` added to the protocol in
      `src/devproxy/platform/base.py`; implemented per platform in linux / macos
      (`shlex.split(editor)` plus the file path, **inheriting stdio** — the editor takes over
      the terminal); `fake.py` records the call instead of opening anything.
    - `_resolve_editor()` / `_edit_log()` and the `--edit` option in `src/devproxy/cli/logs.py`.
  - Verification: 11 `--edit` cases added to `tests/test_logs.py` (normal open / VISUAL
    fallback / probing candidates when EDITOR is unset, exit 2 when all are missing / service
    log exit 2 / unknown route exit 2 / missing name exit 2 / mutual exclusion with --list /
    non-zero editor exit code / missing editor);
    `tests/test_platform_linux.py::test_linux_open_in_editor_splits_command_and_streams`;
    full suite 1074 passed.

- [x] #4: **bash completion**（2026-10-26 registered by Ye Wenbin; 2026-10-26, done by Claude）：
  - Requirement: tab completion for `devproxy` in bash.
  - **Decision**: command and option completion ships with Typer/Click
    (`--install-completion` / `--show-completion`, supporting bash / zsh / fish /
    powershell), and the custom `--` splitting group does not break it — subcommands,
    options, and prefix filtering all verified, and nothing is completed after `--` (correct).
    What was added here is the **value domain**: a new `src/devproxy/cli/_completion.py`,
    completing service names + **registered route names** for `logs <name>`, and route names
    for `down <name>`.
    The candidates are read straight from `routes.json` (not through `build_context`, and no
    logging) — completion runs on every <TAB>, so it must be fast and unfailing: an unreadable
    route table means no candidates, and never an error on stdout (bash turns every output
    line into a completion item).
    `up` / `expose` take new route names and are not completed; path options
    (`--file` / `--config`) fall back to bash's filename completion through the generated
    script's `-o default`.
  - Usage: `devproxy --install-completion` (bash installs to `~/.bash_completions/devproxy.sh`
    and adds a `source` line to `~/.bashrc`; open a new terminal).
  - Verification: `tests/test_completion.py` (6 cases driving Click's completion entry point);
    a real-machine simulation of `_DEVPROXY_COMPLETE=complete_bash` covering subcommands /
    options / route names / prefix filtering / the `--` boundary; full suite 1080 passed.

- [x] #5: **macOS in CI, and the three platform gaps it turned up**（2026-10-26, registered and done by Claude）：
  - Why: the repository ran in GitHub Actions for the first time with a macOS matrix, and
    three tests were red on day one. They turned out to be **three real platform gaps**,
    not broken tests.
  - **#5-1 `app_process_alive` read `/proc/{pid}` directly** (`gateway/sync.py`): macOS has
    no /proc, so **every running app looked dead** and sync tore down and rebuilt apps
    that were working. That is a real fault for macOS users, not just a test problem --
    and it was wrong on Linux too, where /proc merely happens to exist and hid it. Now it
    uses the platform-agnostic `pid_is_running` (/proc on Linux, `ps` on macOS).
  - **#5-2 the process-group cleanup was skipped entirely on macOS**
    (`gateway/runner.py`): macOS has no `WNOWAIT`, so `proc.poll()` reaps the leader and
    the pgid loses its protection (the pid may be reused) -- the code, following "rather
    miss than misreport", gave up on the whole group, and grandchildren survived. Now the
    group is signaled **before** the leader is reaped: without WNOWAIT the zombie state is
    read with `ps` (ps does not reap, so the leader stays a zombie and the pgid stays
    pinned), and the exit code only becomes readable after the reaping -- tracked by the
    `_EXIT_CODE_PENDING` marker.
  - **#5-3 two tests raced the machine's load**: the child process used `sleep(0.5)` to
    stay alive until the readiness probe saw it, which is not enough on a loaded machine --
    the child was gone before the runner noticed the port was up, and the run failed with
    "child exited first" (a property of the machine, not of the behaviour under test).
    They now **wait for the readiness probe to actually connect** (a plain socket: an
    HTTP server's own accept would take the probe's connection).
  - Also fixed: the **rendering assumptions** CI exposed -- in an environment with
    `FORCE_COLOR` set, typer styles option names character by character, splitting
    `--dry-run` into three pieces and making the assertion permanently false (`NO_COLOR`
    only drops colour, not bold; the fix was `typer.rich_utils.COLOR_SYSTEM = None`, which
    yields plain text); one hint is wrapped by Rich according to the console width, so it
    wraps in a different place on a different path length (assert the behaviour, not the
    wrapped text); and the real-worker test asserted wording that depends on whether the
    machine happens to run Caddy (now a shape assertion that holds either way).
  - **Two real-machine rounds (2026-10-26 and 10-27)**: over SSH to `my-mac`
    (WenbindeMBP, macOS 26.1 / 25B78) the whole **group A checklist was run — all 22
    items now have a conclusion** (see the E5 section of
    [docs/verification-checklist.md](../../verification-checklist.md)). Three findings
    worth keeping:
    - **the bundled curl does not trust the keychain** (exit 60) -- the keychain covers
      Safari/Chrome; for curl the CA has to go into `/etc/ssl/cert.pem`;
    - **the `port 5300` directive in `/etc/resolver/<suffix>` IS honoured** by
      mDNSResponder -- that was the group's biggest open question;
    - **the sudo timestamp cache is per-tty**: it hits within one session but **not across
      sessions**, so automation has to feed the password again for every new SSH session.
  - One more settled conclusion: **`security add-trusted-cert -d` cannot set the trust
    flag without a graphical session**, even with sudo
    (`SecTrustSettingsSetTrustSettings: The authorization was denied since no user
    interaction was possible`) -- the certificate lands in the keychain, the trust flag does
    not. Making the browser actually trust it requires running `devproxy trust` in the
    Mac's own terminal.
  - Side effect: that machine's devproxy was re-installed from the **public GitHub repo**
    (it had come from the internal Codeup mirror, so it was still the pre-translation
    build).
