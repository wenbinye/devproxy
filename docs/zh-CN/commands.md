# 命令详解

devproxy 的全部命令。命名对齐 **docker compose**：项目级操作用 `up` / `down` / `ps` / `logs`，
机器级操作（装环境、体检、信任证书）保留自己的名字。

| 命令 | 一句话 |
|---|---|
| [`up`](#up) | 起服务：单个应用（前台）或本项目全部（后台） |
| [`expose`](#expose) | 给**已在运行**的服务开域名 |
| [`down`](#down) | 停服务：单条路由或本项目全部 |
| [`ls` / `ps`](#ls--ps) | 列出路由 |
| [`logs`](#logs) | 跟随日志 |
| [`init`](#init) | 生成 `devproxy.toml` |
| [`setup`](#setup) | 部署本地开发环境（Caddy、dnsmasq、DNS、证书、自启） |
| [`doctor`](#doctor) | 体检：缺什么给什么修复命令 |
| [`trust`](#trust) | 把 CA 根证书装进系统信任库与 NSS 库 |
| [`config`](#config) | 显示当前生效配置 |
| [`selftest`](#selftest) | 隔离实例端到端自检 |
| [`run`](#run) | 前台托管 Caddy（没有 systemd 的环境用） |
| [`uninstall`](#uninstall) | 卸载：停服务、移除单元、DNS 路由与（可选）CA |
| [`--install-completion`](#shell-补全) | 装 shell 补全（bash / zsh / fish / powershell） |

---

## up

起服务。**带路由名** = 前台起单个应用（命令跟着应用跑，Ctrl-C 一起结束）；
**不带路由名** = 在项目目录里应用 `devproxy.toml` 的全部声明（后台跑，compose 的 `up -d`）。

```bash
devproxy up web -- sh -c 'npx vite --port $PORT --host 127.0.0.1'
devproxy up                       # 项目模式：应用 devproxy.toml 的全部声明
```

| 选项 | 说明 |
|---|---|
| `<name>` | 路由名，单标签 `[a-z0-9-]`。省略 = 项目模式 |
| `--port N` | 显式指定端口（默认自动分配）；被占用时直接失败 |
| `--ready-timeout S` | 就绪等待秒数（默认取 `config.toml` 的 `ready_timeout`） |
| `--file <路径>` | *[项目模式]* 显式指定 `devproxy.toml`（默认从当前目录向上查找） |
| `--prune` | *[项目模式]* 移除本文件里已删掉的路由（非交互时须加 `--yes`） |
| `--yes` | *[项目模式]* 非交互模式，不询问 |

- `--` 之后的一切**原样**交给子进程，devproxy 不解析（应用自己的 `--port` 才不会被抢走）。
- 项目模式是**幂等**的：再跑一次，已在跑的跳过，**死掉的应用会被重建**。
- 端口每次可能不同（自动分配）；域名不变。
- 详见 [项目级配置](project-config.md)。

## expose

给**已经在跑**的服务开一个域名（数据库管理台、mock 服务、docker 里的东西……）。

```bash
devproxy expose datahub --port 9002
# ✅ https://datahub.test  (upstream 127.0.0.1:9002)
```

| 选项 | 说明 |
|---|---|
| `<name>` | 路由名（必填） |
| `--port N` | 上游已在监听的端口（必填） |

- 上游必须是 **`127.0.0.1`**：devproxy 代理到**本机**端口。容器里的 `localhost` 是容器自己。
- 上游没起也能注册：提示「上游无响应」，但路由照样写进路由表（常驻，等服务起来就通）。
- 路由是**常驻**的：Caddy 重启、机器重启都还在。要删：`devproxy down <name>`。

## down

注销路由；子进程还在则一并收掉。**带路由名** = 只停那一条；**不带** = 停掉本项目
（`devproxy.toml`）声明的所有应用。

```bash
devproxy down web            # 停一条
devproxy down                # 停本项目全部
```

- 项目外不带名字执行会退 2（不知道该停哪些），并提示用 `devproxy down <名字>`。
- 手工 `up` 出来的路由**不会**被项目模式的 `down` 碰（只有本文件声明过的才停）。
- 停了之后 `devproxy up` 一条命令再拉起来。

## ls / ps

列出所有已注册路由（含可达性）。两个名字等价，`ps` 对齐 docker compose。

```bash
devproxy ls
devproxy ps
```

- **按应用名升序** —— 路由表是集合，输出稳定可预期（与注册顺序无关）。

## logs

跟随日志：服务本身（`caddy` / `dnsmasq`）或 **sync 起的应用**。

```bash
devproxy logs              # 默认跟 caddy
devproxy logs caddy
devproxy logs dnsmasq
devproxy logs my-app       # sync 起的应用
devproxy logs --list       # 列出能跟的目标，不跟随
devproxy logs my-app -e    # 用 $EDITOR 打开日志文件（= --edit）
```

| 选项 | 说明 |
|---|---|
| `--list` | 列出能跟的目标，不跟随。路由日志**逐条给出完整路径**（不用自己拼） |
| `--edit` / `-e` | 用 `$EDITOR`（没有就 `$VISUAL`，再没有就依次找 `vim` / `vi` / `nano`）打开该路由的日志文件 |

- 只读，不改任何东西；**Ctrl-C 退出**（命令自己不会结束）。
- 项目目录里裸跑 `devproxy logs` = 跟**本项目全部应用**，多个应用时每行带彩色标签
  （标签就是应用名）。
- 路由日志从**末尾**开始跟（不倒历史）；文件被轮转会自动接上。
- `--edit` 只对**路由日志**有效：服务日志（caddy / dnsmasq）是 journalctl 里的滚动日志，
  不是文件，编辑器打不开 —— 会明确报错让你改用跟随。
- `--edit` 与 `--list` 互斥；`EDITOR` / `VISUAL` 都没设且 PATH 里找不到
  `vim` / `vi` / `nano` 时报错退出 2。
- 详见 [项目级配置 · 后台进程与日志](project-config.md#后台进程与日志)。

## init

在项目里生成 `devproxy.toml`。

```bash
devproxy init
devproxy init --name web --command "npm run dev"
devproxy init --from my-running-svc   # 从已注册的同名路由导入它的启动命令
```

| 选项 | 说明 |
|---|---|
| `--name <名字>` | 路由名（默认取项目目录名） |
| `--command <命令>` | 启动命令（按 shell 的词法拆，引号和空格都认） |
| `--from <路由名>` | 从已注册的同名路由导入它的启动命令 |
| `--force` | 覆盖已存在的 `devproxy.toml` |

启动命令的来源优先级：`--command` > `--from` > 按项目类型猜（Node / PHP / Makefile）。
**没有明确证据就不猜** —— 猜错比不猜更糟。详见
[项目级配置 · 快速创建](project-config.md#快速创建devproxy-init)。

**前端 + 后端的仓库会生成两条路由，并自动接线**：前端命令会拿到 API 的动态端口
（注入到它自己的配置读取的那个变量里，从 vite 配置里找），外加路由域名
（`__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS`，否则 Vite 6 对陌生 Host 头回 403）。
什么都猜不出来时，文件里写**占位命令**（监听 `$PORT` 的一个 python http.server）——
生成的文件永远可加载、可启动，不会出现 `command = []`。

## setup

部署本地开发环境：Caddy + dnsmasq + DNS 路由 + 证书信任 + 开机自启。**幂等**，可重复执行。

```bash
devproxy setup
devproxy setup --yes                 # 非交互（需要提权时必须加）
```

| 选项 | 说明 |
|---|---|
| `--dns-backend auto\|resolved\|system` | 强制 DNS 后端（默认取 `config.toml`） |
| `--domain-suffix <后缀>` | 临时覆盖域名后缀（会过校验，非法值退 2） |
| `--no-trust` | 跳过证书信任 |
| `--yes` | 非交互模式，不询问（需要提权的场景必须加） |
| `--rollback <目录>` | 从指定备份目录回滚（见 [迁移与回滚](migration-and-uninstall.md)） |

它会**写系统目录、装服务单元、改 DNS 解析路径、把自签 CA 装进系统信任库** ——
动手前先读 [README 的警告](../README.md#%EF%B8%8F-这个工具会修改你的系统)。

## doctor

体检：检查所有前置条件，缺什么给什么修复命令。

```bash
devproxy doctor
devproxy doctor --dns-backend system
```

**只做静态检查**（文件在不在、服务在不在跑、证书装没装、单元指向谁）——
**不做 DNS 解析**，不发起任何 `resolvectl` / `dig` 查询。全部就绪退 0，否则退 3。

## trust

把 CA 根证书装进系统信任库与 NSS 库（macOS 是系统钥匙串）。

```bash
devproxy trust
devproxy trust --yes
```

| 选项 | 说明 |
|---|---|
| `--yes` | 非交互模式，不询问（需要提权时必须加） |

**什么时候需要跑**：第一次注册路由后（Caddy 这时才有 CA）。
**Firefox 不读 NSS 库也不读系统信任库**（默认）—— 需手动导入或开
`security.enterprise_roots.enabled`，详见
[DNS 与证书](dns-and-certs.md#证书精确站点块不是通配符)。

## config

显示当前生效配置（含 storage 留档的解析结果）。

```bash
devproxy config
```

## selftest

隔离 Caddy 实例做端到端自检：注册 → 转发 → 跳转 → 注销 → 证书策略。
**不碰生产的 443/2019**，也不会把沙箱 CA 装进宿主信任库。

```bash
devproxy selftest
```

## run

**前台**托管 Caddy —— 给没有 systemd 的环境（容器、WSL、精简发行版）用。
那里装不了 user 服务，路由就没人加载。

```bash
devproxy run
devproxy run --config /path/to/Caddyfile
```

| 选项 | 说明 |
|---|---|
| `--config <路径>` | 要加载的 Caddyfile（默认主 Caddyfile） |

- **stdio 直通终端** —— Caddy 日志实时打出来；Ctrl-C 退出。
- 不装服务、不写任何状态；退出后路由不再被服务（路由表是文件，但得有个 Caddy 进程去加载）。
- 与 `setup` 互斥：服务在跑时 `run` 会退 3（端口冲突），提示先停服务。
- 详见 [平台支持与差异](platforms.md#没有-systemd-的环境用-devproxy-run-托管)。

## uninstall

卸载：停止服务、移除单元、DNS 路由与（可选）CA。

```bash
devproxy uninstall --dry-run     # 只预览，不做任何变更、不需要 sudo
devproxy uninstall               # 交互确认后执行
```

| 选项 | 说明 |
|---|---|
| `--dry-run` | 只打印将要执行的步骤，不做任何变更、不需要 sudo |
| `--yes` | 非交互模式，不询问 |
| `--purge` | 直接删除 Caddy 存储、备份目录与下载的 caddy 二进制（不给就在交互时问） |
| `--remove-ca` | 直接卸载 CA（不给就在交互时问；迁移来的 CA 默认保留） |

**默认保守**：只删工具自己的东西（服务、单元、DNS 路由、状态文件），
保留你的 `sites.caddy`、`config.toml`、Caddy 存储、备份目录、linger。
详见 [迁移、回滚与卸载](migration-and-uninstall.md)。

## shell 补全

```bash
devproxy --install-completion        # 装当前 shell 的补全（bash / zsh / fish / powershell）
devproxy --show-completion bash      # 只打印脚本，不落盘
```

- **命令名与选项**的补全是 Typer/Click 自带的，不用额外配置。
- bash 装到 `~/.bash_completions/devproxy.sh`，并在 `~/.bashrc` 里加一行 `source`；
  **要重开一个终端**才生效。
- **参数值**由 devproxy 自己补：`logs <名字>` 补服务名（`caddy` / `dnsmasq`）与
  **已注册的路由名**，`down <名字>` 补路由名。
- 补全是**动态**的 —— 每次按 <TAB> 都会跑一次 devproxy 去读路由表；
  路由表读不到（还没 setup、文件损坏）就当没有候选，**不会报错**。
- `up` / `expose` 的名字是**新建**的路由名，不补（补了会让人以为能复用旧名字）。
- `--` 之后的内容原样交给应用，devproxy 不知道应用有哪些参数，因此不补。

---

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 运行失败（子进程退出、reload 失败、路由表损坏、迁移被拒绝、`up` 发现路由与声明冲突、`run` 的 Caddy 配置校验失败……） |
| 2 | 参数错误（非法路由名 / 非法端口、缺少 `--`、未知日志服务名、`logs --edit` 缺目标或找不到任何编辑器、非法 `dns_backend`、非法 `--domain-suffix`、`up` 的配置文件缺失或非法、`run --config` 指定的文件不存在） |
| 3 | 前置条件不满足（Caddy 未运行、主 Caddyfile 不存在、`doctor` 体检不通过） |
| 4 | 需要提权但当前非交互（未加 `--yes`），**或用户在确认提示上选择了「否」** |

注意两个特例：`up` 缺少 `--` 或 `--` 后没有命令，按「参数错误」退出 **2**；
`up` 的退出码在命令正常结束时**就是子进程的退出码**。

退出码 4 表示「**需要人来拍板，但这次没有人拍**」——要么环境非交互且没加 `--yes`，
要么用户在确认提示上答了「否」。这两种情况下**工具什么都没改**，终端上也会明确说明。

还有一处不对称容易踩：同一个非法值，写在 `config.toml` 里与用命令行传，退出码不同 ——
写在 `config.toml` 里属于**配置错误**（退出码 **1**），用 `--domain-suffix` /
`--dns-backend` 传属于**参数错误**（退出码 **2**）。
