# 项目级配置 `devproxy.toml`

## 项目级配置 `devproxy.toml`（`devproxy up` / `devproxy down`）

`config.toml` 是**你机器的**配置（存 `~/.config/devproxy/`，不进版本库）。
`devproxy.toml` 是**项目的**配置 —— 放在项目仓库根目录、**提交进版本库**，
全组每个人 clone 下来跑一次 `devproxy up`，就得到同一套路由。
### 快速创建：`devproxy init`

```bash
cd ~/work/tasks/我的项目
devproxy init                       # 生成 devproxy.toml
devproxy init --name web --command "npm run dev"
devproxy init --from my-running-svc # 从已在跑的路由导入它的启动命令
```

它会：

1. 找到**项目根**（向上找 `.git`；没有就用当前目录），在那里写 `devproxy.toml`；
2. 路由名默认取**目录名**（`My App` → `my-app`），可用 `--name` 改；
3. 启动命令按这个顺序来：
   - `--command` 显式给的（按 shell 的词法拆，引号和空格都认）；
   - `--from <路由名>` —— 从**已注册的同名路由**导入它的启动命令
     （路由表是全局的，所以必须指名，不能"把当前所有路由都导进来"）；
   - 都没有的话，**看项目类型猜**（下表）。**没有明确证据就不猜**，
     猜错比不猜更糟；
4. 写好的文件**能被自己解析**（生成后会用同一套解析器读回来验证）。

```bash
$ devproxy init
✔ Generated /home/me/work/my-app/devproxy.toml
  Route my-app → https://my-app.test
    command: sh -c npm run dev -- --port $PORT
  Route my-app-web → https://my-app-web.test
    command: sh -c cd frontend && BACKEND_PORT=$DEVPROXY_MY_APP_PORT npm run dev
  package.json has a dev script -- confirm this is the start command you want
  the frontend's config reads BACKEND_PORT -- the API's dynamic port is injected into it

Next steps:
  1. Check the commands above (placeholders must be replaced)
  2. devproxy setup    # once: installs Caddy, the DNS route, and CA trust
  3. devproxy up       # start everything this file declares
```

「前端 + 后端」的仓库会生成**两条路由**：API 路由和一条接好线的 `<name>-web` 路由。
前端只在 `frontend/`、`web/`、`client/`、`ui/` 里找（各自要有自己的 `package.json`）；
只有当它的配置真的读了端口变量（名字等于 `PORT` 或以 `_PORT` 结尾，如
`env.BACKEND_PORT`）时才会注入端口 —— 否则生成的命令里没有端口，文件会提示你手写
`<端口变量>=$DEVPROXY_<API>_PORT`。

文件已存在时会拒绝（退出码 2），要覆盖加 `--force`。

#### 能认出的项目类型

| 依据 | 猜出来的命令 |
|---|---|
| `package.json` 有 `dev` / `start` 脚本（不是 vite） | `sh -c "npm run dev -- --port $PORT"` |
| `package.json` 有 `dev` / `start` 脚本且跑的是 vite | `sh -c "./node_modules/.bin/vite --host 127.0.0.1 --port $PORT"`（直接调 vite —— 只有命令行参数能可靠覆盖配置里写死的端口） |
| `composer.json` + `artisan`（Laravel） | `sh -c "php artisan serve --port=$PORT"` |
| `composer.json` + `public/index.php`（Symfony 等） | `sh -c "php -S 127.0.0.1:$PORT -t public"` |
| `composer.json` + 根目录 `index.php` | `sh -c "php -S 127.0.0.1:$PORT"` |
| `composer.json` 有 `serve` / `start` / `dev` 脚本 | `sh -c "composer serve"` ⚠ **端口不可控** |
| `Makefile` 里真有 `dev` 目标 | `make dev` |
| `pyproject.toml`（Python） | 不猜 —— 入口点看不出来；文件里放**占位命令**（`python3 -m http.server $PORT`） |

> **猜出来的命令必须让应用监听 `$PORT`** —— devproxy 的就绪判定就是
> 「应用在 `$PORT` 上可连接」。命令若把应用起在固定端口上，`devproxy up` 会一直等到
> 「就绪超时」，而原因很难猜。所以端口可控的才敢给；端口不可控的（`composer serve`
> 之类）只给命令 + 明确提醒，并会把 `.env` 里定义的端口变量点名
> （`APP_WEB_PORT` 之类），让你知道该改哪个 —— 或者改用 `mode = "expose"`
> 让应用自己起、devproxy 只开域名。
### 手写

```toml
# devproxy.toml —— 项目根目录，提交进仓库

[[routes]]
name = "web"
command = ["npm", "run", "dev"]

[[routes]]
name = "api"
command = ["sh", "-c", "uv run uvicorn app:app --port $PORT"]

[[routes]]
name = "console"
mode = "expose"     # 不起进程，给已经在跑的服务开域名
port = 3000
```
### 字段

| 键 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 路由名，须是单标签 `[a-z0-9-]`（与 `devproxy up <name>` 同一个规则） |
| `command` | `mode="up"` 时是 | **argv 数组**，不是 shell 字符串 |
| `mode` | 否 | `"up"`（默认，起进程）或 `"expose"`（只注册路由） |
| `port` | `mode="expose"` 时是 | `expose` 模式：上游已在监听的端口。`up` 模式：写了就固定端口，不写就自动分配 |
| `ready_timeout` | 否 | 覆盖该条路由的就绪等待秒数 |
### ⚠️ `command` 不过 shell，`$PORT` 不会展开

`command` 是 argv 数组，直接交给 `subprocess.Popen` 执行，**中间没有 shell**。
所以 `["npx", "vite", "--port", "$PORT"]` 里的 `$PORT` 是**字面量**，不是端口号 ——
应用会收到一个叫 `$PORT` 的参数而报错。

要端口参数的应用，用 `sh -c` 包一层（和上面「两种应用，两种写法」一节是同一件事）：

```toml
command = ["sh", "-c", "npx vite --port $PORT --host 127.0.0.1"]
```

`sh` 在子进程启动后才展开 `$PORT`，那时 devproxy 已经把端口写进环境变量了。

### 一个路由的端口，别的路由也拿得到

`devproxy up` 会**先**把本项目所有路由的端口一次定下来，再启动任何应用 —— 写死了
`port = 5173` 的路由保持不变，其余的自动分配 —— 所以一个应用可以指向另一个应用，
而不必写死端口。每个应用都会拿到**同一个项目文件里**所有路由的端口环境变量
（`expose` 模式的路由也算在内，它的端口同样被导出）：

| 路由 | 变量 |
|---|---|
| `api` | `DEVPROXY_API_PORT` |
| `pp-api` | `DEVPROXY_PP_API_PORT` |

名字统一大写，短杠换成下划线。应用自己的端口也用同样的名字：在 `api` 路由里，
`DEVPROXY_API_PORT` 和 `$PORT` 是同一个值。

```toml
[[routes]]
name = "api"
command = ["sh", "-c", "uv run uvicorn app:app --port $PORT"]

[[routes]]
name = "web"
command = ["sh", "-c", "cd frontend && BACKEND_PORT=$DEVPROXY_API_PORT npm run dev"]
```

这正是动态端口能用在前后端组合上的原因：前端代理到后端**实际拿到的**端口，
同一份代码的两个 checkout 也能同时跑。

**引用了本文件没有声明的路由 = 报错**。那个变量会是空的，所以读文件时就会拒绝：

```
/home/me/work/my-app/devproxy.toml: route 'web' uses $DEVPROXY_API_PORT, but no route 'api' is declared in this file (devproxy only exports the ports of this project's routes)
```

（消息里的路径是 `devproxy.toml` 的绝对路径。）

**兄弟路由换了端口，用到它的那个应用会被自动重启**。前端记住的是后端启动时给的端口；
后端重启后换了端口，前端就会悄悄代理到一个死端口。devproxy 记得每个应用启动时拿到的是什么，
发现漂移后用新端口把这个应用重启：

```
↻ restarted web → https://web.test -- sibling port changed (DEVPROXY_API_PORT: 49435 -> 51717); port 49436
```

这类应用是**坏的**，不是「和声明不一样」—— 留着它没有意义，而 `up` 的职责就是让机器与声明
一致。重启**沿用原来的端口**（旧进程先被停掉），域名指向上游不变。

只检查命令里**真正用到**那个变量的路由 —— 根本没读 `$DEVPROXY_API_PORT` 的应用，不关心
后端换没换端口。用旧版 devproxy 启动的应用没有记录环境变量，不会被检查 —— 升级后请把
它们重启一次。

而**声明变了**（命令不同、文件里钉死了端口）仍然只报告不动手：这时候的问题是
「要不要用这个新定义换掉正在跑的那个」，那是你的决定。devproxy 不会杀任何进程，
`devproxy up` 以退出码 1 结束：

```
⚠️  conflict web → https://web.test -- command differs (current [...], expected [...])
   Fix: run devproxy down web and then devproxy up again
```

### 怎么用

```bash
devproxy up                # 从当前目录逐级向上找 devproxy.toml 并同步
devproxy up --file <路径>  # 显式指定
devproxy up --prune        # 顺带移除本文件里已删掉的路由
devproxy stop                # 停掉本项目的所有应用（重新启动：devproxy up）
```

**文件发现**：从当前工作目录逐级向上找 `devproxy.toml`，找到第一个就用
（一路找到文件系统根）。所以在子目录里执行也能找到项目根的那份。

**子进程的工作目录**是 `devproxy.toml` 所在目录，不是你当前所在的子目录 ——
`npm run dev` 这类相对路径的命令才有意义。
### 同步语义（`devproxy up`）

| 情况 | 动作 |
|---|---|
| 路由表里没有这条 | **创建**：`up` 模式起进程并注册，`expose` 模式只注册路由 |
| 路由表里有，声明与现状一致 | **跳过**（幂等，连跑两次第二次不动任何东西） |
| 路由表里有，但命令/端口变了 | **报告冲突，不动手**。打印现状、期望与修复命令，**绝不自动杀进程重启** |
| 路由表里有，但文件里已删掉这条 | 默认**不动**；`--prune` 才移除 |

**`--prune` 只移除"由本文件声明过"的路由** —— 手工 `devproxy up` 出来的路由
（路由表里没有 `source` 标记的）永远不会被它删掉。`--prune` 会按 `down` 的语义
一并收掉子进程，所以非交互环境里必须加 `--yes`（否则退出码 4）。

**冲突时退出码是 1** —— 配置漂移要让 CI 看得见。部分路由失败时会继续处理其余路由，
最后统一退出码 1，并在输出里列清哪些成功、哪些失败。
### 后台进程与日志

`up` 模式在 sync 里启动的应用是**后台进程**（sync 退出后应用继续跑，独立会话），
标准输出与错误收进 `~/.local/state/devproxy/logs/routes/<name>.log`。

**裸 `devproxy logs` 的行为取决于你在哪**：

- **项目目录里**（有 `devproxy.toml`）→ 跟随**本项目所有应用**的日志；
  只有一个应用时就是 `tail -f`；多个时每行前面加彩色标签区分：

  ```
  $ devproxy logs
  Following (Ctrl-C to exit):
    [codeup-api]
    [codeup-web]
  [codeup-api] INFO:     Uvicorn running on http://127.0.0.1:39743
  [codeup-web] VITE v6.4.4  ready in 329 ms
  ```

  标签就是 `devproxy.toml` 里的**应用名**（带各自颜色，补齐对齐）。
- **项目外** → 跟 caddy（老行为）。

看某个应用的输出：

```bash
devproxy logs web              # 跟随（Ctrl-C 退出）
tail -f ~/.local/state/devproxy/logs/routes/web.log   # 等价写法
```

`devproxy logs` 的参数可以是：

| 参数 | 看什么 |
|---|---|
| `caddy` / `dnsmasq` | **服务本身**的日志（Linux 走 journalctl，macOS 是文件） |
| `<路由名>` | **sync 起的应用**的日志 |

参数不认识时退 2，并把当前可用的路由日志名列出来。想知道有哪些可看：

```bash
devproxy logs --list
```

**在项目目录里只列本项目声明的路由**（按 `devproxy.toml` 过滤，别的项目的不混进来）；
不在项目里则列出全部。声明了但还没起过的路由也会提示：

```
Service logs:
  caddy
  dnsmasq
This project (devproxy.toml: /home/me/work/xxl-job/devproxy.toml):
  xxl-job-admin            8 MB
    /home/me/.local/state/devproxy/logs/routes/xxl-job-admin.log
  xxl-job-executor        13 KB
    /home/me/.local/state/devproxy/logs/routes/xxl-job-executor.log
  declared but no logs yet: xxl-job-executor (run devproxy up)
```

每条下面那行是**完整路径** —— 不用自己拼 `~/.local/state/devproxy/logs/routes/<名字>.log`。

看**单个**日志的信息（路径、大小、跟随命令）：

```bash
devproxy logs codeup-api --list
```

想直接用编辑器翻日志（`$EDITOR`，没有就 `$VISUAL`）：

```bash
devproxy logs codeup-api --edit
```

**日志会轮转**：单个文件超过 **10 MB** 就改名成 `.log.1`（覆盖上一份），当前文件从头写。
所以每条路由的日志最多占 20 MB，不会无限涨 —— Spring Boot 这类应用在开发时
能以几 MB/小时 的速度写日志，不轮转的话跑几天就能吃掉几百 MB。

轮转在**写入端**做（devproxy 自己收应用的输出、自己写文件），不是靠 logrotate ——
这些日志是 devproxy 拉起的进程写的，只有写它的人能保证文件不会无限大。
### 停掉本项目全部应用

```bash
devproxy stop
```

一条命令停掉 `devproxy.toml` 声明的**所有**应用（进程 + 路由一起清）。
只动本文件声明的路由 —— 手工 `up` 的、别的项目的不受影响。
全部停了之后 `devproxy up` 一条命令再拉起来。

单条路由的操作仍是 `devproxy down <名字>`。
### 与 `up` / `expose` 的关系

`up` 就是批量版的单应用 `up` + `expose`。两者共用同一套路由表与事务写入 ——
`up` 建的路由和手工单条 `up` 的路由没有区别，`devproxy ls` 一样能列出来，
`devproxy down <name>` 一样能注销。
### 真实案例：codeup-manager

`codeup-manager`（`git@codeup.aliyun.com:msy/tools/codeup-manager.git`，资产清单管理工具，
FastAPI + Vite 前后端分离）原本靠一个已被删除的旧脚本 `devp` 起动态域名。
现在它的 `devproxy.toml` 长这样：

```toml
[[routes]]
name = "codeup-api"
command = ["sh", "-c", "uv run uvicorn codeup_manager.web.app:create_app --factory --reload --host 127.0.0.1 --port $PORT"]

[[routes]]
name = "codeup-web"
command = ["sh", "-c", "cd frontend && API_TARGET=https://codeup-api.test NODE_EXTRA_CA_CERTS=$HOME/.local/share/caddy/pki/authorities/local/root.crt __VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=codeup-web.test ./node_modules/.bin/vite --host 127.0.0.1 --port $PORT"]
```

一次 `devproxy up` 得到 `https://codeup-web.test` 与 `https://codeup-api.test/docs`，
**两个端口都是自动分配的**。这里有三个值得抄的点：

1. **前端要代理到后端时，代理到后端的「域名」而不是端口**（`API_TARGET`）——
   这样后端也能用动态端口，不必牺牲动态分配换静态端口；
2. **node 不读系统信任库**，得用 `NODE_EXTRA_CA_CERTS` 把 devproxy 的根证书喂给它
   （浏览器、curl 走系统信任库，不需要这一步）；
3. **Vite 6 有 Host 校验**，要用自定义域名访问得传
   `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS`。

该仓库的完整说明见它的 `docs/devproxy-quickstart.md`。

---
