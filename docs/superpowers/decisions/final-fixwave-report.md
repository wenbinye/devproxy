# Plan 1 终审修复波报告（fixwave）

- 基线：HEAD `726c4b3`，测试 **150 passed + 18 subtests**
- 修复后：HEAD `2457761`，测试 **214 passed + 18 subtests**（`python3 -m pytest -q --ignore=tests/test_integration.py`）
- 按要求未运行 `devproxy selftest` 与 `tests/test_integration.py`（会起真实 Caddy、碰信任库材料）
- 提交（每条均先写失败测试、确认红、再实现、再转绿）：
  - `4abaf0b` 🐛 fix(devproxy): 前置条件退出码 3 与路由表损坏的干净报错（I-1、I-2）
  - `cfbb5ad` 🐛 fix(devproxy): 进程身份校验防 pid 复用误杀 + 心跳复活竞态（I-3、I-4）
  - `2457761` 🐛 fix(devproxy): Caddyfile 输入值校验与转义 + selftest fail-closed 路径补测（I-5、I-6、杂项）

---

## I-1 — Caddy 未运行要退出码 3，不是 1 —— 已修复

**改动**
- `src/devproxy/proxy/runner.py`：新增 `class PreconditionError(UpError)`；`UpRunner.run` 在 `self.admin.is_alive()` 为假时抛它（原来是普通 `UpError`），消息改为可执行的："Caddy 未运行（Admin API 不可达），请先执行 devproxy setup 或启动 Caddy"。
- `src/devproxy/cli.py`：`up` 先 `except PreconditionError` → `EXIT_PRECONDITION`(3)，再 `except UpError...` → 1。`expose` 在写表前显式检查 `admin.is_alive()`，不满足直接 `fail(..., 3)`。`down`/`ls`/`config` 未动（Caddy 停着也要能读写路由表，reload 失败仍诚实地退 1）。

**覆盖测试**（红 → 绿）
- `tests/test_cli.py::test_up_when_caddy_not_running_exits_3`（修前 exit_code 1）
- `tests/test_cli.py::test_expose_when_caddy_not_running_exits_3`（修前 exit_code 0 且写了 routes.json）
- `tests/test_runner.py::test_run_caddy_not_running_raises_precondition`（断言具体类型与 hint）

**命令 / 输出**
```
$ python3 -m pytest -q tests/test_cli.py::test_up_when_caddy_not_running_exits_3 tests/test_cli.py::test_expose_when_caddy_not_running_exits_3
2 passed
```
真机复核（`admin_addr=127.0.0.1:2099` 复现"Caddy 未运行"）：
```
$ devproxy up codeup -- sleep 5   → devproxy: Caddy 未运行（Admin API 不可达），请先执行 devproxy setup 或启动 Caddy   exit=3
$ devproxy expose legacy --port 3000 → 同上  exit=3
$ devproxy ls                     → （无路由） exit=0
```

---

## I-2 — 损坏的 routes.json 要干净报错，不是 Rich traceback —— 已修复

**改动**
- `src/devproxy/proxy/routes.py`：新增 `class RouteTableError(RuntimeError)`（放 routes.py，避免与 store.py 循环 import）；`load_table` 把 `json.JSONDecodeError/KeyError/ValueError/TypeError/AttributeError` 统一映射为 `RouteTableError("路由表损坏: <path> — <原因>")`。`AttributeError` 是测试逼出来的：顶层是 list 时 `d.get` 抛的就是它。
- `src/devproxy/cli.py`：`up/expose/down/ls/config` 全部显式捕获 `RouteTableError`（新增 `fail()` 辅助：一行 Rich 错误 + 指定退出码）；`main()` 兜底 `except (ConfigError, RouteStoreError, RouteTableError)` → 干净一行 + exit 1。

**覆盖测试**
- `tests/test_cli.py::test_ls_with_invalid_json_routes_file_is_clean_error`（修前：RouteTableError 以 traceback 形态抛出）
- `tests/test_cli.py::test_ls_with_routes_missing_required_key_is_clean_error`
- `tests/test_cli.py::test_down_with_corrupt_routes_file_is_clean_error`
- `tests/test_cli.py::test_config_with_corrupt_routes_file_is_clean_error`
- `tests/test_cli.py::test_main_backstop_turns_domain_errors_into_exit_1`（参数化 RouteStoreError / RouteTableError / ConfigError）

**命令 / 输出**
```
$ python3 -m pytest -q tests/test_cli.py          → 20 passed
$ devproxy ls   （routes.json = "{ broken"）
devproxy: 路由表损坏: /tmp/.../state/routes.json — Expecting property name enclosed in double quotes: line 1 column 3 (char 2)
exit=1
```

---

## I-3 — 按裸 pid 杀进程组可能误杀无关进程 —— 已修复

**改动**
- `src/devproxy/proxy/runner.py`：新增 `proc_start_time(pid)` —— Linux 读 `/proc/<pid>/stat` 第 22 字段（在最后一个 `)` 之后切分，comm 可含空格；索引 19），无 /proc（macOS）退回 `ps -o lstart= -p`，都失败返回 `None`。
- `kill_pid_group(pid, proc_start=None) -> bool`：先读当前身份；进程不在 → False；身份对不上或未记录（None）→ 打印 `⚠️  进程 {pid} 已不是当初那个（pid 被复用），跳过清理` 并返回 False（宁可残留不误杀；`up` 自己的 finally 仍按直接句柄杀自己的孩子）。
- `Route` 新增 `proc_start: str | None`，随 `to_dict`/`from_dict` 持久化；`run()` 注册时写入 `proc_start_time(proc.pid)`；同名旧路由清理时传入 `existing.proc_start`；`cli.down` 传入 `route.proc_start`。

**覆盖测试**
- `tests/test_runner.py::test_kill_pid_group_skips_reused_pid`（伪造不匹配身份 → 进程未被杀；修前：老签名直接 TypeError，且老实现无任何校验）
- `tests/test_runner.py::test_kill_pid_group_skips_when_identity_unknown`（老版本路由表无 proc_start → 不杀）
- `tests/test_runner.py::test_kill_pid_group_kills_when_identity_matches`（身份匹配 → 真杀掉）
- `tests/test_runner.py::test_kill_pid_group_on_dead_pid_is_noop`、`test_proc_start_time_is_stable_and_missing_for_dead_pid`、`test_run_persists_proc_start_with_pid`
- `tests/test_render.py::test_route_roundtrip_keeps_proc_start`（持久化 + 老表缺字段 → None）
- `tests/test_cli.py::test_down_kills_leftover_process_group`（更新为断言 `(pid, proc_start)` 透传）

**命令 / 输出**
```
$ python3 -m pytest -q tests/test_runner.py -k "kill_pid_group or proc_start"   → 6 passed
```

---

## I-4 — 心跳"不复活已注销路由"的检查在锁外（TOCTOU）—— 已修复

**改动**（`src/devproxy/proxy/runner.py::_watch`）
- 存在性检查移入 `mutate` 回调（与写回同一把 flock）：回调内 `t.get(route.name) is None` 时抛模块私有 `_RouteGone`，异常先于渲染/落盘/reload 穿出 `mutate` —— 事务零副作用地中止，既不复活路由也不空转 reload（这点很重要：简单地"检查后仍调 mutate"会让无操作的心跳每 30s 触发一次 reload，现有测试 `test_watch_does_not_revive_removed_route` 正是靠 reload 计数拦住了这个退化）。`_watch` 捕获 `_RouteGone` 后结束线程。

**覆盖测试**
- `tests/test_runner.py::test_watch_does_not_resurrect_route_removed_between_check_and_write`：用实例级 `racing_mutate` 包装器确定性地把"down"插在心跳取锁之前 —— 对**旧实现实测为红**（`assert not t.is_alive()` 失败，路由被复活、线程继续跑），新实现绿。
- 复核方式：临时把 `_watch` 还原为旧的无锁检查版本重跑该测试 → `1 failed`（见下），随后恢复。

**命令 / 输出**
```
$ python3 -m pytest -q tests/test_runner.py::test_watch_does_not_resurrect_route_removed_between_check_and_write
1 passed        # 旧实现下：AssertionError: assert not True (Thread still alive)
```

---

## I-5 — fail-closed 路径零自动覆盖 —— 已修复（补测）

**改动**
- `src/devproxy/proxy/selftest.py`：把"等 caddy 打出 install_trust 已禁用日志"的内联循环提取为 `_wait_trust_disabled_log(log_path, timeout=10.0)`（行为不变，`run_selftest` 照常以默认 10s 调用），从而可单测。
- 新增 `tests/test_selftest.py`（14 个用例，全部不需要真 caddy）：
  - 版本解析：`test_caddy_version_parses_version_strings`（v2.11.7 / v2.7.0 / 2.6.4 / beta）、`..._returns_none_on_unparseable_output`、`..._returns_none_when_binary_fails`（monkeypatch `selftest.subprocess.run`）。
  - 版本地板 fail-closed：`test_selftest_fails_closed_on_caddy_older_than_floor` —— `caddy version` 假回 `v2.6.4`，断言有"过旧"失败项且 `subprocess.Popen`（起沙箱）**从未被调用**。
  - 地板放行：`test_selftest_accepts_caddy_at_floor` —— `v2.7.0` 时无版本地板失败（用假 `adapt` 失败让流程在版本门之后自然早退，避免 15s 的真实探测循环）。
  - 日志证据：`test_trust_disabled_log_line_is_detected`（含 Caddy 原句 → True）、`test_trust_disabled_log_missing_fails_closed`、`test_trust_disabled_log_absent_file_fails_closed`。
  - 常量锁死：`test_min_version_floor_is_2_7_and_log_string_is_pinned` —— `MIN_CADDY_VERSION == (2,7,0)`；证据串已对照本机真机二进制（`~/.local/bin/caddy` v2.11.7，`strings` 命中 `root certificate trust store installation disabled; unconfigured clients may show warnings`）。

**命令 / 输出**
```
$ python3 -m pytest -q tests/test_selftest.py     → 14 passed
```

---

## I-6 — 直通 Caddyfile 的值既不校验也不转义 —— 已修复（纵深双层）

**改动**
1. `src/devproxy/config.py`：`load_config` 对生效配置做值级校验（错误点名键 + 允许取值）：`domain_suffix` 单 DNS 标签（`^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`、无点、≤63）；`dns_backend ∈ auto|resolved|system`；`http_port/https_port` 1..65535 整数；`ready_timeout` 1..3600；`admin_addr` 必须 host:port；`storage/caddy_bin` 必须字符串。
2. `src/devproxy/proxy/routes.py`：新增 `check_renderable(route)`，`render_caddyfile` 渲染前对每条路由校验 `domain`（严格小写主机名，多标签、≤253）与 `upstream`（`host:port`，端口 1..65535），非法抛 `RouteTableError`（消息点名路由与字段）—— 拒绝而非转义：Caddyfile 没有可靠的通用转义，唯一正解是不让元字符进来。手改 routes.json 也被这层拦住。

**覆盖测试**
- `tests/test_config.py::test_invalid_domain_suffix_is_rejected`（参数化 8 个非法值，含 `test }\nimport /tmp/evil\n#`）、`test_domain_suffix_wrong_type_is_rejected`、`test_invalid_config_values_are_rejected`（12 例）、`test_valid_domain_suffixes_pass`、`test_valid_admin_addr_and_ports_pass`
- `tests/test_render.py::test_render_rejects_domain_with_caddyfile_metacharacters`、`test_render_rejects_upstream_with_caddyfile_metacharacters`、`test_render_rejects_domain_with_dots_or_uppercase`、`test_render_rejects_bad_upstream_forms`

**命令 / 输出**
```
$ python3 -m pytest -q tests/test_config.py tests/test_render.py   → 30 + 26 passed
```

---

## 杂项（同波处理）

- **`cli.py` down 传身份**：见 I-3。
- **Rich 标记吞字符集**：`cli.py` 的 `fail()`/`main()` 对错误消息过 `rich.markup.escape`；`up --help` 文案改 `\\[a-z0-9-]`；`ls` 表格单元格同样 escape。测试：`tests/test_cli.py::test_invalid_name_error_keeps_charset_in_output`（修前输出为"限单标签 ，首字符…"，字符集被吞）、`test_up_help_text_keeps_charset`。真机复核：错误行与 help 均完整显示 `[a-z0-9-]`。
- **`caddy.py` validate/reload 漏 `OSError`**：`except (FileNotFoundError, PermissionError)` → `except OSError`（Exec format error 不再裸抛，消息带 errno）。测试：`tests/test_caddy.py::test_validate_wrong_format_binary_returns_false_not_exception`、`test_reload_wrong_format_binary_returns_false_not_exception`。
- **`caddy.py` `api()` 非 JSON 200**：`json.loads` 包 try，`(json.JSONDecodeError, UnicodeDecodeError)` → `(code, None)`。测试：`test_api_non_json_200_returns_code_and_none`。

---

## 无异议说明（非误报，但值得记录）

- `expose` 在"Caddy 在跑、但主 Caddyfile 尚不存在"时会 reload 失败退 1（真机复核时观察到）。这是 Plan 1 既有行为：主 Caddyfile 由 `devproxy setup`（后续 Plan）创建，`build_context` 从不生成它。与本次六个 finding 无关，未改动。
- I-4 若按建议"检查后仍无条件 mutate"会让空闲心跳每 30s 空转一次 reload（被现有 reload 计数测试拦住）；实现改用 `_RouteGone` 异常中止事务，语义更严格。

---

# 全分支终审修复波报告（fixwave · 第二波，17 条 finding）

- 基线：HEAD `a5daddb`，单元套件 **609 passed + 18 subtests passed**（`python3 -m pytest -q --ignore=tests/test_integration.py`）
- 修复后：HEAD `8219d58`，单元套件 **579 passed**（609 − 68 个已删除实现的 `devp` 旧测试 + 38 个本波新增测试）。
  **18 个 subtests 随 `tests/test_devp.py` 一并消失** —— 它们全部写在那个被删除的测试文件里
  （`tests/test_devp.py` 的 `self.subTest`），没有第二个来源。
- 全程未碰真实系统：没有 `sudo`、没有写 `/etc`、没有装系统信任库、没有起真实 Caddy、
  没有动真实的 `~/.config/dnsmasq/`、`~/.config/systemd/user/`、`~/.pki/nssdb`。
- 提交（每条均先写失败测试 → 确认红 → 实现 → 转绿）：
  - `281ed7b` 🐛 fix(devproxy): 信任判定改为指纹比对，Linux 特权写 0644，doctor 不再要密码（1、9、14）
  - `d7dd4e1` 🐛 fix(devproxy): 注册路由后提示信任、拒绝确认退 4、config 只报生效的 storage（2、3、12）
  - `b75a908` 🐛 fix(devproxy): 迁移明说个人站点不再被服务、单元退回遗留二进制（4、5）
  - `2efeb3b` ✨ feat(devproxy): 主 Caddyfile 真正写出 http_port/https_port，换二进制后重启服务（7、13）
  - `7fc8e32` 🐛 fix(devproxy): 事务结束清掉校验临时文件，原子写加 fsync 与唯一临时名（10、11）
  - `0cd1d1e` ✨ feat(devproxy): doctor 报出现役配置里残留的 *.test 通配块（16）
  - `05fb4be` 🔥 chore(devproxy): 删除 1.0 前身 devp 脚本与旧测试，补一份去向说明（6）
  - `bc9aa6b` ✨ feat(devproxy): 变更清单点名 DNS 改动的整机作用域（8）
  - `8219d58` ✨ feat(devproxy): 补 GitHub Actions CI，时序敏感测试改成事件驱动（17）

---

## 1（Critical）— `ca_trusted()` 永久假阴性 —— 已修复

**改动**
`src/devproxy/platform/linux.py`：信任判定的**权威依据改为指纹命中** —— CA 的 SHA-256 指纹
是否出现在**活的信任库**里（`/etc/ssl/certs/ca-certificates.crt` 的拼接内容，或
`/etc/ssl/certs` 下按哈希命名的文件）。暂存目录 `/usr/local/share/ca-certificates/`
里的文件**不算数**（`update-ca-certificates` 没跑成功时它也在那儿，系统却不认）。
`openssl verify` 降级为**补充的肯定信号**（验通了说明信任链成立；验不通**不再**用来否定
指纹命中 —— 同主题多根 CA 时 `openssl` 按 subject 选锚，本来就会误判）。
判定过程吞掉一切异常：判定失败 = 未信任，**绝不抛**。

**覆盖测试**（`tests/test_platform_linux.py`，修前全红）
`test_linux_ca_trusted_true_when_fingerprint_in_live_store`、
`test_linux_ca_trusted_true_when_fingerprint_in_hashed_dir`、
`test_linux_ca_trusted_false_when_only_staged_file_present`、
`test_linux_ca_trusted_false_when_same_subject_other_root`、
`test_linux_ca_trusted_true_when_openssl_verifies`、
`test_linux_ca_trusted_never_raises_on_binary_file`、
`test_linux_ca_trusted_false_when_ca_not_pem`

**命令 / 输出**
```
$ python3 -m pytest -q tests/test_platform_linux.py   → passed
```

---

## 2（Important）— `up`/`expose` 不提示 CA 尚未被信任 —— 已修复

**改动**
`src/devproxy/cli.py`：`up` / `expose` 成功之后，若**存在 CA** 但在系统信任库与 NSS 库里
**都还没有**被信任，就打印既有的信任提示（`devproxy trust`）。三种情况明确区分：
没有 CA → 不提示（`setup` 阶段提示过「CA 尚未生成」）；已被信任 → 不提示；
只有系统库没装但 NSS 装了 → 也不提示（Chrome 已经认了）。

**覆盖测试**（`tests/test_cli.py`）
`test_up_prints_trust_hint_when_ca_not_trusted`、
`test_expose_prints_trust_hint_when_ca_not_trusted`、
`test_expose_does_not_print_trust_hint_when_ca_trusted`、
`test_expose_does_not_print_trust_hint_without_ca`

---

## 3（Important）— 用户拒绝确认却以退出码 0 结束 —— 已修复

**改动**
`src/devproxy/cli.py`：确认提示被拒绝时以 `EXIT_DECLINED`(=**4**) 退出，并明确打印
「已取消」与「什么都没改」。退出码 4 的语义统一为「**需要人拍板而这次没人拍**」
（spec §9.1 与 README「退出码」表同步更新）。理由：`devproxy setup && devproxy up …`
会把「没装」当成「装好了」继续跑。

**覆盖测试**（`tests/test_setup.py`）
`test_cli_setup_aborts_when_user_declines` —— 断言 `exit_code == 4`、`applied is None`、
输出含「已取消」与「什么都没改」。

---

## 4（Important）— 迁移静默停掉个人站点的服务 —— 已修复

**改动**（**取 (b) 方案：保留说明，但明说「不再被服务」**）
`src/devproxy/migrate.py::_write_sites_caddy`：当 `sites.caddy` 已有内容时，迁移**不覆盖**
它（重复站点块会让 Caddy 直接起不来），而是在迁移提示里**点名文件与地址**明说：
这些个人站点**将不再被服务**（旧 Caddyfile 不再被主 Caddyfile import），请手工把站点块
从 `<遗留 Caddyfile>` 合并到 `~/.config/devproxy/sites.caddy`（备份目录里也有原件）。
`sites.caddy` 为空时则照常把个人站点块搬进去（那样它们仍被服务）。

**为什么选 (b) 而不是 (a) 自动合并**：自动合并要处理站点块重名、Caddyfile 语法冲突
与 `import` 顺序，猜错会写出**起不来的** Caddyfile —— 那比「明说不服务」危险得多；
而且合并结果必须由用户确认（站点归属与端口是他的决定）。本条按要求做成了可测的：
提示文案与地址都能断言。

**覆盖测试**（`tests/test_migrate.py`）
`test_migrate_states_that_personal_sites_are_no_longer_served`

---

## 5（Important）— 迁移在下载二进制之前就改写单元 —— 已修复

**改动**
`src/devproxy/migrate.py`：改写服务单元时，二进制路径**先回退到遗留单元的 ExecStart**
（与 `_validate_caddyfile` 同一套写法），而不是直接写新路径 —— 遗留二进制此刻还在，
单元不会因为「指向一个还不存在的文件」而起不来。真正的下载与切换由后续的 setup 步骤完成。

**覆盖测试**（`tests/test_migrate.py`）
`test_migrate_unit_falls_back_to_legacy_binary_when_config_binary_missing`

---

## 6（Important）— 遗留 `devp` 脚本与其测试仍在树里 —— 已修复

**改动**
删除仓库根目录的 `devp`（536 行）与 `tests/test_devp.py`（967 行 / **68 个测试** —— 它们
测的是一个已经不存在的实现），补 `docs/legacy-devp.md` 记录它的来历、为什么被取代、
命令对应关系与机器侧残留物（`~/.local/bin/devp`、旧路由由机器主人处理）。
README 的「仓库里的历史文件」一节同步说明。**任务已明确授权删除这两个被跟踪的文件。**

**命令 / 输出**
```
$ git show --stat 05fb4be
 devp               | 536 ------------
 tests/test_devp.py | 967 ---------------------
 docs/legacy-devp.md|  68 ++
```

---

## 7（Important）— `http_port` / `https_port` 配了不生效 —— 已修复

**改动**
`src/devproxy/proxy/caddy.py::build_main_caddyfile`：把 `http_port` / `https_port`
写进主 Caddyfile 的**全局选项块**（默认仍是 80/443）；`src/devproxy/proxy/store.py` 与
`src/devproxy/migrate.py` 的调用点同步传参。README 里「目前只出现在 config 输出里」
的**陈旧描述**一并更正。

**覆盖测试**（`tests/test_caddy.py`）
`test_build_main_caddyfile_emits_ports`、`test_build_main_caddyfile_default_ports`

---

## 8（Important）— `FallbackDNS` 是整机作用域却没被点名 —— 已修复（只改披露，机制不动）

**改动**
机制**保持不变**（controller 裁定：只改披露）。披露文案补全：
- `src/devproxy/components/dns.py`：抽出常量 `FALLBACK_DNS = "1.1.1.1 8.8.8.8"` 与
  `global_scope_disclosure(port)`；`render_resolved_conf` 与披露文案**共用同一份常量**
  （测试断言两者不会漂移）。`DnsComponent` 新增可选钩子 `global_scope_note()` ——
  非 resolved 后端返回 `None`（macOS 的每域解析器文件与 `system` 后端片段只影响本后缀）。
- `src/devproxy/cli.py`：`_global_scope_notes(plan)` 收集整机作用域披露，在变更清单
  表格**之后**单独打印（Rich 表格单元格装不下 CJK 长文本，更放不下重点）。
- `README.md`：「⚠️ 这个工具会修改你的系统」与「### DNS」两节点名 `DNS=` 的整机作用域、
  `FallbackDNS=1.1.1.1 8.8.8.8` 这一行，以及 dnsmasq 起不来时全机解析退化为公共解析器。

**覆盖测试**（`tests/test_component_dns.py` 5 例 + `tests/test_setup.py` 2 例）
`test_global_scope_note_names_machine_wide_dns_and_fallback`、
`test_global_scope_note_none_for_system_backend`、
`test_global_scope_note_none_for_macos_resolver_backend`、
`test_resolved_plan_description_marks_global_scope`、
`test_fallback_dns_constant_matches_rendered_conf`、
`test_cli_setup_prints_global_scope_disclosure`、
`test_cli_setup_without_global_scope_component_prints_no_disclosure`

---

## 9（Minor）— Linux 特权写出的文件是 `root:root 0600` —— 已修复

**改动**
`src/devproxy/platform/linux.py`：`ElevatedSession.write` / `copy` 改用
`install -m 644 -o root -g root`（不再是 `cp` 把源文件权限带过去、也不是 `mkstemp`
的 0600）。与 macOS 的 `chmod 644` + `chown root:wheel` 对齐：普通用户自己都读不到
的文件会让 `doctor` 永远报「读不到」。

**覆盖测试**（`tests/test_platform_linux.py`）
`test_linux_elevated_write_produces_world_readable_root_file`、
`test_linux_elevated_copy_produces_world_readable_root_file`、
`test_linux_elevated_write_uses_system_temp_then_sudo_install`、
`test_linux_elevated_write_creates_parent_dir_before_install`

---

## 10（Minor）— `routes.caddy.new` / `Caddyfile.new` 留在原地 —— 已修复

**改动**
`src/devproxy/proxy/store.py`：事务结束（**成功或失败**）都在 `finally` 里清掉
校验用的临时文件 —— 留着它们只是噪音，还会让人以为线上有两份配置。

**覆盖测试**（`tests/test_store.py`）
`test_mutate_removes_temp_validation_files`、`test_mutate_removes_temp_validation_files_on_failure`

---

## 11（Minor）— `atomic_write` 不 fsync、临时名固定 —— 已修复

**改动**
`src/devproxy/proxy/routes.py::atomic_write`：`os.replace` 之前 `fsync`；
临时名带上 pid（`{name}.{pid}.tmp`），两个写者不会撞进同一个临时文件；无论成功失败
都清掉自己的临时文件。

**覆盖测试**（`tests/test_render.py`）
`test_atomic_write_uses_unique_temp_name`、`test_atomic_write_fsyncs_before_replace`、
`test_atomic_write_cleans_temp_on_failure`

---

## 12（Minor）— `config` 打印了两个不同的 storage 路径 —— 已修复

**改动**
`src/devproxy/cli.py`：`devproxy config` **先打印生效的 `storage`**（带说明：CA 与证书
都在那儿），再把 `data_dir_default` 明确标成「默认目录，被 config.toml 的 storage
覆盖时以 storage 为准」。

**覆盖测试**（`tests/test_cli.py`）
`test_config_marks_effective_storage_and_labels_default_dir`

---

## 13（Minor）— 版本不符时换了二进制却不重启服务 —— 已修复

**改动**
`src/devproxy/components/caddy.py`：检测到二进制与固定版本不符时，除了替换二进制，
还要**显式重启服务**（「已固定到 2.11.7」的承诺要等到重启才兑现）；`enable --now`
对已 active 的单元不会重启，因此必须显式 `restart`。

**覆盖测试**（`tests/test_component_caddy.py`）
`test_plan_restarts_service_when_binary_is_replaced`、
`test_apply_restarts_service_after_replacing_binary`

---

## 14（Minor）— `doctor` 号称只读却可能提示密码 —— 已修复

**改动**
`src/devproxy/platform/linux.py`：特权读回退改为**非交互**（`sudo -n cat …`）——
没有缓存凭据时立刻失败，绝不等待输入；读不到就返回 `None`（按「读不到」处理，
而不是逼用户输密码）。

**覆盖测试**（`tests/test_platform_linux.py`）
`test_linux_privilege_read_is_non_interactive`、
`test_linux_privilege_read_returns_none_on_failure`、
`test_linux_privilege_read_returns_none_when_sudo_missing`、
`test_doctor_never_asks_for_password`

---

## 15（Minor）— 死代码与过时注释 —— 已修复

**改动**
- `src/devproxy/migrate.py`：删掉未使用的 `was_active`（`components/caddy.py` 里的同名
  变量**仍在使用**，未动）。
- `src/devproxy/platform/macos.py`：DNS 解析超时的注释原写「解析器挂住不能把 setup 的
  自检拖死」并提到 doctor —— 改为准确的「拖死 setup 的自检」，并注明 `dns_query`
  **只在 `verify()` 里被调用**（doctor 不做 DNS 解析）。

---

## 16（Minor）— `doctor` 不报现役配置里残留的 `*.test` 通配块 —— 已修复

**改动**
`src/devproxy/doctor.py`：新增 `find_wildcard_sites()`，扫描仍在生效的 Caddy 配置
（主 Caddyfile / routes.caddy / sites.caddy）里**还带着通配站点块**（如 `*.test`）的配置，
作为一条 finding 报出来，并给出修复动作「跑 `devproxy setup` 完成迁移」。
`src/devproxy/cli.py`：doctor 把它渲染进输出，命中时以退出码 3 结束。

**覆盖测试**（`tests/test_doctor.py`）
`test_find_wildcard_sites_detects_test_wildcard`、
`test_find_wildcard_sites_ignores_other_suffix`、
`test_cli_doctor_reports_wildcard_and_exits_3`、
`test_cli_doctor_ok_when_no_wildcard`

---

## 17（Minor）— 没有 CI，负载高时 4 个时序测试随机翻车 —— 已修复

**改动**
1. **CI**：新增 `.github/workflows/ci.yml` —— push 到 `main` / 提 PR / 手动触发时，
   在 Python 3.11 / 3.12 / 3.13 上跑单元套件。单元套件是**封闭的**（不碰真实系统、
   不需要 caddy 二进制、不需要网络，`HOME` 在测试里被指向临时目录），
   `tests/test_integration.py` 需要真实 caddy 与真实网络，**不进 CI**。
2. **时序敏感测试改成事件驱动**：`tests/support.py` 新增 `wait_until(predicate, timeout, interval)`。
   「先 sleep N 秒、再断言」这种写法在负载高的机器上会随机翻车 —— 睡够了不代表另一端的
   动作真的发生了（子进程可能还没起来、守护线程可能还没跑到第一轮心跳）。
   `tests/test_runner.py` 里所有此类断言改成**轮询到事件发生为止**，并把子进程启动、
   心跳首轮、进程组清理的等待上限放宽到 30~60s：
   - `test_stop_process_group_kills_grandchildren`（孙进程 pid 标记 + 按组清理）
   - `test_run_kills_process_on_ready_timeout`（超时后子进程与孙进程必须被杀）
   - `test_watch_re_registers_when_route_lost_from_caddy`（心跳重新注册）
   - `test_watch_re_registers_when_only_prefixed_domain_remains`（前缀域名不得伪装存活）
   - `test_run_signal_forwards_to_group_and_deregisters`（信号注销后的清理）
   - `test_kill_pid_group_kills_when_identity_matches`
   - `test_watch_does_not_resurrect_route_removed_between_check_and_write`（join 超时 5→30s）
   负向断言（不得误杀、不得复活已注销路由）保留固定等待，但留足余量（0.3s → 1.0s）。
   **最易翻车的四个**（两个心跳重注册、两个进程组清理）正是原先靠固定 sleep 断言的用例；
   本机负载平均在 70~570 之间（与本项目无关的其它负载），固定 sleep 的写法在这种机器上
   必然随机翻车，事件驱动才是根治。
3. **守护测试**：新增 `tests/test_ci.py`（3 例）—— workflow 文件必须存在、必须跑
   `--ignore=tests/test_integration.py` 的单元套件、必须装齐 typer/rich/pytest。

**命令 / 输出**
```
$ python3 -m pytest -q tests/test_ci.py tests/test_runner.py   → 3 + 23 passed
```

---

## 测试数量变化（按提交）

| 提交 | 收集数 | 说明 |
|---|---|---|
| `a5daddb`（基线） | 609 | 本波起点 |
| `281ed7b` | 617 | +8（指纹比对 / 0644 / 非交互读） |
| `d7dd4e1` | 622 | +5（信任提示 / 拒绝确认 / 生效 storage） |
| `b75a908` | 624 | +2（迁移站点说明 / 单元二进制回退） |
| `2efeb3b` | 628 | +4（端口写出 / 换二进制重启） |
| `7fc8e32` | 633 | +5（fsync / 唯一临时名 / 清理临时文件） |
| `0cd1d1e` | 637 | +4（通配块检测） |
| `05fb4be` | 569 | **−68**（删除已废弃实现的 `devp` 旧测试） |
| `bc9aa6b` | 576 | +7（整机作用域披露） |
| `8219d58` | **579** | +3（CI 守护测试） |

净变化：**609 → 579**（−68 个旧测试，+38 个本波新增测试；18 个 subtests 随旧测试文件一起消失）。
基线全绿：`609 passed, 18 subtests passed in 146.54s`（当时本机负载平均 70~570）。
终态全绿：`579 passed in 14.71s`。

---

## 未处理（明确记录，非本波职责）

- **缺 `LICENSE` 文件**：`pyproject.toml` 里已声明 `license = "MIT"`，但仓库里没有
  LICENSE 正文文件。**选许可证是项目主人的决定**，本波只记录、未添加。
- **`pyproject.toml` 的 `[project.urls]`**：本仓库**没有配置 remote**（`git remote -v`
  为空），无法确定公开仓库地址，按要求不猜测、不添加，留待项目主人补。
- **真实机器的信任库、`~/.local/bin/devp`、现役 Caddy 配置**：按要求由机器主人 /
  controller 处理，本波全程未碰。
