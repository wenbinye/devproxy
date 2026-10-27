# 更新日志

本文件记录 devproxy 的**用户可见**变化。格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

> 0.1.0 之前没有正式发布，本文件从 0.1.0 起记。

## [未发布]

### 新增

- **兄弟路由的端口**：同一份 `devproxy.toml` 里的每个应用，都会拿到其它路由的端口
  环境变量 `DEVPROXY_<名字>_PORT`（`api` → `DEVPROXY_API_PORT`）。一个项目的端口现在
  在**任何应用启动之前**就全部分配好，前端因此不必写死后端端口：

  ```toml
  [[routes]]
  name = "api"
  command = ["sh", "-c", "uv run uvicorn app:app --port $PORT"]

  [[routes]]
  name = "web"
  command = ["sh", "-c", "cd frontend && BACKEND_PORT=$DEVPROXY_API_PORT npm run dev"]
  ```

- 命令里引用了本文件没有声明的路由，会在**读文件时**直接报错
  （`route 'web' uses $DEVPROXY_API_PORT, but no route 'api' is declared`）。
- 被引用的兄弟路由换了端口时，握着过期指针的应用会被报成**冲突**
  （`sibling port changed (DEVPROXY_API_PORT: 49435 -> 51717)`），
  而不是悄悄代理到一个死端口。
- `devproxy init` 在「前端 + 后端」的仓库里会生成**两条路由**并自动接线：前端命令
  拿到 API 的动态端口（注入到它自己的配置读取的那个变量里），外加自己的域名
  （`__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS`，否则 Vite 6 对陌生 Host 头回 403）。

### 修复

- `devproxy init` 不再生成 devproxy 自己拒绝加载的文件：没有可猜命令的项目会得到
  `command = []`，`devproxy up` 直接报错「command must be a non-empty array」。
  现在写入的是一个监听 `$PORT` 的占位命令，文件永远可加载、可启动。
- 项目的 dev 脚本跑的是 vite 时（`"dev": "vite --port 5174"`），`devproxy init`
  改为直接调用 vite：给脚本追加 `--port $PORT` 并不能可靠覆盖写死的端口，
  而命令行参数一定可以。

## [0.1.0]（2026-10-27）

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
  `doctor` / `config` / `selftest`。
