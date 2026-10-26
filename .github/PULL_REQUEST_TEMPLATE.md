## 这个 PR 做了什么

<!-- 一两句话。关联 issue：Fixes #123 -->

## 怎么验证的

<!--
不只是「跑通了」。请写清楚：
- 跑了什么命令 / 什么测试；
- 在哪些平台上验过（Linux / macOS）；
- 哪些情况**没有**验（真机行为？需要提权？需要真实 Caddy？）
-->

## 清单

- [ ] `uv run pytest -q` 通过
- [ ] `uv run ruff check src/ tests/` 通过
- [ ] `uv run ruff format --check src/ tests/` 通过
- [ ] `uv run mypy` 通过
- [ ] 改了行为的话，`docs/` 里的对应说明同步更新了
- [ ] 改了 CLI 的话，退出码语义没变（见 `docs/commands.md` 的退出码表）
