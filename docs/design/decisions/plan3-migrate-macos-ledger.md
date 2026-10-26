# SDD ledger — plan: docs/design/plans/2026-10-22-devproxy-migrate-and-macos.md

- 规格（权威）：`docs/design/specs/2026-10-22-devproxy-local-dev-env-tool-design.md`
- 前置：Plan 1（核心网关）与 Plan 2（基础设施自举）均已 COMPLETE（HEAD 4e4b89c，422 tests）
- 工作区：`.superpowers/sdd/2026-10-22-devproxy-migrate-and-macos/`
- 计划修正提交：（Dnsmasq 后端重启目标，Plan 2 期已修 c3bb4ac）

## Plan 2 修复波的残留裁决（Plan 3 开工前一次性裁定）

Plan 2 的最终评审 → 修复波（2 commits，368→422）→ scoped re-review：16 条 finding 全部 ADDRESSED，
但**修复波自身引入 4 项新问题**。按 SDD 规则，修复波只有一次复审机会，残留项在此裁定下传。

| # | 残留 | 裁定 | 落在哪 |
|---|---|---|---|
| N1 | `CaddyComponent.verify()` 硬编码探测 `https://localhost`，只有 `*.test` 站点的机器上**假失败**（setup 退 1，而机器其实正常）。规格 §6.2 写的是 curl localhost，但它同时假设 sites.caddy 含 localhost 站点（§8.1 迁移样例）——工具自建站点时该前提不成立 | **真实且承重**（正确配置的机器上 setup 会失败）。修法：对齐 CertComponent 的做法——优先探测 routes.json 首条路由域名；仅当确有 localhost 站点才探 localhost；无任何站点 → ok + 提示 | Plan 3 Task 15（改 caddy.py） |
| N2 | `DnsComponent.verify()` 的探针在 resolvectl 无答案时回退去问 `dig @127.0.0.1 -p 5353`（那是**用户级 dnsmasq**，天然有 address=/.test/127.0.0.1）→ resolved 路由没生效也报"已生效"，用 dnsmasq 的答案冒充系统解析器 | **真实**：I9 想证明的"解析确实生效"并未被证明。修法：平台层加"只走系统解析器"的查询，或 Dns 组件直接用 resolvectl 语义，不回退到 dnsmasq | Plan 3 Task 16（改 dns.py + platform） |
| N3 | `CertComponent.verify()` 的 HTTPS 探测与上游存活性耦合（上游进程已退出 → Caddy 返 502 → curl -f 判失败 → 报"握手失败"但真实原因是上游挂了）；且路由被 down 清空后回退 https://localhost，无站点却仍探测 → 假失败 | **真实**：证书信任本身没问题却报失败。修法：与 Caddy 组件一致——当前无任何站点时返回 ok + 提示；探测失败时区分"证书不可信"与"上游不可用" | Plan 3 Task 16（改 certs.py） |
| N4 | 无 `loginctl` 的宿主上 `linger_enabled()` 恒 False → 每次 setup 都计划 enable-linger（永不 no-op、每次要 sudo）且失败时 setup 硬退 1。在无 systemd 的容器/WSL 上整个 setup 不可用 | **真实**：把"loginctl 不可用"当作"不适用"跳过，或把 enable-linger 失败降级为警告而非组件失败 | Plan 3 Task 15（改 platform/linux.py） |
| N5 | `check()` 与 `plan()` 对版本判据不一致：check 放行"版本读不出来"，plan 判为"需下载" → 同一行既写"已就绪"又写"下载"，每次 setup 重复下载 | 次要但真实：check/plan 共用同一 `need_binary` 判据即可 | Plan 3 Task 15 |
| N6 | 单元判据只看 `run --config <caddyfile>`，不看 ExecStart 里的二进制路径 → 改了 config 的 caddy_bin 后已装单元仍指旧二进制，不会重装 | 次要 | Plan 3 Task 15 |

**Ruling（对以上全部）:** 这些残留不新开修复波（规则：修复波只有一次复审），全部随 Plan 3 相应任务一并修掉。
Plan 3 Task 15/16 的派发必须携带对应条目；若 Plan 3 结束时仍有未修残留，在最终评审里按"承重"处理。

## 预检冲突扫描（Task 14 派发前）

### 跨计划接口差距（本次扫描的主要发现）

Plan 3 的 `docs/design/plans/2026-10-22-devproxy-migrate-and-macos.md` 写于 Plan 1/2 之前，
它描述的 `Platform` 协议是 **11 个方法**。当前代码库的协议已累积到 **17 个方法**：

```
run / binary_version / service_active / install_service / enable_service / restart_service
service_unit_path / tail_service_log / ca_trusted / dns_route_installed            ← Plan 3 计划里已有
enable_linger / linger_enabled / resolved_active / dns_query / http_ok / nss_trusted  ← Plan 3 计划里没有
```

> **口径更正（2026-10-23，真机首跑时发现）**：本处原写"17 个方法"，**实际是 18 个** ——
> 上面的清单漏了 `privilege`（提权会话）与 `privilege_read`（特权只读）两个，
> 它们在 `base.py` 的 `Platform` 协议里，但没被列进这段枚举。
> 下面的 Ruling 要求的"全部 17 个"因此**实际是全部 18 个**；
> 当年 `macos.py` 确实把 18 个都实现了（含 `privilege` / `privilege_read`），
> 所以**这条 Ruling 的执行结果没有偏差**，只是当时的计数口径写错了。
> 同批 fix wave 的实现者也独立发现了这一点（`base.py` 未被改动）。

**Ruling:** Plan 3 Task 16 的 `macos.py` 必须实现**全部 17 个**方法，缺一个都会让组件在 macOS 上崩：
- `enable_linger()` → macOS 无 systemd-linger，返回 `(0, "不适用")`
- `linger_enabled()` → 返回 True（从而不会计划任何 linger 动作）
- `resolved_active()` → 返回 False（macOS 无 systemd-resolved）
- `dns_query(name)` → 用 macOS 的解析途径（`dscacheutil`/`dig`），有超时
- `http_ok(url)` → 复用 curl（与 Linux 同形）
- `nss_trusted(ca)` → macOS 无 NSS 概念，返回 True（这样证书组件不会去装 NSS 证书）
- `install_service` → 改为 keyword-only（`exec_start`/`exec_reload`/`exec_stop`/`env`），由平台渲染 plist
- `dns_route_installed(backend, suffix)` → 加上 suffix 参数（macOS 要查 `/etc/resolver/<suffix>`）

### Task 间共享文件/接口

| 产出方 | 消费方 | 接口 | 结论 |
|---|---|---|---|
| T14 | T15 | `BackupManager`/`Backup`/`load_backup` | 一致（T15 消费 T14 的备份） |
| T15 | T16 | `Platform.install_service` 新签名、`dns_route_installed(backend, suffix)` | 一致（T15 改协议 + Linux + Fake + 组件调用点 + 测试） |
| T16 | T17 | macOS 平台、组件的平台分支 | 一致 |
| Plan 2 残留 | T15/T16 | N1/N5/N6 → T15；N2/N3 → T16 | 见上表 |

### 需在 T15 同步更新的既有调用点（计划已列，但需逐一确认）

`Platform.install_service` 由 2 参改 keyword-only 后，受影响的是：`platform/linux.py`、`platform/fake.py`、
`components/caddy.py`（install-service 动作）、`components/dnsmasq.py`（install-service 动作）、
`platform/macos.py`（新增）、以及 Plan 2 的 `tests/test_platform_linux.py`、`tests/test_component_caddy.py`、
`tests/test_component_dnsmasq.py`。`dns_route_installed` 加 suffix 后，受影响的是 `platform/linux.py`、
`platform/fake.py`、`components/dns.py`、`platform/macos.py` 与相关测试。

## 进度
- Task 14: fix round 1/5 (1 Important + 3 Minor): ① 【重要】非普通文件目标被静默记为 existed=False（目录 / 符号链接 / **悬空符号链接**）→ manifest 写下假事实"该路径原本不存在"，回滚阶段才炸（IsADirectoryError；悬空链接更糟：restore 的 unlink 会删掉一个原本存在的链接）。Task 15 当前目标是普通文件故不触发，但"备份阶段静默 + 回滚阶段延迟爆炸"是典型陷阱。修法：create() 里大声拒绝非普通文件（符号链接即使断链也要拒，is_symlink() 对断链仍为 True）
  ② sha256 在 copy2 之后对**源文件**求值 → TOCTOU，manifest 记的哈希可能与备份副本不符，失去完整性凭据作用 → 改为对副本求值
  ③ restore() 中途失败会留下半还原状态且丢失"已还原列表" → Task 15 的 CLI 是 `touched = do_rollback(...)` 后打印"已回滚 N 个文件"，异常一抛用户完全看不到已还原了哪些 → 逐条 try/except 收集失败，已还原清单不得丢失
  ④ 复制中途失败留下无 manifest 的孤儿备份目录（备份只增不删 → 永久留存，像备份但 load_backup 抛 FileNotFoundError）→ 失败时删除半成品或写 INCOMPLETE 标记
- Task 14: minor (deferred): ① 符号链接目标被 copy2 解引用（备份副本是普通文件、还原会替换掉原链接）② created_at 与目录名来自两次独立 strftime（跨秒可能差 1 秒，仅观感）③ MANIFEST.txt 的"备份文件"只给裸文件名、清单被单独复制走就丢定位信息 ④ 测试有未使用的 import ⑤ 测试未覆盖目录目标与 restore 幂等两个分支
- Task 14: Ruling（跨任务，reviewer 提示）: **Task 15 的 migrate() 必须把 `paths.sites_caddy` 列入备份目标** —— paths.ensure_files() 会先建空的 sites.caddy，迁移再把用户个人站点写进去；若不备份，回滚后 sites.caddy 仍留着工具写入的个人站点内容，正是全局约束点名的"工具造的残留"。随 Task 15 派发携带
- Task 14: fix round 1/5 result: 4 addressed, 0 open（commit ea38928；复审判定 symlink 拒绝的判据顺序正确（is_symlink 先判，断链亦拒）、manifest 写在 try 外的窗口属 Minor、restore 的"原本不存在→删除"路径未被 symlink 拒绝破坏（os.unlink 删链接本身不跟随））
- Task 14: complete (commits 4e4b89c..ea38928, 1 fix round, review clean)
- Task 14: minor (deferred): ① manifest 写入（manifest.json / MANIFEST.txt）在失败清理的 try 之外 → 写清单失败会留下既无 manifest 也无 INCOMPLETE 标记的孤儿目录（同 finding 4 的类，窗口窄）② restore 的逐条 except 只捕 OSError，非 OSError（如路径含 NUL 的 ValueError）仍会逃出并丢掉已还原清单 ③ restore 对 existed=True 用 copy2 会跟随 e.original 处的符号链接写入其目标（备份时保证是普通文件，但迁移期间可能变成链接）④ create 拒绝任何符号链接（含指向普通文件的）→ Task 15 必须把它作为清晰的中文提示报出，不能裸 traceback
- Task 14: 契约变更（Task 15 必须适配）: `restore()` 全部成功时返回路径列表；部分失败时抛 `BackupRestoreError`（携带 `.restored` 与 `.failures`）。Task 15 的 do_rollback() 必须 `try/except BackupRestoreError as e:` 并使用 e.restored / e.failures，否则已还原清单在 CLI 层拿不到
- Task 15: dispatched (implementer running, BASE ea38928)
- Task 15: 承接项 A–G 全部 done（reviewer 逐项给出行号）：A 平台 install_service keyword-only 化 + dns_route_installed 加 suffix（全部调用点与测试同步）｜B sites_caddy 进备份目标（回滚后回到空而非残留个人站点）｜C do_rollback 捕获 BackupRestoreError 用 .restored/.failures、符号链接给中文提示｜D N1 verify 探首条路由域名｜E N4 loginctl 缺失视为不适用｜F N5 check/plan 共用 need_binary｜G N6 单元判据核对二进制路径
- Task 15: fix round 1/5 (1 Critical + 4 Important + 3 附带):
  ① 【严重·事故的结构性成因未消除】conftest 的 autouse 只 patch `devproxy.cli._platform`，多条缝仍开着：直接 new LinuxPlatform() 调 install_service → 写真实 ~/.config/systemd/user/*.service；enable/restart_service → 真实 systemctl --user；privilege() → 真实 sudo -v 并可 sudo 写 /etc；build_plan/apply_plan 传真实组件 → 同上 + 真实 sudo setcap；default_paths()+ensure_files() → 在真实家目录建空文件；DNSMASQ_CONF/NSS_DIR 解析到真实路径（CertComponent.apply 的 nss.mkdir 还能凭空建出 ~/.pki/nssdb）。修法（评审给的两层，缺一不可）：**平台层护栏**（linux.py 里 install_service/enable_service/restart_service/enable_linger/privilege() 开头 `_assert_may_mutate()`，检测 PYTEST_CURRENT_TEST 或 DEVPROXY_NO_MUTATION → 抛 RuntimeError；只读方法不拦）+ **autouse 家目录重定向夹具**（HOME/XDG_* 指向 tmp，Path.home() 与 os.path.expanduser 都认）——只做 HOME 重定向挡不住 systemctl/sudo 绝对路径，只做平台护栏挡不住 default_paths() 建文件。另加正面测试，并保留已验证有效的 test_cli_never_builds_the_real_platform（去掉隔离即变红）
  ② 【重要】DNS drop-in 的备份目标取错 backend —— cli.py 用 config.dns_backend，而组件流程用解析出的 backend；用户显式 --dns-backend system 时组件写 /etc/dnsmasq.d/、备份的却是 /etc/systemd/resolved.conf.d/ → **真正被改写的文件不在备份里，回滚删不掉**
  ③ 【重要】迁移改写单元前不校验生成的 Caddyfile —— spec §8.2 第 6 步要求 caddy validate；个人站点原样搬进 sites.caddy，若有语法冲突/重复站点块，_restart_caddy 只记一条"未能重启"就继续 → 用户机器上 caddy 起不来。修法：_install_caddy_unit 之前跑 caddy validate，失败即抛（此时未改单元，可安全中止）
  ④ 【重要】遗留全局块的 storage 路径被丢弃 → CA 连续性无保证（spec §8.2/D13 要求路径不变）。本机两者恰好相同故无事，但只要用户遗留 storage 与 config 不同（或设了 DEVPROXY_DATA_DIR）→ Caddy 重生成 CA → 浏览器信任断裂、个人站点的 tls internal 证书全失效。修法：解析遗留 storage，不一致时不得静默忽略
  ⑤ 【重要】"还原不完整"的错误信息里没有备份目录 —— 机器半还原时用户最需要的恰恰是"备份在哪、怎么手动还原"（成功路径打印了，失败路径没有）
  附带 M6 caddy-env.json 不在备份目标（回滚后残留，后续 setup 继续注入遗留 MINIKUBE_IP）｜M7 丢弃 .suffix 块时未列出地址（用户无法判断自己手写的 *.test 块是否被丢）｜M8 失败路径不打印 notes
- Task 15: minor (deferred): 报告称改了 tests/test_component_dnsmasq.py 与 test_component_dns.py 实际没改（这两个文件本就不调那些接口）｜render_systemd_unit 的 service_type 参数无法通过 install_service 触达，Type= 只能靠程序名启发式；旧 render_unit 的 Documentation= 字段重渲染后丢失｜遗留 Caddyfile 顶层非块行（如 import xxx）被抄进 sites.caddy，import 的相对路径基准变了（语义变）｜迁移备份 /etc drop-in 时普通用户读不到 → 走 privilege() → 真实 sudo -v 提示，且发生在用户尚未被告知需要提权的阶段
- Task 15: fix round 1/5 result: C1/I2/I3/I4/I5/M6/M7/M8 全部 ADDRESSED，无新 Critical/Important（复审自行跑了套件 505 passed + 18、并用真实 caddy v2.11.7 在 /tmp 隔离配置上实测了 caddy validate 不绑 admin 端口、缺 import 退 1、重复站点地址报 ambiguous site definition 退 1）
- Task 15: minor (deferred, re-review 新增 5 条 + 2 条重点): ① 【重点】留档的 storage 对 config.toml 是**静默覆盖**（cli.py:112-114），用户改 config.toml 想换 storage 会被无声忽略；唯一出口（删 state/caddy-storage.json）无 CLI/README 提示 → 随 Task 16 补提示 + Task 17 写文档 ② 【重点】config.caddy_bin_path 不存在时**跳过** caddy validate（"换单元前必校验"在该情形形同虚设），遗留单元 ExecStart 里的 caddy 二进制未作 fallback → 随 Task 16 修 ③ 测试 test_cli.py:421 故意不打桩 default_dns_route，备份目标落到真实 /etc/dnsmasq.d/，在已用 system 后端装过 devproxy 的机器上会因 root 拥有而 PermissionError → 用例红（可移植性）④ 护栏抛的 RuntimeError 既非 MigrationError 也非 OSError，在备份/还原路径触发会绕过 _restore_after_failure 的两个 except → 以 traceback 冒到 CLI（仅测试期可达）⑤ auto 后端实时探测且探测两次（备份期/组件期），其间系统状态翻转会重新引入 I2 的错配
- Task 15: 复审引用的既有行为（不在本轮范围，留给最终 review 分诊）: _write_sites_caddy 在 sites.caddy 已有内容时，个人站点块**只留在旧 Caddyfile 里**，而新单元不再 import 旧 Caddyfile → 这些站点迁移后不再被服务（提示只说"仍在旧 Caddyfile 里"）；旧 Caddyfile 全局块除 storage 外的其他选项（email、admin off 等）被整体丢弃
- Task 15: complete (commits ea38928..e515322, 1 fix round, review clean)
- Task 16: dispatched (implementer running, BASE e515322)
- Task 16: 协议覆盖 17/17 全部实现且 macOS 语义正确、MacOSElevated 4/4、Linux 侧 platform/linux.py 零改动（reviewer 从 diff 文件清单核实，非采信）、组件按 platform.name 分派（无 sys.platform 硬编码）、护栏到位、测试全 hermetic
- Task 16: fix round 1/5 (1 Critical + 3 Important + 4 附带):
  ① 【严重】system scope 的 plist 落地为 0600 root（mkstemp 0600 + sudo cp 沿用源 mode；brief 参考实现里的 chown root:wheel + chmod 644 被丢掉）→ 普通用户读不到自己的 plist → `_unit_current()` 读失败被 except OSError 吞掉 → 判 False → **check() 恒 not ok → doctor 恒红；plan() 恒含 install-service → 每次 setup 重新 bootstrap 并再要一次密码 → setup 幂等承诺在 macOS 上不成立**。测试抓不到是因为 _MacPlatform 假实现是以用户身份写 plist（0644 可读），与真实路径模式不一致。修法：system scope 改用 sudo install -m 644（或 cp 后 chmod 644 + chown root:wheel）；且 `_unit_current()` 读不到时不得直接判"不是当前"（PermissionError 应走提权会话读，只有内容真不匹配才算过期）
  ② 【重要】system scope 分支不建日志目录（只有 user scope 建）→ 组件顺序 Caddy 第一，此时 ~/.local/state/devproxy/logs/ 还不存在 → launchd 打不开 StandardOutPath → job 无法 spawn；第二次 setup 因 dnsmasq 组件建了目录才"自愈"，但首跑已失败。修法：两个 scope 都建；并把平台自实现的 _state_dir() 与一直没人用的 Paths.log_dir 统一
  ③ 【重要】service_active 的语义是"已加载"而非"正在运行"（launchctl print 对已注册但已崩溃/退出的 job 也返回 0，Linux 侧 systemctl is-active 能区分 active/failed）→ macOS 上 doctor 把"caddy 崩了"报成"服务已运行"，verify 第一道闸门形同虚设。修法：解析 launchctl print 输出里的 state = running，不确定时 fail closed，并把"已加载但已退出"列入验证清单
  ④ 【重要】root 跑 caddy 会在用户家目录留下 root 属主文件（storage/CA/日志）→ 用户无法轮转删除。属既定取舍的副作用，应进验证清单并在 README 补一句
  附带：dnsmasq 组件的 hint 在 macOS 仍是 systemctl（caddy 已有平台感知 _service_hint）｜MacOSElevated() 可被直接构造绕过护栏（会跑真 sudo -v，通向系统钥匙串）｜platform.run() 不受护栏约束（通用变更入口）｜_dnsmasq_binary() 用 which 优先可能与 Homebrew 版本脱节｜_unit_current() 改 token 匹配后判据变松（"run" 与路径不必相邻）｜Homebrew 自带 dnsmasq.conf 被 --conf-dir 绕过、用户上游被静默忽略｜MacOSPlatform 重复实现 state 目录解析
- Task 16: 验证清单缺口（reviewer 指出实现者的 17 项清单"用了却没列"，直接变成 Task 17 的清单）：① devproxy 自己能否读它装的 plist（权限，而不只是"launchd 能否 bootstrap"）② system scope 的日志目录由谁建、首启前是否存在 ③ launchctl print 成功 ≠ job 在运行（"已加载但已退出"）④ root 跑 caddy 在用户家目录留下的 root 属主文件的可读性与可清理性 ⑤ Homebrew 自带 dnsmasq 配置被绕过、以及系统解析器是否真按每域文件分流
- Task 16: fix round 1/5 result: Critical 1 / Important 2 / Important 3 / 4 项便宜项 / 清单扩展 全部 ADDRESSED，每项都有本轮新增的回归测试；新问题仅一处 Minor
- Task 16: complete (commits 4327b27..ceb3e55, 1 fix round, review clean)
- Task 16: minor (deferred, 交最终 review 分诊): ① `_unit_current()` 的判据收紧（子串 → 严格 argv 前缀比对）是 Linux/macOS 共用代码 → **Linux 侧"单元是否过期"的判据实际变严了**：手工改过的 Linux 单元（ExecStart 加了前置参数/参数顺序不同/带引号路径）以前算"当前"、现在算"过期" → doctor 多报红、setup 多一次重装。方向安全（正是评审要的），但实现者报告"Linux 零行为变化"的说法不准确，已要求更正 ② doctor 的提权回退在"单元读不到"时会 `sudo -v` → doctor 变成可能弹密码提示的非纯只读命令；建议改 `sudo -n` 之类非交互读取 ③ `unit_exec_argv` 的 systemd 分支不认 systemd 引号语法（只影响手写单元，安全方向）④ `migrate.detect_legacy()` 的方向与 caddy 相反：读不到遗留单元时按"没有遗留环境"处理 → 极端情况下迁移会接管用户手工环境，该残留风险未进未验证清单 ⑤ Task 17 的清单模板尚未含新增的第 18-22 项
- Task 17: dispatched (implementer running, BASE ceb3e55)
- Task 17: fix round 1/5 (3 Important + 5 附带):
  ① 【重要·文档不实承诺】README 两处声称 `devproxy doctor` 会解析 foo.<后缀> / 检查系统解析器 —— **它不会**：doctor 只跑 check()（静态检查）；真正的解析在 verify() 里，而 verify 只在 setup 的 apply 阶段被调用（**no-op 路径根本不跑**）。reviewer 用桩化的 resolvectl/dig 放上 PATH 实测：doctor 零调用。修法：更正主语，说明"doctor 只做静态检查、解析是否生效由 setup 变更后的自检阶段完成"
  ② 【重要】`uv tool install devproxy` 今天必然失败（PyPI 404，从未发布），而它是 README 的**首要**安装指令 → 改为以可用的本地安装（uv tool install -e . / pipx install .）为主，PyPI 那行明确标注"发布后可用"
  ③ 【重要】清单 D5 的命令永远跑不通：`python3 -m http.server` 忽略 $PORT（仍绑 8000）→ up 等 60s 超时、不注册路由、后续 curl 无从命中；**同一个坏例子还写在代码的提示里**（cli.py:576 "试试: devproxy up demo -- python3 -m http.server"）→ 清单与代码提示都改成 sh -c 'python3 -m http.server $PORT'
  附带：① C1 的 chmod 000 挡不住 root，到不了它声称要测的分支 → 改述为"sudo 不可用/无权限" ② Justfile 的理由只对一半（just 确实丢子命令的引号，但 -- 分隔符本身在 variadic recipe 下是会透传的）→ 结论对、措辞改 ③ README 措辞：CA 是"第一条路由注册后"才生成（expose 也会触发）；storage 留档的粘性作用于**每个**命令（走 build_context），不只后续 setup ④ 退出码说明：config.toml 里非法 domain_suffix 退 1、同样的值用 --domain-suffix 传退 2 → 补一行 ⑤ 仓库根的遗留 Caddyfile 加头注（措辞要准确：迁移真正跑之前它仍是现役服务在用的文件，不能声称已迁移）
- Task 17: 非本任务决定（留给用户）: 仓库无 LICENSE 文件而 pyproject 声明 license = "MIT" —— 选什么许可证是项目所有者的事，已要求实现者只记录不擅自添加
- Task 17: 复审另行标记的计划收尾项（超出本任务）: 遗留根 Caddyfile 仍含个人绝对路径 /home/yewenbin/.local/share/caddy，真机迁移时会被原样抄进 sites.caddy —— 真机跑迁移前需处理
