# devproxy

本地开发环境一键部署与域名网关。把本地跑起来的服务用 `https://<name>.test` 暴露出去，
开发者只记域名，不记端口。

- 一条命令部署 **Caddy + dnsmasq + DNS 路由 + 证书信任 + 开机自启**（`devproxy setup`）
- 一条命令把服务挂到域名上（`devproxy up <name> -- CMD...`）
- 路由表是**文件**（`routes.json`），Caddy 重启后路由仍在，不需要常驻守护进程

---

## ⚠️ 这个工具会修改你的系统

`devproxy setup` 不是「装个 Python 包就完事」。它会写系统目录、装服务单元、
改 DNS 解析路径、把自签 CA 装进系统信任库。**请先读完这一节再运行。**

它会改动的东西：

| 改动 | 位置（Linux） | 位置（macOS） |
|---|---|---|
| Caddy 二进制 | `~/.local/bin/caddy`（下载，校验 sha256） | 同左 |
| Caddy 主配置 | `~/.config/devproxy/Caddyfile` | 同左 |
| 你的个人站点 | `~/.config/devproxy/sites.caddy`（只 import，不改内容） | 同左 |
| Caddy 服务单元 | `~/.config/systemd/user/caddy.service`（user scope） | `/Library/LaunchDaemons/com.devproxy.caddy.plist`（**root** LaunchDaemon） |
| dnsmasq 服务单元 | `~/.config/systemd/user/dnsmasq.service`（user scope） | `~/Library/LaunchAgents/com.devproxy.dnsmasq.plist` |
| dnsmasq 配置 | 增量合并 `~/.config/dnsmasq/dnsmasq.conf` | 同左 |
| DNS 路由 | `/etc/systemd/resolved.conf.d/devproxy.conf`（或 `/etc/dnsmasq.d/devproxy.conf`） | `/etc/resolver/test` |
| CA 根证书 | `/usr/local/share/ca-certificates/devproxy-local.crt` + NSS 库 `~/.pki/nssdb` | 系统钥匙串 `/Library/Keychains/System.keychain` |
| 开机自启 | `loginctl enable-linger <用户>` | launchd 的 `RunAtLoad` |

**需要 sudo 的步骤**：

- Linux
  - `sudo setcap cap_net_bind_service=+ep ~/.local/bin/caddy`（让 caddy 能绑 80/443）
  - `sudo` 写 `/etc/systemd/resolved.conf.d/devproxy.conf`，并 `sudo systemctl restart systemd-resolved`
  - `sudo loginctl enable-linger <用户>`（开机自启，未登录也跑）
  - `sudo` 把 CA 装进系统信任库并执行 `update-ca-certificates`
- macOS
  - `sudo` 写 `/Library/LaunchDaemons/com.devproxy.caddy.plist`（macOS 没有 `setcap`，
    绑 80/443 只能靠 root 跑的 LaunchDaemon）
  - `sudo` 写 `/etc/resolver/test`
  - `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain <ca>`

**不需要 sudo 的步骤**：下载 caddy、写 `~/.config/devproxy/` 与 `~/.local/state/devproxy/`、
合并 dnsmasq 配置、安装 systemd user 单元 / LaunchAgent、装 NSS 库（需要 `certutil`）。

非交互环境（CI、脚本）里跑 `setup` / `trust`，如果需要提权，必须显式加 `--yes`，
否则工具会以退出码 4 拒绝执行（不会默默跳过，也不会默默提权）。

---

## 安装

**当前项目尚未发布到 PyPI**，只能从源码安装：

```bash
git clone <仓库地址>
cd <仓库地址>
uv tool install -e .
```

或者用 pipx：

```bash
pipx install .
```

发布到 PyPI 之后，`uv tool install devproxy` 才可用 —— 在那之前这条命令会失败
（PyPI 上没有这个包，且这个名字将来可能与别的包冲突）。

依赖：Python ≥ 3.11，运行时依赖只有 `typer` 与 `rich`。

安装后 `devproxy --help` 应能列出全部子命令。

---

## 快速开始

```bash
devproxy setup     # 部署 Caddy + dnsmasq + DNS 路由 + 服务自启（幂等，可重复执行）
devproxy doctor    # 体检（静态检查，不做 DNS 解析）：缺什么给什么修复命令
```

`setup` 之后：

```bash
# 启动一个服务并注册 https://codeup.test
devproxy up codeup -- sh -c 'npx vite --port $PORT --host 127.0.0.1'

# 给已经在跑的服务开域名（不启动它）
devproxy expose legacy --port 3000

devproxy ls                 # 路由清单（含可达性）
devproxy down codeup        # 注销路由（子进程也一并收掉）
devproxy logs caddy         # 跟随日志（Ctrl-C 退出，不会自己结束）
```

### CA 根证书是在第一条路由注册之后才存在的

Caddy 的内部 CA **在第一次加载 `tls internal` 站点时**才生成 —— 也就是**第一条路由被注册时**
（`devproxy up` 或 `devproxy expose` 都算）。所以在一台干净的机器上：

1. `devproxy setup` 成功，但此时系统里**还没有 CA 根证书**（`setup` 结束时会把这条提示打出来）；
2. 第一次注册路由（`devproxy up <name> -- ...` 或 `devproxy expose <name> --port N`）之后，
   Caddy 才生成 CA；
3. **这时还需要跑一次 `devproxy trust`**，curl / Chrome 才会信任它。

`devproxy doctor` 在 CA 尚未生成时显示「尚无 CA 根证书（将在首条路由注册时由 Caddy 生成）」，
这是正常状态，不是故障。

---

## 命令

| 命令 | 说明 |
|---|---|
| `devproxy setup` | 部署本地开发环境（幂等，可重复执行） |
| `devproxy setup --rollback <备份目录>` | 按备份目录回滚（见「迁移与回滚」） |
| `devproxy doctor` | 体检所有前置条件（静态检查：文件在位、服务在跑、证书已装、单元指向本工具），缺什么给什么修复命令 |
| `devproxy up <name> [--port N] [--ready-timeout S] -- CMD...` | 分配端口、启动命令、注册 `https://<name>.test` |
| `devproxy expose <name> --port N` | 给已在运行的服务开域名，注册后立即返回，路由常驻（上游暂时无响应时只告警，路由照样注册） |
| `devproxy down <name>` | 注销路由；若该路由的子进程还在，一并收掉 |
| `devproxy ls` | 列出所有已注册路由（含可达性） |
| `devproxy logs [caddy\|dnsmasq]` | 跟随服务日志（默认 `caddy`） |
| `devproxy trust` | 单独安装 CA 根证书（系统信任库 + NSS 库 / 系统钥匙串） |
| `devproxy config` | 显示当前生效配置与渲染出的路由片段 |
| `devproxy selftest` | 隔离 Caddy 实例端到端自检 |

常用选项：

| 选项 | 适用命令 | 说明 |
|---|---|---|
| `--dns-backend auto\|resolved\|system` | `setup` / `doctor` | 强制 DNS 后端（默认取 `config.toml` 的 `dns_backend`） |
| `--domain-suffix <后缀>` | `setup` | 临时覆盖域名后缀（会过校验，非法值退出码 2） |
| `--no-trust` | `setup` | 跳过证书信任 |
| `--yes` | `setup` / `trust` | 非交互模式，不询问（需要提权的场景必须加） |
| `--rollback <目录>` | `setup` | 从指定备份目录回滚 |
| `--port N` | `up` | 显式指定端口（默认自动分配）；被占用时直接失败 |
| `--port N` | `expose` | **必填**：上游已在监听的端口 |
| `--ready-timeout S` | `up` | 就绪等待秒数（默认取 `config.toml` 的 `ready_timeout`） |

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 运行失败（子进程退出、reload 失败、路由表损坏、参数缺失……） |
| 2 | 参数错误（未知日志服务名、非法 `dns_backend`、非法 `--domain-suffix`） |
| 3 | 前置条件不满足（Caddy 未运行、主 Caddyfile 不存在、`doctor` 体检不通过） |
| 4 | 需要提权但当前非交互（未加 `--yes`） |

注意两个特例：`up` 缺少 `--` 或 `--` 后没有命令，按「运行失败」退出 **1**；
`up` 的退出码在命令正常结束时**就是子进程的退出码**。

还有一处不对称容易踩：同一个非法值，写在 `config.toml` 里与用命令行传，退出码不同 ——
写在 `config.toml` 里属于**配置错误**（退出码 **1**），用 `--domain-suffix` /
`--dns-backend` 传属于**参数错误**（退出码 **2**）。

---

## 应用接入

### 两种应用，两种写法

**1. 原生读 `PORT` 环境变量的应用**（Next.js / CRA 等）——直接写命令：

```bash
devproxy up next-app -- npx next dev
```

devproxy 会把分配到的端口放进子进程的 `PORT` 环境变量。

**2. 需要显式端口参数的应用**（Vite / uvicorn / gunicorn 等）——用 `sh -c` 包一层，
让 shell 在 devproxy 注入 `PORT` **之后**再展开：

```bash
devproxy up codeup-web -- sh -c 'npx vite --port $PORT --host 127.0.0.1'
devproxy up codeup-api -- sh -c 'uv run uvicorn app:app --host 127.0.0.1 --port $PORT'
devproxy up codeup-api -- sh -c 'gunicorn app:app --bind 127.0.0.1:$PORT'
```

**不要直接写 `--port $PORT`**：devproxy 用 `subprocess.Popen(argv)` 直接执行、**不经过 shell**，
命令里的 `$PORT` 会被你自己的 shell 提前展开（此时 devproxy 还没分配端口，值是空串），
应用会收到缺值的参数。用单引号包住 `sh -c` 的脚本即可把展开推迟到子进程启动后。

**`--` 是必须的**：`--` 之前是 devproxy 自己的参数（`name`、`--port`、`--ready-timeout`），
`--` 之后的一切**原样**交给子进程，devproxy 不解析。这样应用自己的 `--port`
不会被 devproxy 抢走。缺少 `--` 时工具直接报错退出（码 1）。

### 应用侧的其它注意点

- **Host 校验放行**：请求的 Host 头是 `<name>.test`。Vite 需要 `server.allowedHosts`、
  Django 需要 `ALLOWED_HOSTS` 里加上该域名。
- **子进程脱离控制终端**：devproxy 以 `start_new_session=True` 启动子进程（独立进程组，
  便于退出时按组清理孙进程）。代价是子进程**没有控制终端**，依赖 stdin 的交互式启动语义会变；
  stdout/stderr 仍直透当前终端。
- **就绪判定**：devproxy 轮询「端口是否可连接」，最多等 `ready_timeout` 秒（默认 60）。
  超时会提示「应用可能未使用 `$PORT`，可用 `--port` 显式指定」。
- **同名路由会被替换**：再次 `up` 同一个名字时，旧路由先注销、旧子进程先收掉。

---

## DNS 与证书

### DNS

| 平台 | 机制 | dnsmasq 监听 |
|---|---|---|
| Linux（默认 `resolved`） | 写 `/etc/systemd/resolved.conf.d/devproxy.conf`，其中 `Domains=~.<后缀>` 把该后缀路由到 dnsmasq | `127.0.0.1:5353` |
| Linux（`system`） | 写 `/etc/dnsmasq.d/devproxy.conf`，由系统 dnsmasq 负责 | `127.0.0.1:5353` |
| macOS | 写 `/etc/resolver/<后缀>`（每域解析器文件），不动 `/etc/resolv.conf` | `127.0.0.1:5300` |

Linux 的 resolved drop-in 内容形如：

```ini
[Resolve]
DNS=127.0.0.1:5353
FallbackDNS=1.1.1.1 8.8.8.8
Domains=~.test
```

**注意**：drop-in 里的 `DNS=` 把这台机器的**全局** DNS 指到了用户级 dnsmasq ——
这是为了不占用 53 端口而做的取舍。作为配套，工具保证 dnsmasq 的配置里**始终带 `no-resolv`**
（除非你自己写了 `resolv-file=`）：dnsmasq 因此**不会**把 `/etc/resolv.conf`
（resolved 机器上是 `127.0.0.53` 桩）当成上游，否则 resolved ↔ dnsmasq 互指，
普通域名的解析会在全机范围内失败。

dnsmasq 的配置是**增量合并**的：你的 `~/.config/dnsmasq/dnsmasq.conf` 里已有的
域名条目、`filter-AAAA` 等都会保留，工具只保证 `port=`、`address=/.<后缀>/`、
`listen-address=127.0.0.1`、`bind-interfaces`、`no-resolv` 与缺失的全局上游存在。
它**只监听回环地址**，不会在局域网里变成一个开放递归解析器。

### `doctor` 与 `setup` 各查什么

- **`devproxy doctor` 只做静态检查**：DNS 路由文件在不在、服务在不在跑、CA 在不在、
  单元是否指向本工具的 Caddyfile 与二进制。**它不做 DNS 解析** —— 不发起任何
  `resolvectl` / `dig` 查询。
- **「解析是否真的生效」由 `setup` 的自检阶段负责**：`setup` 执行完变更后会做 verify，
  真的解析一次 `foo.<后缀>` 并确认结果指向本机。注意这只发生在 **setup 确实做了变更**的时候；
  `setup` 走「所有组件已就绪」的 no-op 分支时，这次自检**不会**发生。

要立刻确认解析是否生效，可以自己查一次：

```bash
resolvectl query foo.test          # Linux
dscacheutil -q host -a name foo.test   # macOS
```

### 证书：精确站点块，不是通配符

每条路由渲染成**一个精确的站点块**：

```caddyfile
codeup.test {
	tls internal
	reverse_proxy 127.0.0.1:34567
}
```

**没有 `*.test` 通配块，也没有 on-demand TLS。** 由此得到一个明确的安全属性：

> **没有注册的域名拿不到任何证书** —— TLS 握手直接失败。

也就是说，不存在「本机任意进程都能为任意 `.test` 名字骗到一个被系统信任的证书」的情况。

信任 CA 需要装进不同的地方：

| 平台 | 装入位置 | 谁读它 |
|---|---|---|
| Linux | 系统信任库（`/usr/local/share/ca-certificates/` + `update-ca-certificates`） | curl / Go / Java 等 |
| Linux | NSS 库 `~/.pki/nssdb` | **Chrome / Chromium 只认 NSS 库** |
| macOS | 系统钥匙串 `/Library/Keychains/System.keychain` | Safari / Chrome 都读 |

两步是独立的：只装系统信任库，Chrome 照样报 `net::ERR_CERT_AUTHORITY_INVALID`；
只装 NSS 库，curl 又不认。Linux 上装 NSS 库需要 `certutil`
（`sudo apt install libnss3-tools`）。

**Linux 上装入 NSS 库后必须完全退出并重启 Chrome** —— 关窗口不算，要从托盘退出进程；
Chrome 只在启动时读取 NSS 库。

CA 根证书的路径是 `<storage>/pki/authorities/local/root.crt`
（默认 `~/.local/share/caddy/pki/authorities/local/root.crt`）。

---

## 配置

`~/.config/devproxy/config.toml`（**全部可选**，不写就用默认值；**工具不会自动生成默认配置文件**）：

```toml
# 域名后缀。devproxy up codeup → https://codeup.test
domain_suffix = "test"

# DNS 后端：auto | resolved | system
dns_backend = "auto"

# Caddy Admin API 地址
admin_addr = "127.0.0.1:2019"

# Caddy 的 HTTP / HTTPS 端口
http_port = 80
https_port = 443

# Caddy 存储（CA + 证书）。迁移时沿用遗留环境的位置，CA 才能连续
storage = "~/.local/share/caddy"

# up 的就绪等待超时（秒）
ready_timeout = 60

# caddy 二进制路径（setup 会下载到此处）
caddy_bin = "~/.local/bin/caddy"
```

各键的准确语义：

| 键 | 默认 | 说明 |
|---|---|---|
| `domain_suffix` | `test` | 单个 DNS 标签（小写字母/数字开头结尾，中间可含连字符，不含点，≤ 63 字符）。非法值在加载配置时就报错 |
| `dns_backend` | `auto` | `auto` = resolved 真的在跑就用 resolved，否则用 system；也可显式指定 |
| `admin_addr` | `127.0.0.1:2019` | `host:port` 形式。既是 Caddy 的 admin 监听地址，也是 reload 时的目标地址 |
| `http_port` / `https_port` | `80` / `443` | **目前只出现在 `devproxy config` 的输出里**：主 Caddyfile 没有 `http_port` / `https_port` 指令，Caddy 仍按默认监听 80/443。改这两个值不会改变实际监听端口 |
| `storage` | `~/.local/share/caddy` | Caddy 的存储目录，CA 与已签发证书都在这里。改了它 = Caddy 重新生成 CA = 浏览器信任全部失效 |
| `ready_timeout` | `60` | `up` 等待应用就绪的秒数（1..3600） |
| `caddy_bin` | `~/.local/bin/caddy` | caddy 二进制路径。setup 会把固定版本下载到这里；改了它必须重跑 setup 让服务单元指向新路径 |

**优先级**：CLI 参数 > `config.toml` > 内置默认值。未知键会直接报错（不会静默忽略）。

看当前生效配置：

```bash
devproxy config
```

**`domain_suffix` 改了不会重写已有路由** —— 已注册路由的域名在注册时固化进 `routes.json`，
否则正在跑的服务会突然失联。旧路由需要 `devproxy down <name>` 后重新 `up`。

### storage 留档：为什么改了 `config.toml` 的 storage 也不生效

迁移遗留环境时，devproxy 会把**遗留 Caddyfile 里声明的 storage 路径**（CA 所在目录）
留档到 `~/.local/state/devproxy/caddy-storage.json`，并在**之后的每一条命令**里继续沿用它
（storage 留档在每次构建运行时上下文时都会被读到 —— `setup`、`up`、`ls`、`config` 都一样）。
换了 storage 位置，Caddy 会重新生成 CA，浏览器里的信任与个人站点已有的 `tls internal`
证书全部失效。

**留档优先于 `config.toml`。** 两者不一致时，devproxy 会在终端上明确提示
（实际生效的路径、被忽略的配置值、以及留档文件的位置），不会静默忽略。

想改回 `config.toml` 里的设置：**删除 `~/.local/state/devproxy/caddy-storage.json`**
（该文件即退出路径），然后重新 `devproxy setup` —— 注意这会让 Caddy 重新生成 CA，
浏览器需要重新信任新的根证书。

---

## 目录与文件

```
~/.config/devproxy/
├── config.toml      # 你的配置（可选，工具不生成）
├── Caddyfile        # 工具生成的主配置：全局块 + 两个 import
└── sites.caddy      # 你的个人站点 —— 工具只 import，绝不改内容

~/.local/state/devproxy/
├── routes.json      # 路由表（真相）
├── routes.caddy     # 由 routes.json 确定性渲染出来的片段，被主 Caddyfile import
├── routes.lock      # 写路由时的 flock
├── caddy-storage.json   # 迁移留下的 storage 留档（见上）
├── caddy-env.json       # 迁移留下的单元环境变量留档（如 MINIKUBE_IP）
├── dns-route.json       # 上次写入的 DNS 路由内容哈希
├── backups/             # 备份目录（每次迁移一个时间戳子目录）
└── logs/                # macOS 上的服务日志落点

~/.local/share/caddy/    # Caddy 存储：CA 根证书与已签发证书
```

**路由表是文件，不是内存。** `routes.json` 是真相，`routes.caddy` 是它的确定性渲染
（同一张表永远产出逐字节相同的文本），主 Caddyfile `import` 这个渲染产物。
因此路由**能活过 Caddy 重启**，也不需要任何常驻守护进程来维持它们。
写路由是一次事务：加锁 → 改表 → 渲染 → `caddy validate` → 落盘 → `caddy reload`，
reload 失败会把落盘内容回滚。

---

## 迁移与回滚

`devproxy setup` 会检测到**本工具未管理的遗留 caddy 服务**（手工写的
`caddy.service` + `Caddyfile`）。检测到之后，迁移按「**先备份，再动手**」的顺序执行：

1. 把**每一个将被改动的文件**备份到 `~/.local/state/devproxy/backups/<UTC时间戳>/`
   （目录里另有 `manifest.json` 与人可读的 `MANIFEST.txt`）；
2. 备份失败 → **一个文件都不改**，迁移中止；
3. 把遗留 Caddyfile 里的**个人站点块**搬到 `~/.config/devproxy/sites.caddy`
   （`.test` 相关的块由工具接管、会被丢弃，旧 Caddyfile 本身完整保留在备份里）；
4. 用 `caddy validate` 校验新配置（校验不过就不换单元）；
5. 改写服务单元、重启 caddy。

回滚：

```bash
devproxy setup --rollback ~/.local/state/devproxy/backups/2026-10-22T12-00-00Z
```

回滚会按备份逐个还原文件（原本不存在的文件会被删掉），部分条目失败时会把
「已还原了哪些」如实打印出来。

非交互环境里做迁移必须加 `--yes`，否则以退出码 4 拒绝（迁移会改写你的单元与配置，
不能不问一声就动手）。

---

## 平台支持与差异

| | Linux | macOS |
|---|---|---|
| 服务单元 | systemd user（`~/.config/systemd/user/`） | launchd：caddy 走 `/Library/LaunchDaemons/`（**root**），dnsmasq 走 `~/Library/LaunchAgents/` |
| 绑定 80/443 | `setcap cap_net_bind_service=+ep`（需要 `libcap2-bin`） | 无 setcap，靠 root 的 LaunchDaemon |
| DNS 路由 | systemd-resolved drop-in 或 `/etc/dnsmasq.d/` | `/etc/resolver/<后缀>` |
| dnsmasq 端口 | `5353` | `5300`（53 被 mDNSResponder 占着） |
| dnsmasq 二进制 | `/usr/sbin/dnsmasq` | 系统不带，需 Homebrew：固定探测 `/opt/homebrew/sbin` → `/usr/local/sbin` → `/usr/sbin` |
| CA 信任 | 系统信任库 + NSS 库 | 系统钥匙串 |
| 开机自启 | `loginctl enable-linger` | launchd `RunAtLoad`（macOS 无 linger 概念） |

**macOS 上 caddy 以 root 运行**：这是 macOS 绑 80/443 的唯一干净解法，代价是所有站点的
运行权限都提到 root，且 CA、storage、日志会留下 root 属主的文件。

**caddy 版本固定为 2.11.7**：sha256 直接写死在源码里，setup 下载后校验。
上游 release 的 `checksums.txt` 与实际托管的资产对不上（实测 tar.gz 与 deb 均不符），
所以工具**不信任**那个文件。

`devproxy selftest` 起一个**隔离的 Caddy 实例**（admin `20191` / https `14443` /
http `14480`，独立 storage 与独立配置目录），完整跑一遍注册 → 转发 → 跳转 → 注销 →
证书策略，**不碰生产的 443/2019**，也不会把沙箱 CA 装进宿主的信任库。

---

## 故障排查

- **`devproxy up` 报「就绪超时」**：应用多半没读 `PORT`。改用 `sh -c '... $PORT ...'`，
  或用 `--port N` 显式指定端口（此时应用必须自己绑这个端口）。
- **`up` 报「Caddy 未运行（Admin API 不可达）」**（退出码 3）：先跑 `devproxy setup`，
  或 `systemctl --user start caddy` / `launchctl kickstart -k system/com.devproxy.caddy`。
- **`up` 报「主 Caddyfile 不存在」**（退出码 3）：还没 setup，先 `devproxy setup`。
- **curl 报证书错误 / Chrome 报 `net::ERR_CERT_AUTHORITY_INVALID`**：
  还没跑 `devproxy trust`，或 CA 是之后才生成的。顺序是：先 `up`（生成 CA）→ 再 `trust`。
  Chrome 只认 NSS 库，装完必须**完全退出并重启 Chrome**。
- **`doctor` 说「CA 根证书未被信任（缺少：系统信任库、NSS 库）」**：跑 `devproxy trust`。
  Linux 上若报缺 `certutil`，先 `sudo apt install libnss3-tools`。
- **`doctor` 说「caddy 版本不符」**：setup 会替换为固定版本，重跑 `devproxy setup`。
- **改了 `config.toml` 的 `storage` 却没生效**：看上面「storage 留档」一节。
- **域名解析到别的 IP**：`doctor` 不查解析（它只做静态检查），自己查一次
  `resolvectl query <name>.test`（macOS 用 `dscacheutil -q host -a name <name>.test`）；
  确认路由文件在位之后，Linux 上必要时 `sudo systemctl restart systemd-resolved`。
- **`devproxy logs caddy` 打印了一堆东西但没有退出**：这是预期行为 —— 它是流式跟随，
  Ctrl-C 退出。
- **路由残留**（devproxy 被 `kill -9`）：`devproxy ls` 会把不可达的路由标出来，
  `devproxy down <name>` 清掉。
- **`devproxy logs dnsmasq`** 看 dnsmasq 的日志；Linux 是 journalctl，macOS 是
  `~/.local/state/devproxy/logs/dnsmasq.log`。

真机验证清单见 [docs/verification-checklist.md](docs/verification-checklist.md) ——
里面列了自动化测试覆盖不到、必须在真实机器上逐条跑的行为。

---

## 仓库里的历史文件

仓库根目录的 `Caddyfile` 是**迁移前**的手工配置。**在 `devproxy setup` 的迁移真正跑过之前，
现役的 `caddy.service` 仍然指向它**（迁移会先备份它、再把单元改指向
`~/.config/devproxy/Caddyfile`）；迁移完成后它才只剩历史参考价值。

根目录的 `devp` 脚本**已删除** —— 它是 `devproxy` 的 1.0 前身：直接 PATCH 现役
Caddy 的 Admin API 注册路由，注册的东西新工具既看不见也删不掉。它的来龙去脉
记在 [docs/legacy-devp.md](docs/legacy-devp.md)。`Justfile` 现在只是转发到
`devproxy` 的兼容壳。
