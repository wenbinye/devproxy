# 真机首跑报告（2026-10-23）

三个开发计划（core-gateway / bootstrap / migrate-and-macos）实现、评审、合并后，
**第一次在真机上完整跑 `devproxy setup`**。本报告记录实测到的状态、暴露的缺陷与根因。

- 基线：`master` = `05107ba`
- 机器：Linux（ThinkPad P16s，Ubuntu，systemd-resolved + 用户级 dnsmasq）
- 触发人：controller（用户批准后执行）

---

## 一、迁移前的机器实况（实测，非推测）

| 项 | 实测值 |
|---|---|
| 现役 caddy 单元 | `~/.config/systemd/user/caddy.service`（手工写的），`ExecStart=… --config ~/work/tasks/local-https/Caddyfile` |
| 遗留 Caddyfile 的站点块 | `http://localhost`、`localhost`、`*.local.yikuaigao.com`、`*.test`（on-demand）、`http://*.test` |
| 现网路由（admin 2019） | 4 条：`localhost`、`mcp-gateway.test`、`*.local.yikuaigao.com`、`*.test` |
| 运行时注册的路由 | **只有 `mcp-gateway.test`** → `127.0.0.1:54727`（`mcp-oauth-gatew` 进程在跑） |
| 存储 | `~/.local/share/caddy`（与工具默认一致 → **CA 连续性可保持**） |
| caddy 二进制 | `~/.local/bin/caddy` v2.11.7（与工具锁定版本一致），已带 `cap_net_bind_service=ep` |
| dnsmasq | **用户级** `~/.config/systemd/user/dnsmasq.service`，监听 `127.0.0.1:5353`，配置 `~/.config/dnsmasq/dnsmasq.conf` |
| resolved | active；drop-in `/etc/systemd/resolved.conf.d/dnsmasq.conf`：`DNS=127.0.0.1:5353` + `Domains=~test ~localhost ~local ~develop ~local.yikuaigao.com ~ywb.yikuaigao.com` |
| sudo | `(ALL : ALL) ALL` + `(ALL) NOPASSWD: ALL` |
| linger | `Linger=yes` |

> **修正此前报告里的一个说法**：`codeup-api.test` / `codeup-web.test` 在首跑时**已经不在现网**
> （现网只剩 4 条路由），对应的 uvicorn / vite 后端进程也已不存在。
> 「两条后端现在正在运行」的说法在首跑前就已过期。

---

## 二、迁移结果（成功）

`devproxy setup --yes` 的迁移阶段全部按预期完成：

- 备份目录：`~/.local/state/devproxy/backups/2026-10-23T02-48-32Z`
- 丢弃 `*.test` / `http://*.test` 两个通配块（工具接管），并在输出中点名
- 个人站点 `http://localhost` / `localhost` / `*.local.yikuaigao.com` 搬进 `~/.config/devproxy/sites.caddy`
- 单元改写为指向 `~/.config/devproxy/Caddyfile`，**保留** `Environment=MINIKUBE_IP=192.168.49.2`，
  并留档到 `~/.local/state/devproxy/caddy-env.json`
- storage 沿用 `~/.local/share/caddy` → **CA 未重新生成，浏览器信任与已有证书全部有效**（实测 `curl` 不加 `-k` 通过）

---

## 三、暴露的两个缺陷（真机首跑才出现，FakePlatform 测不到）

### 缺陷 A：NOPASSWD 机器上 `sudo -v` 非交互失败 → 全部提权步骤瘫痪

**现象**：`✗ dns: 提权失败：sudo: A terminal is required to authenticate`

**根因（实测）**：

```
$ sudo -n true                # rc=0 ✔
$ sudo -v
sudo: A terminal is required to authenticate   # rc=1
```

`sudo -v` 是"无命令"调用，不匹配 `NOPASSWD: ALL` 的命令规范 → 落到 `(ALL : ALL) ALL`
→ 需要认证 → 无 TTY → 失败。

**代码位置**：`src/devproxy/platform/linux.py` `LinuxElevated._authenticate()`
—— `sudo -v` 失败即 `raise PermissionError`，这台机器上**根本不需要密码**却全线失败。

**修法**：`sudo -v` 失败不立即抛；改用 `sudo -n true` 探测免密能力，成功则后续命令一律带 `-n`
（绝不退化成会弹密码框的 `sudo <cmd>`）；两者都失败才抛，错误信息同时含两条命令的输出。
`macos.py` 的 `MacOSElevated` 同款缺陷一并修。

### 缺陷 B：NSS 库按 (issuer, serial) 去重 → CA 永远装不上、永远查不到

**现象**：`✗ cert: CA 仍未被完全信任`

**根因（实测）**：本机 NSS 库里**已经有**这张 CA（旧 `devp` 装的，昵称 `Caddy Local Authority`，标志 `C,,`），
其 issuer/serial 与工具要装的完全一致：

```
issuer=CN=Caddy Local Authority - 2026 ECC Root
serial=EF0F89AAD54544C28010F4E0CA6C3B5F
```

往同一个库里加**同一张**证书 → **静默无操作，rc=0，条目不出现**：

```
$ certutil -d sql:$HOME/.pki/nssdb -A -n "devproxy Local CA" -t "C,," -i root.crt
rc=0   输出: []
$ certutil -L -d sql:$HOME/.pki/nssdb | grep -c "devproxy Local CA"
0
```

对照实验：往同一个库加一张**不同**的证书 → 正常加入；往**全新**库加这张 CA → 正常加入。
→ **NSS 按 (issuer, serial) 去重，证书已存在时 `certutil -A` 静默失败（返回 0）。**

**代码位置**：
- `src/devproxy/platform/linux.py` `nss_trusted()` 只按**固定昵称** `devproxy Local CA` 查 → 库里同证书异昵称时永远 False
- `src/devproxy/components/certs.py` 的 `install-nss-ca` 动作只信 `-A` 的返回码 → 静默失败被当成成功

**功能上其实没坏**：系统信任库有这张 CA（curl/Go/Java 认），NSS 库也有（`C,,`，Chrome 认）。
**坏的是工具认不出**：doctor 永远红、`devproxy trust` 永远失败、`setup` 永远退 1。

**修法**：
1. 判定改为**按证书内容**枚举比对（而非按昵称），并**同时检查信任标志**（`C,,` / `T,,` / `P,,` 才算已信任）。
2. 安装动作必须**复查**：`certutil -A` 之后用按内容判定确认真的被信任；
   库中已有同证书但缺 SSL 信任标志 → `certutil -M -t "C,,"` 补标志。
   **绝不改动用户已有的条目**（不删、不改名）。
3. `certutil -L` 的输出要可靠解析（昵称含空格、列宽 55、超长时溢出、标志串至少两个逗号），
   解析出的候选昵称一律用 `-n` 实际取一次验证。

---

## 四、真机端到端验证（已通过的部分）

| 检查 | 结果 |
|---|---|
| 迁移 | ✅ 单元、Caddyfile、sites.caddy、env 留档、storage 连续性全部正确 |
| `devproxy up demo -- sh -c 'python3 -m http.server $PORT'` | ✅ 分配端口 55189、注入 `$PORT`、注册路由、`https://demo.test/` → 200 |
| `devproxy down demo` | ✅ 注销路由、收掉子进程组；之后 `https://demo.test/` → 000（无路由） |
| 未注册域名 | ✅ 握手失败、拿不到任何证书（安全属性成立） |
| `devproxy expose mcp-gateway --port 54727` | ✅ 迁移掉线的运行时路由用新工具重新注册，`https://mcp-gateway.test/` → 404（上游自身响应，与迁移前基线一致） |
| `devproxy ls` | ✅ 表格输出，`● reachable` |
| 证书信任链 | ✅ `curl` **不加 `-k`** 也通过 → CA 连续性保持 |
| `devproxy doctor` | ✅ 正确指出：主 Caddyfile 已就绪、遗留 Caddyfile 仍含通配块（迁移前）、DNS 路由缺失、CA 未被信任（误报，待缺陷 B 修复） |

---

## 五、手工备份（工具自身备份之外的第二层）

`~/.local/state/devproxy/preflight-20261023/`：

- `caddy.service`（迁移前的单元）
- `dnsmasq.service`、`dnsmasq.conf`（用户级 dnsmasq）
- `Caddyfile`（遗留配置）
- `resolved-dnsmasq.conf`（现有 resolved drop-in）
- `ca-certificates/`（信任库目录快照）
- `nss-before.txt`、`ca-list.txt`、`routes-before.json`

另有工具自己的备份：`~/.local/state/devproxy/backups/2026-10-23T02-48-32Z`
（回滚命令：`devproxy setup --rollback <该目录>`）。

---

## 六、由此产生的待办

1. **旧 `devp` 与新工具并存是危险的**：`devp` 通过 Admin API 直接 PATCH 现网路由，
   注册的路由不在 `routes.json` 里 → 下一次 `devproxy up/expose` 的 reload 就会把它冲掉，
   且 `devproxy ls` 看不到它。处置前必须先把 `codeup-manager` 的 `scripts/devp_dev.sh`
   与 `Justfile` 改成 `devproxy`。
2. `devp ls` 输出是纯文本行（`<name>.test <host:port>`），而 `devproxy ls` 是 Rich 表格 →
   旧脚本的 awk 解析不成立 → 需要给 `devproxy ls` 加机器可读输出（`--json`）。
3. `docs/verification-checklist.md` 的 D1–D8 需要按本次实测回填结果。
