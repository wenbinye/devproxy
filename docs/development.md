# 开发与 CI

## 开发与 CI

单元测试基本上是**封闭的**：不碰真实系统、不需要 caddy 二进制、不需要网络，
也不会写进你的家目录（`HOME` 在测试里被指向临时目录）。

唯一的例外是 NSS 那组用例：它需要用**真实的 `certutil`**（`libnss3-tools`）在临时目录里
建一个真的 NSS 库，验证「按证书内容判定信任」这类只有真工具才验得了的行为。
机器上没有 `certutil` 时整组自动跳过，不影响其余用例；CI 因此专门装了 `libnss3-tools`。

```bash
python3 -m pytest -q --ignore=tests/test_integration.py   # 单元套件
python3 -m pytest -q tests/test_integration.py             # 真机集成（需要真实 caddy）
```

CI 在 `.github/workflows/ci.yml`：push 到 `main`、提 PR 或手动触发时，
在 Python 3.11 / 3.12 / 3.13 上跑**单元套件**（真机集成测试不进 CI ——
它依赖真实 caddy 二进制与真实网络）。
## 仓库里的历史文件

仓库根目录的 `Caddyfile` 是**迁移前**的手工配置。**在 `devproxy setup` 的迁移真正跑过之前，
现役的 `caddy.service` 仍然指向它**（迁移会先备份它、再把单元改指向
`~/.config/devproxy/Caddyfile`）；迁移完成后它才只剩历史参考价值。

根目录的 `devp` 脚本**已删除** —— 它是 `devproxy` 的 1.0 前身：直接 PATCH 现役
Caddy 的 Admin API 注册路由，注册的东西新工具既看不见也删不掉。它的来龙去脉
记在 [docs/legacy-devp.md](docs/legacy-devp.md)。`Justfile` 现在只是转发到
`uv run devproxy` 的兼容壳（跑的是**当前源码**，不是已安装的那份）。
### 代码结构

```
src/devproxy/
├── cli/          # 命令层：一命令一文件（up / expose / down / ls / logs / trust /
│   │             #   config / selftest / setup / doctor / uninstall / sync / run）
│   ├── _shared.py   # 命令模块的公共接缝（console、退出码、build_context、组件装配）；
│   │                #   命令经它**在调用时查找**依赖，测试打桩才能生效
│   ├── _runtime.py  # 退出码常量与 app 入口
│   ├── _context.py  # 上下文组装（build_context：配置、路由表、Caddy 客户端）
│   ├── _render.py   # 表格与提示的渲染
│   ├── _typer.py    # Typer 参数切分组（把 `--` 之后的参数原样交给子命令）
│   ├── _migrate.py  # 迁移与回滚的命令层编排
│   └── _worker.py   # 后台 up 工作进程（sync 拉起应用时用它托管子进程）
├── core/         # 基础设施：config（配置加载与校验）、paths（目录与状态文件）、
│                 #   backup（迁移备份）、caddy_env（单元环境变量留档）
├── gateway/      # 运行期网关：routes（路由模型与确定性渲染）、store（事务写入：
│                 #   flock → 渲染 → validate → 落盘 → reload → 失败回滚）、
│                 #   runner（up 的子进程监管）、caddy（Admin API）、names（域名与路由名）、
│                 #   selftest（隔离自检）、project / sync（项目级声明）、
│                 #   foreground（run 的前台托管）
├── lifecycle/    # 环境生命周期：setup、doctor、uninstall、
│                 #   migrate/{caddyfile, legacy, install, rollback}（迁移与回滚）
├── components/   # 四个可部署组件：caddy / certs / dns / dnsmasq；
│                 #   base 定义组件协议 —— check() -> Status（永不抛异常）、
│                 #   plan() -> list[Action]（空 = 无变更）、apply(actions)
└── platform/     # 平台接缝：base（协议：Platform / Action / Status / ElevatedSession）、
                  #   linux（systemd + resolved）、macos（launchd + /etc/resolver）、
                  #   fake（测试假平台，写入一律进沙箱目录）
```

依赖方向（每层只依赖它下方的层，没有反向依赖）：

```
cli → lifecycle / gateway / components → platform / core
```

- `core` 不依赖任何内部模块 —— 最底层；
- `platform` 只依赖 `core`（用它的路径工具）；
- `components` 依赖 `core` + `platform`，另有一处**函数内**延迟 import
  `gateway.caddy`（渲染主 Caddyfile 的模板，避开循环依赖）；
- `lifecycle` 依赖 `core` / `gateway` / `components` / `platform`；
- `cli` 依赖全部。

测试是**镜像结构**的：`tests/test_component_certs.py` 对着 `components/certs.py`，
以此类推；跨层的端到端行为在 `tests/test_integration.py` 与 `devproxy selftest`。

---
