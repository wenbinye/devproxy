# devproxy 真机验证清单

自动化测试（607 个用例 + 18 个子测试）覆盖不到的、**必须在真实机器上跑一遍**的行为。
发版前逐条打勾；打不了勾的项写明「未验证 + 原因」。

**条目统计**

| 分组 | 条数 | 来源 |
|---|---|---|
| A. macOS 未验证行为 | 22 | `.superpowers/sdd/2026-10-22-devproxy-migrate-and-macos/task-16-report.md` §7 第 1-17 项 + 第二轮第 18-22 项 |
| B. Task 16 评审的遗留缺口 | 5 | 同上目录 `progress.md` 中「Task 16: minor (deferred)」的 ①-⑤ |
| C. 迁移检测的残留风险 | 1 | `migrate.detect_legacy()`「读不到遗留单元 ⇒ 视为没有遗留环境」 |
| **小计（A+B+C）** | **28** | |
| D. Linux 真机检查 | 8 | 单元测试覆盖不到的 Linux 侧行为（不计入 28） |

约定：

- `[ ]` 未验证；`[x]` 已通过；`[!]` 未通过（后面写现象）；`[-]` 无法验证（写原因）。
- 所有命令在**真实机器**上执行；不要用 fake 平台跑。
- 涉及 sudo 的步骤先确认再执行。
- macOS 组（A）需要一台**真 Mac**；本项目的开发机是 Linux，A 组至今**未在真机验证过**。

---

## A. macOS 未验证行为（22 项，本机为 Linux，全部未验证）

> 这一组的行为在代码里都有实现与单元测试（用假 subprocess 验证「调用了什么、参数对不对」），
> 但**真实系统的反馈**只能在 Mac 上取得。逐条执行并记录结果。

### A1. launchd plist 的键语义与生效

- 检查：`devproxy setup` 之后检查 `~/Library/LaunchAgents/com.devproxy.dnsmasq.plist`
  与 `/Library/LaunchDaemons/com.devproxy.caddy.plist`，确认 `Label` / `ProgramArguments` /
  `EnvironmentVariables` / `RunAtLoad` / `KeepAlive` / `StandardOutPath` /
  `StandardErrorPath` / `Description` 八个键都在。
- 预期：launchd 如实接受这些键；`launchctl print gui/$(id -u)/com.devproxy.dnsmasq`
  的输出里 `state = running`，`RunAtLoad` + `KeepAlive` 的组合不会导致进程反复重启。

### A2. `launchctl print` 的实际输出与退出码

- 检查：分别执行 `launchctl print system/com.devproxy.caddy`、
  `launchctl print gui/$(id -u)/com.devproxy.dnsmasq`，记录退出码与输出。
- 预期：两个目标都能打印出 job 信息；**普通用户读 system 域是否需要 root 要在这里定论**
  —— 若需要 root，`service_active("caddy")` 会恒为 False，`devproxy doctor` 会误报
  「caddy 服务未运行」，需要换只读探针。

### A3. `launchctl bootstrap` / `bootout` 的真实行为

- 检查：`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.devproxy.dnsmasq.plist`；
  重复 bootstrap 一次；对已加载的 plist 再 `launchctl bootout`；故意写错域名试一次。
- 预期：重复 bootstrap 失败且**不破坏已运行的 job**；bootout 后 job 停止；
  错误域名返回非零退出码并给出可读报错，devproxy 不会把它当成成功。

### A4. `launchctl kickstart -k` 是否真的重启

- 检查：`launchctl kickstart -k gui/$(id -u)/com.devproxy.dnsmasq`，
  用 `ps` 观察 pid 是否变化；再 `kill -9` 掉进程，观察 `KeepAlive` 是否自动拉起。
- 预期：kickstart 后 pid 变化；进程被杀后被自动拉起（KeepAlive 生效）。

### A5. `/etc/resolver/<后缀>` 的格式与 `port` 指令

- 检查：`cat /etc/resolver/test` 应为 `nameserver 127.0.0.1` + `port 5300`；
  `dscacheutil -q host -a name foo.test` 看是否解析到 127.0.0.1。
- 预期：mDNSResponder 接受该文件；**`port 5300` 这一行被尊重**（不同 macOS 版本行为可能不同，
  这是本组最可疑的一条）；解析结果为 127.0.0.1。

### A6. `security add-trusted-cert -d -r trustRoot` 的真实效果

- 检查：`devproxy trust` 之后用 `security verify-cert -c <ca> -p ssl` 验证；
  Safari 与 Chrome 分别访问 `https://<name>.test`。
- 预期：命令不需要交互式额外授权（只依赖 `sudo` 缓存）；Safari/Chrome 都信任该 CA；
  Chrome 无需重启即生效（macOS 走钥匙串，不像 NSS 要重启进程）。

### A7. `security verify-cert` 的退出码与输出格式

- 检查：`security verify-cert -c <ca> -p ssl; echo $?`，再用一张未受信任的证书对调。
- 预期：受信任时退出码 0、未受信任时非 0 —— `ca_trusted()` 依赖这个退出码判断。

### A8. root 的 LaunchDaemon 能否绑定 80/443

- 检查：`sudo lsof -nP -iTCP:80 -sTCP:LISTEN` 与 443；访问 `https://<name>.test`。
- 预期：caddy（root 身份）成功监听 80/443，SIP / launchd 策略下没有拦截；
  80 端口自动 308 跳转到 443。

### A9. `dscacheutil -q host -a name` 的输出格式与 dig 回退

- 检查：`dscacheutil -q host -a name foo.test` 看输出行；再临时让 dnsmasq 不响应，
  观察是否回退到 `dig +short`。
- 预期：输出含 `ip_address:` 或 `ipv6_address:` 行，工具取首个 A/AAAA 答案；
  回退路径能拿到同样的答案。

### A10. macOS 自带 curl 的行为

- 检查：`curl -fsS --max-time 3 https://<name>.test`；再对一个不存在的域名跑同一条命令。
- 预期：对已注册域名成功；对**未注册**域名失败（TLS 握手失败）——
  这正是「未注册域名拿不到任何证书」的安全属性在 macOS 上同样成立。

### A11. Homebrew 的 dnsmasq 安装位置

- 检查：`ls -l /opt/homebrew/sbin/dnsmasq /usr/local/sbin/dnsmasq /usr/sbin/dnsmasq`；
  `brew --prefix`。
- 预期：Apple Silicon 上是 `/opt/homebrew/sbin/dnsmasq`、Intel 上是 `/usr/local/sbin/dnsmasq`；
  工具选中的路径与实际安装位置一致（单元里写的是绝对路径，launchd 不认 PATH）。

### A12. BSD 版 `install -d` / `cp`

- 检查：`sudo install -d /etc/resolver` 与 `sudo cp <src> /etc/resolver/test`（在干净机器上）。
- 预期：目录被创建、文件被覆盖成功，无 BSD/GNU 行为差异导致的报错。

### A13. `tail -f` 的流式跟随与 Ctrl-C

- 检查：`devproxy logs dnsmasq`，观察是否持续输出并能 Ctrl-C 退出。
- 预期：实时跟随、不缓冲整段输出；Ctrl-C 能立刻退出且不打印 traceback。

### A14. `sudo -v` 预认证是否够用

- 检查：一次 `devproxy setup`（含多个提权动作），数一数被要求输入几次密码。
- 预期：**整轮只提示一次**（提权会话被缓存复用）。

### A15. plist 的归属与权限

- 检查：`ls -l /Library/LaunchDaemons/com.devproxy.caddy.plist`。
- 预期：权限 0644、归属 `root:wheel`（`chown` 失败不阻断，但 `chmod 644` 失败必须报错）。

### A16. 提权会话缓存覆盖整轮

- 检查：同 A14，但要在 `setup` 之后紧接着跑 `devproxy trust --yes`，观察是否再要密码。
- 预期：同一轮运行内只认证一次。

### A17. `doctor` 在 macOS 上的措辞与退出码

- 检查：`devproxy doctor`；故意停掉 dnsmasq 再跑一次。
- 预期：不健康时退出码 3；提示语里出现 `launchctl` 而不是 `systemctl`。

### A18. devproxy 自己能否读它装下的 plist

- 检查：`cat /Library/LaunchDaemons/com.devproxy.caddy.plist`（**普通用户**，不加 sudo）。
- 预期：能读到（0644）；`_unit_current()` 的提权回退在真机上拿得到内容，
  doctor 不会因为「读不到单元」而永远报红。

### A19. system scope 的日志目录由谁建、首启前是否已存在

- 检查：`ls -ld ~/.local/state/devproxy/logs`；在全新用户下先跑一次 setup，
  确认目录在第一次 launchd spawn **之前**就已存在。
- 预期：目录由**用户身份**创建（属主是用户），root 跑的 caddy 能往里写；
  家目录在网络卷或 SIP 限制下也不丢日志。

### A20. `launchctl print` 成功 ≠ job 在运行

- 检查：`launchctl print system/com.devproxy.caddy` 看 `state` 行的确切写法
  （`state = running`？缩进如何？是否有多个 state 行？）；再把 caddy 进程杀掉，
  看 `state` 变成什么。
- 预期：已加载但已退出时 `state` **不是** `running`，工具据此判为「未运行」；
  看不到 `state` 行时按「没跑」处理（fail closed），doctor 不会误报绿。

### A21. root 跑 caddy 在用户家目录留下的 root 属主文件

- 检查：`ls -l ~/.local/share/caddy/pki/authorities/local/root.crt`、
  `ls -l ~/.local/state/devproxy/logs/`；尝试以普通用户身份读取与删除这些文件。
- 预期：文件可读（浏览器能读 CA、`tail` 能读日志）；用户**删得掉**这些文件
  （root 属主的文件在用户家目录里通常因父目录权限可删，但要确认）。

### A22. Homebrew 自带 dnsmasq 配置被绕过

- 检查：确认 `~/.config/dnsmasq/dnsmasq.conf` 是本工具写的；
  `brew services list` 看 Homebrew 的 dnsmasq 是否在跑、是否抢 5300 端口。
- 预期：只有本工具的 dnsmasq 在跑（单元用 `--conf-dir=~/.config/dnsmasq`，
  Homebrew 那份默认配置不生效）；`/etc/resolver/test` 确实让系统解析器按域分流。

---

## B. Task 16 评审的遗留缺口（5 项）

> 来源：`progress.md` 中「Task 16: minor (deferred, 交最终 review 分诊)」的 ①-⑤。
> ①-④ 是 Linux/macOS 共用代码的副作用，需在真机上确认方向；⑤ 是清单本身的缺口。

### B1. `_unit_current()` 判据收紧对 Linux 的影响

- 检查：Linux 上手工改 `~/.config/systemd/user/caddy.service` 的 `ExecStart=`
  （加一个前置参数 / 调整参数顺序），然后跑 `devproxy doctor` 与 `devproxy setup`。
- 预期：判为「单元过期」（方向安全 —— 会重装单元），doctor 多报一次红，
  setup 多一次重装。可接受，但需确认不会每次 setup 都索要密码。

### B2. doctor 的提权回退会弹密码

- 检查：把 `~/.config/systemd/user/caddy.service` 改成 `chmod 000`（或 root 属主 0600），
  然后跑 `devproxy doctor`。
- 预期：doctor **可能弹出 sudo 密码提示**（提权读单元的代价）——
  确认这一行为可接受；若不可接受，应改成 `sudo -n` 之类的非交互读取。

### B3. `unit_exec_argv` 不认 systemd 的引号语法

- 检查：把 `ExecStart=` 写成带引号的路径
  （`ExecStart="/path with space/caddy" run --config …`），跑 `devproxy doctor`。
- 预期：被判为「单元过期」→ setup 重装单元（安全方向，不会跑错配置）。

### B4. `migrate.detect_legacy()` 的方向问题

- 检查：见 C 组（本项与 C 是同一风险的两种表现，C 是可执行版本）。

### B5. 清单是否覆盖了第 18-22 项

- 检查：把本文档 A 组与 `task-16-report.md` 的「未验证行为清单（22 项）」逐条对照。
- 预期：22 条一一对应，无遗漏（A18-A22 即评审点名要补的五项）。

---

## C. 迁移检测的残留风险（1 项）

### C1. `migrate.detect_legacy()`：读不到遗留单元 ⇒ 视为「没有遗留环境」

- 背景：`detect_legacy()` 在**读不到**遗留的 `caddy.service` 时（普通用户无权限、
  sudo 不可用）返回 `None`，即「没有遗留环境」。极端情况下迁移会**接管用户手工维护的
  Caddyfile 与单元环境变量**（例如 `MINIKUBE_IP`），而用户毫不知情。
  代码里已加提权回退，但「提权也读不到」这条路径仍在。
- 检查：
  1. 造一个遗留环境：`~/.config/systemd/user/caddy.service` 指向另一个 Caddyfile，
     `ExecStart` 里带 `Environment=MINIKUBE_IP=1.2.3.4`；
  2. 把该单元改成 `chmod 000`（普通用户与 `sudo` 都被拒的极端情形用 `chmod 000` + 无 sudo 缓存模拟）；
  3. 跑 `devproxy setup`，看它是否报告「检测到遗留的 caddy 服务」。
- 预期：**要么**正确检测到遗留环境并给出备份与回滚提示，**要么**明确提示
  「遗留单元读不到，无法确认是否有遗留环境」而不是静默接管。
  若出现静默接管（无任何提示地改写单元），本项记为 `[!]` 并上报。

---

## D. Linux 真机检查（8 项，不计入 28）

> 这些是单元测试（全部 hermetic，subprocess 层全部打桩）**无法覆盖**的 Linux 侧行为。

### D1. 干净用户下的 `devproxy setup` 全流程

- 检查：用一个新用户（或 `HOME` 指向临时目录）跑 `devproxy setup`，再跑 `devproxy doctor`。
- 预期：setup 全程跑通，doctor 四个组件全部就绪；
  过程中提示 sudo 的次数与「需要 sudo 的步骤」清单一致。

### D2. `setup` 的幂等性

- 检查：紧接着再跑一次 `devproxy setup`。
- 预期：输出「所有组件已就绪，无需变更。」，**不再索要密码**、不再重启
  systemd-resolved；CA 尚未生成时额外打印「首次 devproxy up 会生成 CA…」提示。

### D3. `caddy validate` 对含真实站点块的配置

- 检查：把用户自己的站点块写进 `~/.config/devproxy/sites.caddy`
  （含 `tls internal`、`reverse_proxy`、`{$MINIKUBE_IP:…}` 之类），然后
  `~/.local/bin/caddy validate --config ~/.config/devproxy/Caddyfile`。
- 预期：校验通过；迁移流程里（换单元之前）也会跑一次同样的校验。

### D4. `dig` / `resolvectl` 对真实 resolver

- 检查：`dig @127.0.0.1 -p 5353 foo.test +short` 与 `resolvectl query foo.test`；
  再 `resolvectl status` 看 `~.test` 的路由。
- 预期：两者都把 `foo.test` 解析到 `127.0.0.1`（或 `::1`）；
  `/etc/systemd/resolved.conf.d/devproxy.conf` 生效，`Domains=~.test` 已注册。

### D5. `curl` 对真实的 `https://<name>.test`

- 检查：先 `devproxy up demo -- python3 -m http.server`，
  再 `curl -v https://demo.test`；再 `curl -v https://not-registered.test`。
- 预期：已注册域名握手成功、证书由 devproxy 的 CA 签发；
  **未注册域名握手失败**（拿不到任何证书），这正是安全属性的体现。

### D6. NSS 库与 Chrome 的信任

- 检查：`certutil -L -d sql:~/.pki/nssdb` 看 `devproxy Local CA` 与信任标志 `C,,`；
  再用 Chrome 访问 `https://<name>.test`。
- 预期：NSS 库里有该 CA；Chrome **完全退出并重启后**不再告警
  （只装系统信任库时 Chrome 必然报 `net::ERR_CERT_AUTHORITY_INVALID`）。

### D7. `loginctl enable-linger`

- 检查：`loginctl show-user $USER -p Linger --value` 应为 `yes`；
  `systemctl --user status caddy` 在**未登录**的会话里也应是 active。
- 预期：linger 已启用，caddy 开机自启（未登录也跑）。

### D8. 备份 / 回滚往返（真实机器）

- 检查：在有遗留环境（手工 `caddy.service` + `Caddyfile`）的机器上跑 `devproxy setup`，
  确认 `~/.local/state/devproxy/backups/<UTC时间戳>/` 里出现
  `manifest.json`、`MANIFEST.txt` 与各文件的 `.bak`；然后
  `devproxy setup --rollback ~/.local/state/devproxy/backups/<UTC时间戳>`。
- 预期：回滚后遗留的 `caddy.service` 与旧 `Caddyfile` **逐字节还原**
  （`sha256sum` 对比 manifest 里的哈希），原本不存在的文件被删除，
  caddy 服务被重启。

---

## 附：不在本清单内的自动化覆盖

以下内容已有自动化测试覆盖，**不需要**在真机上重复验证：

- 路由表的增删改查、渲染的确定性、非法 domain / upstream 的拒绝 → `tests/test_render.py`、`tests/test_names.py`
- 路由写入的事务性（加锁 → 渲染 → validate → 落盘 → reload，失败回滚）→ `tests/test_store.py`
- 端口分配、子进程生命周期、进程组清理、就绪超时 → `tests/test_runner.py`
- 迁移的 Caddyfile 拆分、storage 解析、备份清单与回滚 → `tests/test_migrate.py`、`tests/test_backup.py`
- 四个组件的 check/plan/apply/verify 与 macOS 分支 → `tests/test_component_*.py`、`tests/test_platform_macos.py`
- CLI 各子命令与退出码 → `tests/test_cli.py`
- 端到端自检 → `tests/test_selftest.py`、`tests/test_integration.py`、`devproxy selftest`
