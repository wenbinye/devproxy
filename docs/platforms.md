# 平台支持与差异

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
