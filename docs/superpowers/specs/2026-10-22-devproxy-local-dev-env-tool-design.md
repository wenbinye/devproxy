# devproxy — 本地开发环境一键部署与域名网关 设计文档

- 日期：2026-10-22
- 状态：设计已确认，待实施
- 落点：`local-https/` 仓库（重构为 `devproxy` Python 包）

---

## 1. 背景与目标

本地多项目并行开发时，每个项目的 web 服务各有固定端口，存在两个痛点：

1. **端口冲突** —— 多个项目抢同一端口（3000、8000 等）
2. **记忆负担** —— 要记住哪个服务在哪个端口

加上本地开发环境的搭建本身也是负担：装 Caddy、授权 80/443、配 DNS、装证书信任、手写 systemd 单元——目前全部靠 `Justfile` 和手工配置完成，换一台机器要重来一遍。

**目标形态**：

```bash
uv tool install devproxy     # 装工具
devproxy setup               # 一条命令拉起本地开发环境
devproxy up codeup -- sh -c 'npx vite --port $PORT'
# → https://codeup.test
```

开发者只记域名，不记端口；换机器只跑一条 `setup`。

### 1.1 范围

**做**：

- `uv tool install` 可分发的 CLI 工具（Python ≥ 3.11）
- 幂等的 `setup`：部署 Caddy、dnsmasq、DNS 路由、证书信任、systemd/launchd 自启
- 动态端口映射反代：包装启动（`up`）与给已有服务开域名（`expose`）
- 通过应用域名访问本地服务（`https://<name>.test`）
- Linux + macOS 双平台

**不做**（YAGNI，明确排除）：

- 常驻守护进程（`daemon`）—— 路由文件化后不再需要
- 多上游 / 多域名映射到同一服务（1:1 路由模型）
- 容器化 / 远程开发环境
- 通配符域名证书（见 §3 的 D5）
- 自动修改用户已有的 Caddyfile 以外的任何用户配置

### 1.2 现状盘点（迁移前）

| 组件 | 现状 | 归属 |
|---|---|---|
| Caddy | `~/.local/bin/caddy` + systemd user unit + `setcap` 授权 80/443 | `Justfile` |
| DNS | 用户级 dnsmasq 监听 `127.0.0.1:5353`，systemd-resolved 路由 `~test` | 手工配置 |
| 动态代理 | `devp` 单文件 stdlib CLI（536 行）：分配端口 → 注入 `PORT` → Admin API 注册 → 心跳/注销 | `devp` 脚本 |
| 证书 | Caddy 内部 CA，on-demand 逐域名签发 | `Justfile` |
| 测试 | `tests/test_devp.py`（967 行） | pytest |

---

## 2. 关键决策

| # | 决策点 | 结论 | 理由 |
|---|---|---|---|
| D1 | 工具职责边界 | 全栈自举：`setup` 接管基础设施，`doctor` 复用同一套检查 | "快速部署"是核心目标；分开做等于用户还要自己拼 |
| D2 | DNS 后端 | 双后端自动探测：`resolved`（默认）/ `system`，`--dns-backend` 可强制 | 单一后端在非 resolved 环境直接不可用 |
| D3 | 映射能力 | `up`（包启动 + 动态端口）+ `expose`（已有服务 + 固定端口） | 两种都是高频场景，路由模型共用 |
| D4 | 目标平台 | Linux + macOS 双平台，内部做平台分层 | 强制把平台差异逼进抽象层，避免散落在代码里 |
| D5 | TLS 策略 | **精确站点块 + `tls internal`，不用 on-demand，不用通配 `*.test`** | 已实测：通配块与精确块共存会导致握手失败；精确块方案下未注册域名拿不到证书（安全面收紧） |
| D6 | CLI 框架 | Typer + Rich | 用户可见的工具，`setup`/`doctor` 的输出可读性直接影响体验 |
| D7 | 包名 | `devproxy`（PyPI 包名与 CLI 命令一致） | 自解释、PyPI 可用、覆盖核心动词 |
| D8 | Caddyfile 边界 | 工具只管自己生成的那段；个人站点留在 `sites.caddy` 被 import | 工具不接管用户全部 Caddy 配置 |
| D9 | 路由生命周期 | 文件持久化 + 无守护进程 | 路由是 Caddy 配置的一部分，重启/重载都不丢 |
| D10 | 路由存储 | `routes.json`（真相）+ `routes.caddy`（渲染产物） | `ls` 需要结构化信息（模式/PID/命令），不靠解析文本 |
| D11 | `up` 自检 | 保留 30s 轻量自检 | 兜住"Caddy 未加载我们的 import"与"有人手改产物" |
| D12 | sudo 提权 | 集中在 APPLY 阶段统一提权 | 用户只被打断一次，失败时状态可控 |
| D13 | CA 存储 | 复用 `~/.local/share/caddy` | CA 不变 → 浏览器/curl/NSS 信任状态全部保留，Chrome 无需重启 |
| D14 | 迁移触发 | `setup` 内联迁移（检测到遗留布局 → 打印计划 → 确认 → 执行 → 备份） | 不额外增加命令，升级路径短 |
| D15 | Justfile | 保留为转发壳，标注 deprecated | 保留肌肉记忆，逻辑全部收敛到工具 |
| D16 | 服务单元命名 | 沿用 `caddy.service` / `dnsmasq.service` | 现有 systemctl 习惯不变，迁移只改 `ExecStart` |
| D17 | 日志后端 | Linux 走 journald（保持现状），macOS 写文件 | 切换到文件会让 Linux 丢失 journald 的结构化能力 |
| D18 | 端口分配 | `bind(':0')` 探测后释放（沿用现有实现） | 竞态窗口极小，实现简单 |
| D19 | Caddy 下载源 | GitHub release 固定版本 + **源码内置 sha256** 校验 | 上游 `caddy_2.11.7_checksums.txt` 与实际托管的 tar.gz/deb 均不一致（实测），不能信任该文件；改为内置观测到的哈希（TOFU 于实现时），并额外校验解包后 `caddy version` |
| D20 | 迁移备份 | 迁移前强制备份所有将被修改的配置文件到 `~/.local/state/devproxy/backups/<UTC时间戳>/`，附 `MANIFEST.txt` | 迁移动的是正在用的服务配置；备份失败则整个迁移中止 |

### 2.1 已实测的技术前提

以下结论均由真实 Caddy 实例验证（隔离端口 + 隔离 storage，不碰生产 443/2019）：

| # | 假设 | 结果 |
|---|---|---|
| S1 | 精确域名 + `tls internal` 能通过客户端主机名校验 | ✅ SAN `DNS:codeup.test`，curl `tls=0` |
| S2 | 精确块与 `*.test` on-demand 通配块共存 | ❌ Caddy 合并为同一 server 后策略冲突，握手 `internal error` |
| S3 | 仅精确块时，未注册域名不会被签发证书 | ✅ `evil.test` 握手失败，证书清单只有已注册域名 |
| S4 | 精确块与异后缀通配块（`*.local.yikuaigao.com`）共存 | ✅ 四项测试全通，零 error |
| S5 | 多个 `import` 片段可组合（被 import 文件不含全局块） | ✅ |

**S2 是 D5 的依据**，S3 是 D5 的额外收益（未注册域名无法获得被信任的证书）。

### 2.2 Caddy 发布包观测哈希（v2.11.7）

上游 `caddy_2.11.7_checksums.txt` 与 GitHub 实际托管的资产**均不一致**（tar.gz 与 deb 都对不上，说明上游在校验和文件生成后重打了包）。因此**不信任该文件**，改为在源码中内置实测哈希：

| 资产 | sha256 |
|---|---|
| `caddy_2.11.7_linux_amd64.tar.gz` | `727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b` |
| `caddy_2.11.7_linux_arm64.tar.gz` | `d8fc6d179a5d283028a472a5618564f6ad8a86fed513e64f032b3b0b7cc45e42` |
| `caddy_2.11.7_mac_arm64.tar.gz` | `cda3030e5d5b13eb9f0b6fb541037f633bddd958c070c5f561d7a5cccb581665` |
| `caddy_2.11.7_mac_amd64.tar.gz` | `110c53e5c093e4cfce6eda787f94f23bef913c309f620fab26b4f678031841ad` |

资产命名规则：`caddy_{version}_{os}_{arch}.tar.gz`，其中 `os ∈ {linux, mac}`（**macOS 是 `mac` 不是 `darwin`**）、`arch ∈ {amd64, arm64}`。tarball 内含 `caddy`、`LICENSE`、`README.md` 三个文件，二进制在根目录。

下载源：`https://github.com/caddyserver/caddy/releases/download/v{version}/{asset}`。校验分两层：内置 sha256 比对 + 解包后 `caddy version` 输出的版本号比对。

---

## 3. 架构

### 3.1 三条数据流

```
控制面  devproxy ──HTTP──▶ Caddy Admin API (127.0.0.1:2019)
         (validate / reload)         ▲
                                    │
数据面  浏览器 ──DNS──▶ resolved ──▶ Caddy :443 ──▶ 127.0.0.1:$PORT
                                     (TLS internal)
                                     ▲
状态面  routes.caddy ◀── 工具生成，Caddy 用 import 加载
         ↑ 真相在这个文件里，不在内存
```

**状态面是本次重构的核心变化**。迁移前路由只活在 Caddy 内存里，靠 `devp` 的 30s 心跳兜底；改成文件后路由是 Caddy 配置的一部分，Caddy 重启、机器重启、工具进程退出都不影响。

### 3.2 三层职责

```
┌─ CLI 层 (cli.py) ─────────────────────────────────────┐
│  setup / doctor / up / expose / down / ls / logs /     │
│  trust / config / selftest                             │
│  只做参数解析与编排，不含系统逻辑                        │
└──────────────────────────┬───────────────────────────────┘
                           │
┌─ 组件层 (components/) ── 统一 check/install/verify ──┐
│  CaddyComponent   DnsmasqComponent   DnsComponent      │
│  CertComponent    ServiceComponent                      │
│  RouteTable  ← 唯一的"状态持有者"                       │
└──────────────────────────┬───────────────────────────────┘
                           │
┌─ 平台层 (platform/) ─────────────────────────────────┐
│  linux.py: systemd user unit / setcap / update-ca / NSS │
│  macos.py: launchd plist / security add-trusted-cert   │
└────────────────────────────────────────────────────────┘
```

平台层只暴露**能力**（"装一个 user service"、"把证书加进信任库"、"授权绑定低位端口"、"写 DNS 路由"），组件层用这些能力拼装出具体组件。macOS 支持只需新增 `platform/macos.py`，组件层基本不动。

**组件统一接口**：

```python
class Component(Protocol):
    def check(self) -> Status: ...     # ok / missing / broken + detail，永不抛异常
    def install(self) -> None: ...     # 幂等；需要提权的步骤登记到 SudoPlan
    def verify(self) -> Status: ...    # install 后自检
```

`check()` 永不抛异常是硬约束 —— `doctor` 要一次性列出所有组件的问题。

### 3.3 目录布局（安装后）

```
~/.local/bin/caddy                        工具安装的 Caddy 二进制

~/.config/devproxy/
├── config.toml            用户配置（可选）
├── Caddyfile              工具生成的主配置（唯一全局块在这）
└── sites.caddy            用户的个人站点 —— 工具只 import，不读不写

~/.local/state/devproxy/
├── routes.json            路由真相
├── routes.caddy           渲染产物
├── routes.lock            flock
└── logs/caddy.log         Caddy 日志（仅 macOS 使用）

~/.local/share/caddy/      复用：Caddy storage（CA 根证书 + 已签发证书）
~/.pki/nssdb/              复用：NSS 库（Chrome 信任）
~/.config/systemd/user/caddy.service     服务单元（用户级，沿用现有名字）
~/.config/systemd/user/dnsmasq.service    服务单元（用户级，沿用现有名字）
```

---

## 4. 命令面

```
devproxy setup     [--dns-backend auto|resolved|system] [--domain-suffix .test] [--no-trust] [--yes]
devproxy doctor
devproxy up        <name> [--port N] [--ready-timeout S] -- CMD...
devproxy expose    <name> --port N
devproxy down      <name>
devproxy ls
devproxy logs      [caddy|dnsmasq] [-f]
devproxy trust     [--scope system|nss|all]
devproxy config    [routes]
devproxy selftest
```

约定：

- `setup` 与 `doctor` 共用同一套 `check()` —— `doctor` 的输出就是 `setup` 的执行计划
- `up` 保持前台（子进程退出即注销路由）；`expose` 是一次性的（注册完立即返回，路由常驻直到 `down`）
- `up` 与 `expose` 的差别只有两点：谁负责上游（子进程 vs 已有服务）、是否需要端口注入。路由表结构完全一样
- **没有 `daemon` 子命令** —— 路由文件化后常驻进程不再必要

---

## 5. 路由模型

### 5.1 存储

```
~/.local/state/devproxy/
├── routes.json     ← 结构化真相（工具读写）
├── routes.caddy    ← 渲染产物（Caddy import 这个）
└── routes.lock     ← flock，并发写保护
```

`render(routes.json) → routes.caddy` 是纯函数。工具只读 json、从不解析 caddy 文件。

**写入顺序**（保证两者永不脱节）：

```
1. flock(routes.lock)
2. 内存里改 routes.json
3. render → 新 routes.caddy 内容
4. caddy validate --config <Caddyfile>      ← 用新内容试跑
5. 通过 → 原子写 routes.json + routes.caddy（tmp + os.replace）
6. caddy reload --config <Caddyfile>
7. reload 失败 → 回滚两份文件到旧内容，报错
8. unlock
```

第 4 步是关键：**先验证再落盘**。渲染出语法错误的 Caddyfile 时，不应让用户的 Caddy 挂掉。

### 5.2 路由表结构

```json
{
  "version": 1,
  "domain_suffix": "test",
  "routes": [
    {
      "name": "codeup",
      "domain": "codeup.test",
      "port": 42133,
      "mode": "up",
      "upstream": "127.0.0.1:42133",
      "command": ["sh", "-c", "npx vite --port $PORT"],
      "pid": 12345,
      "created_at": "2026-10-22T10:00:00Z"
    },
    {
      "name": "legacy",
      "domain": "legacy.test",
      "port": 3000,
      "mode": "expose",
      "upstream": "127.0.0.1:3000",
      "created_at": "2026-10-22T10:00:00Z"
    }
  ]
}
```

`mode: "up"` 才有的字段：`command`、`pid`。

**记 PID 是对现有实现的改进** —— 迁移前 `devp` 被 `kill -9` 后路由残留且无人知道该杀谁（README 记为"已知边界"）。记了 PID 后 `devproxy down <name>` 能顺手收掉残留的进程组。

### 5.3 渲染规则

```caddyfile
# 由 devproxy 自动生成 —— 请勿手工编辑

codeup.test {
	tls internal
	reverse_proxy 127.0.0.1:42133
}

legacy.test {
	tls internal
	reverse_proxy 127.0.0.1:3000
}
```

**渲染必须是确定性的**（不含时间戳等易变内容）—— 同样的路由表必须渲染出逐字节相同的文本，否则幂等性无法验证、diff 噪音也无法消除。

**一个路由 = 一个精确站点块**。这是 D5 的落地形态。

工具生成的主 Caddyfile（`~/.config/devproxy/Caddyfile`）：

```caddyfile
{
	admin 127.0.0.1:2019
	storage file_system /home/yewenbin/.local/share/caddy
}

# 工具生成的动态路由
import /home/yewenbin/.local/state/devproxy/routes.caddy

# 你自己的个人站点（工具不管内容）
import /home/yewenbin/.config/devproxy/sites.caddy
```

### 5.4 子命令流程

**`up`** —— 唯一保留"长跑"语义的命令：

```
devproxy up codeup -- sh -c 'npx vite --port $PORT'
│
├─ 校验 name（^[a-z0-9-]{1,63}$）；同名已存在 → 先 down 旧的（会终止其子进程组）
├─ 分配端口：给了 --port N 则校验 N 未被占用（占用则报错退出）；否则 bind(':0') 探测后释放
├─ 起子进程：注入 PORT，start_new_session=True（独立进程组）
│            stdout/stderr 直透终端
├─ 就绪轮询 127.0.0.1:<port>，默认 60s 超时
│    超时 → 杀整个进程组 → 不注册路由 → 退出码 1
├─ 注册路由（§5.1 的 8 步）
├─ 打印 ✅ https://codeup.test
└─ 守护
     子进程退出 → 注销路由 → 以子进程退出码退出
     SIGINT/SIGTERM/SIGHUP → 先转发给子进程组 → 注销路由 → 退出
     每 30s 轻量自检：查 Admin API 里路由是否还在，不在则重新注册
```

30s 自检的价值从"补内存态的洞"变成了"兜住 Caddy 未加载我们的 import"与"有人手改渲染产物"两种场景（D11）。

**`expose`** —— 一次性，注册完就返回：

```
devproxy expose legacy --port 3000
├─ 校验 name / 端口（上游固定为 127.0.0.1，不支持远程主机）
├─ 探测上游可达性
│    不可达 → 警告"127.0.0.1:3000 当前无响应"，但仍注册
├─ 注册路由（同 up 的注册步骤）
└─ 打印 ✅ https://legacy.test + 提示 down 可移除
```

**`down`** —— 清理一条路由：

```
devproxy down codeup
├─ 从 routes.json 移除 → 渲染 → reload
├─ 若 mode=up 且记录的 pid 还活着 → SIGTERM 进程组（pid == pgid）
└─ 打印已清理
```

**`ls`** 输出：

```
NAME     DOMAIN          PORT    MODE     UPSTREAM         STATUS
codeup   codeup.test     42133   up       127.0.0.1:42133  ● reachable
legacy   legacy.test     3000    expose   127.0.0.1:3000   ● reachable
```

### 5.5 并发

多个 `devproxy up` 同时跑是常见场景。**持锁范围覆盖"改 json → 渲染 → validate → 落盘 → reload"整段**，否则两个进程会各自基于旧快照改写、后写的覆盖先写的。`reload` 本身是 Caddy 的原子操作（`POST /load`），并发 reload 安全。

---

## 6. setup 流程

### 6.1 执行模型：check → plan → apply → verify

```
devproxy setup
│
├─ ① PLAN    对每个组件调 check() → 收集 Status，打印成表格
│            组件 / 现状 / 将要做什么
│            ─────────────────────────────────────────
│             Caddy     二进制 v2.10.0 已装   复用
│             Caddy     setcap 授权 缺失     sudo setcap …
│             Caddy     user unit 指向旧路径  改写 ExecStart
│             Dnsmasq   已运行                复用
│             DNS       resolved 路由缺失     sudo 写 conf.d
│             Cert      NSS 库缺失            certutil -A
│            ─────────────────────────────────────────
│            2 项需 sudo → 继续？(Y/n)
│
├─ ② APPLY   按依赖序执行：routes → service → caddy → dnsmasq → dns → cert
│            （routes 必须在 caddy 之前 —— Caddyfile 的 import 目标必须先存在）
│            sudo 提权点集中在这里，一次性收集执行
│            机制：先 `sudo -v` 预认证（sudo 时间戳默认 15 分钟），
│            后续各步骤的 sudo 调用复用该时间戳，不再重复提示密码
│
└─ ③ VERIFY  每个组件的 verify() → 汇总 PASS/FAIL
```

### 6.2 组件（以 Linux + resolved 路线为例）

| 组件 | check() 看什么 | install() 做什么 | verify() 验什么 |
|---|---|---|---|
| **Caddy** | 二进制存在？版本？`getcap` 有 `cap_net_bind_service`？user unit 存在且指向工具的 Caddyfile？active？ | 二进制已存在则复用；否则从 GitHub release 下载固定版本 tarball，按**内置 sha256** 校验后解包；`sudo setcap`；写 `~/.config/devproxy/Caddyfile`；装 systemd user unit + `enable-linger` | `curl https://localhost` TLS 握手成功 |
| **Dnsmasq** | `~/.config/dnsmasq/dnsmasq.conf` 存在？用户级 dnsmasq 在跑且监听 5353？ | 写配置（`port=5353`、`address=/.test/127.0.0.1`、上游 `server=…`、`no-resolv`）；装 user unit | `dig @127.0.0.1 -p 5353 foo.test` → `127.0.0.1` |
| **Dns** | resolved 后端：`/etc/systemd/resolved.conf.d/devproxy.conf` 内容正确？system 后端：`/etc/dnsmasq.d/` 有对应段？ | 写 resolved conf（`DNS=127.0.0.1:5353` + `Domains=~.test`）或系统 dnsmasq 段；system 后端需处理 53 冲突 | `resolvectl query foo.test` → `127.0.0.1` |
| **Cert** | 根证书在系统信任库？在 NSS 库？ | Linux：`update-ca-certificates` + `certutil -A`；macOS：`security add-trusted-cert` | `openssl verify` + 一次真实 HTTPS 请求 |
| **Routes** | `routes.caddy` 存在？`Caddyfile` 的 import 路径对？ | 写初始 `routes.caddy`（空） | `caddy validate` 通过 |

**Dnsmasq 配置位置**：工具接管 `~/.config/dnsmasq/dnsmasq.conf`（`--conf-dir` 会加载目录下所有 `.conf`）。**只做增量修改，绝不重写整个文件**：保留既有的所有 `address=` 条目（如 `.ywb.yikuaigao.com`、`.develop`、`.local.yikuaigao.com`）、`no-resolv` / 上游 server / `filter-AAAA` 等设定，仅确保 `port=5353` 与 `address=/.test/127.0.0.1`（含 `::1`）存在。

### 6.3 平台差异

| 环节 | Linux | macOS |
|---|---|---|
| Caddy 二进制 | `caddyserver.com/api/download` | 同一 API（arch 为 `darwin_*`） |
| 80/443 授权 | `setcap cap_net_bind_service=+ep` | **无 setcap** → 只能 root 跑 LaunchDaemon |
| 服务管理 | systemd **user** unit | launchd **LaunchDaemon**（`/Library/LaunchDaemons/`，root） |
| DNS 守护 | 用户级 dnsmasq @5353 | 用户级 dnsmasq @**5300**（避开 mDNSResponder 占用 5353） |
| DNS 路由 | `/etc/systemd/resolved.conf.d/devproxy.conf` | `/etc/resolver/test`（`nameserver` + `port`） |
| 证书信任 | 系统信任库 **+** NSS 库（Chrome 只认 NSS） | 单一系统钥匙串（Safari/Chrome 都读） |
| 80→443 跳转 | Caddy 自动 | Caddy 自动 |
| 日志 | journald | 文件（`~/.local/state/devproxy/logs/caddy.log`） |

**macOS 的 80/443 是该平台最麻烦的一点**：`brew services` 装的是用户级 LaunchAgent，绑不了 80/443；macOS 没有 setcap 的等价物。唯一干净的解法是往 `/Library/LaunchDaemons/` 写一个 root 运行的 plist，由 `launchctl bootstrap system` 加载。代价是要 sudo 写系统目录 —— 与 Linux 上 `sudo setcap` 的提权需求对称。

---

## 7. 配置模型

`~/.config/devproxy/config.toml`（**全部可选**，缺省用内置默认值）：

```toml
# 域名后缀。devproxy up codeup → https://codeup.test
domain_suffix = "test"

# DNS 后端：auto | resolved | system
dns_backend = "auto"

# Caddy Admin API 与端口
admin_addr = "127.0.0.1:2019"
http_port = 80
https_port = 443

# Caddy 存储（CA + 证书）。沿用现有位置，迁移时零成本
storage = "~/.local/share/caddy"

# up 的就绪等待超时（秒）
ready_timeout = 60
```

**优先级**：CLI 参数 > `config.toml` > 内置默认值。**不生成默认配置文件** —— 避免"工具写了文件、用户改了、工具又覆盖"的困惑。看当前生效配置：`devproxy config`。

**`domain_suffix` 的变更语义**：已注册路由的 `domain` 在注册时固化到 `routes.json`，修改 `domain_suffix` **不会**自动重写已有路由的域名（否则正在跑的服务会突然失联）。新注册的路由使用新后缀；旧路由需 `devproxy down <name>` 后重新 `up`。

平台相关默认值（DNS 端口、systemd vs launchd、证书信任方式）由平台层提供，不进配置文件。

---

## 8. 迁移

### 8.1 现有 Caddyfile 的拆分

**迁移前**：

```caddyfile
{
	storage file_system /home/yewenbin/.local/share/caddy
}
http://localhost { root * …/site; file_server }
localhost { tls internal; root * …/site; file_server }
*.local.yikuaigao.com { tls internal; reverse_proxy {$MINIKUBE_IP:192.168.49.2}:30800 { … } }
*.test { tls { on_demand; issuer internal }; respond "…" 404 }
http://*.test { redir https://{host}{uri} permanent }
```

**迁移后** —— `~/.config/devproxy/Caddyfile`（工具生成）：

```caddyfile
{
	admin 127.0.0.1:2019
	storage file_system /home/yewenbin/.local/share/caddy
}
import /home/yewenbin/.local/state/devproxy/routes.caddy
import /home/yewenbin/.config/devproxy/sites.caddy
```

`~/.config/devproxy/sites.caddy`（从旧 Caddyfile 拆出的个人站点）：

```caddyfile
http://localhost {
	root * /home/yewenbin/work/tasks/local-https/site
	file_server
}
localhost {
	tls internal
	root * /home/yewenbin/work/tasks/local-https/site
	file_server
}
*.local.yikuaigao.com {
	tls internal
	reverse_proxy {$MINIKUBE_IP:192.168.49.2}:30800 {
		header_up X-Real-IP {remote_host}
	}
}
```

**变化点**：

| 旧 | 新 | 原因 |
|---|---|---|
| `*.test` on-demand 块 | 删除 | 由精确站点块取代（S2/S3） |
| `http://*.test` 跳转块 | 删除 | Caddy 对已注册域名自动生成 80→443 跳转 |
| 全局 `storage` 块 | 移到工具的 Caddyfile | 路径不变，CA 连续（D13） |
| — | 新增 `admin 127.0.0.1:2019` | 工具需要 Admin API |

`caddy.service` 的 `Environment=MINIKUBE_IP=…` **原样保留** —— `{$MINIKUBE_IP:…}` 依赖它。

### 8.2 迁移流程

`setup` 检测到遗留布局（非本工具管理的 caddy 服务）时：

```
1. 备份所有将被修改的原始配置文件
   → ~/.local/state/devproxy/backups/<UTC时间戳>/
     caddy.service.bak        ← ~/.config/systemd/user/caddy.service
     Caddyfile.bak            ← <repo>/Caddyfile
     dnsmasq.conf.bak         ← ~/.config/dnsmasq/dnsmasq.conf
     dnsmasq.service.bak      ← ~/.config/systemd/user/dnsmasq.service
     resolved.conf.bak        ← /etc/systemd/resolved.conf.d/*.conf
     MANIFEST.txt             ← 列出每个备份的原始路径与 sha256
2. 拆出个人站点块 → ~/.config/devproxy/sites.caddy
3. 生成 ~/.config/devproxy/Caddyfile（全局块 + import 两个片段）
4. 改写 unit 的 ExecStart → 指向新 Caddyfile
5. systemctl --user restart caddy
6. 验证
```

**备份是强制前置步骤** —— 备份目录创建失败则整个迁移中止，不做任何修改。备份目录带 UTC 时间戳，因此多次迁移不会互相覆盖；`MANIFEST.txt` 记录每个备份对应的原始路径与 sha256，便于回滚时精确还原。备份目录只增不删，由用户自行清理。

**验证步骤**（每步都要过）：

```
1. caddy validate --config ~/.config/devproxy/Caddyfile    → 语法通过
2. systemctl --user restart caddy
3. curl https://localhost                                  → 200（静态站还在）
4. curl https://nginx.local.yikuaigao.com                  → 200（minikube 网关还在）
5. devproxy up smoke -- python3 -m http.server            → https://smoke.test 可访问
```

第 3、4 步是现有能力的回归测试 —— **迁移不能让 minikube 那条链路断掉**。

### 8.3 迁移回滚

迁移动的是正在用的服务配置，回滚是硬要求：

```
从 MANIFEST.txt 找到最近一次备份目录
→ 逐个还原原始文件到其原始路径
→ systemctl --user restart caddy
→ 验证 https://localhost 恢复
→ 报告失败点
```

---

## 9. 错误处理与可观测性

### 9.1 错误模型

```
组件层     抛 ComponentError(message, hint)
             message = 发生了什么
             hint    = 用户该做什么（可执行的具体命令）
           ↓
CLI 层     捕获 → Rich 渲染 → 决定退出码
```

错误消息形态：

```
✗ Dnsmasq 未在 5353 端口监听
  期望: 127.0.0.1:5353
  实际: 未监听
  修复: systemctl --user start dnsmasq
        systemctl --user status dnsmasq
```

**退出码**：

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 运行失败（组件 error、reload 失败、up 启动失败） |
| 2 | 参数错误（Typer 默认） |
| 3 | 前置条件不满足（doctor 发现问题 / up 时 Caddy 未运行） |
| 4 | 需要提权但处于非交互环境（无 TTY 且未 `--yes`） |

### 9.2 失败时不留半成品

| 场景 | 处理 |
|---|---|
| `up` 就绪轮询超时 | 杀整个子进程组 → **不注册路由** → 退出码 1 |
| `up` 注册时 Caddy validate 失败 | 路由不落盘（先验证后写盘）→ 退出码 1 |
| `up` 的 reload 失败 | 回滚 `routes.json` + `routes.caddy` → 退出码 1 |
| `setup` 组件安装失败 | 已成功的组件不回滚（幂等，重跑跳过），但**运行态被破坏的改动必须回滚** |
| `down` 目标不存在 | 幂等，返回成功 + "未找到"提示 |
| 迁移中途失败 | 恢复备份并重启旧服务 → 报告失败点 |

### 9.3 日志

`devproxy logs` 的 UX 在两个平台一致，实现走平台层：

| 平台 | Caddy 日志去向 | `devproxy logs` 实现 |
|---|---|---|
| Linux | stderr → systemd journal（**保持现状**） | `journalctl --user -u caddy -f` |
| macOS | 显式写文件（launchd 的 stdout 进 system log，不可读） | `tail -f ~/.local/state/devproxy/logs/caddy.log` |

macOS 的工具生成的 Caddyfile 全局块会多一段 `log { output file … }`，Linux 不加（D17）。

---

## 10. 测试策略

### L1 单元测试（纯函数，无 IO，无外部进程）

```
test_names.py       路由名校验：合法/非法/边界（长度 63、含点、含大写、尾随换行）
test_render.py      render(routes) → caddy 文本；空路由；多路由；特殊字符转义
test_config.py      配置合并优先级 CLI > file > default；toml 解析错误
test_ports.py       端口分配：可用端口、--port 显式指定、端口被占用
test_runner.py      子进程启动/信号转发/进程组清理/退出码（系统调用 fake 化）
```

### L2 集成测试（起隔离的真实 Caddy 实例，不碰生产 443/2019）

沿用现有 `devp selftest` 的成熟思路 —— 独立 admin 端口、独立 https 端口、独立 storage：

```
test_integration.py
  ├─ 注册路由 → 真实 HTTPS 请求 → 200 + 主机名校验通过
  ├─ 注销路由 → 再请求 → 拿不到证书（TLS 握手失败），确认路由与站点块均已消失
  ├─ 未注册域名 → 拿不到证书（S3 固化为回归测试）
  ├─ caddy reload 后路由仍在 ← 持久化的核心回归测试
  ├─ 80 → 443 的 301 跳转
  └─ 与 *.local.yikuaigao.com 通配块共存（S4）

test_caddy_admin.py  Admin API 封装：注册/幂等/退避重试/注销重试；
                     区分"Admin 不可达"与"路由表为空"
```

### L3 平台编排测试（fake 注入，不碰真实系统）

平台层所有系统交互都走可注入接口（`SystemdOps`、`PortBinder`、`CertTrust`、`DnsOps`、`ServiceManager`），测试注入 fake：

```
test_components.py
  ├─ setup 的 PLAN 阶段：fake 报告"已装" → 跳过；报告"缺失" → 排入计划
  ├─ sudo 步骤被集中收集，一次提权（断言提权次数 == 1）
  ├─ macos.py 的编排分支（用 fake 跑，不真碰 launchd）
  └─ 迁移流程：备份 → 拆分 → 改写 → 验证；注入失败 → 断言回滚发生
```

### 10.1 现有测试资产的迁移

`tests/test_devp.py`（967 行）已覆盖的用例**直接迁移**，不重写：

| 现有用例 | 归属 |
|---|---|
| name 校验（拒绝尾随换行、含点） | `test_names.py` |
| 端口分配 | `test_ports.py` |
| Admin API 封装（注册/幂等/退避重试/注销重试） | `test_caddy_admin.py` |
| 路由构造 + 插前逻辑 | `test_render.py`（从"内存列表"改为"渲染文本"） |
| 进程组信号转发、退出码、清理 | `test_runner.py`（系统调用 fake 化） |
| `selftest` 端到端 | `test_integration.py` |
| `ls` 区分 Admin 不可达 vs 空路由 | `test_caddy_admin.py` |

**最大的改造点**：现有实现把路由当"内存里的 route 对象数组"直接 POST 给 Admin API；新实现是"路由表 → 渲染文本 → 写文件 → reload"。所以 `test_render.py` 是新增的核心，Admin API 相关用例要从"构造 route 对象"改成"验证渲染结果 + 验证 reload 后的实际路由"。

### 10.2 `devproxy selftest` 保留

与 L2 集成测试共用同一套代码：

```
devproxy selftest
  → 起隔离 Caddy（独立 admin/https 端口、独立 storage）
  → 完整跑注册/转发/注销/跳转/证书校验
  → 不碰生产 443/2019
  → 通过: ✔ selftest passed (6 checks, 1.2s)
```

### 10.3 需人工真机验证的项

无法自动化，列进 README 的发布前检查清单：

- macOS：`/etc/resolver/test` 的 `port` 指令是否生效
- macOS：mDNSResponder 对 `127.0.0.1:5353` 的占用情况
- macOS：`security add-trusted-cert` 的交互授权流程
- Linux：真实 systemd 环境下的 `setup` 全流程（CI 用 fake 覆盖，发布前人工跑一遍）

---

## 11. 仓库重构

```
local-https/                    →  变成 devproxy 的源码仓库
├── pyproject.toml              新增（uv 可打包）
├── src/devproxy/               新增（包源码，从 devp 脚本重构而来）
├── tests/                      从 tests/test_devp.py 重构而来
├── README.md                   重写（安装 → 使用）
├── Justfile                    保留为转发壳，标注 deprecated
├── Caddyfile                   迁移后不再被服务使用，保留作历史/参考
├── site/                       你的静态站目录（不变）
└── docs/superpowers/specs/     设计与实施计划
```

`Justfile` 瘦身为转发壳（`just setup` → `devproxy setup`，`just logs` → `devproxy logs`，`just status` → `devproxy doctor`），不再有第二份实现（D15）。

`devp` 那个 536 行的单文件脚本 → 重构进 `src/devproxy/proxy/runner.py` + `routes.py` + `caddy.py`。**不保留** —— 两份实现会立刻分叉。

### 11.1 包结构

```
src/devproxy/
├── __init__.py
├── cli.py              # Typer 应用与子命令
├── config.py           # 配置加载（tomllib）+ 默认值
├── paths.py            # XDG 目录约定
├── components/
│   ├── base.py         # Component 协议 + Status + ComponentError
│   ├── caddy.py
│   ├── dnsmasq.py
│   ├── dns.py
│   ├── certs.py
│   └── service.py
├── platform/
│   ├── base.py         # 平台能力协议
│   ├── linux.py
│   └── macos.py
├── proxy/
│   ├── routes.py       # 路由表 CRUD + 渲染
│   ├── caddy.py        # validate / reload / Admin API
│   └── runner.py       # up 的子进程管理与信号处理
├── migrate.py          # 遗留环境检测与迁移
└── doctor.py
```

### 11.2 分发

```toml
# pyproject.toml
[project]
name = "devproxy"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["typer>=0.12", "rich>=13"]

[project.scripts]
devproxy = "devproxy.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

```bash
uv tool install devproxy          # 从 PyPI
uv tool install -e .               # 本地开发
uvx devproxy doctor               # 免安装试用
```

`requires-python = ">=3.11"` 是因为 `tomllib` 在 3.11 才进标准库。

---

## 12. 实施顺序概览

1. **包骨架** —— `pyproject.toml` + `src/devproxy/` 骨架 + `paths.py` / `config.py`，CI 能跑
2. **平台层** —— `platform/base.py` 协议 + `linux.py` 实现 + fake
3. **组件层** —— 五个组件的 `check/install/verify`
4. **路由层** —— `routes.py`（存储 + 渲染）+ `caddy.py`（validate/reload）+ `runner.py`（进程管理）
5. **CLI** —— 全部子命令
6. **迁移** —— `migrate.py` + `setup` 内联
7. **测试** —— L1 → L2 → L3
8. **README 与发布** —— 含 macOS 真机验证清单
