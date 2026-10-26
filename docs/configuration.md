# 配置（config.toml）

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
