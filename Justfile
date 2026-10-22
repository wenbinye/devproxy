# 已废弃：请直接使用 devproxy
#
# 本文件只是一个转发壳，只为兼容旧的 `just` 习惯：
# 全部逻辑都在 devproxy 命令里，这里不做任何实现。
#
#   just setup   →  devproxy setup
#   just doctor  →  devproxy doctor
#
# 注意：本文件不提供 `up` 目标。just 的 variadic `{{args}}` 确实会把 `--`
# 原样带过去，但**引号在转发时会被丢掉**（'sh -c "…"' 被拆成一个个独立的词，
# 交给子进程时语义已经变了）；而定长 recipe 遇到 `--` 会被 just 当成目标名
# 直接报错（Justfile does not contain recipe '--'）。两种做法都会改变
# `devproxy up` 的语义，因此请直接执行 `devproxy up <name> -- CMD...`。

# 默认：显示 devproxy 的帮助
default:
    @devproxy --help

# 部署本地开发环境（Caddy + dnsmasq + DNS 路由 + 证书信任 + 自启）
setup:
    devproxy setup

# 体检所有前置条件
doctor:
    devproxy doctor

# 跟随服务日志（默认 caddy）
logs service="caddy":
    devproxy logs {{service}}

# 健康检查（等价于 doctor）
status:
    devproxy doctor

# 注销路由
down name:
    devproxy down {{name}}

# 列出已注册路由
ls:
    devproxy ls

# 安装 CA 根证书
trust:
    devproxy trust

# 显示当前生效配置
config:
    devproxy config

# 隔离 Caddy 实例端到端自检
selftest:
    devproxy selftest

# 本地开发安装（可编辑）
install:
    uv tool install -e .

# 旧名保留：过去装的是 devp 脚本，现在装的是 devproxy 包
install-devp: install
