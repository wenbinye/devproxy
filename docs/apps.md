# 应用接入

## 应用接入
### 服务已经在跑？给它开域名就行

不是所有服务都由 devproxy 启动 —— 数据库管理台、mock 服务、docker 里的东西，
往往早就在跑了。这类用 `expose`：

```bash
devproxy expose datahub --port 9002
# ✅ https://datahub.test  (上游 127.0.0.1:9002)
#    移除: devproxy down datahub
```

要点：

- **上游写 `127.0.0.1`**：devproxy 代理到**本机**端口。容器里的 `localhost`
  是容器自己 —— 那种情况要用宿主 IP 或 `host.docker.internal`；
- **上游没起也能注册**：会提示「上游无响应」，但路由照样写进 `routes.json`
  （路由常驻，等服务起来就通）；
- **路由是常驻的**：Caddy 重启、机器重启都还在（"文件即真相"）；
- 想让它进版本库共享，写进 `devproxy.toml`（见「项目级配置」一节）：

  ```toml
  [[routes]]
  name = "datahub"
  mode = "expose"     # 不起进程，只给已在跑的服务开域名
  port = 9002
  ```
### 两种应用，两种写法

**1. 原生读 `PORT` 环境变量的应用**（Next.js / CRA 等）——直接写命令：

```bash
devproxy up next-app -- npx next dev
```

devproxy 会把分配到的端口放进子进程的 `PORT` 环境变量。

**2. 需要显式端口参数的应用**（Vite / uvicorn / gunicorn 等）——用 `sh -c` 包一层，
让 shell 在 devproxy 注入 `PORT` **之后**再展开：

```bash
devproxy up codeup-web -- sh -c 'npx vite --port $PORT --host 127.0.0.1'
devproxy up codeup-api -- sh -c 'uv run uvicorn app:app --host 127.0.0.1 --port $PORT'
devproxy up codeup-api -- sh -c 'gunicorn app:app --bind 127.0.0.1:$PORT'
```

**不要直接写 `--port $PORT`**：devproxy 用 `subprocess.Popen(argv)` 直接执行、**不经过 shell**，
命令里的 `$PORT` 会被你自己的 shell 提前展开（此时 devproxy 还没分配端口，值是空串），
应用会收到缺值的参数。用单引号包住 `sh -c` 的脚本即可把展开推迟到子进程启动后。

**`--` 是必须的**：`--` 之前是 devproxy 自己的参数（`name`、`--port`、`--ready-timeout`），
`--` 之后的一切**原样**交给子进程，devproxy 不解析。这样应用自己的 `--port`
不会被 devproxy 抢走。缺少 `--` 时工具直接报错退出（码 2）。
### 应用侧的其它注意点

- **Host 校验放行**：请求的 Host 头是 `<name>.test`。Vite 需要 `server.allowedHosts`、
  Django 需要 `ALLOWED_HOSTS` 里加上该域名。
- **子进程脱离控制终端**：devproxy 以 `start_new_session=True` 启动子进程（独立进程组，
  便于退出时按组清理孙进程）。代价是子进程**没有控制终端**，依赖 stdin 的交互式启动语义会变；
  stdout/stderr 仍直透当前终端。
- **就绪判定**：devproxy 轮询「端口是否可连接」，最多等 `ready_timeout` 秒（默认 60）。
  超时会提示「应用可能未使用 `$PORT`，可用 `--port` 显式指定」。
- **同名路由会被替换**：再次 `up` 同一个名字时，旧路由先注销、旧子进程先收掉。

---
