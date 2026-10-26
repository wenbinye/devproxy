# 迁移前的遗留文件

这里存放的是 **2026-10-22 devproxy 迁移跑之前**这台机器上的手工配置：

- `Caddyfile` —— 手工维护的 Caddy 配置。迁移把它拆成
  `~/.config/devproxy/Caddyfile` + `sites.caddy`，并备份、改写了
  `caddy.service` 的 `ExecStart`。

保留在这里只是为了**查历史**（比如「当时那个 `MINIKUBE_IP` 的站点块长什么样」）。
这些文件**不再被任何服务读取** —— 现役配置在 `~/.config/devproxy/` 下。

同理，仓库根目录曾经的 `caddy.service`（未跟踪）与 `site/`（一个测试页）已删除，
两者都是迁移期的临时产物。
