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
  - **落定**（2026-10-26 与用户确认；同日按用户反馈修订）：
    - `EDITOR` 没设就看 `VISUAL`；两个都没有 → **依次探测 `vim` / `vi` / `nano`**
      （`shutil.which`，用第一个在 PATH 里的）；一个都没有 → **报错退出 2** 并给出
      `export EDITOR=vim` 的提示（候选是探测出来的，不是硬塞 —— 硬塞一个没装的
      只会换来 "command not found"）。
    - 短选项 `-e` 与 `--edit` 等价（用户反馈第一条就是想敲 `logs wrs-api -e`）。
    - **服务日志不支持 `--edit`**：caddy / dnsmasq 是 journalctl 里的滚动日志，不是文件，
      编辑器打不开 → 明确报错并指向 `devproxy logs caddy`（跟随）。
    - `--edit` 必须带目标名；与 `--list` 互斥（同时给 → 退出 2）。
    - 编辑器退出码非 0 → 退出 1 并报出退出码；编辑器不存在 → 干净报错，不抛 traceback。
  - 实现落点：
    - `src/devproxy/platform/base.py` 的 `Platform` 协议加 `open_in_editor(path, editor) -> int`；
      linux / macos 各自实现（`shlex.split(editor)` 拆词 + 文件路径，**继承 stdio** —— 编辑器要接管终端）；
      `fake.py` 记录调用不真开。
    - `src/devproxy/cli/logs.py` 的 `_resolve_editor()` / `_edit_log()` 与 `--edit` 选项。
  - 验证：`tests/test_logs.py` 新增 11 个 `--edit` 用例（正常打开 / VISUAL 兜底 /
    没设 EDITOR 时探测候选、候选全缺退 2 / 服务日志退 2 / 未知路由退 2 / 缺目标名退 2 /
    与 --list 互斥 / 编辑器非 0 退出码 / 编辑器不存在）；
    `tests/test_platform_linux.py::test_linux_open_in_editor_splits_command_and_streams`；
    全量 1074 passed。

- [x] #4: **bash 补全**（2026-10-26 Ye Wenbin 登记；2026-10-26，Claude 执行）：
  - 需求：`devproxy` 在 bash 里有 Tab 补全。
  - **落定**：命令名与选项的补全 Typer/Click **本来就带**（`--install-completion` /
    `--show-completion`，支持 bash / zsh / fish / powershell），自定义的 `--` 切分组
    没有破坏它 —— 实测子命令、选项、前缀过滤都正常，`--` 之后不再补（正确）。
    本次补的是**参数值域**：新增 `src/devproxy/cli/_completion.py`，
    `logs <名字>` 补服务名 + **已注册路由名**、`down <名字>` 补路由名。
    候选来源直接读 `routes.json`（不走 `build_context`，也不打日志）——
    补全每次 <TAB> 都会跑一遍，必须又快又不能炸：读不到路由表就当没有候选，
    绝不往 stdout 打报错（bash 会把每行输出都当成补全项）。
    `up` / `expose` 的名字是新建路由名，不补；路径类选项（`--file` / `--config`）
    靠生成脚本里的 `-o default` 回退到 bash 的文件名补全。
  - 使用：`devproxy --install-completion`（bash 装到 `~/.bash_completions/devproxy.sh`
    并在 `~/.bashrc` 加 source，重开终端生效）。
  - 验证：`tests/test_completion.py`（6 个用例，直接驱动 Click 的 ShellComplete）；
    真机模拟 `_DEVPROXY_COMPLETE=complete_bash` 验证子命令 / 选项 / 路由名 /
    前缀过滤 / `--` 边界；全量 1080 passed。

- [x] #5: **macOS 进 CI，并修掉它翻出来的三处平台缺口**（2026-10-26，Claude 登记并执行）：
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
