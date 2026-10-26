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

- **默认语言改为英文**：CLI 输出、错误消息、代码注释与文档统一用英文；
  中文文档仍以**完整镜像**的形式维护在 `docs/zh-CN/` 下（以及 `README.zh-CN.md`）。
- 命令面对齐 **docker compose**：项目级操作统一为 `up` / `down` / `ps` / `logs`，
  原来的 `sync` / `stop` 并入 `up` / `down`。
- `devproxy logs` 在项目目录里裸跑 = 跟**本项目全部应用**（多个应用时每行带彩色标签）。

### 修复

- **macOS**：`sync` 不再把**还在跑**的应用拆了重建 —— 存活判定读的是 `/proc`，
  而 macOS 没有它，于是每个活着的应用都像死了。
- **macOS**：后台应用的孙子进程又能被清理了。那边整个进程组清理被跳过
  （没有 `WNOWAIT`，leader 在清组之前就被回收），结果留下一堆野进程。
- **已在真 Mac 上验证**（2026-10-26 / 10-27）：macOS 的整份真机验证清单在一台
  macOS 26.1 上跑完，每一条都有了结论。顺带记下两个行为差异：`security add-trusted-cert`
  在**没有图形会话**时设不上信任标志 —— 所以 `devproxy trust` 得在那台 Mac 自己的
  终端里跑；跑过之后，钥匙串就是**唯一**的信任库 —— 自带 `curl` 也认它
  （不需要往 `/etc/ssl/cert.pem` 里塞 CA）。
- 站点块补 `X-Forwarded-Port` —— 应用的绝对 URL 不再带动态端口。
- 子进程调用加超时：`certutil` 之类卡在密码提示上的进程不再无限写盘
  （曾把 `/home` 撑到 100%）。
- 域名后缀白名单：阻止把整机解析劫持到非预期后缀（如 `.com`）。

## [0.1.0]

- 首个版本：`setup` / `up` / `expose` / `down` / `ls` / `logs` / `trust` /
  `doctor` / `config` / `selftest`。
