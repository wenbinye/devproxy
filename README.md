# local-https

基于 [Caddy](https://caddyserver.com/) 的本地 HTTP/HTTPS 静态文件服务器，纯用户目录运行（无 root 常驻进程），通过 systemd 用户级服务开机自启，用于本地开发调试。

- `http://localhost`（80 端口）
- `https://localhost`（443 端口，本地 CA 自动签发证书，已启用 HTTP/2、HTTP/3）
- `https://*.local.yikuaigao.com`（443 端口，通配符反代到 minikube 中的 APISIX 网关，如 `nginx.local.yikuaigao.com`、`vault.local.yikuaigao.com`）

## 快速开始

前置要求：`just`、`curl`、`sudo`、x86_64 架构（arm64 需改 Justfile 中下载链接的 `arch` 参数）

> `just setup` 过程中有 2 次 sudo 授权：`setcap`（绑定 80/443）与 `loginctl enable-linger`（开机自启）。

```bash
just setup   # 安装 caddy + 授权 80/443 + 注册 systemd 自启动服务
just trust   # 信任本地 CA 根证书（curl 与 Chrome 各需装一次）
just status  # 健康检查
```

## 证书信任

`tls internal` 使用 Caddy 内置 CA 签发证书。不同客户端读不同的信任库，**两个都要装**：

| 客户端 | 信任库 | 命令 |
|---|---|---|
| curl、Go、Java 等 | 系统信任库 | `just trust-system` |
| **Chrome / Chromium** | NSS 库 `~/.pki/nssdb` | `just trust-nss` |

`just trust` = 两者都执行。Chrome **不读**系统信任库，只认 NSS 库——只装系统信任库会导致 Chrome 报 `net::ERR_CERT_AUTHORITY_INVALID`，而 curl 一切正常。NSS 库安装依赖 `certutil`（`sudo apt install libnss3-tools`）。

装入 NSS 库后需**完全退出并重启 Chrome**（关闭所有窗口可能只是退到后台进程，需从托盘退出），Chrome 只在启动时读取 NSS 库。

## 命令

| 命令 | 说明 |
|---|---|
| `just setup` | 一键安装（= `install` + `grant` + `enable`） |
| `just install` | 下载 caddy 二进制到 `~/.local/bin` |
| `just grant` | `sudo setcap` 授权绑定 80/443（升级 caddy 后需重跑） |
| `just enable` / `just disable` | 注册/取消 systemd 用户级自启动服务 |
| `just start` / `just stop` / `just restart` | 启动 / 停止 / 重启服务（systemd 管理） |
| `just reload` | 修改 Caddyfile 后热加载（不断开现有连接） |
| `just logs` | 实时查看服务日志（journalctl） |
| `just run` | 前台运行（调试用，不经 systemd） |
| `just validate` / `just fmt` | 校验 / 格式化 Caddyfile |
| `just trust` | 信任本地 CA 根证书（系统信任库 + NSS 库） |
| `just trust-system` | 仅装入系统信任库（curl 等） |
| `just trust-nss` | 仅装入 NSS 库（Chrome/Chromium） |
| `just status` | systemd 状态与 http/https 响应检查 |

## systemd 用户级服务

服务单元安装于 `~/.config/systemd/user/caddy.service`（源文件在本仓库），已开启 **linger**（`loginctl enable-linger`），开机后无需登录用户会话即自动运行。

常用原生命令：

```bash
systemctl --user status caddy      # 查看状态
systemctl --user restart caddy     # 重启
journalctl --user -u caddy -f      # 跟踪日志
loginctl show-user $USER | grep -i linger   # 确认 linger 开启
```

## 目录结构

```
.
├── Caddyfile           # Caddy 配置（80/443 + tls internal 本地证书）
├── Justfile            # 安装与运维命令
├── devp                # 本地开发动态域名网关 CLI（Python stdlib 单文件）
├── caddy.service       # systemd 用户级服务单元（本机文件，未纳入 git）
├── README.md
├── docs/               # 设计规格等文档
├── site/               # 站点根目录，静态文件放这里（本机文件，未纳入 git）
└── tests/              # devp 单元/集成测试
```

数据与证书存放于 `~/.local/share/caddy/`，本地 CA 根证书路径：

```
~/.local/share/caddy/pki/authorities/local/root.crt
```

## 本地应用网关（转发到 minikube APISIX）

`*.local.yikuaigao.com`（如 `nginx.local.yikuaigao.com`、`vault.local.yikuaigao.com`）不是
Caddy 自己托管的站点，而是**反向代理**到 minikube 里的 APISIX 网关，由 APISIX 按 Host 头
路由到具体的 k8s 服务：

```
浏览器 https://<app>.local.yikuaigao.com
  │  DNS：dnsmasq address=/.local.yikuaigao.com/127.0.0.1
  ▼
Caddy :443（tls internal 通配符证书 *.local.yikuaigao.com，保留原 Host 头）
  │  HTTP → http://<minikube-ip>:30800
  ▼
minikube apisix-gateway NodePort 30800 → APISIX 9080
  │  按 Host 匹配 ApisixRoute
  ▼
对应 ns 里的 Service（如 default/nginx:80、vault/vault:8200）
```

- **通配符站点块**：Caddyfile 里是 `*.local.yikuaigao.com` 一个块承接所有本地应用；
  `tls internal` 会签发通配符证书。新增本地应用**只需在 k8s 侧加一条同 ns 的 ApisixRoute**
  （ApisixRoute CRD 的 backends 不支持跨 namespace 引用），Caddyfile 不用动。
  未注册 ApisixRoute 的域名会被转发到 APISIX 并返回 404。某个域名要走不同逻辑时，再加一个
  具体域名的站点块即可（具体块优先于通配符）。
- **上游地址**：`{$MINIKUBE_IP:192.168.49.2}:30800`。`MINIKUBE_IP` 由 `caddy.service` 的
  `Environment=` 提供；minikube 换 IP 时改 systemd 环境变量并 `systemctl --user restart caddy`
  即可，不用改 Caddyfile（冒号后的值是环境变量缺失时的兜底，保证手动 `caddy reload` 也能工作）。
- **Host 头**：`reverse_proxy` 默认保留原始 Host，APISIX 才能按域名匹配路由；不要加
  `header_up Host {upstream_hostport}`。
- **客户端真实 IP**：APISIX 的 nginx `real_ip_header` 固定为 `X-Real-IP`，Caddy 默认不发这个头，
  所以在 `reverse_proxy` 块里补了 `header_up X-Real-IP {remote_host}`，否则网关日志里的
  `$remote_addr` 只是 minikube 网关 IP。
- **80 端口**：这些域名在 80 上自动 308 跳转到 443。

对应的 k8s 侧清单示例：`k8s-deployment` 仓库的 `nginx/overlays/local/apisixroute.yaml`。

## 常见问题

- **升级 caddy 后服务反复重启失败**（`permission denied` 绑定 80/443）：`setcap` 授权随二进制替换丢失，重跑 `just grant` 后 `just restart`。
- **Chrome 报 `net::ERR_CERT_AUTHORITY_INVALID`（连接不是私密连接）**：Chrome 不读系统信任库，只认 NSS 库。执行 `just trust-nss` 后**完全退出并重启 Chrome**（不是只关窗口，需从托盘退出进程）。若 `certutil` 缺失，先 `sudo apt install libnss3-tools`。诊断：`certutil -L -d sql:~/.pki/nssdb` 应能看到 `Caddy Local Authority` 且信任标志为 `C,,`。
- **Firefox 仍提示证书不受信任**：Firefox 使用独立证书库，在「设置 → 隐私与安全 → 证书」中导入上面的 `root.crt`。
- **Caddy 重新生成了 CA 之后证书又失效**：根证书路径随 CA 变化，重跑 `just trust` 重新装入两个信任库。
- **移动项目目录**：Caddyfile 中两处 `root` 与 `caddy.service` 中两处路径为绝对路径，需同步修改后 `just enable` 重新安装。
- **想要 80 自动跳转 443**：删除 Caddyfile 中的 `http://localhost` 块，然后 `just reload`，Caddy 会自动在 80 上生成到 443 的重定向。
- **`just run` 端口冲突**：前台调试前先 `just stop` 停掉 systemd 实例。

## devp — 本地开发动态域名网关

`*.test` 域名下的本地项目反向代理：一条命令启动，自动分配空闲端口并注册
`https://<name>.test`，正常退出/信号退出自动注销。`.test`（本机进程）与
`.local.yikuaigao.com`（minikube/APISIX）两条链路互不影响。

前置条件：已完成 `just setup`（Caddy 服务运行，Admin API 在 `127.0.0.1:2019`），
且 dnsmasq 已配置 `*.test` 解析到本机。

```bash
just install-devp                      # 安装 CLI（~/.local/bin/devp）

devp up codeup-web -- sh -c 'npx vite --port "$PORT" --host 127.0.0.1'            # → https://codeup-web.test
devp up codeup-api -- sh -c 'uv run uvicorn app:app --host 127.0.0.1 --port "$PORT"'  # → https://codeup-api.test
devp up codeup-api -- sh -c 'gunicorn app:app --bind 127.0.0.1:$PORT'             # gunicorn 不读 PORT

devp ls                                # 查看已注册路由（含可达性）
devp down codeup-web                   # 手动清理残留路由
devp selftest                          # 隔离实例端到端自检
```

### 命令

| 命令 | 说明 |
|---|---|
| `just install-devp` | 安装 `devp` 到 `~/.local/bin/devp`（修改 `devp` 脚本后需重跑） |
| `devp up <name> [--port N] [--ready-timeout S] -- CMD...` | 分配端口、启动命令并注册 `https://<name>.test`；`--` 后的启动命令必填 |
| `devp ls` | 列出所有已注册的 devp 路由（域名 → 上游端口 → 可达性） |
| `devp down <name>` | 注销 `<name>.test`，用于清理崩溃残留 |
| `devp selftest` | 起隔离 Caddy 实例做端到端自检 |

### `--` 分隔

`devp up` 的完整写法是：

```bash
devp up <name> [--port N] [--ready-timeout S] -- CMD...
```

`--` **之前**是 devp 自己的参数（name、`--port`、`--ready-timeout`），`--` **之后**
全部原样传给子进程。缺少 `--` 会直接报错。示例：

```bash
# 原生读 PORT 环境变量的应用：直接写命令
devp up next-app -- npx next dev
devp up cra-app -- npm start

# 需要显式端口参数的应用：用 sh -c 让 shell 在 devp 注入 PORT 后再展开
devp up codeup-web -- sh -c 'npx vite --port "$PORT" --host 127.0.0.1'
devp up codeup-api -- sh -c 'uv run uvicorn app:app --host 127.0.0.1 --port "$PORT"'

# 应用完全不读 PORT：显式指定端口
devp up fixed --port 3000 -- python3 -m http.server 3000
```

要点：

- **应用接入分两种**：
  1. **原生读 `PORT` 环境变量的应用**（Next.js / CRA 等）——直接写命令即可，
     无需显式 `--port`。
  2. **需要显式端口参数的应用**（Vite / uvicorn / gunicorn 等）——用 `sh -c '...'` 包一层，
     让 shell 在 devp 启动后展开 `$PORT`。注意 **gunicorn 不原生读 `PORT`**，必须写成
     `sh -c 'gunicorn app:app --bind 127.0.0.1:$PORT'`。
- **不要直接写 `--port $PORT`**：devp 用 `subprocess.Popen(argv)` 直接执行、**不经过
  shell**，命令里的 `$PORT` 会被用户自己的 shell 提前展开（此时 devp 尚未分配端口，值为
  空串），应用会收到缺值参数。用单引号包住 `sh -c` 的脚本即可把展开推迟到 devp 启动后。
- **Host 校验放行**：请求的 Host 头是 `<name>.test`，Vite 需 `server.allowedHosts`、
  Django 需 `ALLOWED_HOSTS` 加上该域名。
- **子进程脱离控制终端**：devp 以 `start_new_session=True` 启动子进程（独立进程组，便于
  退出时按组清理孙进程）。代价是子进程**没有控制终端**，依赖 stdin 的交互式启动语义会变
  （需要 TTY 交互的启动方式不适用）；stdout/stderr 仍直透当前终端。
- **caddy 重启后**最长 30s 内已注册域名 404（心跳周期内自动恢复）。
- 路由按单标签 `<name>.test` 注册，`name` 由 `[a-z0-9-]` 校验（长度 ≤ 63），不得含点；
  分支信息编码进名字（如 `codeup-featx`）。
- 注册协议：Admin API GET 路由数组 → 插到 `*.test` 通配兜底之前 → PATCH 回写
  （POST 追加会被通配路由遮蔽）；注销按 `@id` DELETE。详见
  `docs/superpowers/specs/2026-10-21-devp-dynamic-domain-gateway-design.md`。

### 证书

`*.test` 站点使用 Caddy 的 **on-demand TLS**：每个域名首次访问时由本地 CA 单独签发
证书（`~/.local/share/caddy/certificates/local/<域名>/`）。**不要**改回 `tls internal`
的通配证书——`*.test` 属于二级通配符，OpenSSL 与浏览器都会拒绝其主机名匹配
（实测 `curl` exit 60: no alternative certificate subject name matches）。
on-demand 未配置 `ask` 端点（Caddy 启动日志会有 abuse 告警）：仅本机使用，可接受。
这意味着**本机任意进程都能为任意 `.test` 名称取得被系统信任的证书**——信任范围仅限本机，
已作为可接受风险记录（若要收紧，需给 Caddy 配 `ask` 回调做白名单）。

### 自检（selftest）

`devp selftest` 起一个**隔离的 Caddy 实例**（admin 20191 / https 14443 / http 14480，
独立 storage），完整跑一遍注册、转发、注销与 80→443 的 301 跳转，**不碰生产 443/2019**。
它使用沙箱自身 CA 的校验上下文发起真实 HTTPS 请求（带主机名校验），能抓住「证书退回通配」
这类回归。

### 已知边界

- **caddy 重启/重载窗口**：已注册域名最长 30s 内返回 404，心跳周期内自动恢复。
- **崩溃残留**：devp 被强杀（如 `kill -9`）时路由可能残留；`devp ls` 会标注不可达，
  用 `devp down <name>` 手动清理。
