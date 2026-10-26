# TODO — milestone 0.1

记录本 milestone 的待办事项。格式约定：

```
- [x] #<id>: **主题**（YYYY-MM-DD 名登记；YYYY-MM-DD，名执行）：
- [ ] #<id>: **主题**（YYYY-MM-DD 名登记）：
```

- `id` 全文件唯一，纯数字自增；
- 状态由 checkbox 表达，无状态词；
- 括号内日期为登记日（分号后为执行日）。

## 未完成

（暂无）

## 已完成

- [x] #1: **ls 命令列表要按应用名称排序**（2026-10-26 Ye Wenbin 登记；2026-10-26，Claude 执行）：
  - `devproxy ls` / `devproxy ps` 目前的表格顺序 = `routes.json` 里的注册顺序（`RouteStore.list()` 直接返回 `load_table(...).routes`，未排序）。
  - 需求：按应用名（`Route.name`，即表格 `NAME` 列）升序排序，输出稳定可预期。
  - 实现落点：`src/devproxy/cli/ls.py` 的 `_list_routes()`，排序 `routes` 后再逐行渲染；或改 `RouteStore.list()` 返回排序结果（需评估是否影响其他调用方）。
  - **落定**：改在 `RouteStore.list()`（`src/devproxy/gateway/store.py`）—— 路由表是**集合**，
    `routes.json` 里的先后只是写入顺序，读出来就排好序，输出才稳定。
    其他调用方已评估：`store.get()` 按名字取（顺序无关）、`cli/sync.py` 的 `build_sync_plan`
    把 `existing` 收进 `by_name` 字典（顺序无关），只有 `plan.prune` 的名字顺序跟着变（变成升序，更可预期）。
  - 验证：`tests/test_cli.py::test_ls_sorts_routes_by_name`（注册顺序 zebra/alpha/mike → 输出按名升序）；
    全量 1072 passed。

- [x] #2: **logs --list 要显示日志文件名**（2026-10-26 Ye Wenbin 登记；2026-10-26，Claude 执行）：
  - 现状：`devproxy logs --list` 每行只打印应用名（`path.stem`）+ 大小（`src/devproxy/cli/logs.py:185`），完整路径只在 `logs <名字> --list` 单查时才有（`_print_one` 里 "完整路径：{path}"）。
  - 需求：列表里直接能看到每个日志文件的名字/路径，否则用户还得自己拼 `~/.local/state/devproxy/logs/routes/<名字>.log`。
  - 待定：显示文件名（`<名字>.log`）还是完整绝对路径；长路径在 80 列下会折行，可能需要换表格或单独成行。
  - **落定**（2026-10-26 与用户确认）：**保留现在的对齐列表，每个条目下面缩进一行完整绝对路径**。
    不用表格 —— 表格里的长路径列会被挤扁；单独成行折了也不影响上面对齐。
    路径行用 `soft_wrap=True` 打印：Rich 不折行，路径在输出流里仍是**一整行**（可整段复制）；
    终端自己软换行无所谓。
    非项目模式的表头从「路由日志（~/.local/state/devproxy/logs/routes/）：」简化为「路由日志：」——
    目录已经由每行的完整路径说清了。
  - 验证：`tests/test_logs.py::test_logs_list_shows_full_path`、
    `::test_logs_list_in_project_shows_full_path`（断言完整路径整段出现在输出里）。

- [x] #3: **logs 新增参数：用默认 EDITOR 打开日志文件**（2026-10-26 Ye Wenbin 登记；2026-10-26，Claude 执行）：
  - 需求：新增一个选项（如 `devproxy logs <名字> --edit`），用环境变量 `EDITOR`（可回退 `VISUAL`）打开对应的日志文件，而不是只能跟随。
  - 现状：`logs` 目前只有 `--list` 一个选项，跟随走 `follow_log` / `follow_many`（`src/devproxy/cli/logs.py:198`）。
  - 待定：`EDITOR` 未设置时的行为（报错退出 2 / 回退到某个默认编辑器）；服务日志（caddy / dnsmasq）没有文件，是否支持 --edit。
  - **落定**（2026-10-26 与用户确认）：
    - `EDITOR` 没设就看 `VISUAL`；两个都没有 → **报错退出 2** 并给出 `export EDITOR=vim` 的提示
      （不猜：硬塞一个 nano/vi 比报错更糟）。
    - **服务日志不支持 `--edit`**：caddy / dnsmasq 是 journalctl 里的滚动日志，不是文件，
      编辑器打不开 → 明确报错并指向 `devproxy logs caddy`（跟随）。
    - `--edit` 必须带目标名；与 `--list` 互斥（同时给 → 退出 2）。
    - 编辑器退出码非 0 → 退出 1 并报出退出码；编辑器不存在 → 干净报错，不抛 traceback。
  - 实现落点：
    - `src/devproxy/platform/base.py` 的 `Platform` 协议加 `open_in_editor(path, editor) -> int`；
      linux / macos 各自实现（`shlex.split(editor)` 拆词 + 文件路径，**继承 stdio** —— 编辑器要接管终端）；
      `fake.py` 记录调用不真开。
    - `src/devproxy/cli/logs.py` 的 `_resolve_editor()` / `_edit_log()` 与 `--edit` 选项。
  - 验证：`tests/test_logs.py` 新增 9 个 `--edit` 用例（正常打开 / VISUAL 兜底 / 没设 EDITOR 退 2 /
    服务日志退 2 / 未知路由退 2 / 缺目标名退 2 / 与 --list 互斥 / 编辑器非 0 退出码 / 编辑器不存在）；
    `tests/test_platform_linux.py::test_linux_open_in_editor_splits_command_and_streams`；
    全量 1072 passed。
