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

**DNS 路由的改动是整机范围的（Linux `resolved` 后端）** —— 这一点必须单独说清楚：

- 写进 drop-in 的 `DNS=127.0.0.1:5353` 落在 resolved 的**全局段**，它决定的是
  **这台机器全部域名**的解析先去问用户级的 dnsmasq，**不只是 `*.test`**；
- 同一份 drop-in 里还有一行 `FallbackDNS=1.1.1.1 8.8.8.8`，它是**整机兜底**：
  dnsmasq 起不来的时候，全机解析退化为这两个公共解析器；
- `Domains=~.<后缀>` 才是把 `<后缀>` 交给 dnsmasq 的那一条 —— 但在
  `DNS=` 已经把全机解析指向 dnsmasq 的前提下，它只决定 dnsmasq 内部怎么分流。

`devproxy setup` 在打印变更清单时会把这段整机作用域**单独打一遍**（表格里放不下，
也放不下重点），你点确认之前就会看到。macOS 的每域解析器文件与 Linux `system`
后端的 dnsmasq 片段**只影响本后缀**，不涉及整机。

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

**想撤回？** 见下面的[「卸载」](#卸载)：`devproxy uninstall --dry-run` 可以先看它会做什么。

---

## 安装

**当前项目尚未发布到 PyPI**，从源码仓库安装。最省事的一条 —— `uv` 会自己建隔离环境、
装好入口：

```bash
uv tool install 'git+ssh://git@codeup.aliyun.com/msy/tools/devproxy.git'
```

需要能通过 SSH 访问该仓库（密钥已登记）。想钉住分支 / 标签 / 提交：

```bash
uv tool install 'git+ssh://git@codeup.aliyun.com/msy/tools/devproxy.git@main'
uv tool install 'git+ssh://git@codeup.aliyun.com/msy/tools/devproxy.git@<commit>'
```

不写 `@…` 时取仓库的默认分支（当前是 `main`）。升级到远端最新：

```bash
uv tool upgrade devproxy
```

没装 `uv` 的话：macOS `brew install uv`，Linux 用官方安装脚本。

不想用 uv，也可以用 pipx（同样的 git 地址）：

```bash
pipx install 'git+ssh://git@codeup.aliyun.com/msy/tools/devproxy.git'
```

**本地开发**用可编辑安装 —— 改源码即生效，不必每次重装：

```bash
git clone git@codeup.aliyun.com:msy/tools/devproxy.git
cd devproxy
uv tool install -e .
```

发布到 PyPI 之后，`uv tool install devproxy` 才可用 —— 在那之前这条命令会失败
（PyPI 上没有这个包，且这个名字将来可能与别的包冲突）。

依赖：Python ≥ 3.11（uv 会自动挑一个合适的解释器），运行时依赖只有 `typer` 与 `rich`。

安装后 `devproxy --help` 应能列出全部子命令。命令落在 `~/.local/bin/devproxy`；
如果 `command -v devproxy` 找不到，跑一次 `uv tool update-shell`，或把 `~/.local/bin` 加进 `PATH`。

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

# 项目里有一份 devproxy.toml（提交进仓库、全组共享）时：
devproxy sync               # 按声明同步路由
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
| `devproxy logs [caddy\|dnsmasq\|<路由名>]` | 跟随日志：服务本身（默认 `caddy`）或 sync 起的应用 |
| `devproxy trust` | 单独安装 CA 根证书（系统信任库 + NSS 库 / 系统钥匙串） |
| `devproxy config` | 显示当前生效配置与渲染出的路由片段 |
| `devproxy selftest` | 隔离 Caddy 实例端到端自检 |
| `devproxy uninstall` | 卸载：停服务、移除单元、DNS 路由与（可选）CA。默认保守保留你的配置、个人站点、Caddy 存储与备份 |
| `devproxy sync` | 按项目根目录的 `devproxy.toml` 同步路由声明（全组共享一份） |
| `devproxy run` | **前台**托管 Caddy（不装服务、不留状态，Ctrl-C 退出）。没有 systemd 的环境（容器 / WSL）用它替代服务 |
| `devproxy init` | 在项目里生成 `devproxy.toml`（路由名取目录名，启动命令按项目类型猜或用 `--command` / `--from` 指定） |

常用选项：

| 选项 | 适用命令 | 说明 |
|---|---|---|
| `--dns-backend auto\|resolved\|system` | `setup` / `doctor` | 强制 DNS 后端（默认取 `config.toml` 的 `dns_backend`） |
| `--domain-suffix <后缀>` | `setup` | 临时覆盖域名后缀（会过校验，非法值退出码 2） |
| `--no-trust` | `setup` | 跳过证书信任 |
| `--yes` | `setup` / `trust` | 非交互模式，不询问（需要提权的场景必须加） |
| `--rollback <目录>` | `setup` | 从指定备份目录回滚 |
| `--dry-run` | `uninstall` | 只打印将要执行的步骤，不做任何变更、不需要 sudo |
| `--purge` | `uninstall` | 直接删除 Caddy 存储、备份目录与下载的 caddy 二进制（不给就在交互时问） |
| `--remove-ca` | `uninstall` | 直接卸载 CA（不给就在交互时问；迁移来的 CA 默认保留） |
| `--port N` | `up` | 显式指定端口（默认自动分配）；被占用时直接失败 |
| `--port N` | `expose` | **必填**：上游已在监听的端口 |
| `--ready-timeout S` | `up` | 就绪等待秒数（默认取 `config.toml` 的 `ready_timeout`） |
| `--file <路径>` | `sync` | 显式指定 `devproxy.toml`（默认从当前目录逐级向上查找） |
| `--prune` | `sync` | 移除本文件里已删掉的路由（只动本文件声明过的；非交互时须加 `--yes`） |
| `--config <路径>` | `run` | 要加载的 Caddyfile（默认主 Caddyfile） |
| `--name <名字>` | `init` | 路由名（默认取项目目录名） |
| `--command <命令>` | `init` | 启动命令，如 `"npm run dev"`（不给就按项目类型猜） |
| `--from <路由名>` | `init` | 从已注册的同名路由导入它的启动命令 |
| `--force` | `init` | 覆盖已存在的 `devproxy.toml` |

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 运行失败（子进程退出、reload 失败、路由表损坏、迁移被拒绝、**`sync` 发现路由与声明冲突**、`run` 的 Caddy 配置校验失败……） |
| 2 | 参数错误（非法路由名 / 非法端口、缺少 `--`、未知日志服务名、非法 `dns_backend`、非法 `--domain-suffix`、**`sync` 的配置文件缺失或非法**、**`run --config` 指定的文件不存在**） |
| 3 | 前置条件不满足（Caddy 未运行、主 Caddyfile 不存在、`doctor` 体检不通过） |
| 4 | 需要提权但当前非交互（未加 `--yes`），**或用户在确认提示上选择了「否」** |

注意两个特例：`up` 缺少 `--` 或 `--` 后没有命令，按「参数错误」退出 **2**；
`up` 的退出码在命令正常结束时**就是子进程的退出码**。

退出码 4 表示「**需要人来拍板，但这次没有人拍**」——要么环境非交互且没加 `--yes`，
要么用户在确认提示上答了「否」。这两种情况下**工具什么都没改**，终端上也会明确说明。

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
不会被 devproxy 抢走。缺少 `--` 时工具直接报错退出（码 2）。

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
这是为了不占用 53 端口而做的取舍。落到实际语义上：

- `DNS=127.0.0.1:5353` 写在 resolved 的**全局段**，决定的是**这台机器全部域名**的
  解析都先问用户级 dnsmasq，**不只是 `*.test`**；
- `FallbackDNS=1.1.1.1 8.8.8.8` 是同一份 drop-in 里的**整机兜底**：dnsmasq 起不来、
  或它答不了的时候，全机解析退化为这两个公共解析器（而不是整机断网）；
- `Domains=~.<后缀>` 决定 dnsmasq 内部把哪个后缀解析到本机。

`Domains=~.<后缀>` 里的后缀**为什么只能是保留顶级域**，见
[「域名后缀只允许保留顶级域」](#域名后缀只允许保留顶级域) ——
真实公共顶级域会让**整台机器**该后缀的解析都被劫持。

作为配套，工具保证 dnsmasq 的配置里**始终带 `no-resolv`**
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
| 任意 | **Firefox 不读上面任何一处** | Firefox 只认自己 profile 里的 `cert9.db` |

两步是独立的：只装系统信任库，Chrome 照样报 `net::ERR_CERT_AUTHORITY_INVALID`；
只装 NSS 库，curl 又不认。Linux 上装 NSS 库需要 `certutil`
（`sudo apt install libnss3-tools`）。

**Linux 上装入 NSS 库后必须完全退出并重启 Chrome** —— 关窗口不算，要从托盘退出进程；
Chrome 只在启动时读取 NSS 库。

#### Firefox 要单独信任（工具不替你装）

**Firefox 有自己的证书库**（`~/.mozilla/firefox/<profile>/cert9.db`）——
它既不读 NSS 库，也不读系统信任库。因此 `devproxy trust` 跑完之后，
**Chrome 正常了，Firefox 仍会报证书错误** —— 这不是工具坏了，是 Firefox 的设计。

两种解法（**都要完全退出并重启 Firefox** 才生效）：

1. **手动导入**：Firefox → 设置 → 隐私与安全 → 证书 → 查看证书 → 导入 →
   选 `<storage>/pki/authorities/local/root.crt`
   （默认 `~/.local/share/caddy/pki/authorities/local/root.crt`）；
2. **让 Firefox 也认系统信任库**：在 `about:config` 里打开
   `security.enterprise_roots.enabled`（布尔值改为 `true`）。

`devproxy doctor` 在检测到本机装了 Firefox 时会把这条提示打出来；
`devproxy trust` 装完之后也会提醒一句。**这是提示，不是缺陷** ——
Firefox 没装 CA 不影响 doctor 的体检结论（Chrome / curl 侧可能完全正常）。

CA 根证书的路径是 `<storage>/pki/authorities/local/root.crt`
（默认 `~/.local/share/caddy/pki/authorities/local/root.crt`）。

---

## 配置

`~/.config/devproxy/config.toml`（**全部可选**，不写就用默认值；**工具不会自动生成默认配置文件**）：

```toml
# 域名后缀。devproxy up codeup → https://codeup.test
# 只允许 RFC 2606 / 6761 保留给本地使用的顶级域：test / example / localhost / invalid
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

# 额外放行的域名后缀（默认空 —— 不放行任何公共顶级域）。
# 见下面的「域名后缀只允许保留顶级域」。
# allowed_domain_suffixes = ["dev"]
```

各键的准确语义：

| 键 | 默认 | 说明 |
|---|---|---|
| `domain_suffix` | `test` | 单个 DNS 标签（小写字母/数字开头结尾，中间可含连字符，不含点，≤ 63 字符），**且必须是保留给本地使用的顶级域**（`test` / `example` / `localhost` / `invalid`）。非法值在加载配置时就报错 |
| `allowed_domain_suffixes` | `[]` | 额外放行的域名后缀（字符串数组）。默认**不放行任何公共顶级域**；写在这里 = 用户明确担责 |
| `dns_backend` | `auto` | `auto` = resolved 真的在跑就用 resolved，否则用 system。Linux 上可显式指定 `resolved` / `system`；**macOS 没有 systemd-resolved，显式写 `resolved` 会被当成参数错误拒绝（退出码 2）**，`auto` 在 macOS 上静默走每域解析器 |
| `admin_addr` | `127.0.0.1:2019` | `host:port` 形式。既是 Caddy 的 admin 监听地址，也是 reload 时的目标地址 |
| `http_port` / `https_port` | `80` / `443` | Caddy 的**全局选项**，写进主 Caddyfile 的全局块，改了就真的换监听端口 |
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

### 域名后缀只允许保留顶级域

`domain_suffix` 只能是 **RFC 2606 / RFC 6761 保留给本地使用的顶级域**：

| 顶级域 | 出处 |
|---|---|
| `test` | RFC 6761（**推荐**） |
| `example` | RFC 2606 |
| `localhost` | RFC 6761 |
| `invalid` | RFC 2606 |

`com` / `net` / `org` / `cn` / `dev` / `local` 这些**一律拒绝**，原因是三条：

- **整机解析会被劫持**：后缀会写进**整机范围**的 DNS 路由
  （Linux：`/etc/systemd/resolved.conf.d/devproxy.conf` 里的 `Domains=~.<后缀>`；
  macOS：`/etc/resolver/<后缀>`）。写成 `Domains=~.com` 之后，
  **这台机器全部 `.com` 域名**的解析都先问本地 dnsmasq —— 真实公共顶级域的解析被劫持；
- **浏览器 cookie 会串味**：拿真实域名做本地站点，登录态会带到真实站点上去；
- **HSTS 会直接拒绝**：`https://codeup.com` 被 HSTS 强制 HTTPS 且不允许用户例外，
  本地自签证书一律报错。

用**白名单**而不是黑名单是刻意的：将来新增的公共顶级域不会漏网
（黑名单一定会漏），而白名单里那四个顶级域本来就是给本地开发用的。

#### 确实要用别的后缀：显式放行

工具允许，但**默认不放行任何公共顶级域** —— 放行是**你明确担责**的选择，
写在 `config.toml` 里：

```toml
domain_suffix = "dev"
allowed_domain_suffixes = ["dev"]
```

- `allowed_domain_suffixes` 是**精确名单**，不是开关：放行了 `dev` 不等于放行了 `com`；
- 名单里的每一项仍要是单个 DNS 标签（小写字母/数字开头结尾、不含点）；
- 名单只影响 `domain_suffix` —— 工具**不会**把它写进任何系统配置，
  只是允许你把这个后缀用作本地站点的后缀。

**退出码的差别**（同一个非法值，来源不同）：

| 写法 | 退出码 | 含义 |
|---|---|---|
| 写在 `config.toml` 里 | **1** | 配置错误 |
| 用 `--domain-suffix` 传 | **2** | 参数错误 |

#### 升级提示：既有用户可能突然报错

加这道护栏之前，本机 `~/.config/devproxy/config.toml` 里可能已经写着一个
不在白名单里的 `domain_suffix`（例如 `dev`）。升级后**每一条命令都会报错**
（配置错误，退出码 1）。两种处理方式：

1. **改用保留顶级域**（推荐）：把 `domain_suffix` 改成 `test`（或 `example` /
   `localhost` / `invalid`），然后按下面这节的说明处理已有路由；
2. **显式放行**：在 `config.toml` 里加上
   `allowed_domain_suffixes = ["<你原来的后缀>"]`，行为与升级前一致。

```bash
devproxy down <name>    # 旧路由的域名在注册时就固化了 —— 换后缀要重新注册
devproxy up <name> -- <原来的启动命令>
```

（`domain_suffix` 改了**不会**自动重写已有路由，见上面「各键的准确语义」一节。）

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

## 项目级配置 `devproxy.toml`

`config.toml` 是**你机器的**配置（存 `~/.config/devproxy/`，不进版本库）。
`devproxy.toml` 是**项目的**配置 —— 放在项目仓库根目录、**提交进版本库**，
全组每个人 clone 下来跑一次 `devproxy sync`，就得到同一套路由。

### 快速创建：`devproxy init`

```bash
cd ~/work/tasks/我的项目
devproxy init                       # 生成 devproxy.toml
devproxy init --name web --command "npm run dev"
devproxy init --from my-running-svc # 从已在跑的路由导入它的启动命令
```

它会：

1. 找到**项目根**（向上找 `.git`；没有就用当前目录），在那里写 `devproxy.toml`；
2. 路由名默认取**目录名**（`My App` → `my-app`），可用 `--name` 改；
3. 启动命令按这个顺序来：
   - `--command` 显式给的（按 shell 的词法拆，引号和空格都认）；
   - `--from <路由名>` —— 从**已注册的同名路由**导入它的启动命令
     （路由表是全局的，所以必须指名，不能"把当前所有路由都导进来"）；
   - 都没有的话，**看项目类型猜**：`package.json` 里有 `dev` 脚本就猜
     `npm run dev`，有 `start` 就猜 `npm run start`，有 `Makefile` 就猜 `make dev`。
     **没有明确证据就不猜**（`pyproject.toml` 存在但看不出入口点时只给提示），
     猜错比不猜更糟；
4. 写好的文件**能被自己解析**（生成后会用同一套解析器读回来验证）。

```bash
$ devproxy init
✔ 已生成 /home/me/work/my-app/devproxy.toml
  路由名: my-app  →  https://my-app.test
  启动命令: npm run dev
  package.json 里有 dev 脚本 —— 请确认它就是你要的启动命令

下一步：
  1. 检查上面的启动命令对不对（多半要改）
  2. devproxy setup    # 只需一次：装 Caddy、写 DNS 路由、装 CA 信任
  3. devproxy sync     # 起服务
```

文件已存在时会拒绝（退出码 2），要覆盖加 `--force`。

### 手写

```toml
# devproxy.toml —— 项目根目录，提交进仓库

[[routes]]
name = "web"
command = ["npm", "run", "dev"]

[[routes]]
name = "api"
command = ["sh", "-c", "uv run uvicorn app:app --port $PORT"]

[[routes]]
name = "legacy"
mode = "expose"     # 不起进程，给已经在跑的服务开域名
port = 3000
```

### 字段

| 键 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 路由名，须是单标签 `[a-z0-9-]`（与 `devproxy up <name>` 同一个规则） |
| `command` | `mode="up"` 时是 | **argv 数组**，不是 shell 字符串 |
| `mode` | 否 | `"up"`（默认，起进程）或 `"expose"`（只注册路由） |
| `port` | `mode="expose"` 时是 | `expose` 模式：上游已在监听的端口。`up` 模式：写了就固定端口，不写就自动分配 |
| `ready_timeout` | 否 | 覆盖该条路由的就绪等待秒数 |

### ⚠️ `command` 不过 shell，`$PORT` 不会展开

`command` 是 argv 数组，直接交给 `subprocess.Popen` 执行，**中间没有 shell**。
所以 `["npx", "vite", "--port", "$PORT"]` 里的 `$PORT` 是**字面量**，不是端口号 ——
应用会收到一个叫 `$PORT` 的参数而报错。

要端口参数的应用，用 `sh -c` 包一层（和上面「两种应用，两种写法」一节是同一件事）：

```toml
command = ["sh", "-c", "npx vite --port $PORT --host 127.0.0.1"]
```

`sh` 在子进程启动后才展开 `$PORT`，那时 devproxy 已经把端口写进环境变量了。

### 怎么用

```bash
devproxy sync                # 从当前目录逐级向上找 devproxy.toml 并同步
devproxy sync --file <路径>  # 显式指定
devproxy sync --prune        # 顺带移除本文件里已删掉的路由
```

**文件发现**：从当前工作目录逐级向上找 `devproxy.toml`，找到第一个就用
（一路找到文件系统根）。所以在子目录里执行也能找到项目根的那份。

**子进程的工作目录**是 `devproxy.toml` 所在目录，不是你当前所在的子目录 ——
`npm run dev` 这类相对路径的命令才有意义。

### 同步语义

| 情况 | 动作 |
|---|---|
| 路由表里没有这条 | **创建**：`up` 模式起进程并注册，`expose` 模式只注册路由 |
| 路由表里有，声明与现状一致 | **跳过**（幂等，连跑两次第二次不动任何东西） |
| 路由表里有，但命令/端口变了 | **报告冲突，不动手**。打印现状、期望与修复命令，**绝不自动杀进程重启** |
| 路由表里有，但文件里已删掉这条 | 默认**不动**；`--prune` 才移除 |

**`--prune` 只移除"由本文件声明过"的路由** —— 手工 `devproxy up` 出来的路由
（路由表里没有 `source` 标记的）永远不会被它删掉。`--prune` 会按 `down` 的语义
一并收掉子进程，所以非交互环境里必须加 `--yes`（否则退出码 4）。

**冲突时退出码是 1** —— 配置漂移要让 CI 看得见。部分路由失败时会继续处理其余路由，
最后统一退出码 1，并在输出里列清哪些成功、哪些失败。

### 后台进程与日志

`up` 模式在 sync 里启动的应用是**后台进程**（sync 退出后应用继续跑，独立会话），
标准输出与错误收进 `~/.local/state/devproxy/logs/routes/<name>.log`。

看某个应用的输出：

```bash
devproxy logs web              # 跟随（Ctrl-C 退出）
tail -f ~/.local/state/devproxy/logs/routes/web.log   # 等价写法
```

`devproxy logs` 的参数可以是：

| 参数 | 看什么 |
|---|---|
| `caddy` / `dnsmasq` | **服务本身**的日志（Linux 走 journalctl，macOS 是文件） |
| `<路由名>` | **sync 起的应用**的日志 |

参数不认识时退 2，并把当前可用的路由日志名列出来。

**日志会轮转**：单个文件超过 **10 MB** 就改名成 `.log.1`（覆盖上一份），当前文件从头写。
所以每条路由的日志最多占 20 MB，不会无限涨 —— Spring Boot 这类应用在开发时
能以几 MB/小时 的速度写日志，不轮转的话跑几天就能吃掉几百 MB。

轮转在**写入端**做（devproxy 自己收应用的输出、自己写文件），不是靠 logrotate ——
这些日志是 devproxy 拉起的进程写的，只有写它的人能保证文件不会无限大。

### 与 `up` / `expose` 的关系

`sync` 就是批量版的 `up` + `expose`。两者共用同一套路由表与事务写入 ——
`sync` 建的路由和手工 `up` 的路由没有区别，`devproxy ls` 一样能列出来，
`devproxy down <name>` 一样能注销。

### 真实案例：codeup-manager

`codeup-manager`（`git@codeup.aliyun.com:msy/tools/codeup-manager.git`，资产清单管理工具，
FastAPI + Vite 前后端分离）原本靠一个已被删除的旧脚本 `devp` 起动态域名。
现在它的 `devproxy.toml` 长这样：

```toml
[[routes]]
name = "codeup-api"
command = ["sh", "-c", "uv run uvicorn codeup_manager.web.app:create_app --factory --reload --host 127.0.0.1 --port $PORT"]

[[routes]]
name = "codeup-web"
command = ["sh", "-c", "cd frontend && API_TARGET=https://codeup-api.test NODE_EXTRA_CA_CERTS=$HOME/.local/share/caddy/pki/authorities/local/root.crt __VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=codeup-web.test ./node_modules/.bin/vite --host 127.0.0.1 --port $PORT"]
```

一次 `devproxy sync` 得到 `https://codeup-web.test` 与 `https://codeup-api.test/docs`，
**两个端口都是自动分配的**。这里有三个值得抄的点：

1. **前端要代理到后端时，代理到后端的「域名」而不是端口**（`API_TARGET`）——
   这样后端也能用动态端口，不必牺牲动态分配换静态端口；
2. **node 不读系统信任库**，得用 `NODE_EXTRA_CA_CERTS` 把 devproxy 的根证书喂给它
   （浏览器、curl 走系统信任库，不需要这一步）；
3. **Vite 6 有 Host 校验**，要用自定义域名访问得传
   `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS`。

该仓库的完整说明见它的 `docs/devproxy-quickstart.md`。

---

## 目录与文件

```
项目根目录/
└── devproxy.toml     # 项目级路由声明（可选，提交进仓库，全组共享）

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
    └── routes/          # sync 起的后台应用的日志（<name>.log）

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

**有两种遗留环境会被工具直接拒绝迁移**，而不是无备份地硬来 —— 两种情况都以退出码 1 中止，
**一个文件都不改**：

- **`ExecStart=` 里解析不出 `--config`** —— 无法确认它用的是哪个 Caddyfile，猜错就会改错文件；
- **单元使用 `EnvironmentFile=`** —— 变量不在单元里，迁移无法把它们安全带到新单元。

修好单元后重跑 `setup` 即可：`--config` 写成 `--config <路径>` 或 `--config=<路径>` 都认；
`EnvironmentFile` 里的变量需要内联到 `Environment=`，或直接移除 `EnvironmentFile=`。

回滚：

```bash
devproxy setup --rollback ~/.local/state/devproxy/backups/2026-10-22T12-00-00Z
```

回滚会按备份逐个还原文件（原本不存在的文件会被删掉），部分条目失败时会把
「已还原了哪些」如实打印出来。

非交互环境里做迁移必须加 `--yes`，否则以退出码 4 拒绝（迁移会改写你的单元与配置，
不能不问一声就动手）。

---

## 卸载

```bash
devproxy uninstall --dry-run   # 先看它会做什么，不做任何变更、不需要 sudo
devproxy uninstall             # 交互式：逐项提问，最后再确认一次
devproxy uninstall --yes       # 非交互（CI / 脚本）必须加，否则退 4
```

**交互式会问什么**（这两个选择都有对应的 flag，给了就不再问）：

1. 「还要删除 Caddy 存储（CA 与证书）、迁移备份与下载的 caddy 二进制吗？」—— 默认**否**
2. 「要把 CA 从系统信任库卸载吗？」—— 只在 CA 属于「迁移前就存在」时问，默认**否**
3. 最后确认「即将执行以上卸载步骤，继续？」—— 默认**否**

答「是」之后工具会**重建并重画计划**，让你看清多出来的是哪几步，再进入最终确认。

**默认会做**（按这个顺序）：

1. 收掉由本工具启动、还挂着的子进程（按 `routes.json` 里的 pid + 启动时刻校验，不会误杀）
2. 停止并移除 `caddy`、`dnsmasq` 两个服务单元
3. 删除工具写入的 DNS 路由文件，并让解析器重新加载
4. 卸载 CA —— 但受下面两条规则约束
5. 删除工具生成的状态文件（`Caddyfile`、`routes.*`、`dns-route.json`、`caddy-storage.json`、`caddy-env.json`、`logs/`）

**默认保留**（不删）：

| 保留 | 原因 |
|---|---|
| `sites.caddy` | 你的个人站点 |
| `config.toml` | 你的配置 |
| `~/.local/share/caddy` | CA 与全部证书 —— 你自己另起的 caddy 可能也在用 |
| `~/.local/bin/caddy` | 可能是你自己装的 |
| `~/.config/dnsmasq/dnsmasq.conf` | 是**增量合并**的，原本就有。有改动前的备份就从最新备份还原，没有就原样保留并提示 |
| `backups/` | 回滚的唯一依据 |
| `loginctl enable-linger` | 整机级设置，你别的 user 服务可能依赖它 |

**CA 的两条规则**：

- **按指纹精确匹配**来删，不按昵称 —— 同一台机器上同名不同内容的根证书不会被误删；
- **迁移来的 CA 默认不卸载**：如果存在 `caddy-storage.json` 留档，说明这个 CA 在本工具接管**之前**就存在、
  你的站点在用。此时跳过并提示；确实要卸就显式加 `--remove-ca`。

**「连存储一起删」被选中时才额外删除**（交互答「是」，或显式 `--purge`）：
`~/.local/share/caddy`、`backups/`、`config.caddy_bin_path`。
注意最后一项默认是 `~/.local/bin/caddy` —— 如果你自己装过 caddy，这一步会把它一起删掉。

**迁移过的机器**：卸载不会自动还原遗留环境。原始 `caddy.service` 与 `Caddyfile` 在
`backups/<时间戳>/` 里，工具会在结尾把路径打出来；要回到迁移前的状态用
`devproxy setup --rollback <目录>`。备份默认保留，所以「先卸载、再回滚」也能恢复；
但 `--purge` 会把备份一起删掉，之后就来不及了。

`uninstall` 是幂等的：已经卸干净之后再跑一次会说「没有可卸载的东西」并成功退出。

---

## 没有 systemd 的环境：用 `devproxy run` 托管

`devproxy setup` 在 Linux 上靠 **systemd user 单元**托管 Caddy，在 macOS 上靠 **launchd**。
容器、WSL、精简发行版里这两样常常没有 —— 此时路由就没人托管了
（路由表是文件，但**得有个 Caddy 进程去加载它**）。

`devproxy run` 就是那个宿主：

```bash
devproxy run                    # 前台托管 Caddy，Ctrl-C 退出
devproxy run --config <路径>    # 加载另一份 Caddyfile（通常配另一组端口）
```

它的行为：

- **stdio 直通终端** —— Caddy 的日志实时打出来（`devproxy logs caddy` 跟随的是
  journal / 日志文件，这是直接接管进程本身）；
- **Ctrl-C 干净退出** —— 转发 SIGTERM 给 Caddy，等它优雅收尾（最多 10 秒，
  超时才强杀），devproxy 退出码 0；
- **不装服务、不写任何状态** —— 前台模式 = 不留痕迹（`routes.json` 之类一概不动）。

启动前有三道前置检查（不满足时退出码 **3**，并给出该平台对应的修复命令）：

| 情况 | 提示 |
|---|---|
| Caddy 二进制不在 | `devproxy setup` |
| 主 Caddyfile 不存在 | `devproxy setup` |
| **现役 caddy 服务还在跑** | Linux：`systemctl --user stop caddy`；macOS：`launchctl bootout system/com.devproxy.caddy` |

> 第三条只在加载**主 Caddyfile** 时生效 —— 那份正是服务单元加载的，两边抢同一批端口。
> `--config` 指向别的文件时通常用的是另一组端口，不该拦；真撞上端口冲突，
> Caddy 自己会报 `address already in use`，那比工具的推测准确。

**它和别的命令的区别**：

| | `run` | `setup` | `selftest` | `logs caddy` |
|---|---|---|---|---|
| 托管方式 | 前台进程 | systemd / launchd 服务 | 前台进程（跑完即删） | 不托管，只跟随日志 |
| 用哪套配置 | 现役主 Caddyfile | — | **隔离**实例（临时端口与 storage） | — |
| 写系统状态 | 不写 | 装服务、改 /etc | 不写（都在临时目录） | 不写 |
| 退出后 | 路由不再被服务 | 路由继续在 | 什么都没留下 | 无所谓 |

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
| 停掉 caddy 服务 | `systemctl --user stop caddy` | `launchctl bootout system/com.devproxy.caddy` |

**macOS 上 caddy 以 root 运行**：这是 macOS 绑 80/443 的唯一干净解法，代价是所有站点的
运行权限都提到 root，且 CA、storage、日志会留下 root 属主的文件。

**caddy 版本固定为 2.11.7**：sha256 直接写死在源码里，setup 下载后校验。
上游 release 的 `checksums.txt` 与实际托管的资产对不上（实测 tar.gz 与 deb 均不符），
所以工具**不信任**那个文件。

`devproxy selftest` 起一个**隔离的 Caddy 实例**：admin / HTTPS / HTTP 三个端口都在运行时
**动态取空闲端口**（固定端口会和并发自检、或你机器上已有的服务抢同一个实例），
配独立 storage 与独立配置目录。完整跑一遍注册 → 转发 → 跳转 → 注销 → 证书策略，
**不碰生产的 443/2019**，也不会把沙箱 CA 装进宿主的信任库。

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
- **Chrome 正常但 Firefox 仍报证书错误**：Firefox 有自己的证书库
  （`~/.mozilla/firefox/<profile>/cert9.db`），**不读** NSS 库也不读系统信任库。
  手动导入 CA，或在 `about:config` 打开 `security.enterprise_roots.enabled`，
  两种做法都要**完全退出并重启 Firefox**。详见上面「Firefox 要单独信任」一节。
- **升级后每条命令都报「配置项 domain_suffix 非法」**：新加的顶级域白名单挡住了你原来的
  `domain_suffix`。看上面「升级提示：既有用户可能突然报错」一节。
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
- **`devproxy run` 提示「caddy 服务仍在运行，80/443 已被占用」**：先按提示停掉服务
  （Linux `systemctl --user stop caddy`，macOS `launchctl bootout system/com.devproxy.caddy`），
  或者用 `--config` 指向一份用别的端口的 Caddyfile。

真机验证清单见 [docs/verification-checklist.md](docs/verification-checklist.md) ——
里面列了自动化测试覆盖不到、必须在真实机器上逐条跑的行为。

---

## 开发与 CI

单元测试基本上是**封闭的**：不碰真实系统、不需要 caddy 二进制、不需要网络，
也不会写进你的家目录（`HOME` 在测试里被指向临时目录）。

唯一的例外是 NSS 那组用例：它需要用**真实的 `certutil`**（`libnss3-tools`）在临时目录里
建一个真的 NSS 库，验证「按证书内容判定信任」这类只有真工具才验得了的行为。
机器上没有 `certutil` 时整组自动跳过，不影响其余用例；CI 因此专门装了 `libnss3-tools`。

```bash
python3 -m pytest -q --ignore=tests/test_integration.py   # 单元套件
python3 -m pytest -q tests/test_integration.py             # 真机集成（需要真实 caddy）
```

CI 在 `.github/workflows/ci.yml`：push 到 `main`、提 PR 或手动触发时，
在 Python 3.11 / 3.12 / 3.13 上跑**单元套件**（真机集成测试不进 CI ——
它依赖真实 caddy 二进制与真实网络）。

### 代码结构

```
src/devproxy/
├── cli/          # 命令层：一命令一文件（up / expose / down / ls / logs / trust /
│   │             #   config / selftest / setup / doctor / uninstall / sync / run）
│   ├── _shared.py   # 命令模块的公共接缝（console、退出码、build_context、组件装配）；
│   │                #   命令经它**在调用时查找**依赖，测试打桩才能生效
│   ├── _runtime.py  # 退出码常量与 app 入口
│   ├── _context.py  # 上下文组装（build_context：配置、路由表、Caddy 客户端）
│   ├── _render.py   # 表格与提示的渲染
│   ├── _typer.py    # Typer 参数切分组（把 `--` 之后的参数原样交给子命令）
│   ├── _migrate.py  # 迁移与回滚的命令层编排
│   └── _worker.py   # 后台 up 工作进程（sync 拉起应用时用它托管子进程）
├── core/         # 基础设施：config（配置加载与校验）、paths（目录与状态文件）、
│                 #   backup（迁移备份）、caddy_env（单元环境变量留档）
├── gateway/      # 运行期网关：routes（路由模型与确定性渲染）、store（事务写入：
│                 #   flock → 渲染 → validate → 落盘 → reload → 失败回滚）、
│                 #   runner（up 的子进程监管）、caddy（Admin API）、names（域名与路由名）、
│                 #   selftest（隔离自检）、project / sync（项目级声明）、
│                 #   foreground（run 的前台托管）
├── lifecycle/    # 环境生命周期：setup、doctor、uninstall、
│                 #   migrate/{caddyfile, legacy, install, rollback}（迁移与回滚）
├── components/   # 四个可部署组件：caddy / certs / dns / dnsmasq；
│                 #   base 定义组件协议 —— check() -> Status（永不抛异常）、
│                 #   plan() -> list[Action]（空 = 无变更）、apply(actions)
└── platform/     # 平台接缝：base（协议：Platform / Action / Status / ElevatedSession）、
                  #   linux（systemd + resolved）、macos（launchd + /etc/resolver）、
                  #   fake（测试假平台，写入一律进沙箱目录）
```

依赖方向（每层只依赖它下方的层，没有反向依赖）：

```
cli → lifecycle / gateway / components → platform / core
```

- `core` 不依赖任何内部模块 —— 最底层；
- `platform` 只依赖 `core`（用它的路径工具）；
- `components` 依赖 `core` + `platform`，另有一处**函数内**延迟 import
  `gateway.caddy`（渲染主 Caddyfile 的模板，避开循环依赖）；
- `lifecycle` 依赖 `core` / `gateway` / `components` / `platform`；
- `cli` 依赖全部。

测试是**镜像结构**的：`tests/test_component_certs.py` 对着 `components/certs.py`，
以此类推；跨层的端到端行为在 `tests/test_integration.py` 与 `devproxy selftest`。

---

## 仓库里的历史文件

仓库根目录的 `Caddyfile` 是**迁移前**的手工配置。**在 `devproxy setup` 的迁移真正跑过之前，
现役的 `caddy.service` 仍然指向它**（迁移会先备份它、再把单元改指向
`~/.config/devproxy/Caddyfile`）；迁移完成后它才只剩历史参考价值。

根目录的 `devp` 脚本**已删除** —— 它是 `devproxy` 的 1.0 前身：直接 PATCH 现役
Caddy 的 Admin API 注册路由，注册的东西新工具既看不见也删不掉。它的来龙去脉
记在 [docs/legacy-devp.md](docs/legacy-devp.md)。`Justfile` 现在只是转发到
`devproxy` 的兼容壳。
