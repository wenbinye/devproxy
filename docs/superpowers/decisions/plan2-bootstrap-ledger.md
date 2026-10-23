# SDD ledger — plan: docs/superpowers/plans/2026-10-22-devproxy-bootstrap.md

- 规格（权威）：`docs/superpowers/specs/2026-10-22-devproxy-local-dev-env-tool-design.md`
- 前置：Plan 1（核心网关）已 COMPLETE（分支推进到 2457761，214 tests；修复波后 HEAD 为 2457761）
- 工作区：`.superpowers/sdd/2026-10-22-devproxy-bootstrap/`
- 计划修正提交：c3bb4ac（DnsComponent system 后端重启目标修正）

## 预检冲突扫描（Task 8 派发前）

| 产出方 | 消费方 | 接口 | 结论 |
|---|---|---|---|
| Plan 1 | T8–T11 | `Paths`/`Config`/`build_main_caddyfile`/cli 的 `build_context` | 一致 |
| T8 | T9 | `Platform.install_service(name, unit)`、`privilege()`、`binary_version`、`service_active`、`enable_service` | 一致（旧 2 参签名；Plan 3 Task 15 改 keyword-only 并同步测试，计划已记） |
| T8 | T10 | `Platform.dns_port`、`dns_route_installed` | 一致 |
| T8 | T11 | `Status`/`Action`/`FakePlatform` | 一致 |
| T9/T10 | T11 的 cli `_components()` | 四个组件构造签名 | 一致 |
| T11 内部 | setup.py ↔ doctor.py | `ComponentReport`（doctor 复用 setup 的 dataclass） | 一致（含 component 字段的修正版） |

### 各 Task 自洽性

| Task | 用例数 | 结论 |
|---|---|---|
| T8 | 9 | 一致（`test_linux_*` 用假 caddy 脚本，不碰真机） |
| T9 | 10 | 一致（`download_caddy` 含 sha256 校验；单测不打真网络，常量有锁死测试） |
| T10 | 10 | 一致（预检修复后 system 后端重启 dnsmasq） |
| T11 | 10 | 一致（`build_plan` 已是含 component 字段的版本） |

### 扫描发现与裁决

| # | 发现 | 裁决 | 代价（若错） |
|---|---|---|---|
| F1 | `DnsComponent.plan()` 对 system 后端也返回 `systemctl restart systemd-resolved` | 计划缺陷，已修（c3bb4ac）：system 后端改重启 dnsmasq | 极低 |
| F2 | T8–T11 的实现含真实系统调用（systemctl/setcap/update-ca-certificates），但测试全部走 FakePlatform；真机端到端留到"Plan 2 完成标准" | **Ruling: 本会话不自动执行任何改宿主系统的动作**（装 unit、写 /etc、setcap、update-ca-certificates、真机 setup）。代码与测试完成后向用户呈报完成标准清单，由用户决定何时真机验证 | 若 FakePlatform 语义与真实有偏差，真机首跑暴露 |
| F3 | Plan 1 遗留 M-18：现役 Caddyfile 有 `*.test { on_demand }` 通配块，与精确块方案冲突（spec S2：共存会致握手整体失败） | **Ruling: 记账** —— Plan 2 的 setup 生成工具自己的 Caddyfile，不碰现役那份；"真机 `devproxy up` 验证"必须等 Plan 3 迁移完成，Plan 2 完成标准里该项标注"待 Plan 3" | 用户可能疑惑 Plan 2 后 `.test` 域名为何仍不通 |

扫描结论：无未裁决冲突，可派发 Task 8。

## 进度
- Task 8: complete→fix (commits c3bb4ac..dc7f2fa, review: 规格✅ 质量Approved, 3 Important 全部 plan-mandated)
- Task 8: Ruling: 3 项 Important 全部真缺陷且 Task 10/11 直接建在其上 → 现在修，不留到 Plan 3 重构：
  ① `LinuxElevated.write` 把临时文件建在**目标目录**内（`path.with_name(...)`），非特权用户对 /etc/dnsmasq.d、/etc/systemd/resolved.conf.d、/usr/local/share/ca-certificates 无写权 → `sudo cp` 跑之前就 PermissionError；且无 try/finally，泄漏 .tmp。修法：tempfile.mkstemp（用户可写处）+ sudo cp + finally 清理
  ② `tail_service_log` 用 capture_output=True 跑 `journalctl -f` → 永不返回且什么都不显示，`devproxy logs` 会静默挂死。修法：不捕获、继承 stdio、Ctrl-C 自然传播
  ③ daemon-reload/enable --now/restart 全部丢弃返回码 → 畸形 unit 写盘后静默失败。修法：非零即抛 RuntimeError 带命令与 stderr
  附带：FakeElevated.copy 不记录 written/commands（与 write 不一致，会让后续测试因不一致的原因失败）；read 把权限拒绝也映射成 FileNotFoundError
- Task 8: fix round 1/5 已派发（测试全部 monkeypatch `_run`/`subprocess.run`，不碰真机）
- Task 8: minor (deferred): ① 未使用 import（linux.py 的 os/shutil、test 的 os/subprocess/pytest/Path，brief 逐字）② `_authenticated` 只写不读的死状态 ③ binary_version 硬编码 `version` 子参数（对 `--version` 型二进制返回空）④ (stdout or stderr) 使"成功但 stderr 有警告行"时解析错行
- Task 8: fix round 1/5 result: 5 addressed, 0 open（commit ef74c15；scoped re-review 5/5 ADDRESSED，无新 Critical/Important）
- Task 8: complete (commits c3bb4ac..ef74c15, 1 fix round, review clean)
- Task 8: minor (deferred, re-review 新增): ① `read` 的 "No such file" 判定依赖英文 locale（zh_CN 下缺失文件误判为 PermissionError；修法 `_run` 传 LC_ALL=C 或 `sudo test -e` 二次确认；当前 read 无消费方）② `sudo cp` 使特权目标变 root:root 0600（mkstemp 的 mode 被沿用；root 守护进程读取无碍，可加 cp --mode=0644）③ `write` 现在要求目标父目录已存在（缺失时报错够响但归类偏）④ journalctl 不存在时裸 FileNotFoundError ⑤ install_service 在 daemon-reload 失败前已写盘 unit（半成品，重跑覆盖）⑥ FakeElevated.write 仍不记录 commands（与 copy 轻微不对称）
- Task 9: dispatched (implementer running, BASE ef74c15)
- Task 9: fix round 1/5 (1 Critical + 1 Important):
  ① 【严重】实现者偏离 #3 的前提被 reviewer 当场证伪（task-11-brief 与 plan 全文零处引用 download_caddy，计划内唯一调用点就是 Task 9 自己的 apply 草稿）→ download_caddy 成死代码，新机 setup 静默跳过下载 → setcap 打在不存在的二进制上；且"二进制版本不符+服务 active"路径下固定版本替换承诺失效。Ruling: 规格为准（§6.2 install() 明确含下载）→ 恢复 apply 内下载，经构造参数注入 downloader 保持测试离线
  ② 【重要】setcap 只在"需下载"时入 plan → "上次下载成功但 setcap 失败"场景无自愈（重跑无 setcap 动作，服务装好却绑不了 80/443，check/verify/doctor 均不可见）。修法：setcap 条件改为"组件未完全就绪"（二进制缺失 / 版本不符 / 服务未 active 任一）
- Task 9: Ruling（偏离裁决）: #1（check 去 Caddyfile 检查）、#2（plan 条件化）、#4（verify 加二进制检查）三处偏离经 reviewer 核实为 brief 自相矛盾的正确裁决（paths.ensure_files 不建 Caddyfile、plan()==[] 测试、FakePlatform.enable/restart 都 active.add）；#3 前提错误，已回滚
- Task 9: minor (deferred): ① plan() 靠中文子串匹配 status.detail（与 check 文案隐式耦合，brief 原样）② FakePlatform.binary_version 硬编码 v2.11.7 → 版本闸门与"版本不符"分支零覆盖 ③ 测试文件 4 个未使用 import ④ download_caddy 的 version 参数可传非默认值但 sha 表固定 2.11.7，非默认版本必报误导性"校验失败"⑤ 主 Caddyfile 被手删时 check ok/plan 空永不重建（边缘）
- Task 9: fix round 1/5 result: 2 addressed, 0 open（commit 2299e70；scoped re-review 2/2 ADDRESSED，无新破坏；复审人自行把 download_caddy 换成"调用即抛"桩重跑焦点测试仍 14 passed，独立证实离线；全套 243+18 复现一致）
- Task 9: complete (commits ef74c15..2299e70, 1 fix round, review clean)
- Task 9: minor (deferred, re-review 新增残留): ① caddy.py:161 docstring 仍写"setcap 与下载绑定"（已过期）② 版本不符+服务 active 时会重下载替换二进制但**无 restart 动作**，运行中的 caddy 仍以旧版本服务 ③ apply 的 setcap 分支忽略返回码（失败当场不可见，靠下一轮自愈）④ check 不再检查主 Caddyfile 是否存在，而 plan 的"生成主 Caddyfile"挂在 `not service_active` 条件上 → 主 Caddyfile 被手删而服务在跑时 setup 不修复、doctor 报已就绪 ⑤ binary_version 在二进制存在但不可执行时返回 "" → check 跳过版本判断 → 不会重新下载
- Task 10: dispatched (implementer running, BASE 2299e70)
- Task 10: 规格符合 / 质量 Approved（27 新增用例，270+18 全量；reviewer 独立复算 merge 的纯/增量/幂等语义，并把 HOME 指向临时目录重跑 → 27 passed 且临时 HOME 零文件创建，交叉验证"未碰真实系统"属实）
- Task 10: fix round 1/5 (3 Important + 3 附带):
  ① 【重要·生产隐患】unit 用 Type=simple 且缺 --keep-in-foreground → dnsmasq 默认 daemonize → systemd 认定主进程已退出 → unit 变 inactive → check() 永远报"未运行"、doctor 永久报错。已实证：现役 unit 用 Type=forking + PIDFile（可用），dnsmasq 2.92 支持 -k。修法：Type=simple + --keep-in-foreground，去掉 --pid-file（同时消除对 ~/.cache 存在的依赖）
  ② 【重要·生产隐患】全新机器无既有 dnsmasq.conf → 生成的配置缺 no-resolv + server= → dnsmasq 以上游 127.0.0.53（resolved stub，已实证本机 /etc/resolv.conf 即如此）→ 而 resolved drop-in 又把全局 DNS 指向 127.0.0.1:5353 → **resolved↔dnsmasq 死循环、全机解析挂死**。本机幸免仅因既有配置已含 no-resolv + server=。修法：merge 时若无任何 server= 行则补 no-resolv + 默认上游；用户已有 server= 则不干预
  ③ 【重要】plan() 恒非空 → setup 永不是 no-op → 第二次 setup 仍要 sudo 密码并**重启系统解析器**（全网 DNS 抖动），且违反 spec §6.1 的"所有组件已就绪"与 Plan 2 完成标准第 2 条。修法：按 CaddyComponent 先例改为状态驱动；dnsmasq 比对合并后内容与现文件；dns 因 /etc 不可非特权读，改用状态目录 ~/.local/state/devproxy/dns-route.json 记录上次写入内容哈希
  附带：dns.py 无条件 privilege() → no-op 运行仍触发 sudo -v（与③的目的一击而破）→ 改为仅当存在特权动作时取会话；dns.py 的 write 动作无视 action.privileged 一律走提权写 → 改为按标志；DNSMASQ_CONF 死常量（~ 未展开）与组件内 Path.home() 路径定义并存 → 统一
- Task 10: minor (deferred): ① `install-service` 伪命令不校验服务名（与 caddy.py 同款先例）② check() 未覆盖"监听 5353"与配置内容正确性（doctor 对陈旧/被改配置盲）③ test_auto_backend_prefers_resolved 依赖宿主存在 /etc/systemd/resolved.conf.d，精简容器会假失败（已另加确定性版本，建议改 skipif）④ system 后端的 apply 路径零覆盖 ⑤ merge 把 CRLF 归一为 LF；`port=5300 # 注释` 行的注释在替换时丢失
- Task 10: fix round 1/5 result: A/B/C + 3 minor 全部 ADDRESSED（reviewer 另做了独立实证：dnsmasq 2.92 前台模式根本不写 pid 文件，印证去掉 --pid-file 的正确性）
- Task 10: Ruling: **我 Q1 分析的一半被 reviewer 用更强证据推翻** —— dnsmasq 手册明写 `server=` 不会抑制 /etc/resolv.conf 上游（只有 --no-resolv 会），reviewer 本机实测 `using nameserver 127.0.0.53#53` 证实；且我对 **system 后端会成环**的推论是错的（工具 dnsmasq 恒在 5353，system 后端的 drop-in 不含 port=，:53 仍属系统 dnsmasq → 不成环）。以 reviewer 结论为准
- Task 10: fix round 2/5 (1 Important + 3 Minor + 1 安全):
  ① 【重要·机器级 DNS 故障】"有 server= 行" 的判据用 startswith("server=") → **误匹配域名限定的 `server=/corp/10.0.0.1`**（它只服务该域，非全局上游）。此类配置被判"有上游"→ 不加 no-resolv → 一般域名查询走 127.0.0.53 → resolved → 工具 drop-in → dnsmasq → 回环 → **全机一般域名解析失败**（非仅 .test），且由工具自己的 drop-in 造成。修法（reviewer 给的最小规则）：区分全局/域名限定上游；**恒保 no-resolv**（除非用户显式 resolv-file=，二者语义互斥）；仅在无全局上游时补公共上游
  ② dns 就绪判定的两个自欺窗口：/etc 路由文件被删/被改仍判 no-op（而 doctor 走存在性检查说"缺失"→ 死结）→ `_route_in_place()` 追加 `target_path.exists()`；`_record_state()` 在特权 reload 之前落盘（reload 失败时 state 已写 → 下一轮仍判 no-op）→ 移到全部动作成功之后
  ③ `_record_state()` 不校验 `action.path == target_path` → 未来无关写动作会污染 dns-route.json
  ④ `_conf_dir_spec()` 相对路径 → systemd 相对 `/` 解析 → 单元起不来（潜在陷阱，当前不触发）
  ⑤ 【安全】生成的 dnsmasq 配置缺 `listen-address=127.0.0.1` 与 `bind-interfaces` → 新机上绑定 0.0.0.0:5353 = **局域网暴露的开放递归解析器**（DNS 放大/滥用面）。本机手写配置恰好有这两行。修法：纳入 merge 的"保证存在"集合（用户已设为别的值则不覆盖）
- Task 10: minor (deferred, reviewer out-of-scope): ① 工具会无声覆盖用户手写的 ~/.config/systemd/user/dnsmasq.service（本机是 Type=forking + PIDFile 的手写单元）→ 建议装前备份（归 Plan 3 备份机制）② merge 会改写用户自己的 port=（本机恰为 5353 无变化）③ resolved drop-in 的 DNS=127.0.0.1:5353 是**全局** DNS → 全机 DNS 改走用户态 dnsmasq、绕过网络/企业/VPN 自带 DNS，dnsmasq 一挂全机 DNS 全挂（设计层面，建议后续用 per-link 或 DNSStubListener 细化）④ 生成的配置未限制监听地址（本次已修）
- Task 10: fix round 2/5 result: 5 addressed, 0 open（commit e1848c7；scoped re-review 5/5 ADDRESSED、无新破坏；reviewer 独立跑了判据矩阵 7 例形态 + 幂等性，全对；Q1 resolv-file 处理与 Q2 IPv4-only 监听两问均确认实现正确）
- Task 10: complete (commits 2299e70..e1848c7, 2 fix rounds, review clean)
- Task 10: minor (deferred, 残留): ① 重载失败后重跑 setup 仍返回空计划、resolver 不会被真正重启（架构上缺"resolved 是否已加载该 drop-in"的信号，非回归）② 提权写入产出 0600 root 文件 → "用户改了 /etc 文件"的检测在生产中失效（只有删除检测生效）；可让提权写入后补 chmod 0644 ③ no-resolv 现无条件补齐 → 依赖 /etc/resolv.conf 提供上游（企业 split-DNS 场景）的用户会失去该路径，逃生口是显式 resolv-file=（规格决定的行为变更，值得写进用户文档）④ ExecStart 写死 /usr/sbin/dnsmasq ⑤ merge 不识别大小写变体（.TEST）
- Task 11: dispatched (implementer running, BASE e1848c7)
- Task 11: Needs fixes —— 7 Important + 若干 minor。评审确认编排层（setup/doctor/trust/logs 四段流程、退出码 0/1/3/4、逐组件 PASS/FAIL、空计划不索要密码、测试零真实系统）全部正确且经独立实测（对比 ~/.pki/nssdb mtime 与系统信任库文件列表）
- Task 11: fix round 1/5 (7 Important + 5 附带):
  ① 【重要·注入面】`--domain-suffix` 用 replace() 直接换掉冻结 dataclass，绕过了只在 load_config() 里调用的 `_validate_config()` → 带换行的后缀可让 dns.py 渲染出额外行，经 elevated.write 以 root 写进 /etc/systemd/resolved.conf.d/devproxy.conf；dnsmasq.conf 同理。config.py 自己的注释就写明"必须在配置入口就拦下"。修法：验证函数公开化，load_config 与 CLI replace 之后都调
  ② 【重要·一致性】doctor 用 `_components(paths, config)` 的默认 "auto"，而 setup 用 `dns_backend or config.dns_backend` → config.toml 写 dns_backend="system" 时 setup 装到 /etc/dnsmasq.d/，doctor 却查 resolved 路径 → 报"缺失"退 3。修法：_components 默认取 config.dns_backend，doctor 也加 --dns-backend
  ③ 【重要·Chrome 信任静默缺失】check()/plan() 只以系统信任库为唯一判据，NSS 状态完全不进入决策 → 系统库已装而 NSS 未装时 plan() 返回 []、打印"已信任，无需变更"退 0，Chrome 永远拿不到证书；doctor 同样报"✔ 就绪"。装系统 CA 与装 NSS 是两条独立路径，只做一边就掉进这个洞。修法：Platform 增 nss_trusted(ca) 能力（Linux + fake 实现），check/plan 要求系统库与 NSS **都**含该 CA
  ④ 【重要·干净机必败】plan() 不检查 ca_path 是否存在 → root.crt 是 Caddy 内部 CA，只在加载含 tls internal 的站点时才生成；setup 时 routes.caddy/sites.caddy 都空 → CA 不存在 → elevated.copy 复制不存在的源 → PermissionError → cert 组件 FAIL → setup 退 1。brief 的 check 提示误判了这一前提。修法：CA 不存在时 check() 返回 ok + 说明性 detail（不是缺陷），plan() 返回 []
  ⑤ 【重要·假阳性】update-ca-certificates 的返回码被吞（无 ca-certificates 包时退 127，CA 只躺在目录里没进信任库）；且 verify() 走的 platform.ca_trusted() **只比对暂存文件内容、不查真实信任库** → setup 可打印"✔ CA 已信任"退 0，而 curl/Go 仍不信任。修法：检查返回码并抛错；ca_trusted 改为真正验证 CA 在系统信任库里（如 openssl verify -CApath /etc/ssl/certs），openssl 不可用时退回原比对
  ⑥ 【重要·brief 测试是错误权威】NSS 动作被标 privileged=True（执行上并未提权）→ sudo 清单谎报"需要 sudo 共 2 项"、非交互环境仅剩免 root 的 NSS 步骤也退 4。Ruling: **采纳 reviewer 判断，brief 的测试是错的**（brief 自己的实现就标 privileged=False，其本意是"只有系统信任库需提权"，测试的 all(...) 把单条语义过度泛化）→ 改回 False 并把断言改为 any(...)。这是本次会话第二次推翻"以测试为准"
  ⑦ 【重要·违反硬约束】CertComponent.check() 可抛异常（UnicodeDecodeError 逃出 linux.py 的 except OSError）→ doctor 在 build_report 里崩成 traceback 而非打印体检表。"check() 永不抛异常"是 load-bearing 约束（doctor 与 build_plan 都依赖）
  附带：trust/logs 的 PermissionError 应退 4 而非 1 且 trust 缺 --yes；--dns-backend 不校验（非法值静默按 auto）；build_plan 的 plan 阶段未隔离组件异常（只有 apply 阶段隔离了）；cert 动作顺序把特权步骤排在免特权步骤之前（用户拒绝 sudo 则 NSS 步骤永不执行）；logs 调 build_context() 会创建家目录文件（只读命令的多余副作用）
- Task 11: fix round 1/5 result: 7 Important + 6 minor 全部 ADDRESSED（commit a5e93ea；复审独立用一次性 CA 验证了 openssl verify -CApath 的语义、确认 nss_trusted 协议新增不破坏任何既有实现、认可空计划退 3 与 EXIT_USAGE=2 的自洽性）
- Task 11: fix round 2/5 (修复波引入的新 Important + 1 Minor):
  ① 【重要·回归】`nss_trusted` 只按昵称判断（certutil -L -n 返回码），**完全忽略证书内容** → Caddy 重生成 CA 后旧 NSS 条目仍在 → 报"已信任" → plan() 跳过重装 → Chrome 拒绝新 CA，且 devproxy trust 也计划为空 → **无任何修复路径**。这比修复前更糟（修复前 ca_trusted 为假时两条动作都会计划）。修法：用 certutil -L -n <nick> -a 取出存储的 PEM，与 ca 归一化后比对（内容一致且 rc=0 才算 True），任何失败一律 False（nss_trusted 喂给 check()，不能抛）
  ② 【次要】NSS 步排在特权步之前（这是上一轮的正确要求）+ apply 遇首个失败即抛 → 机器缺 libnss3-tools 时 update-ca-certificates 永远不会被触达，curl/Go 也失去信任。修法：CertComponent.apply() 改为跑完全部动作、收集各步失败、最后抛一条合并错误（两条独立子步骤互相隔离）
- Task 11: minor (deferred, 复审 out-of-scope): ① ca_trusted 在暂存文件 /usr/local/share/ca-certificates/devproxy-local.crt 不存在时提前返回 False，即便 openssl 能验证该 CA 已被信任（假阴性，只导致一次多余的重装）② logs 只捕 PermissionError，缺 journalctl 时仍会 FileNotFoundError traceback ③ `--dns-backend ""` 因 `value or config.dns_backend` 静默回落，未按 exit 2 拒绝 ④ 鸭子类型的 fake 若缺 nss_trusted，AttributeError 在 certs.py:62 的 try 之外求值 → check() 会抛（doctor 无隔离）；Plan 3 的 macOS 平台必须实现该方法
- Task 11: fix round 2/5 result: 2 addressed, 0 open（commit 60e7cc8；复审自建一次性 NSS DB 实测 certutil -L -a 的输出形态，确认 PEM 归一化字节一致、假阳/假阴均排除；无新 Critical/Important）
- Task 11: fix round 3/5 (1 致命遗留 + 1 Minor):
  ① 【重要·真实机器上功能全废】certs.py 用 `certutil -V` 的返回码判断 certutil 是否缺失，但 `certutil -V` 缺 `-n <nickname>` 时**恒退 255**（controller 本机实测：/usr/bin/certutil 存在，`certutil -V` 仍退 255 并打印 "nickname is required"）→ 真实机器上 NSS 动作永远报"缺少 certutil"、Chrome 信任永远装不上，且给出错误修复提示。测试抓不到是因为 FakePlatform.run 恒返回 0。修法：改用 shutil.which("certutil") 探测，`certutil -N/-D/-A` 也不再以探测返回码为门
  ② 【次要】收集式失败处理把 PermissionError 转成 RuntimeError → `trust` 的 `except PermissionError → EXIT_SUDO(4)` 分支不可达，拒不 sudo 时退 1 而非 4。修法：全部失败都是 PermissionError 时按 PermissionError 抛出
- Task 11: minor (deferred, 复审 out-of-scope): ① 协议 docstring 仍把 nss_trusted 描述为"库里是否已有该 CA"（存在性），实际已是内容比对 ② 比对只看内容不看 NSS 信任标志（同证书但标为 prohibited 仍算信任）③ 报告称"6 条平台测试"实为 4 新增 + 1 重写（总数 358→365 自洽）
- Task 11: fix round 3/5 result: 2 addressed, 0 open（commit 107435f；复审确认 shutil.which 是唯一的 certutil 存在性判据、-N/-D/-A 不再依赖探测返回码、PermissionError 保留逻辑混用时不会掩盖真实失败）
- Task 11: complete (commits e1848c7..107435f, 3 fix rounds, review clean)
- Task 11: minor (deferred, 复审 out-of-scope): ① 测试 patch 的是共享 shutil 模块的 which（当前只有一个调用点，无害；将来多调用点时应加模块级 seam）② 测试断言 `"其余 1 个步骤已执行" in message or "已执行" in message` 的 fallback 弱化了断言意图 ③ "缺 certutil 时不留空目录" 无专门断言
- Plan 2 全部 4 个 Task 完成（c3bb4ac..107435f，368 tests），进入整分支最终 review
- Plan 2 最终整分支评审（opus，56 次工具调用）：**Needs fixes** —— 3 Critical + 6 Important。评审在本机只读地逐个跑了组件的 check()/plan()，并内存复现了 apply_plan 掩盖失败
- Plan 2: Ruling: **C1 的根因是 brief 的测试写错，不是计划要求错** —— bootstrap plan 第 797 行明确要求 check「主 Caddyfile 不存在」，而 Task 9 的 `test_check_ok_when_binary_and_service_ready` 构造了「无 Caddyfile 却要 ok=True」的场景。Task 9 的 reviewer 当时以「测试为准」接受了去掉该检查（reviewer 确实核实了两者矛盾）；最终评审在本机实测出后果：本机 caddy 二进制在位+服务 active 但 ~/.config/devproxy/Caddyfile 不存在 → check ok、plan 空 → setup 报"已就绪"却不写 Caddyfile → up 必然 reload 失败退 1。**裁定：计划/规格要求优先，改代码 + 改那条测试**
- Plan 2: fix wave 已派发（单个 opus agent，3 Critical + 6 Important + 7 附带）：
  C1 Caddy 组件纳入主 Caddyfile 内容比对 + 单元 ExecStart 判据；up/expose 加 Caddyfile 前置检查退 3
  C2 特权写入建父目录（/etc/systemd/resolved.conf.d 与 /etc/dnsmasq.d 本机 dpkg -S 无归属，干净机可能不存在 → DNS 组件必败）
  C3 apply_plan 在 report.status.ok 为 False 时保留失败，不被 verify() 覆盖（评审实测：plan 抛异常的组件被报成 ok=True 且 setup 退 0）
  I4 auto 后端改用 resolved_active()（systemctl is-active）而非目录存在性
  I5 干净机 setup 后 CA 必未信任且用户看不到提示 → setup 表格打印说明 + 结尾提示 devproxy trust
  I6 doctor 加逐组件异常隔离；certs.py 的 nss_trusted 属性访问移入 try（Plan 3 的 macOS 漏实现会崩）
  I7 logs 捕 OSError（缺 journalctl 现为 traceback）；未知服务退 2
  I8 补 loginctl enable-linger（spec §6.2 明确要求，旧 Justfile 也做；缺失则重启后未登录前用户服务不启动）
  I9 verify() 从浅层代理升级为功能验证：平台加 dns_query()/http_ok()，Caddy 验 https://localhost、Dnsmasq/Dns 验真实解析、Cert 验一次真实 HTTPS
  I10 改写用户 dnsmasq.conf 前备份到 paths.backup_dir
  附带：setcap 返回码检查、下载后 caddy version 二次校验 + tmp+os.replace（避免 ETXTBSY）、未知架构报不支持、--dns-backend "" 退 2、setup 加交互确认、nss_trusted docstring、unit 路径校验
