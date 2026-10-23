# `docs/superpowers/` —— 历史规划与决策记录

这个目录是 **2026-10-21 ~ 2026-10-23 建造期**留下的过程记录，**不是当前设计的说明**。

## 里面有什么

| 目录 | 内容 |
|---|---|
| `specs/` | 两份设计规格：`devp`（1.0 前身）与 `devproxy`（本工具）的设计决策表 |
| `plans/` | 四份实施计划：按 bootstrap → core/gateway → migrate/macos → 收尾的顺序推进 |
| `decisions/` | 三个阶段的完成台账（ledger）与真机实测报告 |

## ⚠️ 它们描述的是**更早**的代码结构

计划与规格里写的模块布局是**增量搭建时**的样子（`proxy.py` / `migrate.py` / `cli.py`
等平铺模块）。**当前**的结构已经按职责重排过：

```
src/devproxy/
├── cli/          # 命令层（一命令一文件 + 公共接缝）
├── core/         # 配置、路径、备份
├── gateway/      # 路由表、事务写入、子进程监管、Admin API
├── lifecycle/    # setup / doctor / uninstall / migrate
├── components/   # caddy / certs / dns / dnsmasq
└── platform/     # linux / macos / fake
```

**当前结构、命令清单、配置项与行为约定，一律以仓库根目录的
[README.md](../../README.md) 为准。** 这里的内容在需要追溯「当时为什么这么定」时才查。

## 为什么留着

- **决策要可追溯**：为什么用精确站点块而不是通配符、为什么先备份再动手、
  为什么路由表用文件而不是内存 —— 这些理由在规格的决策表里，重写代码时不能丢。
- **真机实测报告有独立价值**：`decisions/fixwave-real-machine-report.md` 与
  `real-machine-first-run-report.md` 记的是**真实机器上跑出来的**结果与踩过的坑
  （macOS 的 launchd 行为、`/etc/resolver` 格式、非图形会话里改系统信任设置被拒…）。
  这些不是代码能表达的。
- **待办清单不是待办**：`plans/` 里的步骤都已完成（见 `decisions/` 里的台账），
  别把它当成还没做完的待办清单。

## 与其它文档的关系

- [README.md](../../README.md) —— **现状**：命令、配置、目录结构、故障排查
- [../verification-checklist.md](../verification-checklist.md) —— 必须在真机上跑的行为
- [../legacy-devp.md](../legacy-devp.md) —— 旧脚本 `devp` 为什么消失
