# SDD ledger — plan: docs/design/plans/2026-10-22-devproxy-core-gateway.md

- 规格（权威）：`docs/design/specs/2026-10-22-devproxy-local-dev-env-tool-design.md`
- 工作区：`.superpowers/sdd/2026-10-22-devproxy-core-gateway/`
- 代码分支：`worktree-devproxy`（worktree 位于 `.claude/worktrees/devproxy`）
- 计划基线提交：`5cfd722`
- 计划修正提交：`1056457`（down 补杀残留进程组）

---

## 预检冲突扫描（Task 1 派发前）

### 共享文件 / 接口的 Task 对

| 产出方 | 消费方 | 接口 | 结论 |
|---|---|---|---|
| T1 | T2 | `Paths` / `Config` | T2 不用它们，无冲突 |
| T1 | T3 | `Config.admin_addr` / `storage_path` | T3 的 `build_main_caddyfile` 收纯参数、不 import Config。接口注记略高估，无代码冲突 |
| T1 | T6 | `Paths`、`Config`、`load_config`、`default_paths` | 一致 |
| T2 | T4 | `Route`/`RouteTable`/`load_table`/`render_caddyfile`/`save_table`/`atomic_write` | T4 的 import 列表与之逐项吻合 |
| T2 | T5 | `Route`、`domain_for`、`validate_name` | 一致 |
| T3 | T4 | `CaddyConfig.validate(caddyfile=None)`、`build_main_caddyfile` | T4 的 `_validate` 调用签名吻合 |
| T3 | T5 | `CaddyAdmin.api()` / `.is_alive()` | T5 用到两个方法，都存在 |
| T3 | T6 | `CaddyAdmin`、`CaddyConfig` | 一致 |
| T4 | T5 | `RouteStore.get()` / `.mutate()` | 一致 |
| T4 | T6 | `RouteStore.get()` / `.mutate()` / `.list()` | 一致 |
| T5 | T6 | `UpRunner`、`UpError`、`kill_pid_group` | 一致（`kill_pid_group` 为计划修正后新增的公开名） |
| T6 | T7 | `app`（Typer 实例） | T7 追加子命令，顺序执行无冲突 |
| T1 | T7 | `pyproject` 的 `pythonpath = ["src"]` | T7 的集成测试依赖它 ✓ |

### 各 Task 自洽性

| Task | 测试 vs 代码 | 结论 |
|---|---|---|
| T1 | 8 个用例 vs `paths.py` / `config.py` | 一致 |
| T2 | 27 个用例 vs `names.py` / `routes.py` | 一致（渲染确定性已在计划中固化） |
| T3 | 7 个用例 vs `caddy.py` | 一致（假 caddy 脚本已修正） |
| T4 | 9 个用例 vs `store.py` | 一致 |
| T5 | 12 个用例 vs `runner.py` | 一致（子进程必须真监听 `$PORT`；心跳自检已补） |
| T6 | 9 个用例 vs `cli.py` | 一致（测试改为 patch `build_context`） |
| T7 | 1 个用例 vs `selftest.py` | 一致（http→https 检查已前置到注销之前） |

### 扫描发现与裁决

| # | 发现 | 裁决 | 代价（若错） |
|---|---|---|---|
| F1 | T6 的 `down` 只删路由，不杀 `mode=up` 路由的残留进程组 —— 与规格 §5.2/§5.4 冲突 | **规格为准**：改计划（`_kill_pid_group` → 公开 `kill_pid_group`，`down` 调用它，补一条测试）。计划修正已提交 `1056457` | 极低：多杀一个本该杀的残留进程组 |
| F2 | T5 的心跳自检（规格 D11）在计划里没有实现 | 已补：`_route_live` + `_watch` 线程 + 2 个测试 | 无 |
| F3 | T5 的测试子进程不监听 `$PORT`，无法验证端口注入 | 已修正为真监听后退出 | 无 |
| F4 | T6 的测试 monkeypatch 了不存在的 `_reload_caddy` | 已改为 patch `build_context` 注入假 Caddy | 无 |
| F5 | T7 的 http→https 检查排在注销路由之后，必然失败 | 已前置 | 无 |
| F6 | T3 的假 caddy 脚本有无效分支 | 已简化 | 无 |

扫描结论：无未裁决的冲突，可以派发 Task 1。

---

## 进度

（每行格式：`Task <N>: complete (commits <base7>..<head7>, review clean)` 或 `Task <N>: fix round <R>/5 (...)`）

- Task 1: complete (commits 1056457..a4a281e, review clean — 1 Important 为 plan-mandated，已裁定下传)
- Task 1: Ruling: 存储路径双源（`DEVPROXY_DATA_DIR` 环境变量 vs `config.storage` 键，二者可独立覆盖且无优先级） — 规格为准：spec §7 定义 `storage` 是配置键、D13 要求 CA 连续，故 **`storage` 是唯一决定 Caddyfile 存储路径的键；`DEVPROXY_DATA_DIR` 降级为 `storage` 未被 config.toml 设置时的默认值提供者**（优先级 storage > env > 内置默认，与"CLI > config.toml > 默认"一致）。修法：`load_config` 里 storage 未被文件设置时读 env；补一条测试。**随 Task 6 派发带给实现者**（Task 6 建 build_context/config 命令时一并改 config.py）。若错，代价：设了 env 的用户 Caddyfile 仍指向旧存储、CA 断连续。
- Task 1: minor (deferred, 交最终 review 分诊): ① `load_config` 对畸形 TOML 抛 `TOMLDecodeError` 而非 `ConfigError`（Task 6 main() 只捕获 ConfigError 会漏成 traceback，随 Task 6 一并修）② `dns_backend`/`http_port` 等值未校验类型与枚举 ③ `default_paths()` 缺 env 未设时的回归测试、`load_config(None)`、`ensure_files` 对已有非空 sites.caddy 不覆盖的断言 ④ `caddy_bin` 键在实现里有、spec §7 没列（收尾时更新 spec）⑤ paths.py docstring 说"XDG 约定"但未读 XDG_*_HOME 变量；空字符串 env 得 `Path(".")` ⑥ Pyright 不认 pytest `pythonpath=["src"]` 的诊断噪音
- Task 2: complete (commits a4a281e..ad50a30, review clean)
- Task 2: minor (deferred, 交最终 review 分诊): ① test_render.py 顶部未使用的 `import json`（brief 逐字要求，无 lint 门禁，零影响）② `atomic_write` 固定 `.tmp` 后缀非并发安全（本工具单进程写 routes.json/Caddyfile，无该场景；真正的残余风险是并发写同一目标）③ 双路由渲染测试用子串断言弱于单路由的字节比对；"不含时间戳"仅查一个词；确定性测试未覆盖两个独立构造的相等表
- Task 3: fix round 1/5 (1 Important — validate/reload 无子进程超时、缺二进制时异常穿透；Ruling: 真缺陷非误报——挂死的 reload 会无限持有 flock 阻塞所有命令，违反 spec §9.2"失败时状态可控"意图，故不入 parked 直接修。修法：两方法加 timeout=30.0 参数 + FileNotFoundError/PermissionError/TimeoutExpired → (False, msg)，补 3 组测试)
- Task 3: minor (deferred): ① `api()` 对 200 但非 JSON 响应体抛 `JSONDecodeError` 穿透 tuple 契约（边缘场景，Caddy 正常返回 JSON）② test_caddy.py 未使用的 `import subprocess`（brief 逐字，无 lint 门禁）③ 真实 caddy 的 flag 拼写只有假脚本 argv 钉死（`--config`/`--address` 按规格采信；Task 7 的集成测试会碰真实 caddy 兜住这一点）④（re-review 观察）`caddy_bin` 存在但格式错误（Exec format error）抛普通 `OSError` 仍逃逸，可并入 `except OSError`；`PermissionError` 分支无专测
- Task 3: fix round 1/5 result: 1 addressed, 0 open（commit b42be1f；scoped re-review 判定 ADDRESSED，无新破坏）
- Task 3: complete (commits ad50a30..b42be1f, 1 fix round, review clean)
- Task 4: dispatched (implementer running, BASE b42be1f)
- Task 4: complete (commits b42be1f..fa51ad4, review clean)
- Task 4: minor (deferred, 交最终 review 分诊): ① 临时校验文件 `routes.caddy.new`/`Caddyfile.new` 事务后不清理（state_dir 内积累，下次覆盖，无害）② store.py 未使用 `import os`（brief 逐字）③ `_restore` 的 `old_caddy is None` 分支实际死代码（`ensure_files()` 先跑，routes_caddy 首读必存在空文件，回滚写 `""` 而非 unlink；功能等价）④ flock 互斥性无直接测试（仅断言锁文件存在）
- Task 5: dispatched (implementer running, BASE fa51ad4)
- Task 5: 实现者披露 2 处对 brief **测试代码** 的必要偏离（实现零偏离）：① brief 测试漏 `from devproxy.proxy.routes import Route`（原样必 NameError）② brief 的 `test_run_injects_port_and_registers_route` 在 `run()` 返回后断言路由仍在，与 brief 自身的清理要求（子进程退出→注销）及 `test_run_cleans_up_route_when_child_exits` **在任何合规实现下互斥**（必 IndexError）→ 改为在 `on_ready`（注册落盘瞬间）抓快照。Ruling: 计划缺陷，实现者的修法正确且未削弱断言（4 条断言原样保留，port 断言仍能证明 PORT 注入生效）。reviewer 独立核对后判定一致。
- Task 5: fix round 1/5 (2 Important + 2 Minor): ① 【重要·真缺陷】信号落在 `store.mutate` 事务中间 → SystemExit 穿出、store 不回滚（只 catch Exception 不 catch BaseException）、且 `registered=True` 在 mutate 之后才置位 → finally 跳过注销 → **留下指向死进程的路由**，违反 spec §5.4/§9.2 不变式。修法：`registered=True` 前置到注册事务之前 + 注销失败改为 stderr 打印可执行告警（旧 devp 同款）而非静默吞掉 ② 【重要·真缺陷】心跳 `_route_live` 用裸子串匹配 → `"x.test"` 命中 `"ax.test"` → 路由丢了永不重注册，D11 自检静默失效。修法：匹配 JSON 带引号的 host（`f'"{domain}"'`），并补"前缀路由仍在"的反例测试 ③ 【次要】就绪超时用例未断言子进程组真被杀（spec 明确要求杀组）④ 【次要】信号路径零测试覆盖 —— 要求实现者尝试真实 SIGTERM 用例，若不可靠则显式上报为 gap（不许弱化断言）。注：reviewer 把 ② 归因到"规格 D11 明文规定子串匹配"，我核对后确认子串匹配是**计划**的实现细节、规格 §5.4 只说"查 Admin API 里路由是否还在"——修法与规格一致。
- Task 5: minor (deferred, 交最终 review 分诊): ① 显式端口的占用检查是 connect 探针非 bind 探针（只监听非 loopback 或仅 IPv6 的服务探测不到 → 误判空闲 → 报错呈现为"子进程先退出"而非"端口被占用"）② 注销失败被静默吞掉（本次 fix 已改为 stderr 告警）③ 就绪超时预算被超出（每轮 reachable(timeout=1)+sleep(0.3)，实际可比 ready_timeout 多 1s+）④ 信号路径零覆盖（本次 fix 已尝试补，若不可靠则留给最终 review）
- Task 5: fix round 1/5 result: 5 addressed, 0 open（commit 0ee8526；scoped re-review 判定全部 ADDRESSED、无新破坏；新信号测试用 SignalingCaddy 在注册 reload 内发真 SIGTERM，确定性无时序竞争，回退修复必失败）
- Task 5: minor (deferred, re-review 观察): ① `on_signal` 两信号几乎同时到达且均在 stop.set() 前的理论双 SystemExit 竞争（修复前既有结构）② `test_run_kills_process_on_ready_timeout` 断言失败时会泄漏一对最长 60s 的 sleep 进程（无 finally 兜底，仅测试失败时的卫生问题）③ `kill_pid_group` 的 pid 复用误伤面（devp 原有语义）
- Task 5: complete (commits fa51ad4..0ee8526, 1 fix round, review clean)
- Task 6: dispatched (implementer running, BASE 0ee8526, 携带 Task 1 裁定的 2 处 config.py 修正)
- Task 6: complete (commits 0ee8526..4d7f9ef, review clean — 2 commits: CLI 3c6988e + config 修正 4d7f9ef)
- Task 6: 携带修复 A/B 已落地并有测试：storage 优先级 config.toml > DEVPROXY_DATA_DIR > 内置默认（config.py:57-60，三方向均测）；畸形 TOML → ConfigError("配置文件语法错误: …")，main() 捕获后 exit 1（config.py:49-52）
- Task 6: 已披露行为偏差（reviewer 判定可接受）：`up codeup npx vite`（无 `--`）被 Click 的 "Got unexpected extra arguments" 先拦，exit 2 而非 brief 代码路径的 1 —— 更贴合全局约定"2=参数错误（Typer 默认）"，brief 测试（!=0 且输出含 `--`）均成立
- Task 6: minor (deferred, 交最终 review 分诊): ① `up` 无 `--` 的友好提示在最典型误用形态下不可达（`up codeup npx vite` 被 Click 先拦，输出不含 `--` 用法提示；brief 的引导文案仅对裸 `up name` 生效）② `up` 的 `ignore_unknown_options=True` 可能静默吞掉 `--` 之前拼错的 devproxy 选项（如 `--pot`）③ lint 噪音：config 命令解包未用的 store、test_cli.py 未用的 runner fixture / Route import、EXIT_PRECONDITION 未用（均 brief 逐字）④ `down` 只看 pid 不看 mode（仅 up 路径写 pid，功能等价；仅手工编辑 routes.json 可能出现 expose+pid 被误杀的构造性边角）
- Task 7: dispatched (implementer running, BASE 4d7f9ef)
- Task 7: 实现者披露 5 处对 brief 的必要偏离（reviewer 独立复核后全部判定正确且必要）：① brief 的 selftest.py 引用未定义的 _Handler/_fetch → 模块内自备 ② **brief 流程本身跑不通**——空路由表时 Caddy 启动不 provision 本地 CA（reviewer 用 `caddy adapt` 探针独立证实：Caddyfile adapter 只产出 apps={http,tls}，无 pki app）→ 显式声明 pki app ③ `install_trust:false` 经 adapt→JSON 注入（避开 Caddyfile adapter 不暴露该字段）④ XDG_CONFIG_HOME/XDG_DATA_HOME 指向 tmp，防沙箱覆写生产 ~/.config/caddy/autosave.json（reviewer 用探针证实 autosave 归服务端进程所有）⑤ 加 CERT_NONE 探针 + 一项额外检查（reviewer 认可：brief 的检查对通配 *.test 块也会通过，变异测试让通配方案失败 3 项）
- Task 7: 规格符合 / 质量已批准（reviewer 用 opus，35 次工具调用，核对了上游 install_trust 源码语义、adapt 产物端口隔离、autosave 归属）
- Task 7: fix round 1/5 (1 Important + 4 Minor): ① 【重要】`install_trust:false` 在旧版 Caddy 上会**静默降级**（Go JSON 忽略未知字段）→ 沙箱 CA 装进宿主信任库，而 caddy run 的 stdout/stderr 被 DEVNULL 丢弃 → 自检无法区分"保护生效"与"保护被忽略"。Ruling: 真缺陷——本命令的卖点就是"不污染宿主"，必须可验证而非假定。修法：stderr 改存 tmp 文件 + 断言 Caddy 的 "trust store installation disabled" 消息（缺失即记**失败**检查）+ caddy version 下限 2.7 门禁 ② proc.kill() 后缺 wait() → 僵尸 ③ http.client.HTTPException 逃逸成 traceback ④ 第一项检查近乎恒真（尚无监听，一律 ECONNREFUSED 被当成功；同路径在第二项却算失败，不自洽）→ 改为显式断言"沙箱尚未服务"，且确保真正有信号的那项（注册后未注册域名仍拿不到证书）保留 ⑤ 集成测试在无 *.test 解析的机器上失败而非跳过 → 补 skip 守卫
- Task 7: minor (deferred, 交最终 review 分诊): ① 沙箱 https/http 监听在所有网卡（*:14443/*:14480）——修它要动生产渲染器 render_caddyfile 为测试专用目的，不划算，park ② 跳转检查只断言 https:// 前缀 ③ selftest.py 未复用 build_main_caddyfile，自检不覆盖生产的全局选项/站点装配（brief 同样如此）④ devproxy selftest 并非完全无副作用（build_context → paths.ensure_files 会创建 ~/.config/devproxy 并 touch sites.caddy/routes.caddy）⑤ 固定端口不可并发 ⑥ tests/test_integration.py 照抄 brief 留约 40 行死代码 ⑦ `_adapt` 整体覆盖 apps.pki 而非合并 ⑧ 未使用变量
- Task 7: 实现者实验曾污染宿主（覆写生产 autosave.json、往系统/NSS/Java 装了 2 个临时 CA 根），已全部清理；controller 事后独立核实：生产 CA 与服务完好、autosave.json 已恢复生产内容、NSS 数据无损（早前读到的 SEC_ERROR_BAD_DATABASE 是清理时 certutil 正在写库的瞬时锁，非损坏）
- Task 7: fix round 1/5 result: 5 addressed, 0 open（commit 726c4b3；scoped re-review 判定全部 ADDRESSED、无新破坏；reviewer 独立核对了版本地板 2.7.0 与上游 install_trust 引入版本吻合、日志常量与真实二进制逐字一致）
- Task 7: complete (commits 4d7f9ef..726c4b3, 1 fix round, review clean)
- Task 7: minor (deferred, 新增薄弱点，交最终 review 分诊): **新增的 fail-closed 分支（版本地板 `MIN_CADDY_VERSION`、日志佐证 `TRUST_DISABLED_LOG`、`_caddy_version` 解析）零自动化回归覆盖** —— 实现者验证降级路径用的是临时脚本未提交；后续有人改坏这些常量套件不报警。建议补一个不依赖真实 caddy 的单测（monkeypatch `subprocess.run` 返回假版本 + 假日志文件）
- Task 7: minor (deferred): test_integration.py 仍留 brief 带下来的未使用 import（shutil/ssl/subprocess/tempfile/time/unittest，ruff 报 F401；项目无 ruff 配置，非阻塞）
- re-review 的 out-of-scope 误报澄清：它把 `/usr/local/share/ca-certificates/caddy-local.crt` 与 NSS 的 "Caddy Local Authority" 条目当作"上一轮遗留的一次性根"建议清理 —— 实为**生产 CA 本身**（controller 已核对：与 ~/.local/share/caddy/.../root.crt 指纹与 serial 均一致）。**不得清理**，否则破坏 Chrome 信任
- Plan 1 全部 7 个 Task 完成（分支起点 5cfd722），进入整分支最终 review
- Plan 1 最终整分支评审（opus，54 次工具调用）：**Needs fixes** —— 0 Critical，6 Important。评审人逐条走查了 `RouteStore.mutate` 的全部失败路径，确认"Caddy 永不被本工具弄坏"这条底线成立（validate 失败/save_table 失败/atomic_write 失败/reload 失败/信号打断/进程被杀），并用只读探针复现了 corrupt routes.json 的 Rich traceback 与 Rich markup 吞字符集。150 passed + 18 subtests（未跑集成测试）
- Plan 1: Ruling: 最终评审的 6 项 Important **全部接受为必修**，一次性派发单个修复 agent（skill 要求 ONE fix wave，不按发现拆人）：
  - I-1 退出码 3 从未产生（`EXIT_PRECONDITION` 定义了但全仓零引用）——违反 spec §9.1 与计划验收项「up 在 Caddy 未运行时给出退出码 3」。修法：新增 `PreconditionError(UpError)`，CLI 先捕获它 → exit 3；`expose` 同样需要 Caddy 在跑 → 显式前置检查 → 3；`down`/`ls`/`config` 保持可在 Caddy 停止时工作（reload 失败仍是 1）
  - I-2 routes.json 损坏 → 裸 Rich traceback（评审人实测复现）。修法：`routes.py` 新增 `RouteTableError`（放 routes.py 而非 store.py，避免循环导入），`load_table` 把 JSONDecodeError/KeyError/ValueError/TypeError 包成它；CLI 各命令与 `main()` 兜底捕获
  - I-3 按裸 PID 杀进程组 → **PID 复用后 `devproxy down` 会杀掉无关进程组**。修法：注册时记录进程身份（Linux 读 /proc/<pid>/stat 第 22 字段 starttime，注意 comm 可能含空格须在最后一个 `)` 后切分；无 /proc 时回退 `ps -o lstart=`；都拿不到则存 None），`Route` 增 `proc_start` 字段；杀前比对，不一致或无法验证则**不杀**并提示
  - I-4 `_watch` 的"用户已 down 就不复活"检查在锁外 → TOCTOU，路由会被复活指向死端口。修法：把存在性判断移进 mutate 回调（同一把 flock 内）
  - I-5 selftest 的 fail-closed 路径（版本地板 2.7.0 / 日志佐证 / _caddy_version）零自动化覆盖 —— 评审分诊判定 must-fix-before-merge。修法：monkeypatch subprocess.run 造假版本 + 假日志文件，补 3 个单测
  - I-6 进入 Caddyfile 的值不校验不转义 —— `domain_suffix` 来自 config.toml 且完全未校验，可注入额外站点块且 validate 会通过；spec §10 L1 明写要覆盖"特殊字符转义"。修法（纵深防御）：load_config 校验 domain_suffix/dns_backend/端口类型/admin_addr；render_caddyfile 校验 domain/upstream 白名单
  - 附带修：Rich markup 吞掉 `[a-z0-9-]`（评审实测消息里字符集整个消失）；caddy.py 的 `OSError` 与 `JSONDecodeError` 逃逸 `(False,msg)`/`(code,body)` 契约
- Plan 1: 评审的 26 条 deferred minor 已逐条分诊：仅 I-5 判为 must-fix（已并入修复波），其余 can defer 或 not a real issue（含："on_signal 双 SystemExit 竞态"被判 not a real issue——Python 信号处理器只在主线程串行执行且 `stop.set()` 在 raise 之前；"ignore_unknown_options 静默吞选项"被判 not a real issue——评审实测 `up codeup --pot 8080 -- cmd` 被 click 拦下退出 2，未被静默）
- Plan 1: 跨计划记账（M-18）："未注册域名拿不到证书"这条不变量只在**无任何 `*.test` 通配块**时成立，而本机现役 Caddyfile 里正有 `*.test { tls { on_demand } }`（spec S2：二者共存会致握手整体失败）。Plan 3 的迁移会拆掉它，但 devproxy 工具本身**不检测"线上还残留通配块"**这个前提 → Plan 2/3 落地时须补检测/提示
- Plan 1: 修复波已派发（单个 opus agent，BASE 726c4b3）
- 环境清理（经用户批准后执行，非本次改动造成）：系统信任库 30 个 Caddy 根 + /tmp 下 9 个对应私钥 → 全部清除，只保留生产 CA。**过程中我的脚本误删了 NSS 里生产 CA 的条目**（昵称 "Caddy Local Authority" 不带 serial，被按 serial 匹配的逻辑误判），已用 certutil -A 重新装入并核对 serial=EF0F89AAD54544C28010F4E0CA6C3B5F 与生产 CA 一致。最终状态：系统信任库目录 2 个（均生产）、bundle 122 张含生产×2、Java 1 条、NSS 1 条、悬空链接 0、/tmp 私钥 0；生产 caddy active，https://localhost 与 https://nginx.local.yikuaigao.com 均 200



- Plan 1: 修复波 re-review 结果: 6 项 Important + 3 项杂项全部 ADDRESSED，无新 Critical/Important 破坏；re-viewer 独立复跑 214 passed + 18 subtests；I-4 的异常中止替代方案经源码级核实（store.py:72 回调无包裹、异常先于任何写/reload）确认真正关死 TOCTOU
- Plan 1: 修复波新增 out-of-scope 观察（全部 non-blocking，记入下计划）: ① `_valid_admin_addr` 对裸 IPv6 "::1" 误放行（漏拒非误拒，后果是干净退 3）② HOSTPORT_RE 不接受 [::1]:8080 形态 IPv6 upstream 与大写 host（工具自产恒为 127.0.0.1:port，从严取舍可接受）③ kill_pid_group 警告走裸 print 而非 err_console ④ validate_name 无长度上限，超长 name 到渲染层才以域名>253 被拒 ⑤ expose 在 Caddy 在跑但主 Caddyfile 未 setup 时 reload 失败退 1（Plan 1 既有行为，Plan 2 的 setup 落地后消解）
- Plan 1: **COMPLETE**（5cfd722..2457761，12 个实现/修复 commit + 最终修复波 3 commit；7 Task 全过任务级 review，4 轮任务级 fix，1 轮整分支 fix wave；150→214 tests）
- Plan 1+2+3 整分支最终评审（opus，143 次工具调用，46 commits/56 files/+14016−307）：**Needs fixes**。逐任务与逐计划评审看不出的跨计划问题
- 最终评审: Critical — `ca_trusted()` 用 `openssl verify -CApath` 按 **subject** 选锚点，信任库有多张同主题 Caddy 根时选错 → error 18 → **永久假阴性 → setup 永远退 1、doctor 恒红**；且工具自己的动作修不好它（再装一份同主题根只会更多）。修法：改为**指纹比对**（该证书是否逐字节存在于生效信任库），不依赖 openssl 链式查找
- 最终评审: Critical(环境) — 机器上残留 5 张陈旧 Caddy 根，与 ledger 记载"已清理"不符。controller 复核：我的清理记录（16:20）对当时属实，但**20:54 Task 15 事故那次运行**又产生了 4 张新的（实现者探针脚本用 `caddy run` + 临时 storage 触发 Caddy 自动装根）。controller 实测：`caddy validate` **不**污染信任库（6→6），但会在 storage 里生成 CA；`caddy run` 才会装 → 那 4 张是**探针产物**而非工具缺陷
- 最终评审: 认定的必修项 — I1 up/expose 首条路由后不提示 CA 未被信任｜I3 用户取消退 0（"什么都没装"被当成成功）｜I4 sites.caddy 已有内容时静默让个人站点失服（新单元不再 import 旧 Caddyfile）｜I5 迁移在二进制下载前就改写单元（下载失败则用户现役 caddy 被改坏）｜I7 遗留 devp 仍能 PATCH 现网、其 68 个测试占全量 11%｜spec §7 补 caddy_bin
- 最终评审: 驳回/不采纳 — ① 「spec §7 缺 caddy_bin」**不成立**：controller 复核 spec 第 447 行已有（Task 17 已补）② I6 的 DNS 机制**保留不改**（systemd-resolved drop-in 无法表达按后缀的 DNS 服务器，且这正是本机现役配置；改为**补披露** —— README 与 setup 的变更清单要写明 `DNS=` 是整机级、`FallbackDNS` 是整机 fallback 策略）
- 最终评审: Important 但非必修 — I2 http_port/https_port 是惰性键（spec §7 列了但主 Caddyfile 不写）｜I8 macOS 零真机验证｜I9 无 CI + 4 个计时敏感用例高负载会红
- 最终评审: Minor 16 条（临时校验文件不清理、atomic_write 无 fsync 且固定 .tmp 后缀、Linux 特权写仍 0600 root、NSS 昵称体系与机器上既有条目不一致、config 打印两个 storage 路径、doctor 的提权回退会弹密码、selftest 固定端口、detect_legacy 方向、unit_exec_argv 不认 systemd 引号、dnsmasq 合并边角、端口占用用 connect 探针、版本不符下载后不重启、迁移备份目标含真实系统路径、migrate 的死变量、macOS 里过时注释）
- 最终评审: 其"Deferred triage"中约 20 条判为 not a real issue（已实测：ignore_unknown_options 不会静默吞拼错选项、on_signal 无双 SystemExit 竞态、_route_live 已修为带引号 host 匹配）
- 最终评审: 测试诚实度 — 覆盖真行为的：路由事务与回滚、渲染字节级确定性、备份/还原、迁移的备份/拆分/校验、selftest 的隔离与不变式断言｜只覆盖桩行为的：FakeElevated.run 恒返回 (0,"")（"sudo 密码错/无权限"这条最可能在真机首跑触发的路径**零覆盖**）｜零覆盖的：macOS 全部真实行为、loginctl enable-linger 实际生效、update-ca-certificates 真实返回码、真实 setcap、openssl verify 在同主题多根下的语义（正是 C1 的漏网处）
- 最终评审: 端到端追踪发现 — **现网 Caddy 的 6 条路由**里，`codeup-api.test` / `codeup-web.test` / `mcp-gateway.test` 是旧 devp **运行时注册**的（不在遗留 Caddyfile 里）→ 迁移只读遗留 Caddyfile，**这 3 条不会被带走**，其中两条的后端服务**现在正在运行**（codeup-manager 的 `just devp` → scripts/devp_dev.sh，14h25m 的 uvicorn 与 vite）。新旧 CLI 形状一致（`devp up <name> -- CMD` / `devp down <name>`），切换是改名
- 环境清理（controller 执行，用户此前已批准同一范围）: 删掉 4 张 20:54 探针遗留的 Caddy 根（信任库 + NSS），保留生产 CA。复核：信任库目录 2 个（均生产）、NSS 1 条、bundle 122 张、**`openssl verify -CApath` 现在返回 OK** —— 证实假阴性根因就是同主题根歧义
