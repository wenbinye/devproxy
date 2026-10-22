# 旧脚本 `devp` 的去向

`devp` 是 `devproxy` 的 **1.0 前身**：一个单文件 Python 脚本，只做一件事 ——
把本地跑起来的服务挂到 `https://<name>.test` 上。

它已于本轮收尾中从仓库删除（连同它的测试 `tests/test_devp.py`）。这份说明
留在仓库里，是为了让"它为什么不见了"可追溯，而不是无声消失。

## `devp` 做过什么

- `devp up <name> -- CMD...`：分配一个空闲端口、启动子进程（独立进程组），
  通过 Caddy Admin API 把路由插进 `/config/apps/http/servers/srv0/routes`
- `devp down <name>`：删掉路由，并把子进程组一起收掉
- `devp ls`：列出已注册的路由
- `devp selftest`：起一个隔离的 Caddy 实例做端到端自检
- 路由状态存在 `~/.local/state/devp/` 下的一个 JSON 文件里，用 flock 串行化

设计文档留在 `docs/superpowers/specs/2026-10-21-devp-dynamic-domain-gateway-design.md`。

## 为什么被 `devproxy` 取代

`devp` 直接 PATCH **现役 Caddy** 的 Admin API 配置：

```
PATCH /config/apps/http/servers/srv0/routes
```

这带来两个结构性问题：

1. **它注册的东西，别的工具看不见也删不掉。** 路由藏在 Caddy 的内存配置里，
   没有独立的状态文件；`devproxy` 只能看到自己写进 `routes.json` 的路由。
   两套工具同时用同一台机器的 Caddy，路由就会分裂。
2. **它不碰系统。** 没有部署、没有 DNS 路由、没有证书信任、没有开机自启 ——
   这些都得手工来。新工具把这些一次性做完。

`devproxy` 用**文件即真相**的方式取代它：路由写在 `state/routes.json`，
渲染成 `state/routes.caddy`，由主 Caddyfile `import`；改动走
"加锁 → 改表 → 渲染 → 校验 → 落盘 → reload" 的事务，reload 失败会回滚。

## 迁移到 `devproxy` 之后

| 旧命令 | 新命令 |
|---|---|
| `devp up <name> -- CMD...` | `devproxy up <name> -- CMD...` |
| `devp down <name>` | `devproxy down <name>` |
| `devp ls` | `devproxy ls` |
| `devp selftest` | `devproxy selftest` |

命令名与参数语义保持一致，行为上的主要差别：

- `devproxy up` 的路由会**常驻**在 `routes.json` 里（`devp` 的心跳机制
  会在上游挂掉时自动注销路由）；
- `devproxy` 首次注册路由时会让 Caddy 生成内部 CA，需要再跑一次
  `devproxy trust` 才能让 curl / Chrome 信任；
- `devproxy` 需要先跑一次 `devproxy setup`（部署 dnsmasq、DNS 路由、
  证书信任、开机自启）。

旧的 `devp` 状态目录 `~/.local/state/devp/` 可以直接删掉 —— 它不再被读取。

## 环境侧的收尾（不在仓库范围内）

仓库里删掉了脚本，但**机器上**还可能留着：

- `~/.local/bin/devp`（PATH 上的旧副本）
- 旧 `devp` 往 Caddy 里插的路由（需要手工从 Caddy Admin API 里删掉，
  或直接跑 `devproxy setup` 让新工具接管整份配置）

这两件事由机器的主人处理。
