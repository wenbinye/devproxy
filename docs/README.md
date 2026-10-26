# devproxy 文档

[README](../README.md) 是快速上手（安装 → 起服务 → 常用命令）；这里是各专题的详细说明。

## 按场景找

| 你想做的事 | 看哪篇 |
|---|---|
| 第一次装、第一次跑 | [README](../README.md) |
| 查某个命令怎么用、退出码是什么 | [commands.md](commands.md) |
| 把自己的服务挂到域名上 | [apps.md](apps.md) |
| 域名解析不生效 / 证书报错 | [dns-and-certs.md](dns-and-certs.md) |
| 改配置（后缀、端口、存储路径） | [configuration.md](configuration.md) |
| 让全组共享一套域名（`devproxy.toml`） | [project-config.md](project-config.md) |
| 迁移遗留的 caddy 服务 / 回滚 / 卸载 | [migration-and-uninstall.md](migration-and-uninstall.md) |
| 容器 / WSL 里没有 systemd | [platforms.md](platforms.md) |
| 出问题了 | [troubleshooting.md](troubleshooting.md) |
| 改代码、跑测试 | [development.md](development.md) |

## 全部文档

- [commands.md](commands.md) —— 全部命令、选项、退出码
- [apps.md](apps.md) —— 应用接入：`expose`、`$PORT` 的两种写法、注意事项
- [dns-and-certs.md](dns-and-certs.md) —— DNS 路由、证书信任（含 Firefox）、`doctor` 与 `setup` 分工
- [configuration.md](configuration.md) —— `config.toml` 全部配置项、域名后缀白名单、storage 留档
- [project-config.md](project-config.md) —— `devproxy.toml`：`init` / `up` / `down` 的声明式工作流
- [platforms.md](platforms.md) —— 目录与文件、平台差异、没有 systemd 时用 `devproxy run`
- [migration-and-uninstall.md](migration-and-uninstall.md) —— 迁移与回滚、卸载
- [troubleshooting.md](troubleshooting.md) —— 故障排查
- [development.md](development.md) —— 开发与 CI、代码结构、仓库里的历史文件

## 其它

- [verification-checklist.md](verification-checklist.md) —— 真机验证清单（自动化测试覆盖不到、必须在真机器上跑的行为）
- [legacy-devp.md](legacy-devp.md) —— 旧脚本 `devp` 为什么消失
- `design/` —— 建造期的设计规格与决策记录（历史档案，不是当前设计）
