# 更新日志

本文件记录 devproxy 的**用户可见**变化。格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

> 0.1.0 之前没有正式发布，本文件从 0.1.0 起记。

## [未发布]

### 新增

- `devproxy logs <名字> --edit`（短选项 `-e`）：用 `$EDITOR` 打开日志文件。
  没设 `EDITOR` / `VISUAL` 时依次探测 `vim` / `vi` / `nano`。
- `devproxy logs --list` 逐条给出日志文件的**完整路径**（不用自己拼）。
- `devproxy ls` / `ps` 按**应用名升序**输出 —— 路由表是集合，输出稳定可预期。
- shell 补全：`devproxy --install-completion`（bash / zsh / fish / powershell），
  并补上参数值域（`logs` / `down` 补路由名与服务名）。
- `devproxy uninstall`：停服务、移除单元、删 DNS 路由，可选连 CA 一起卸
  （`--remove-ca` / `--purge`），默认保守保留你的配置与站点。
- `devproxy init`：在项目里生成 `devproxy.toml`（PHP / Node / Makefile 项目都能用）。

### 变更

- 命令面对齐 **docker compose**：项目级操作统一为 `up` / `down` / `ps` / `logs`，
  原来的 `sync` / `stop` 并入 `up` / `down`。
- `devproxy logs` 在项目目录里裸跑 = 跟**本项目全部应用**（多个应用时每行带彩色标签）。

### 修复

- 站点块补 `X-Forwarded-Port` —— 应用的绝对 URL 不再带动态端口。
- 子进程调用加超时：`certutil` 之类卡在密码提示上的进程不再无限写盘
  （曾把 `/home` 撑到 100%）。
- 域名后缀白名单：阻止把整机解析劫持到非预期后缀（如 `.com`）。

## [0.1.0]

- 首个版本：`setup` / `up` / `expose` / `down` / `ls` / `logs` / `trust` /
  `doctor` / `config` / `selftest`。
