# devproxy-init

给项目接上 [devproxy](https://github.com/wenbinye/devproxy)：识别技术栈 → 生成 `devproxy.toml` → 启动服务 →
用 curl 验证 `https://<name>.test` 真的通。**终点是验证过，不是「配置写好了」。**

## 触发方式

```
「给这个项目配一下 devproxy」
「用 devproxy 把本地服务挂成 https 域名」
「devproxy.toml 怎么写」
「devproxy init / devproxy setup / devproxy up 怎么用」
「怎么让本地服务用 https 域名访问」
```

## 前置条件

1. **装 devproxy**：`uv tool install devproxy`
   （或 `curl -fsSL https://raw.githubusercontent.com/wenbinye/devproxy/main/scripts/install.sh | sh`）
2. **装整套环境**：`devproxy setup` —— 会改系统（装 Caddy/dnsmasq、改 DNS 解析路径、把自签 CA 装进系统信任库、
   开机自启），**执行前要跟用户说明并取得同意**；先用 `devproxy uninstall --dry-run` 看它会动什么。
3. **信任证书**：macOS 上 `devproxy trust` 必须在该机器的图形终端里跑（信任标志需要人工点击，SSH 里做不到）；
   Linux 上由 `setup` 一并完成。

## 工作流程

1. **识别技术栈**：看 `package.json` / `composer.json` / `pyproject.toml` / `go.mod` / `Makefile`；
   `frontend/`、`web/` 旁边还有 API，就是**两条路由**。
2. **规划路由**：应用必须监听 `$PORT`；前端指向后端用 `$DEVPROXY_<API 名>_PORT`（兄弟端口注入），
   不要写死端口——那正是这个工具要消灭的东西。
3. **写 `devproxy.toml`**：把非显然的决定写进注释（为什么覆盖某个环境变量、为什么钉死端口）。
4. **启动并验证**：`devproxy up`，然后 curl **前端域名下的真实接口路径**（同源代理那条路），
   只 curl 首页证明不了前后端接通。

细节模板见 `references/stacks.md`（各技术栈的启动命令、Vite 代理变量、CA 信任、Host 白名单）。

## 常见坑

| 现象 | 处理 |
|---|---|
| `devproxy up` 报 ready timeout | 命令没监听 `$PORT`，改命令，别只加超时 |
| Vite 返回 403 | 缺 `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=<域名>` |
| 报 `restarted … sibling port changed` | 兄弟路由换了端口，devproxy 已用新端口把该应用重启（它此前指着死端口），无需处理 |
| 报 `port N is already in use` | 该端口被别的进程占了（如自己手起的实例），`ss -tlnp \| grep :N` 找出来停掉 |
| 生成的命令里端口变量是空的 | 装的 devproxy 版本太老，不支持兄弟端口注入 |
