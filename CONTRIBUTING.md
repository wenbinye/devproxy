# 贡献指南

感谢你愿意为 devproxy 花时间。这个工具会**修改使用者的机器**（装 CA 进系统信任库、
改整机 DNS 解析路径、写服务单元），所以改动要格外克制：**宁可少做，不要做一半**。

## 开发环境

项目用 [uv](https://docs.astral.sh/uv/) 管理依赖（锁文件 `uv.lock` 必须提交）。

```bash
uv sync --locked --all-groups   # 按锁文件装齐（含 dev 依赖）
uv run pytest -q                # 单元测试（不需要 root，不碰真实系统）
uv run ruff check src/ tests/   # lint
uv run ruff format src/ tests/  # 格式化
uv run mypy                     # 类型检查
```

本地安装成可编辑模式（跑的是当前源码，不是已安装的那份）：

```bash
uv tool install -e .
```

## 提交之前

CI 会跑这四条，本地先跑一遍省得来回：

| 检查 | 命令 |
|---|---|
| 单元测试 | `uv run pytest -q` |
| lint | `uv run ruff check src/ tests/` |
| 格式 | `uv run ruff format --check src/ tests/` |
| 类型 | `uv run mypy` |

改了行为还要同步：

- `docs/` 里对应的说明（命令、配置、退出码表）；
- `CHANGELOG.md` 的「未发布」段；
- 改了 CLI 就**别改退出码语义** —— `docs/commands.md` 的退出码表是公开契约，
  外部脚本靠它判断结果。

## 代码风格

- 命令层**一命令一文件**（`src/devproxy/cli/*.py`），共享逻辑放 `_shared.py`；
  命令模块经 `from devproxy.cli import _shared as s` **在调用时**查找依赖
  （`s.build_context(...)`），这样测试对 `_shared.<名字>` 打桩才生效。
- 注释与 docstring 写中文，说清**为什么**（约束、坑、边界），不复述代码。
- 领域异常必须映射成可读信息 + 退出码，**绝不让 traceback 漏到终端**。
- 新增依赖前先问：这个功能能不能用标准库做？

## 提 PR 之前确认

- [ ] 上面四条检查都过了
- [ ] 文档同步了
- [ ] 新行为有测试覆盖；改 bug 的话，**先写能复现的失败测试**
- [ ] 碰过平台层（`src/devproxy/platform/`）的，说明在 Linux 与 macOS 上分别怎么验的
- [ ] 碰过真机行为的（装服务、改 `/etc`、动证书），在
      `docs/verification-checklist.md` 里补一条真机检查项

## 需要提权的改动

`devproxy setup` / `trust` / `uninstall` 会动系统。这类改动要特别注意：

- 提权一律走 `sudo -n`（绝不弹密码框），非交互时明确报错退出；
- 任何写操作前先把「要改什么」打印出来，让用户能拒绝；
- 失败必须可回滚（`devproxy setup --rollback <目录>`）。

## 报告 Bug

用仓库的 **Bug 模板**（新建 issue 时会弹出来）。最有用的是
**完整命令 + 退出码 + `devproxy doctor` 的输出** —— 这个工具的报错大量靠退出码
语义定位（见 `docs/commands.md` 的退出码表）。

安全问题请看 [SECURITY.md](SECURITY.md)，**不要**开公开 issue。
