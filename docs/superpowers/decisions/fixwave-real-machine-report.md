# 真机首跑缺陷修复波报告（fixwave-real-machine）

分支：`fix/real-machine-nss-sudo`（基于 master `05107ba`）
日期：2026-10-23

## 提交
- d60dd99 🐛 fix(devproxy): 展开 NSS 库路径的 ~，并让删除失败与静默无效都报错
- 8b6914a 🐛 fix(devproxy): 让陈旧 CA 清理真正可达，并检查 certutil -D 的返回码
- f625899 🐛 fix(devproxy): 收紧 NSS 信任判据并修正三列标志（评审 fix round 1）
- 76ee23e 🐛 fix(devproxy): 修真机首跑暴露的两个缺陷（免密提权 + NSS 去重）

## 测试

```
657 passed in 17.51s
```

## 实现者与三轮评审的完整报告

# Fix wave 报告：真机首跑暴露的两个缺陷

分支：`fix/real-machine-nss-sudo`（基于 master `05107ba`）
日期：2026-10-23

---

## 0. 动手前的实测复现（任务书要求）

先在临时目录里用**真的** `certutil` 与**真的** `sudo` 复现任务书描述的现象，确认与任务书一致后才动手。

### 0.1 缺陷 A：`sudo -v` 在 NOPASSWD 机器上失败

本机 sudoers 与任务书描述完全一致：

```
$ sudo -n -l
User yewenbin may run the following commands on yewenbin-ThinkPad-P16s-Gen-3:
    (ALL : ALL) ALL
    (ALL) NOPASSWD: ALL
```

| 命令 | 返回码 | 输出 |
|---|---|---|
| `sudo -n true` | 0 | （无输出） |
| `sudo -v < /dev/null` | 1 | `sudo: A terminal is required to authenticate` |
| `sudo -n -v` | 1 | `sudo: interactive authentication is required` |

**与任务书一致。** 补充一条任务书没有的实测：在**有 TTY** 的情况下 `sudo -v` 会弹出密码提示
（`script -qec 'sudo -v'` → `[sudo: authenticate] Password:`）—— 因为 `sudo -v` 是"无命令"调用，
不匹配 `NOPASSWD: ALL` 的命令规范，只能落到默认的 `(ALL : ALL) ALL`。真机首跑之所以报
"A terminal is required"而不是弹框，是因为那次运行没有可用的控制终端。

### 0.2 缺陷 B：NSS 按 (issuer, serial) 去重

在临时库里造一张自签 CA 后实测：

```
$ certutil -d sql:$DB -A -n "devproxy Local CA" -t "C,," -i root.crt   # rc=0
$ certutil -L -d sql:$DB
devproxy Local CA                                            C,,

$ certutil -d sql:$DB -A -n "另一个昵称" -t "C,," -i root.crt          # rc=0
$ certutil -L -d sql:$DB
devproxy Local CA                                            C,,      ← 条目没有新增
```

**与任务书一致**（返回 0、条目不新增）。另外实测到一条任务书没有写明的细节：

> `certutil -A` 对**已在库里的证书**不是完全"无操作"——它会**保留原昵称、把该条目的信任标志改成
> `-t` 指定的值**。例：库里已有 `OldName`（标志 `,,`），再 `-A -n NewName -t "P,,"` 之后
> `certutil -L` 显示的仍是 `OldName`，标志变成了 `P,,`。

这条实测结果**不影响**修法（判据仍必须按内容 + 标志，不能按昵称），但它解释了为什么
`certutil -M` 那条兜底路径在本机几乎不会被触发（`-A` 已经把标志改对了）—— `-M` 保留为
"某些 NSS 版本 / 某些情形下 `-A` 没能改标志"时的兜底。

### 0.3 本机真机状态（缺陷 B 的现场）

```
$ certutil -L -d sql:$HOME/.pki/nssdb
Certificate Nickname                                         Trust Attributes
                                                             SSL,S/MIME,JAR/XPI

Caddy Local Authority                                        C,,
```

库里**没有** `devproxy Local CA`；而旧代码只按这个固定昵称查 → 永远查不到 → 永远判"未信任"。

---

## 1. 缺陷 A：免密机器上 `sudo -v` 失败导致全部提权步骤瘫痪

### 根因

`LinuxElevated._authenticate()` 只做了一件事：跑 `sudo -v`，返回码非 0 就
`raise PermissionError("提权失败：…")`。而 `sudo -v` 是"无命令"调用，在
`(ALL : ALL) ALL` + `(ALL) NOPASSWD: ALL` 这套 sudoers 上**必然**落到需要认证的默认策略，
无 TTY 时直接失败。于是这台**根本不需要密码**的机器上，所有特权动作全部瘫痪
（真机首跑的 `✗ dns: 提权失败：sudo: A terminal is required to authenticate`）。

### 改了什么

**`src/devproxy/platform/linux.py`**

- `LinuxElevated._authenticate()`（第 284–302 行）：改成三段式
  1. `sudo -v` 成功 → 维持现状（时间戳已预热，后续 `sudo <cmd>` 复用）；
  2. `sudo -v` 失败 → **不抛异常**，改用 `sudo -n true` 探测免密能力；成功即记下
     "必须用 `-n`"（`self._noninteractive = True`），提权可用；
  3. 两条都失败 → 才抛 `PermissionError`，且信息**同时带上两条命令的输出**
     （`提权失败：sudo -v: <输出>；sudo -n true: <输出>`），
     用户能分辨"这台机器没免密"与"sudo 本身坏了"。
- `LinuxElevated.run()`（第 304–307 行）：免密路径下用 `sudo -n <argv>`，
  绝不退化成会弹密码框的 `sudo <argv>`（非交互环境会直接报 "A terminal is required"）。
- `_noninteractive` 做成**类属性**（第 276 行）：测试里用 `__new__` 造出的会话没有实例属性，
  也必须能正常跑。

**`src/devproxy/platform/macos.py`**（第 146–193 行）：`MacOSElevated` 同一套语义
（同样的三段式 `_authenticate` + `run` 的 `-n` 分支 + 类属性 `_noninteractive`）。
macOS 本轮无法真机验证，但缺陷模式完全相同，不一并修就是留一个同样的坑。

### 为什么不在探测顺序上做文章

任务书规定的顺序是"先 `sudo -v`、失败再 `sudo -n true`"，实现严格照此执行。
另一种做法（先探 `sudo -n true`）在免密机器上可以省掉一次多余的密码提示，但在
**密码机器且已有缓存时间戳**的情况下会误走 `-n` 路径：时间戳中途过期时
`sudo -n cmd` 直接失败，而 `sudo cmd` 本来还能弹框让用户补密码。
任务书的顺序在这两种机器上都不吃亏，故不偏离。

---

## 2. 缺陷 B：NSS 按 (issuer, serial) 去重 → CA 永远装不上、永远查不到信任

### 根因

两处叠加：

1. **判定**（`LinuxPlatform.nss_trusted`）只按固定昵称 `CA_NICKNAME`
   （`devproxy Local CA`）查一条。库里那张 CA 挂在别的昵称下（真机是
   `Caddy Local Authority`）→ 查不到 → 永远判"未信任"。
2. **安装**（`CertComponent._apply_one` 的 `install-nss-ca`）只信 `certutil -A` 的返回码。
   由于 NSS 按 (issuer, serial) 去重，`-A` 对"已存在的证书"是**静默无操作**（返回 0，
   条目并不新增）→ 代码认为装上了，库里却并没有 `devproxy Local CA` 这条。

结果：`devproxy trust` 永远失败、`doctor` 永远红
（真机首跑的 `✗ cert: CA 仍未被完全信任`）。

### 改了什么

**`src/devproxy/platform/linux.py`**

- 新增模块级常量与解析（第 36–48 行）：
  - `NSS_TRUSTED_FLAGS = ("C", "T", "P")` —— NSS 标志首字符的 SSL 信任语义
    （`C`/`T`=受信任的 CA、`P`=受信任的对端、`G`/`U`=有效但不信任、`,`/`.`/空=无信任）；
  - `_NSS_ROW_RE` —— `certutil -L` 表格行的正则
    `^(?P<name>.*?)[ ]+(?P<flags>[A-Za-z]*,{2,}[A-Za-z]*)$`。
    昵称列宽 55、**昵称可以含空格**、超长会溢出（昵称与标志之间只剩一个空格），
    所以只能从行尾切标志，绝不能按"第一个空格"切昵称。
- `_nss_rows(listing)`（第 134–152 行）：把表格解析成 `[(昵称, 标志)]`。
  表头两行与空行自然被过滤（表头里没有"连续两个逗号"，匹配不上标志的形状）。
- `_nss_flags_trusted(flags)`（第 155–157 行）：首字符是否属于受信任集合。
- **新增非协议方法 `LinuxPlatform.nss_entries(ca)`**（第 543–582 行）：
  列出库里与这张 CA **内容相同**的条目 `[(昵称, 标志)]`。
  做法：先 `certutil -L -d sql:<db>` 拿表格 → 解析出候选昵称（工具自己的昵称排在最前，
  即任务书要求的"快路径"）→ 每个候选都用 `certutil -L -d sql:<db> -n <昵称> -a`
  **实际取一次**（取不到就跳过）→ 用 `_pem_fingerprints`（DER 的 sha256）
  比对内容，命中才收下。**正则只是候选，判定靠实际取证书** —— 解析有偏差时宁可漏判也不能误判。
- `LinuxPlatform.nss_trusted(ca)`（第 584–600 行）改为
  "这张 CA 在库里 **且** 带 SSL 信任标志"，基于 `nss_entries` 实现；
  整个判定被 try/except 兜住，异常一律当"没信任"（`check()` 永不抛异常）。
- 顺带删除了因此不再使用的 `_pem_body` / `_pem_digest`（内容比对改用与
  `ca_in_trust_store` 同一套 `_pem_fingerprints`，语义更硬）。

**`src/devproxy/components/certs.py`**

- `CertComponent._nss_trusted(ca=None)`（第 67–73 行）：允许复查别的 CA 文件。
- `CertComponent._nss_entries(ca=None)`（第 75–95 行）：用
  `getattr(self.platform, "nss_entries", None)` 探测**非协议方法**；
  macOS 没有这个方法 → 退化成"只按自己的昵称处理"。
- `install-nss-ca` 动作（第 244–297 行）改成：
  1. **已经信任就直接返回**（库里这张 CA 已带信任标志 → 一条 certutil 命令都不发，
     更不会去动用户已有的那条）；
  2. 删掉工具自己昵称下的旧条目（幂等，"不存在"的失败忽略）；
  3. `certutil -A` 安装（返回码非 0 → 报错）；
  4. **复查** `platform.nss_trusted` —— `-A` 返回 0 **不代表真的装上了**；
  5. 复查仍否 → 库里这张 CA 缺 SSL 信任标志 → 用
     `certutil -M -d sql:<db> -n <昵称> -t "C,,"` 补标志（昵称来自 `nss_entries`）→ 再复查；
  6. 仍否 → 抛 `RuntimeError`，信息带上 certutil 的实际输出。

**`src/devproxy/platform/fake.py`**（第 81–107 行）

`FakePlatform.run()` 现在会把 `certutil -A` / `-D` 映射到假平台的 NSS 状态上
（`-A` 把证书放进 `self.nss`、`-D` 按昵称删掉）。
**理由**：组件现在会在安装之后**复查**"这张 CA 到底进库了没"，假平台若不跟着变，
复查永远失败 → 安装这一步永远报错。假平台必须照着真语义变。
这不是放宽护栏 —— `_assert_may_mutate()` 与 `isolated_home` 夹具都原样未动。

**`src/devproxy/platform/base.py`**：**未改动**（协议方法一个没加、没删、没改）。

---

## 3. 新增测试

### 3.1 `tests/test_nss_real_certutil.py`（新建，9 个用例，**用真的 certutil**）

没有 certutil 就整组 `pytest.skip`（本机有；CI 里新装了 `libnss3-tools`）。
证书用 `openssl` 现场生成（真的自签 CA，内容可比对），库建在 `tmp_path` 里。

| 用例 | 断言什么 |
|---|---|
| `test_certutil_add_dedups_by_issuer_and_serial` | **复现真机现象**：同一张证书换个昵称再 `-A`，返回码仍是 0，但条目**不新增** |
| `test_nss_trusted_when_cert_stored_under_other_nickname` | 同一张 CA 用别的昵称入库 → `nss_trusted` 为 True；`nss_entries` 给出 `("Caddy Local Authority", "C,,")` |
| `test_nss_trusted_false_when_entry_has_no_trust_flag` | 库里有这张 CA 但标志是 `,,` → **不算**已信任；`nss_entries` 仍报出昵称与标志 |
| `test_nss_trusted_false_when_other_cert_stored` | 库里是别的证书 → 不算这张 CA 已信任；`nss_entries` 为空 |
| `test_nss_entries_finds_entry_whose_nickname_has_spaces` | 昵称含空格的条目也能找到（解析不能按第一个空格切） |
| `test_plan_is_empty_when_ca_trusted_under_other_nickname` | 库里已有这张 CA 且带信任标志 → `check()` ok、`plan()` **为空**（B-5） |
| `test_apply_does_not_touch_existing_entry` | 已信任时执行安装动作：**不报错**，且用户已有的条目**原样不动**（昵称与标志都没变） |
| `test_apply_repairs_missing_trust_flag` | 库里有这张 CA 但缺信任标志 → 装完必须有信任标志（`check()` 转绿） |
| `test_apply_installs_into_empty_database` | 空库 → 正常装上，昵称就是 `devproxy Local CA`、标志 `C,,` |

### 3.2 `tests/test_platform_linux.py`（新增 8 个用例）

| 用例 | 断言什么 |
|---|---|
| `test_linux_elevated_falls_back_to_passwordless_sudo` | `sudo -v` 失败 + `sudo -n true` 成功 → **不抛异常**；探测命令确实跑过；后续特权命令是 `sudo -n install -d …`，且**不出现** `sudo install …` |
| `test_linux_elevated_keeps_plain_sudo_when_pre_auth_succeeds` | `sudo -v` 成功 → 维持现状：`sudo <cmd>`，且不再多跑一次免密探测 |
| `test_linux_elevated_raises_when_both_sudo_probes_fail` | 两条都失败 → 抛 `PermissionError`，信息里同时有 `sudo -v` 与 `sudo -n true` 两条命令的输出 |
| `test_linux_elevated_passwordless_path_survives_a_failing_command` | 免密路径下命令失败照常返回错误码，不被当成提权失败 |
| `test_linux_nss_trusted_true_when_cert_stored_under_other_nickname` | 假 certutil 桩：换昵称入库 → 判为已信任 |
| `test_linux_nss_trusted_false_when_entry_has_no_trust_flag` | 桩：标志 `,,` → 不算已信任 |
| `test_linux_nss_entries_reports_nickname_and_flags` | 桩：`nss_entries` 给出昵称与标志（组件补标志靠它） |
| `test_linux_nss_entries_ignores_nickname_that_cannot_be_fetched` | 桩：取不到证书的候选昵称必须被跳过（宁可漏判不能误判） |
| `test_linux_nss_entries_empty_without_db` | 没有 NSS 库 → 一条 certutil 都不该跑 |
| `test_linux_nss_rows_parses_nickname_with_spaces_and_long_names` | 表格解析：含空格的昵称、超长溢出列宽的昵称都能切对 |

（原有两个 NSS 桩用例 `…_true_when_stored_cert_matches` / `…_ignores_blank_lines_and_crlf`
被改写成"表格 + `-a` 取 PEM"的真实桩形态，断言意图不变。）

### 3.3 `tests/test_platform_macos.py`（新增 3 个用例）

`test_macos_elevated_falls_back_to_passwordless_sudo`、
`test_macos_elevated_keeps_plain_sudo_when_pre_auth_succeeds`、
`test_macos_elevated_raises_when_both_sudo_probes_fail` —— 与 Linux 同名用例同一套断言。

### 3.4 `tests/test_component_certs.py`（新增 3 个用例）

| 用例 | 断言什么 |
|---|---|
| `test_apply_adds_trust_flag_to_existing_entry` | 库里有这张 CA 但缺信任标志 → 必须发 `certutil -M`，且 `-n` 是**库里那条的昵称**、`-t` 是 `C,,` |
| `test_apply_raises_when_cert_never_becomes_trusted` | 装完复查仍不是"已信任" → 必须抛 `RuntimeError`（信息含"信任标志"） |
| `test_apply_skips_nss_install_when_already_trusted` | 已信任时执行安装动作 → **一条 certutil 命令都不发** |

---

## 4. 全量测试结果

```
$ python3 -m pytest -q --ignore=tests/test_integration.py
605 passed in 14.67s
```

（修复前是 579 个用例：577 passed + 2 failed —— 失败的正是被本次改动改变了契约的两个 NSS 桩用例；
新增 26 个用例：真 certutil 9 个 + Linux 平台 10 个 + macOS 平台 3 个 + 组件 3 个 + CI 守护 1 个。
`tests/test_integration.py` 需要真实 caddy 二进制与真实网络，
按仓库约定不进 CI，未运行。）

`ruff check` 对本次改动涉及的文件无新增告警（仓库里原有的 27 处告警未动）。

---

## 5. 真机验证（改完之后在这台机器上实测）

只做了**只读**操作（`certutil -L` / `sudo -n id -u` / `devproxy doctor` / `devproxy trust`），
没有往 `/etc` 或用户家目录写任何东西。

```
$ python3 -c "…LinuxPlatform().nss_trusted(真实 CA)…"
nss_trusted(真实 CA) = True                      # 修复前是 False
nss_entries = [('Caddy Local Authority', 'C,,')]

$ python3 -c "…LinuxElevated()…"
提权会话建立成功： True                          # 修复前是 PermissionError
sudo -n id -u -> (0, '0')
```

```
$ devproxy doctor
│ cert    │ ✔ 就绪 │ CA 根证书已信任（系统信任库 + NSS 库） │  │   ← 修复前是 ✗ 缺失
```

```
$ devproxy trust
CA 根证书已信任，无需变更。                       ← 修复前会反复重装并仍然报"未完全信任"
```

`doctor` 里剩下的 `dns │ ✗ 缺失 │ DNS 路由缺失（resolved 后端）` 是**预期**的：
真机首跑正是在这一步因提权失败而中断的，那次没能把 `/etc/systemd/resolved.conf.d/devproxy.conf`
写下去。提权修好之后重新跑一次 `devproxy setup` 就能补上（这一步会写系统，
按规矩没有用户的明确授权不代替用户执行）。

---

## 6. 偏离任务书的地方（逐条说明）

1. **平台协议的方法数**：任务书说"`Platform` 协议保持 17 个方法不变"。
   实测 `src/devproxy/platform/base.py` 里其实是 **18 个**方法
   （`privilege / run / binary_version / service_active / install_service / enable_service /
   restart_service / service_unit_path / tail_service_log / enable_linger / linger_enabled /
   resolved_active / dns_query / http_ok / ca_trusted / privilege_read / nss_trusted /
   dns_route_installed`）。我**没有动 base.py**：一个方法都没加、没删、没改签名，
   新能力全部走 `getattr` 探测的非协议方法（`nss_entries`，只在 `LinuxPlatform` 上有）。
   顺带一提，`macos.py` 的 `MacOSPlatform` **也没有**实现 `nss_entries` ——
   与任务书"macOS 不实现也不影响"一致。

2. **`install-nss-ca` 增加了"已经信任就直接返回"这一步**（任务书 B-3 的第 1 步是"先删同名"）。
   理由：任务书第 3 步明确要求"库里本来就有的另一昵称 —— **都不要动用户已有的条目**"。
   若先删再 `-A`，`-A` 会把用户那条的**信任标志改写**成 `C,,`（实测：`P,,` 会被改成 `C,,`），
   与"不要动"冲突。先做"已信任就返回"，才真正做到一条 certutil 命令都不发。
   代价：工具自己昵称下若残留着**旧 CA** 的条目，这一轮不会被清理
   （那是个无害的旧根，Chrome 认它但没有任何站点用它）。
   正常流程下 `plan()` 看到 NSS 已信任就不会产生这个动作，所以这条只在"直接 apply"时才有影响。

3. **`certutil -A` 对已存在证书的实际行为与任务书的描述略有出入**（见 0.2）：
   它不是"完全静默无操作"，而是"保留原昵称、改写信任标志"。
   修法不受影响（判据仍按内容 + 标志），但这意味着任务书 B-3 里
   "用 `certutil -M` 补标志"那条兜底路径在本机几乎不会被触发 ——
   `-M` 保留为"某些 NSS 版本/情形下 `-A` 没能改标志"时的兜底，
   并且**有专门的单元测试**覆盖它（`test_apply_adds_trust_flag_to_existing_entry`）。

4. **组件层复查用的是平台自己的库路径**（`LinuxPlatform` 读 `~/.pki/nssdb`，
   与组件默认的 `nss_dir` 是同一个路径）。组件的 `nss_dir` 注入参数只是测试接缝，
   两者不一致时复查会查错库 —— 新测试里两者是同一个路径，
   代码里也写了注释说明。**没有**为此改 `nss_trusted` 的签名（协议不能动）。

5. **改了 `FakePlatform`**：让 `certutil -A` / `-D` 影响假平台的 NSS 状态（第 4 节已说明理由）。
   假平台必须照真语义变，否则"装完复查"这条新逻辑永远无法被测到。

6. **动了 `.github/workflows/ci.yml`**（加一行 `apt-get install libnss3-tools`）：
   新的 NSS 用例要"真的 certutil"才跑得起来，GitHub 的 ubuntu 镜像默认没有 `certutil`，
   不装的话那组用例在 CI 上会整组 skip —— 等于没有回归护栏。
   装了之后 CI 上这组用例**会真的执行**（库仍然建在临时目录里，不碰真实系统）。

7. **顺带删掉了 `linux.py` 里因此不再使用的 `_pem_body` / `_pem_digest`**：
   内容比对改用 `_pem_fingerprints`（DER 的 sha256），与同文件里 `ca_in_trust_store`
   的做法一致，避免留两套 PEM 归一化逻辑。

---

## 7. 遗留与提醒（不是本次任务的阻塞项）

- **免密机器 + 有 TTY 时，`sudo -v` 仍会弹一次不必要的密码框**（实测），
  用户输对密码即可（走"预热成功"的正常路径）。这是任务书指定的探测顺序带来的，
  不影响正确性，只是体验上多一次提示。
- **`LinuxPlatform.enable_linger()` 仍然直接跑 `sudo loginctl …`（不带 `-n`）**，
  没有走提权会话。在 `NOPASSWD: ALL` 的机器上它不需要密码（实测正常），
  但在"只有部分命令免密"的机器上，它仍可能弹框或失败。
  这是本次改动之前就有的路径，不属于任务书列出的两个缺陷，**未改**。
- **NSS 标志首字符 `P`（受信任的对端）按任务书算作"已信任"**。
  严格说 `P,,` 的根 CA 不是 SSL 的信任锚点（Chrome 不会拿它当 CA 用），
  但任务书明确把 `P` 归在受信任一组，而本工具自己装的是 `C,,` —— 按任务书执行。
- **`doctor` 里剩下的 `dns │ ✗ 缺失` 需要用户自己跑一次 `devproxy setup`** 才能补上
  （那一步要写 `/etc` 与重启 resolved，属于系统变更，按规矩不代替用户执行）。

---

# Fix round 1（评审后修订）

> 说明：本节的行号是 round 1 提交（`f625899`）时的位置。round 2 的改动让行号整体下移，
> 后续若有出入以函数/方法名为准。

评审独立在真 `certutil`（NSS 3.120）与 Chromium 源码上验证，给出 2 个 Important + 2 个 Minor
（Minor-7 可选），1 条明确不修（Minor-5）。本轮全部按条目处理，Minor-5 未动。

## 动手前的复核（我先自己验了一遍，再改）

**Important-1（`P` / `T` 是假阳性）—— 评审结论成立，我第一轮的测量方法是错的。**

我第一轮用 `certutil -V -u V -n <CA 自身>` 验过 `P,,`，得到 "valid"，据此认为 `P` 可用。
评审指出该结论有问题后，我改用**正确的测法**：造一张由该 CA 签发的**叶子证书**，把 CA 以
各种标志入库，再验叶子链。结果：

```
CA=C,,    leaf 链验证 rc=0    valid
CA=C,C,   leaf 链验证 rc=0    valid
CA=C,C,C  leaf 链验证 rc=0    valid
CA=T,,    leaf 链验证 rc=255  issuer has been marked as not trusted
CA=P,,    leaf 链验证 rc=255  issuer has been marked as not trusted
CA=,,     leaf 链验证 rc=255  issuer has been marked as not trusted
```

第一轮之所以看到 `P,,` "valid"，是因为那次验的是 **CA 证书自身作为对端**——
`P` 的语义正是"受信任的对端"，当然通过；但它**不是签发者锚点**，
用它签发的站点证书一律不被信任。评审的结论正确，我的旧测法无效。

**Important-2（三列标志）—— 评审结论成立，真机现场复现：**

```
$ certutil -A -n "Caddy Local Authority" -t "C,C,C" -i ca1.crt
$ certutil -L -d sql:$db | cat -A
Caddy Local Authority                                        C,C,C$     ← 行尾没有补空格

$ python -c "from devproxy.platform.linux import _nss_rows; print(_nss_rows('…C,C,C'))"
[]                                                                       ← 旧正则解析不出来

$ certutil -A -n "devproxy Local CA" -t "C,," -i ca1.crt   # rc=0
$ certutil -L -d sql:$db | cat -A
Caddy Local Authority                                        C,,  $     ← 另外两列被抹掉
```

两个 Important 都确认后开始改。

## Important-1：`NSS_TRUSTED_FLAGS` 收窄为 `("C",)`

**改了什么**

- `src/devproxy/platform/linux.py:36-54`：`NSS_TRUSTED_FLAGS = ("C",)`，并新增
  `NSS_TRUSTED_COLUMN = 0`；注释重写成"三列 = `SSL,S/MIME,JAR/XPI`，只认 SSL 列的 C"，
  写清 `C`（可签服务端证书）/ `T`（可签客户端证书，Chromium 的 `TrustStoreNSS` 根本不引用
  `CERTDB_TRUSTED_CLIENT_CA`）/ `P`（受信任叶子，不是 CA 锚点）三者的真实语义，
  并附上 `certutil -V -u V` 的实测结果作为依据。
- `src/devproxy/platform/linux.py:178-184`：`_nss_flags_trusted` 改为按列取
  （`flags[NSS_TRUSTED_COLUMN] in NSS_TRUSTED_FLAGS`），语义不变但明确了"只看第一列"。

**新增测试**

| 测试 | 断言 |
|---|---|
| `test_platform_linux.py::test_linux_nss_flags_trusted_only_accepts_ssl_column_c`（8 个参数） | `C,,`/`C,C,`/`C,C,C` → True；`T,,`/`P,,`/`,C,`/`,,`/空 → False |
| `test_nss_real_certutil.py::test_nss_trusted_false_for_trusted_peer_flag` | 真库里以 `P,,` 入库 → `nss_trusted` False，`nss_entries` 仍报出 `("Caddy Local Authority","P,,")` |
| `test_nss_real_certutil.py::test_nss_trusted_false_for_client_ca_flag` | 真库里以 `T,,` 入库 → False |
| `test_nss_real_certutil.py::test_nss_trusted_false_when_only_smime_column_trusted` | 真库里以 `,C,` 入库（先断言 certutil 真的存成 `,C,`）→ False |

## Important-2：正则能表达三列标志

**改了什么**

- `src/devproxy/platform/linux.py:63-71`：正则改为
  `^(?P<name>.*?)[ ]+(?P<flags>[A-Za-z]*(?:,[A-Za-z]*){2})$`，
  注释说明"三列、两列逗号"的形态，并记下 `C,C,C` 行尾**没有**补空格（`C,,` 有两个），
  所以必须先 rstrip。
- 保留"候选昵称必须 `certutil -L -n <昵称> -a` 实际取一次"的护栏（取不到就跳过）。
- `src/devproxy/components/certs.py:326-334`：**证书已在库里但没有 SSL 信任时不再走 `-A`**，
  直接 `-M` 补标志。原因正是本条的反面：`-A -t "C,,"` 对已存在证书会**改写整行**，
  抹掉另外两列。
- 新增 `_nss_repair_flags`（`linux.py:187-197`）+ 非协议方法
  `LinuxPlatform.nss_trust_flags`（`linux.py:676-682`）：补标志时**只把 SSL 列补成 C，
  保留 S/MIME 与 JAR 两列**（`,C,` → `C,C,`），不是一律 `C,,`。
- `certs.py:398-436` 的 `_repair_flags` 用平台给的标志串调 `-M`。

**新增测试**

| 测试 | 断言 |
|---|---|
| `test_platform_linux.py::test_linux_nss_rows_parses_every_three_column_form`（5 个参数） | `C,,`/`C,C,`/`C,C,C`/`,C,`/`,,` 都能解析 |
| `test_platform_linux.py::test_linux_nss_rows_parses_row_without_trailing_padding` | 行尾无补空格的 `C,C,C` 行能解析 |
| `test_platform_linux.py::test_linux_nss_rows_ignores_header_lines` | 两行表头不被当成条目 |
| `test_platform_linux.py::test_linux_nss_trust_flags_repairs_only_ssl_column` | `,C,`→`C,C,`、`,C,C`→`C,C,C`、非法形态退回 `C,,` |
| `test_nss_real_certutil.py::test_nss_trusted_true_for_three_column_flags` | 真库 `C,C,C` → 已信任 |
| `test_nss_real_certutil.py::test_plan_is_empty_for_three_column_flags` | `C,C,C` → `check()` ok、`plan()` 空（B-5） |
| `test_nss_real_certutil.py::test_apply_keeps_three_column_flags` | `apply()` 之后条目**仍是 `C,C,C`** |
| `test_nss_real_certutil.py::test_apply_repairs_only_ssl_column` | `,C,` 修成 `C,C,`（S/MIME 信任保留） |
| `test_component_certs.py::test_apply_repair_uses_platform_flag_helper` | `-M -t` 用的是平台给的三列标志 |

## Minor-3：早退路径清掉自有昵称下的陈旧 CA

**改了什么**

- `src/devproxy/platform/linux.py:650-674`：新增非协议方法
  `nss_stale_nickname(ca, nickname, nss_dir=None) -> bool` ——
  该昵称下有条目、且内容**不是**这张 CA。判不了（读不到 / 不是 PEM）一律 False，
  宁可漏删不能误删。
- `src/devproxy/components/certs.py:385-396`：`_drop_stale_own_entry`，
  只在"确实陈旧"时发 `certutil -D -n "devproxy Local CA"`。
- 三条返回路径都接上了：已信任早退（`certs.py:306-308`）、补标志前（`certs.py:324-330`）、
  安装前无条件删（`certs.py:336`，此时库里没有当前 CA，自有昵称下若有条目只可能是旧 CA）。

**新增测试**

| 测试 | 断言 |
|---|---|
| `test_nss_real_certutil.py::test_apply_removes_stale_entry_under_own_nickname` | 旧 CA 在自有昵称 + 当前 CA 在异昵称已信任 → 删旧根，用户那条不动 |
| `test_nss_real_certutil.py::test_apply_removes_stale_entry_while_repairing_flags` | 补标志那条路径同样清陈旧条目 |
| `test_nss_real_certutil.py::test_apply_keeps_own_entry_when_it_is_the_current_ca` | 自有昵称下就是当前 CA → 不删 |
| `test_component_certs.py::test_apply_drops_stale_own_entry_when_trusted_elsewhere` | 已信任时只发 `-D`，不发 `-A` |
| `test_platform_linux.py::test_linux_nss_stale_nickname_*`（4 个） | 陈旧 True / 同一张 False / 不存在 False / 判不了 False |

## Minor-4：复查的库必须与写库一致（fail loud）

**改了什么**

- `src/devproxy/platform/linux.py:583-589`：新增非协议方法 `nss_db_path() -> Path`。
- `src/devproxy/platform/linux.py:591-648`：`nss_entries(ca, nss_dir=None)` 增加可选库路径参数。
- `src/devproxy/components/certs.py:365-378`：`_require_same_nss_db` —— 平台提供
  `nss_db_path()` 时，与组件写入路径（`resolve()` 后）比较，不一致直接
  `RuntimeError`（信息带两个路径），**在任何写操作之前**。
- `src/devproxy/components/certs.py:75-105`：`_nss_entries` 把 `nss_dir` 传给平台；
  平台不接受这个参数时（旧实现 / 别的平台）退回无参调用。
- 平台没有 `nss_db_path()` 时（`FakePlatform`、macOS）跳过校验 —— 无从比较。

**新增测试**

| 测试 | 断言 |
|---|---|
| `test_nss_real_certutil.py::test_apply_fails_loud_when_platform_db_differs` | 两边不一致 → RuntimeError，信息含两个路径，**两个库都没被写过** |
| `test_component_certs.py::test_apply_fails_loud_when_component_nss_dir_differs` | 同上（假平台带 `nss_db_path`），且 `platform.commands == []` |
| `test_component_certs.py::test_apply_skips_when_platform_db_path_matches` | 一致时照常安装，不误报 |
| `test_platform_linux.py::test_linux_nss_db_path_points_at_user_home` | 路径正确 |
| `test_platform_linux.py::test_linux_nss_entries_accepts_explicit_nss_dir` | 所有 certutil 调用都指向显式给的库 |

## Minor-6：失败信息保留最后一次 `-M` 的输出

**改了什么**

- `src/devproxy/components/certs.py:398-436`：`_repair_flags` 记住最后一次 `-M` 的
  `(code, msg)`（失败时带返回码），安装路径再把 `-A` 的输出作为初始值传入。
  最终错误信息形如：
  `安装 NSS 证书失败：NSS 库里找不到这张 CA 的 SSL 信任标志（库中条目: [...]; certutil: (255) …）`。

**新增测试**

| 测试 | 断言 |
|---|---|
| `test_component_certs.py::test_apply_repair_error_keeps_last_modify_output` | 错误信息含 `-M` 的真实输出与库中条目（不再出现空的 `（certutil: ）`） |

## Minor-7：真正的快路径

`nss_entries`（`linux.py:591-648`）现在会先看表格里**自有昵称**那一行：
若它带信任标志且内容匹配，直接返回 —— 一次 `-L` + 一次 `-L -n … -a` 就结束，
不再逐条枚举。未命中（自有昵称不存在 / 内容不同 / 标志不可信）才走全量枚举。
实测本机真实库 5 次 `nss_trusted` 共 0.098s。

## Minor-5：**未修**（按 controller 裁定）

`enable_linger()` 的 `sudo loginctl` 保持原样，未改。

## 本轮变更文件

- `src/devproxy/platform/linux.py`：标志常量与注释、正则、`_nss_repair_flags`、
  `nss_db_path` / `nss_entries(nss_dir=)` / `nss_stale_nickname` / `nss_trust_flags`
- `src/devproxy/components/certs.py`：`_require_same_nss_db`、`_drop_stale_own_entry`、
  `_repair_flags`、安装流程重排（不再对已存在的证书用 `-A`）
- `tests/test_platform_linux.py`、`tests/test_component_certs.py`、
  `tests/test_nss_real_certutil.py`

`src/devproxy/platform/base.py` **仍未改动**（协议一个方法都没加/删/改）；
`_assert_may_mutate()` 与 `isolated_home` 夹具未动；`check()` 仍旧不抛异常
（`_nss_trusted` / `_nss_entries` / `_nss_stale` 全部 try/except 兜住）。

## 本轮测试结果

```
$ python3 -m pytest -q --ignore=tests/test_integration.py
643 passed in 17.01s
```

真机只读复验（未写任何系统文件）：

```
nss_trusted(真实 CA) = True
nss_entries = [('Caddy Local Authority', 'C,,')]
nss_db_path = /home/yewenbin/.pki/nssdb
stale own nickname = False
标志判定: C,,=True C,C,C=True T,,=False P,,=False ,C,=False
devproxy doctor → cert ✔ 就绪
devproxy trust  → CA 根证书已信任，无需变更。
```

## 本轮对任务书的偏离

上一轮第 7 节里那条"`P` 按任务书算作已信任"的说明**作废** —— 那是误读，
任务书只是罗列字母语义，并没有要求把 `T`/`P` 都算受信任。本轮按评审结论收窄为只认 `C`。
其余偏离（协议实际是 18 个方法、`install-nss-ca` 的早退、`FakePlatform` 跟随 certutil 语义、
CI 装 `libnss3-tools`）仍然成立，见上一轮第 6 节。

---

# Fix round 2（定向复审的残留）

复审确认两个 Important 已真正解决、无新增 Critical/Important。本轮收口 4 条 Minor：
2 条必收口（清理动作在生产入口不可达、`certutil -D` 失败被吞） + 2 条顺手（空转的测试、注释漂移）。

## 必收口 1：Minor-3 的清理在生产入口不可达

**问题**：上一轮把清理放在 `install-nss-ca` 的早退分支里，但那个场景
（当前 CA 已以异昵称被信任 + 自有昵称残留旧 CA）恰恰是 `plan()` 为空、
`cli.py` 与 `setup.py` **根本不调用 `apply()`** 的场合 —— 那段清理永远执行不到。

**改了什么**

- `src/devproxy/components/certs.py:223-283`（`plan()`）：新增分支 ——
  当 NSS 已信任（因而不会产出安装动作）且 `_stale_own_entry()` 为真时，
  产出一个**非特权**动作
  `Action(command=["remove-stale-nss-ca", ca, nss_dir], privileged=False)`，
  描述为"清理 NSS 库中工具昵称下的陈旧 CA 根证书"。
  位置排在系统信任库安装动作之前：用户若拒绝 sudo，清理仍能跑完（与
  "非特权步骤优先"的既有约定一致）。
- `src/devproxy/components/certs.py:345-356`（`_apply_one`）：新增
  `remove-stale-nss-ca` 分支（certutil 探测 + `_require_same_nss_db` + `_drop_stale_own_entry`）。
- `src/devproxy/components/certs.py:194-221`（`check()`）：**保持 `ok=True`**，
  但 detail 追加"；NSS 库中工具昵称下残留陈旧 CA 根（devproxy trust 可清理）"。

**为什么 `check()` 判 ok 而不是判红（这是任务书让我自己判断的点）**

`Status.ok` 在本仓库同时决定 doctor 的 ✔/✗、`report_is_healthy()` 与 doctor 的
退出码（`cli.py:704` → `EXIT_PRECONDITION`）。陈旧根不影响任何 HTTPS 握手 ——
系统信任库与 NSS 库都正常，工具的所有就绪承诺都成立；把它判成"缺失"会让
每一个曾经重新生成过 Caddy CA 的正常用户在 `doctor` 里看到红叉并拿到非零退出码，
属于把卫生问题误报成故障。但也不能装作没事：detail 里点名，且 `plan()` 会产出
清理动作 —— `devproxy setup` / `devproxy trust` 都会顺手把它做掉。
（`setup` 的 `apply_plan` 在有动作时会先 apply 再 verify，所以这条动作不会被漏执行；
若用户拒绝确认，动作不执行，doctor 的 detail 仍会把这件事说出来。）

**测试**

| 测试 | 断言 |
|---|---|
| `test_nss_real_certutil.py::test_plan_offers_stale_cleanup_when_ca_trusted_elsewhere`（真 certutil） | `check().ok is True` 且 detail 含"陈旧"；`plan()` 恰好只有一条 `remove-stale-nss-ca`（非特权、**没有**安装动作）；执行后自有昵称条目消失、用户的异昵称条目原样不动；再 `plan()` 彻底为空 |
| `test_component_certs.py::test_plan_offers_stale_cleanup_when_ca_trusted` | 同上（假平台），并断言动作非特权、command 带 CA 路径 |
| `test_component_certs.py::test_remove_stale_action_runs_certutil_delete` | 新动作确实发 `certutil -D -n "devproxy Local CA"` |

原来断言 `plan() == []` 的用例已改写（它的本意是"已信任时不需要**安装**动作"，
现在断言"没有安装动作、但有清理动作，清理后彻底为空"）。

## 必收口 2：`certutil -D` 的失败被静默吞掉

**改了什么**：`src/devproxy/components/certs.py:447-475`（`_drop_stale_own_entry`）
现在检查返回码：

- `code == 0` → 成功；
- 非 0 → 再用非协议方法 `nss_has_nickname(nickname, nss_dir=…)`
  （`src/devproxy/platform/linux.py:683-694`）确认条目**是否真的还在**：
  - 返回 False（条目确实不存在，并发下已被删/本来就没有）→ 幂等成功；
  - 返回 None（平台不提供探测能力）→ 看 certutil 输出，
    含 `could not find` / `not found` 才算"不存在"（`_nss_missing_output`，
    `certs.py:29-37`）；
  - 其余一律 `RuntimeError`，信息带 certutil 的实际输出。

两条调用点（新清理动作 + 安装路径的幂等删除）都走这个函数，因此都受保护。

**测试**

| 测试 | 断言 |
|---|---|
| `test_component_certs.py::test_remove_stale_action_raises_when_delete_fails` | 条目仍在 + `-D` 返回 255 → `RuntimeError`，信息含 `database locked` |
| `test_component_certs.py::test_remove_stale_action_tolerates_missing_entry` | 探测到条目已不存在 → 不抛异常（幂等） |
| `test_component_certs.py::test_remove_stale_action_uses_message_when_platform_cannot_probe` | 平台无 `nss_has_nickname` 时按输出判断"不存在" |
| `test_platform_linux.py::test_linux_nss_has_nickname_true_and_false` / `…_false_without_db` | 探测本身的语义 |

## 顺手 3：一条测试本轮起空转 + 快路径缺调用计数断言

- `test_platform_linux.py::test_linux_nss_entries_ignores_nickname_that_cannot_be_fetched`
  已改写：让自有昵称那条的**内容也对不上**（快路径不命中），从而真正走到枚举分支，
  断言"取不到证书的候选被跳过、其余仍被枚举出来"。
- 新增 `test_platform_linux.py::test_linux_nss_entries_fast_path_stops_after_own_nickname`：
  4 条目的库里自有昵称命中且可信 → 断言 `certutil` 只被调用 **2 次**
  （一次列表 + 一次取自有昵称的 PEM），全量枚举会是 5 次。

## 顺手 4：两处注释/契约漂移

- `src/devproxy/platform/linux.py:593-612`：`nss_entries` 的 docstring 如实写明
  "快路径下可能只含自有昵称那一条，不保证列出全部匹配项"，并说明调用方何时可以
  把它当全量用（补标志的前提本来就是自有昵称不命中）。
- `src/devproxy/platform/linux.py:157-166`：`_nss_rows` 的 docstring 更正为
  新正则下的真实原因 —— 表头 `SSL,S/MIME,JAR/XPI` 是因为列内容含 `/` 而不匹配
  `[A-Za-z]*(?:,[A-Za-z]*){2}`，不再是"没有连续两个逗号"。

## 本轮变更文件

- `src/devproxy/components/certs.py`：`plan()` 清理动作、`check()` detail、
  `remove-stale-nss-ca` 处理器、`_drop_stale_own_entry` 的返回码检查、
  `_nss_missing_output` / `_nss_has_nickname` / `_stale_own_entry`
- `src/devproxy/platform/linux.py`：新增非协议方法 `nss_has_nickname`；两处 docstring 更正
- `tests/test_component_certs.py`、`tests/test_platform_linux.py`、`tests/test_nss_real_certutil.py`

`src/devproxy/platform/base.py` 仍未改动；`check()` 仍不抛异常
（新逻辑全部经 `_nss_stale` / `_nss_has_nickname` 的 try/except 兜住）；
护栏未放宽。

## 本轮测试结果

```
$ python3 -m pytest -q --ignore=tests/test_integration.py
651 passed in 17.94s
```

真机只读复验：`doctor` 的 cert 仍为 ✔ 就绪（本机自有昵称下没有陈旧条目，
`nss_stale_nickname` 为 False，因此不会凭空多出一个清理动作）；
`devproxy trust` 仍输出"CA 根证书已信任，无需变更。"。

---

# Fix round 3（P0 阻断 + P1/P2）

复审判定不能合并：上一轮的头号目标（让陈旧 CA 清理在生产入口可达）**在生产上仍然是死的**。
本轮先自己复现，再修。

## 我自己的复现（都是动手之前、在真 certutil 上跑的）

### P0：`~` 从未展开

`NSS_DIR = Path("~/.pki/nssdb")`，pathlib 不展开波浪号。我用**与 `cli.py` 完全相同的生产构造**
（`CertComponent(platform, paths, cfg)`，不传 `nss_dir`）复现：

```
[生产构造] nss_dir = PosixPath('~/.pki/nssdb')  cert9 存在 = False
[生产构造] plan  = ['install-system-ca']            ← 没有清理动作
[注入路径] plan  = ['remove-stale-nss-ca', 'install-system-ca']   ← 有
```

注入绝对路径才有效 —— 与复审结论一致。**这是本轮的核心教训**：
我的测试全都注入了绝对路径（或桩无条件返回 True），所以整轮都没碰到这条。

### P1：读不了 ≠ 不存在

```
$ chmod 000 "$DB/cert9.db"
$ certutil -L -d sql:$DB -n "devproxy Local CA"    # rc=255
certutil: function failed: SEC_ERROR_BAD_DATABASE: security library: bad database.
$ chmod 644 "$DB/cert9.db"                          # 条目仍在
```

`-L -n` 在「条目不存在」和「库读不了」两种情况下**都**返回非零，文案完全不同。
旧实现一律 `return code == 0` → 删除失败被当成幂等成功。

### P2：`-D` 返回 0 也可能没删

```
$ chmod 500 "$DB"                                   # 目录 r-x
$ certutil -D -d sql:$DB -n "devproxy Local CA"; echo rc=$?
0                                                   # 返回成功
$ chmod 700 "$DB"; certutil -L -d sql:$DB
devproxy Local CA                                   C,,   ← 条目还在
```

复审结论成立。

## P0 修法

1. **一处展开**：`src/devproxy/components/certs.py:54-60` ——
   `self.nss_dir = (Path(nss_dir) if nss_dir else NSS_DIR).expanduser()`，
   并在注释里写清为什么必须在这里展开（不展开时 `(nss_dir/"cert9.db").exists()`
   查的是「当前工作目录下的 `~/.pki/nssdb`」）。
2. **纵深防御**：`src/devproxy/platform/linux.py` 的 `nss_entries`（:614-620）、
   `nss_stale_nickname`（:682-688）、`nss_has_nickname`（:726-732）入口都对
   `nss_dir` 做 `expanduser()` —— 平台方法可能被别处直接用。
3. **原有的 `install-nss-ca` 路径里的 `Path(action.command[2]).expanduser()` 原样保留**
   （`certs.py:375`），没有顺手去掉。

### 生产构造路径的真实验证（本轮要求说明的部分）

我不只用测试断言，**自己按生产构造跑了一遍真 certutil 端到端**：

1. 造一个假 HOME：`$HOME/.pki/nssdb` 里放两条 —— `devproxy Local CA` = 旧 CA，
   `Caddy Local Authority` = 当前 CA（`C,C,C`）；当前 CA 放在
   `$HOME/.local/share/caddy/pki/authorities/local/root.crt`（默认 storage 路径）。
2. 用**与 `cli.py` 一模一样的构造**（`LinuxPlatform()` + `CertComponent(platform, paths, cfg)`，
   不传 `nss_dir`，靠 `HOME` 定位）执行 `check()` / `plan()`：
   ```
   组件 nss_dir = /tmp/dp-real-OPZI9B/home/.pki/nssdb
   plan = [('remove-stale-nss-ca', False), ('install-system-ca', True)]
   ```
   （修复前同一脚本的 `plan` 里没有 `remove-stale-nss-ca`。）
3. 只执行那条非特权清理动作（不碰 `/etc`，不执行需要 sudo 的 install-system-ca）：
   ```
   清理后 plan = [('install-system-ca', True)]
   certutil -L → 只剩 Caddy Local Authority   C,C,C
   ```
   旧根被删掉，用户的 `C,C,C` 条目**原样未动**。
4. 回归防线：`tests/test_nss_real_certutil.py::test_production_construction_detects_stale_entry`
   把这条路径固化成测试 —— 不注入 `nss_dir`，库放在 `isolated_home/.pki/nssdb`，
   平台用默认 `Path.home()`。我**验证过它在去掉 `expanduser()` 之后会失败**
   （`assert comp.nss_dir == isolated_home / ".pki" / "nssdb"` 报错），
   加回后通过 —— 这条测试确实能挡住同样的回退。

另外真机（本机真实 HOME）只读复验：`doctor` 的 cert 仍是 ✔ 就绪，
`devproxy trust` 仍是"无需变更"（本机自有昵称下没有陈旧条目）。

## P1 修法：`nss_has_nickname` 变成三态

- `src/devproxy/platform/linux.py:710-745`：返回 `bool | None`。
  - `-L -n` 返回 0 → `True`；
  - 非零且输出**明确**说找不到（`could not find` / `PR_FILE_NOT_FOUND_ERROR` /
    `not found`，集中在新增的 `_nss_missing_cert()`，`linux.py:184-199`）→ `False`；
  - 其余非零（锁 / 权限 / `SEC_ERROR_BAD_DATABASE`）→ **`None`（判不了）**。
- `src/devproxy/components/certs.py:161-182`：`_nss_has_nickname` **原样透传三态**
  （这是本轮的一个真实 bug：原来写了 `bool(getter(...))`，把 `None` 压成了 `False`，
  等于把"判不了"重新变回"不存在" —— 被新测试抓出来后改掉）。
- `src/devproxy/components/certs.py:464-500`（`_drop_stale_own_entry`）：
  `-D` 非零时 —— `present is False` 才算幂等成功；`present is None` 且平台确有探测能力
  → **抛错**（信息说明"库可能被锁或不可读"）；平台根本没有探测能力时才退回看 certutil 文案。

## P2 修法：删除后复验 + 文案

- `src/devproxy/components/certs.py:495-500`：`-D` 返回 0 之后**再查一次**
  `_nss_stale(ca, nss)`；仍在 → `RuntimeError`（"删除未生效……库目录可能不可写"）。
- `src/devproxy/cli.py:730-734`：`trust` 的成功文案按动作类型区分 ——
  只含 `remove-stale-nss-ca` 时打印"已清理 NSS 库中的陈旧 CA 根证书"，
  不再对纯清理计划说"已安装 CA 根证书"。

## P2（便宜）：check() 警示前置

`src/devproxy/components/certs.py:228-236`：陈旧警示**前置**到 detail 开头
（doctor 表格会截断长串末尾），并放进 `Status.hint`（`devproxy trust（清理陈旧 CA 根）`），
让表格的"修复"列直接给出动作。`ok` 仍为 `True` —— 复审已同意这个判断。

## 新增/调整的测试

| 测试 | 断言 |
|---|---|
| `test_nss_real_certutil.py::test_production_construction_detects_stale_entry` | **生产构造**（不注入 nss_dir）下 `nss_dir` 已展开、`check()` 报出陈旧、`plan()` 恰好一条清理动作、执行后旧根消失且用户 `C,,`… 条目不动、再 plan 为空 |
| `test_nss_real_certutil.py::test_apply_reports_when_delete_silently_does_nothing` | 真 certutil + `chmod 500` 库目录 → `-D` 返回 0 但条目仍在 → `RuntimeError("未生效")`（root 下 skip） |
| `test_platform_linux.py::test_linux_nss_has_nickname_unknown_when_database_unreadable` | `SEC_ERROR_BAD_DATABASE` → `None`（判不了） |
| `test_component_certs.py::test_remove_stale_action_raises_when_delete_unverifiable` | 探测返回 None → 报错且带 certutil 输出 |
| `test_component_certs.py::test_remove_stale_action_raises_when_delete_reports_success_but_fails` | `-D` 返回 0 但条目仍在 → `RuntimeError("未生效")` |
| `test_component_certs.py::test_cli_trust_reports_cleanup_not_install` | 纯清理时输出"已清理"，不含"已安装" |
| `_StalePlatform` 桩（测试基础设施） | 改为**有状态**：`-D` 成功后陈旧条目消失 —— 之前无条件 True 的桩会让"删除后复验"这条逻辑永远测不到真实语义 |

## 本轮变更文件

- `src/devproxy/components/certs.py`、`src/devproxy/platform/linux.py`、`src/devproxy/cli.py`
- `tests/test_nss_real_certutil.py`、`tests/test_component_certs.py`、`tests/test_platform_linux.py`

`src/devproxy/platform/base.py` 未改动（协议 18 个方法原样）；`check()` 仍不抛异常；
测试护栏（`_assert_may_mutate()` / `isolated_home`）未放宽。

## 本轮测试结果

```
$ python3 -m pytest -q --ignore=tests/test_integration.py
657 passed in 17.62s
```

`ruff check` 对改动文件无告警。

## 三轮修复的要求原文

### fix-round-1

# Fix round 1/5 —— 评审发现

评审者独立在真 `certutil`（NSS 3.120）与 Chromium 源码上验证，给出 2 个 Important 必须修，
2 个 Minor 建议同批修。以下是 controller 分诊后的结论。

---

## Important-1（必修）：`NSS_TRUSTED_FLAGS` 收窄为只认 `C`

**位置**：`src/devproxy/platform/linux.py:41`（`NSS_TRUSTED_FLAGS`）、`155-157`（`_nss_flags_trusted`）

**问题**：现在把首字符 `C` / `T` / `P` 都算"已信任"，是**假阳性**。
评审的两路独立证据：

1. `certutil -V -u V`（SSL **Server** 链验证）实测：
   - `C,,` / `C,C,` / `C,C,C` → valid
   - `T,,` / `P,,` / `,,` / `,C,` → `Peer's certificate issuer has been marked as not trusted by the user`
2. NSS 信任字符串语义：`C` = trusted CA to issue **server** certs；`T` = trusted CA to issue **client** certs；
   `P` = trusted peer。Chromium 的 `TrustStoreNSS` 只把 `CERTDB_TRUSTED_CA`（即 `C`）当锚点，
   `CERTDB_TRUSTED_CLIENT_CA`（`T`）根本未被引用，`CERTDB_TRUSTED`（`P`）只构成"受信任叶子"、不是 CA 锚点。

本工具的 CA 是给站点证书当 issuer 的**根 CA**，Chrome 需要的是 `C`。工具自己装的也是 `C,,`。
收窄为 `("C",)` 没有任何兼容性代价。

**修法**：
- `NSS_TRUSTED_FLAGS = ("C",)`
- 更新注释，写清 `C` / `T` / `P` 三者的真实语义与"为什么只认 C"
- 补单测：`T,,`、`P,,`、`,C,` 都必须判为**未信任**
- 补真 certutil 用例：以 `P,,` 入库 → `nss_trusted` 为 False；以 `T,,` 入库 → 同样 False

> 注：任务书里写"C/T/P 的含义"只是罗列字母语义，没有要求三者都算受信任。是任务书的表述被误读了，
> 以本条为准。

---

## Important-2（必修）：`_NSS_ROW_RE` 解析不了多列信任标志

**位置**：`src/devproxy/platform/linux.py:48`（正则）、`134-152`（`_nss_rows`）

**问题**：正则 `[A-Za-z]*,{2,}[A-Za-z]*` 要求"至少两个**连续**逗号"，
只能匹配 S/MIME 列为空的三列形态（`C,,`、`P,,`）。真实 NSS 会输出 `C,C,`、`C,C,C`、`,C,` —— 全部解析失败。

**评审实测（真 NSS 3.120 库）**：
- 库里 `Caddy Local Authority` 标志为 `C,C,C` → `_nss_rows()` 返回 `[]`
  → `check()` 报未信任、`plan()` 非空 → **违反 B-5**
- 更糟：`apply()` 走 `-A -t C,,` 后，该条目被**改写成 `C,,`** —— `C,C,C` 里另外两列的信任被静默抹掉
  → **违反 B-3 的"不要动用户已有的条目"**

现有 `tests/test_nss_real_certutil.py:249` 只覆盖 `C,,`，所以没暴露。

**修法**：
- 正则改为能表达三列标志，例如 `^(?P<name>.*?)[ ]+(?P<flags>[A-Za-z]*(?:,[A-Za-z]*){2})$`
  （仍保留"候选昵称必须用 `certutil -L -n <nick> -a` 实际取一次"的护栏 —— 解析有偏差时宁可漏判不能误判）
- 补真 certutil 用例：`C,C,C` 入库 → `nss_trusted` 为 True、`plan()` 为空、
  `apply()` 之后条目**仍是 `C,C,C`**（不是被改写成 `C,,`）
- 补单测覆盖 `C,C,`、`C,C,C`、`,C,`、`,,` 四种形态的解析

---

## Minor-3（建议同批修）：早退路径要清掉工具自有昵称下的陈旧条目

**位置**：`src/devproxy/components/certs.py:252-255`

**场景**（评审已用真 certutil 复现）：`devproxy Local CA` 下残留**旧 CA**（内容不同），
当前 CA 以 `Caddy Local Authority` 被信任 → `check()` ok、`plan()` 为空 → 旧根永久留在 NSS 库。

**为什么不是"无害"**：浏览器会永久信任一个旧根。若旧 CA 私钥仍可恢复（备份、旧 storage 未清理），
持有者就能对该浏览器配置做 HTTPS 中间人。这是安全卫生问题。

**修法**：早退之前做一次**定向**清理 —— 若 `devproxy Local CA` 下有条目且其内容**不等于**当前 CA，
`certutil -D -n CA_NAME` 删掉（那个昵称归工具所有，安全）。
若当前 CA 正好在自有昵称下且已信任，直接返回，不发任何命令。
补真 certutil 用例覆盖"旧 CA 在自有昵称 + 当前 CA 在异昵称"这个组合。

---

## Minor-4（建议同批修）：组件复查的库路径必须与写库路径一致

**位置**：`certs.py:45/165`（写库用注入的 `nss_dir`）vs `certs.py:254/285/292`（复查走 `platform.nss_trusted` → `linux.py:553` 固定 `user_home/.pki/nssdb`）

**场景**（评审已复现）：平台库已信任、组件注入库为空 → `plan()` 返回 `[]`、`apply()` 对着注入库
"安装"却什么都没做，**且不报错**。生产路径当前同源，所以是潜在隐患；但"复查"正是用来兜住
`-A` 说谎的安全网，查错库等于安全网失效。

**修法**（协议 `Platform.nss_trusted(ca)` 不能动）：
- 给 `LinuxPlatform` 加**非协议**方法 `nss_db_path() -> Path`（返回 `self.user_home / ".pki" / "nssdb"`）
- `CertComponent` 在 `install-nss-ca` 里：若平台有 `nss_db_path()`，以它为准；与注入的 `nss_dir` 不一致时
  **显式报错**（fail loud），绝不静默查另一个库
- 复查走 `nss_entries(ca, nss_dir=<那个路径>)`（给 `nss_entries` 加可选 `nss_dir` 参数，默认仍是平台路径）
- 现有真 certutil 测试应当把 `LinuxPlatform(user_home=tmp)` 与组件的 `nss_dir` 指到同一个临时路径；
  若有不一致的用例，改成一致或改成断言"报错"

---

## Minor-6（顺带，便宜）：失败信息里的 certutil 输出常常是空的

**位置**：`certs.py:266-281`（`msg` 来自 `-A`）、`288-291`（`-M` 的返回码/输出被丢弃）、`294-297`

`-A` 返回 0 但复查失败时 `msg` 为空 → 报错变成 `（certutil: ）`；真正有用的 `-M` 失败输出丢失。
保留**最后一次 `-M`** 的 `(code, msg)` 与复查时 `certutil -L` 的输出，并入错误信息。

---

## Minor-7（可做可不做）：实现真正的"快路径"

现在 `rows.sort()` 只是把自有昵称排前面，仍然逐条取 PEM。30 条目的库单次 `nss_trusted` 约 0.32s，
而 `check()`/`plan()`/`apply()` 各调用多次。可先单查自有昵称（一次 `certutil -L -n CA_NICKNAME -a`，
命中且标志可信即返回），未命中再全量枚举。

---

## Minor-5（**不修**，controller 已裁定）

`enable_linger()` 直接跑 `sudo loginctl`（不带 `-n`）**不属于本次缺陷的同一根因**：
缺陷 A 是 `sudo -v`（无命令调用）不匹配 `NOPASSWD` 命令规范；`sudo loginctl ...` 是具体命令，
在 `NOPASSWD: ALL` 机器上正常（目标机器实测如此）。它只在"只有部分命令免密"的机器上出问题。

**而且不要简单地改成 `privilege().run(...)`** —— 部分免密机器上 `sudo -n true` 探测会失败，
反而把原本可用的 `loginctl` 也堵死。记为 follow-up，本轮不动。

---

## 约束（不变）

1. `Platform` 协议（`base.py`）**一个方法都不能加/删/改签名**。新能力一律走 `getattr` 探测的非协议方法。
2. `check()` 永不抛异常。
3. **不要放宽测试隔离护栏**（`_assert_may_mutate()` 与 `isolated_home` 夹具）。
4. 中文注释。
5. 全量测试绿：`python3 -m pytest -q --ignore=tests/test_integration.py`
6. 提交到当前分支，不 push。
7. 不派发子 agent。

## 报告

更新 `.superpowers/sdd/fixwave-real-machine/task-report.md`（追加"fix round 1"一节，
逐条写：改了什么、文件:行号、新增测试名与断言、有没有偏离）。

**返回给我的只要**：状态、提交 SHA、一行测试摘要、顾虑。不要重复报告正文。

### fix-round-2

# Fix round 2/5 —— 定向复审的残留

复审结论是"这一轮能收"：两个 Important 真正解决，无新增 Critical/Important。
但留下 4 条 Minor，其中 2 条 controller 判定**必须收口**（否则等于交了一个永远不会执行的修复）。

---

## 必收口 1：Minor-3 的清理在生产入口上不可达

**复审结论原文**：

> 代码本身正确（`nss_stale_nickname` 只在"条目存在且指纹不同"时返回 True，三条路径都接上），
> 但**生产入口不可达**：`test_nss_real_certutil.py:441` 自己断言了 `comp.plan() == []`，
> 而 `cli.py:712-718` 与 `setup.py:104-105` 在 plan 为空时**根本不调用 apply**。
> 评审复现的那个场景（当前 CA 在异昵称已信任、自有昵称残留旧根）永远不会进 `_apply_one` 的早退分支。

也就是说：**Minor-3 想解决的那个具体场景，正是 `plan()` 为空、`apply()` 根本不跑的场合。**
现在这段清理代码只在"需要补标志""CA 重新生成"这两条路径上才会被执行到，而那两条路径下本来就不会残留旧根。

**修法**：让 `plan()` 能产出这个清理动作。

- `CertComponent.plan()` 增加一个分支：当 CA **已被信任**（`fully_trusted()` 为 True）
  但 `platform.nss_stale_nickname(ca)` 为 True 时，产出一个**非特权**动作
  （`command=["remove-stale-nss-ca", ...]`，`privileged=False`），描述写清"清理 NSS 库中工具昵称下的陈旧 CA 根"。
- `_apply_one` 加对应处理：跑 `certutil -D -n CA_NAME`，**检查返回码**（见下条）。
- `check()` 的语义要想清楚：这属于"卫生问题"不是"未就绪"。建议 `check()` 仍返回 ok，
  但 detail 里不要谎称完全没问题 —— 或者保持 ok 并在 plan 的 action 描述里说明。
  **你自己判断哪种更不误导用户，并在报告里说明理由。**
- 补测试：构造"当前 CA 已以异昵称被信任 + 自有昵称残留旧 CA"→ `plan()` **非空**且动作是非特权清理；
  执行后自有昵称下不再有该条目，用户那条异昵称条目**原样不动**。
  原来那条断言 `plan() == []` 的用例（`test_nss_real_certutil.py:441`）需要相应调整 —— 它的本意是
  "已信任时不需要**安装**动作"，现在应当断言"没有 install 类动作，但有清理动作"。

---

## 必收口 2：`certutil -D` 的失败被静默吞掉

**位置**：`certs.py:396`

复审指出：注释声称只忽略"条目不存在"，实现是**忽略全部失败**。
失败场景：陈旧条目已确认存在，`certutil -D` 因库被锁 / 权限 / 其他原因返回非 0
→ 不检查返回码、不报错 → `apply` 仍报成功，旧的可被浏览器信任的根继续留在 NSS 库。

**修法**：检查返回码。只在"条目确实不存在"（`certutil -L -n <nick>` 失败 / 输出表明找不到）时忽略，
其余非 0 一律当失败（`RuntimeError`，信息带 certutil 输出）。两条 `-D` 调用点（新清理动作 + 原有的幂等删除）都要处理。

---

## 建议顺手（便宜）

### 3. 一条测试本轮起空转
`tests/test_platform_linux.py:703-719` 的 `test_linux_nss_entries_ignores_nickname_that_cannot_be_fetched`：
新快路径在自有昵称命中后直接返回，该用例声称的"取不到就跳过、继续枚举其余"实际没被执行。
**改成真正走到枚举分支**（自有昵称不命中 / 库中没有自有昵称条目），否则删掉并在报告里说明。
另：Minor-7 没有"只查自有昵称就返回"的调用计数断言，可补一个。

### 4. 两处注释/契约漂移
- `linux.py:594-605`：docstring 说返回"内容相同的条目"（复数），但快路径只返回自有昵称一条。
  改成如实描述（"返回内容匹配的条目；快路径下可能只含自有昵称那一条"）。
- `linux.py:158-161`：`_nss_rows` docstring 仍用"连续两个逗号"解释表头过滤，
  新正则下真实原因是 `/` 等非法字符。表头实测仍被正确过滤，只是注释过期 —— 更正措辞。

---

## 约束（不变）

1. `Platform` 协议（`base.py`）一个方法都不能加/删/改签名；新能力走非协议方法。
2. `check()` 永不抛异常。
3. 不放宽测试隔离护栏。
4. 中文注释。
5. 全量测试绿：`python3 -m pytest -q --ignore=tests/test_integration.py`
6. 提交到当前分支，不 push。
7. 不派发子 agent。

## 报告

追加 `.superpowers/sdd/fixwave-real-machine/task-report.md` 的 "Fix round 2" 一节。

**返回给我的只要**：状态、提交 SHA、一行测试摘要、顾虑。不要重复报告正文。

### fix-round-3

# Fix round 3/5 —— 复审发现 P0 + 两条 P1/P2

复审结论：**不能合并**。上一轮的头号目标（让陈旧 CA 清理在生产入口可达）**在生产上仍然不可达**，
而且 `certutil -D` 的失败处理在两种权限场景下仍会静默报成功。

---

## P0（阻断）：`~` 从未展开，陈旧判定在生产构造下恒为假

**位置**：`src/devproxy/components/certs.py:55`、`certs.py:173`、`src/devproxy/platform/linux.py:668`

**机制**：
- `CertComponent.__init__` 在未注入 `nss_dir` 时取 `NSS_DIR = Path("~/.pki/nssdb")`
  （`src/devproxy/platform/base.py:14`），**`Path("~/.pki/nssdb")` 不展开 `~`**。
- 新加的 `_stale_own_entry()`（`certs.py:169-173`）把这个**未展开**的路径传给
  `nss_stale_nickname`，而后者 `nss = Path(nss_dir)` 也没有 `expanduser()`（`linux.py:668`）。
- 于是 `(nss / "cert9.db").exists()` 查的是「**当前工作目录下名为 `~` 的目录**」，恒为 False。
- 结果：`check()`（`certs.py:217`）与 `plan()`（`certs.py:254`）在生产构造下永远认为没有陈旧条目。
  `cli.py:183/402/711` 三处生产构造都没传 `nss_dir`。

复审在 `/tmp` 用真 certutil 复现：同一份库，
- 生产方式 `CertComponent(platform, paths, cfg)` 构造 → `check()` 详情干净、`plan() == []`
- 注入绝对路径 `nss_dir=db` → `check()` 出现「陈旧」、`plan()` 产出 `remove-stale-nss-ca`

**为什么之前没抓到**：测试全都绕过了这一点 ——
`tests/test_nss_real_certutil.py:222/438` 注入了绝对路径，
`tests/test_component_certs.py:545` 的 `Stale` 桩无条件返回 True（连路径都不看）。

> 注意：原有的 `install-nss-ca` 处理路径里有 `Path(action.command[2]).expanduser()`，所以安装从来没问题 ——
> 只有这次新加的路径漏了。修的时候**不要**顺手把已有的展开去掉。

**修法**：
1. 在**一处**把 `~` 展开，别在多处各展开一次。建议在 `CertComponent.__init__` 里
   `self.nss_dir = (Path(nss_dir) if nss_dir else NSS_DIR).expanduser()`，这样组件内部任何消费方都拿到绝对路径。
2. 同时在 `nss_stale_nickname` / `nss_has_nickname` / `nss_entries` 等平台方法入口也做 `expanduser()`
   作为纵深防御（平台方法可能被别处直接用）。
3. **补一条走生产构造路径的测试**：不注入 `nss_dir`，把临时 NSS 库放在 `LinuxPlatform(user_home=tmp)` 的
   `.pki/nssdb` 下，用 `CertComponent(platform, paths, cfg)` 构造，
   断言 `check()` 能报出陈旧、`plan()` 产出 `remove-stale-nss-ca`。
   这条测试是本轮的**验收核心** —— 没有它，同样的问题还会再犯。

---

## P1：`nss_has_nickname` 把「读不了」误判成「不存在」

**位置**：`src/devproxy/platform/linux.py:683-693`、`src/devproxy/components/certs.py:466-469`

`nss_has_nickname` 用 `certutil -L -n <昵称>` 的返回码判存在，**非零一律返回 False**。
但库被锁（另一个进程持有 sqlite 排他锁）或不可读时 `-L` 同样非零 —— 复审实测 `chmod 000 cert9.db`
或持锁时 `-L -n` 返回 255 而条目仍在。

后果：`certs.py:467` 判 `present is False` → 把删除失败当幂等成功吞掉。这正是任务书点名的「库被锁/权限」场景。

**修法**：让平台方法能区分三态（存在 / 不存在 / 判不了）。
- 只有 certutil 输出**明确**表明找不到（如 `could not find` / `not found` / `PR_FILE_NOT_FOUND_ERROR`），才算「不存在」。
- 其余非零（锁、权限、DB 损坏）算「判不了」。
- 「判不了」在调用方必须 **fail loud**（抛错），不能当成功。
- `tests/test_platform_linux.py:906` 那条把「任何非零 = 不存在」固化下来的用例要相应改掉。

---

## P2：删除后没有复验，`-D` 返回 0 也可能没删

**位置**：`src/devproxy/components/certs.py:461-465`

复审做了两轮独立实验确认：NSS 库**目录只读**时（例如历史上被 root 运行过），
`certutil -D` 返回 **0 但并未删除**。当前实现没有删除后复验 → `apply` 报成功、旧根仍在，
`trust` 还会打印「已安装 CA 根证书」（`cli.py:729` —— 对纯清理计划这句文案本身也不准确）。

**修法**：
- `-D` 之后**复验**：再查一次该昵称下是否还存在内容匹配的陈旧条目；仍在 → 抛错（信息说明"删除未生效"）。
- `cli.py:729` 的 `trust` 成功文案要能区分「安装」与「仅清理」，别在只做清理时说"已安装"。

---

## P2（顺带，便宜）：`check()` 详情里的陈旧警示位置

复审同意 `check().ok` 保持 `True`（卫生问题不该让 doctor 变红、不该触发 `EXIT_PRECONDITION`）。
但保留意见：警示放在长字符串**末尾**，`doctor` 表格可能截断。

**修法**：把警示**前置**，或放进 `Status.hint`（`doctor` 的「修复」列会直接显示 `devproxy trust`）。

---

## 已确认无需改动的（不要动）

- `remove-stale-nss-ca` 的 `privileged=False`、按命令名分派、`setup.needs_sudo` 的交互 —— 复审核查无失配。
- 误删风险：判据是 DER SHA-256 内容比对，读不到/非 PEM 一律 False；
  NSS 实测同一昵称不接受第二张证书（`SEC_ERROR_ADDING_CERT`），不会误删用户异昵称条目。保持现状。

---

## 约束（不变）

1. `Platform` 协议（`base.py`）一个方法都不能加/删/改签名；新能力走非协议方法。
2. `check()` 永不抛异常。
3. 不放宽测试隔离护栏。
4. 中文注释。
5. 全量测试绿：`python3 -m pytest -q --ignore=tests/test_integration.py`
6. 提交到当前分支，不 push。
7. 不派发子 agent。

## 报告

追加 `.superpowers/sdd/fixwave-real-machine/task-report.md` 的 "Fix round 3" 一节。
**特别地**：说明你**自己**是否在"生产构造路径"上真实验证过（不是注入路径），以及怎么验证的。

**返回给我的只要**：状态、提交 SHA、一行测试摘要、顾虑。
