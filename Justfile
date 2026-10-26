# 两类配方，各管一摊：
#
# **一、开发者用的**（test / lint / format / typecheck）—— 这些是仓库自己的
#     工程动作，不属于 devproxy 命令本身。CI 跑的就是这几条。
#
# **二、转发给 devproxy 的兼容壳** —— 全部逻辑都在 devproxy 命令里，这里
#     不做任何实现，只是兼容旧的 `just` 习惯：
#         just setup   →  devproxy setup
#         just doctor  →  devproxy doctor
#
# 注意：转发壳不提供 `up` 目标。just 的 variadic `{{args}}` 确实会把 `--`
# 原样带过去，但**引号在转发时会被丢掉**（'sh -c "…"' 被拆成一个个独立的词，
# 交给子进程时语义已经变了）；而定长 recipe 遇到 `--` 会被 just 当成目标名
# 直接报错（Justfile does not contain recipe '--'）。两种做法都会改变
# `devproxy up` 的语义，因此请直接执行 `devproxy up <name> -- CMD...`。

# -- 开发者用的配方 -------------------------------------------------------

# 单元测试（不需要 root，不碰真实系统）
test:
    uv run pytest -q

# lint（ruff check + 格式检查 + 类型检查）
lint:
    uv run ruff check src/ tests/
    uv run ruff format --check src/ tests/
    uv run mypy

# 自动修（格式 + 可自动修的 lint 问题）
fix:
    uv run ruff check --fix src/ tests/
    uv run ruff format src/ tests/

# 按锁文件装齐开发环境
sync:
    uv sync --locked --all-groups

# 默认：显示 devproxy 的帮助
default:
    @uv run devproxy --help

# 部署本地开发环境（Caddy + dnsmasq + DNS 路由 + 证书信任 + 自启）
setup:
    uv run devproxy setup

# 体检所有前置条件
doctor:
    uv run devproxy doctor

# 跟随服务日志（默认 caddy）
logs service="caddy":
    uv run devproxy logs {{service}}

# 健康检查（等价于 doctor）
status:
    uv run devproxy doctor

# 注销路由
down name:
    uv run devproxy down {{name}}

# 列出已注册路由
ls:
    uv run devproxy ls

# 安装 CA 根证书
trust:
    uv run devproxy trust

# 显示当前生效配置
config:
    uv run devproxy config

# 隔离 Caddy 实例端到端自检
selftest:
    uv run devproxy selftest

# 本地开发安装（可编辑）
install:
    uv tool install -e .

# 旧名保留：过去装的是 devp 脚本，现在装的是 devproxy 包
install-devp: install

# 干净 ubuntu 容器里的端到端验证（验证清单 F10；需要 docker 与网络）
verify-clean:
    bash scripts/verify-clean-machine.sh
