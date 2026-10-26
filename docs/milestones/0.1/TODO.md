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

- [x] #5: **macOS 进 CI，并修掉它翻出来的三处平台缺口**（2026-10-26, registered and done by Claude）：
  - 起因：仓库第一次在 GitHub Actions 上跑，CI 加了 macOS 矩阵 —— 头一天就红了三个用例。
    查下来是**三处真的平台缺口**，不是测试写错。
  - **#5-1 `app_process_alive` 硬编码读 `/proc/{pid}`**（`gateway/sync.py`）：macOS 没有
    /proc，于是**每一个活着的应用都被判成「已死」**，sync 会把还在跑的应用拆了重建。
    这是 macOS 用户的实际故障，不只是测试问题；在 Linux 上也是错的，只是 /proc 恰好存在
    把它掩盖了。改用平台无关的 `pid_is_running`（Linux 走 /proc、macOS 走 ps）。
  - **#5-2 macOS 上进程组清理整组被跳过**（`gateway/runner.py`）：macOS 没有 `WNOWAIT`，
    `proc.poll()` 会顺手回收 leader，pgid 于是失去保护（pid 可能被复用），代码按
    「宁可漏杀也不误杀」的原则放弃整组 —— 结果孙子进程留在后台。改成**回收之前先清组**：
    没有 WNOWAIT 时用 `ps` 看僵尸态（ps 不回收，leader 一直是僵尸，pgid 一直有保护），
    清完组再回收；退出码在回收之后才读得到，用 `_EXIT_CODE_PENDING` 标记这个
    「已知退出、码待读」的状态。
  - **#5-3 两个测试在跟机器的忙闲赛跑**：子进程用 `sleep(0.5)` 撑到「就绪」被看见，
    慢机器上这半秒根本不够，子进程先退出，run 报 "child exited first"（是机器的忙闲，
    不是被测行为）。改成**等就绪探针真的连上来再退**（用裸 socket：HTTP server 自己的
    accept 会把探针的连接抢走）。
  - 顺带修掉 CI 暴露的**渲染环境假设**：设了 `FORCE_COLOR` 的环境里 typer 给选项名
    逐字符上样式，把 `--dry-run` 拆成三段，断言恒假（`NO_COLOR` 只去色不去粗体，
    最后是把 `typer.rich_utils.COLOR_SYSTEM` 置 None 才拿到纯文本）；一条提示文本被
    Rich 按宽度折行，换个路径长度就折在别处（改成断言行为，不盯折行文本）；
    `test_sync` 的真 worker 用例断言的文案取决于本机有没有 Caddy（改成环境无关的形状断言）。
  - 现状：CI 在 Linux 与 macOS × Python 3.11/3.12/3.13 上全绿（2026-10-26）。
    **但 A 组的真机条目仍然待跑** —— 套件跑在假平台、假家目录、假进程上，
    真的 launchd / 钥匙串 / `/etc/resolver` 仍然没验过。
