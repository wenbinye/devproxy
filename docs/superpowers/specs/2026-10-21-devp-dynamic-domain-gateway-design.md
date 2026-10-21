# devp — 本地开发动态域名网关 设计文档

- 日期：2026-10-21
- 状态：设计已确认，待实施
- 落点：`local-https/` 仓库（`devp` 脚本 + Caddyfile 改动 + Justfile + README）

## 1. 背景与目标

本地多项目并行开发时，每个项目的 web 服务各有固定端口，存在两个痛点：

1. **端口冲突**：多个项目抢同一端口（3000、8000 等）。
2. **记忆负担**：要记住哪个服务在哪个端口。

现有基础设施（已就绪，零改动）：

- dnsmasq：`address=/.test/127.0.0.1`（含 `::1`），任意 `*.test` 已解析到本机。
- Caddy（systemd 用户级服务）：80/443 已监听，Admin API 已开在 `127.0.0.1:2019`，内部 CA 已受系统信任。
- 内部 CA 可为 `*.test` 下的具体域名逐张签发证书；`*.test` 本身是二级通配符，OpenSSL/浏览器会拒绝其主机名校验，不能作为当前方案（见 §2 / D21）。

目标形态：一条命令启动项目 → 自动分配空闲端口 → 自动注册 `https://<name>.test` → 正常退出/信号退出自动注销。开发者只记域名。

## 2. 关键决策

| 决策点 | 结论 | 备注 |
|---|---|---|
| 域名规范 | `<name>.test` = 本机 devp 动态注册；`<name>.local.yikuaigao.com` = minikube/APISIX 链路 | 两条链路天然隔离，无需保留名单 |
| 端口机制 | 包装器注入 `PORT` 环境变量（应用侧配合引用 `$PORT`） | 真正动态端口，零冲突；不做端口扫描兜底（二期候选） |
| 实现 | Python stdlib 单文件，并入 `local-https/` | 零三方依赖，系统 python3 直接跑 |
| TLS | on-demand TLS 逐域名签发 | `*.test` 是二级通配符，OpenSSL/浏览器主机名校验会拒绝（实测 curl exit 60）；改为每个域名首次握手时由内部 CA 单独签发（D21） |

## 3. 架构

```
$ devp up codeup -- just dev
  │ ① bind(':0') 探测空闲端口 → PORT
  │ ② 注入 PORT 环境变量，以独立 session/进程组启动子进程（日志直透终端）
  │ ③ 轮询 127.0.0.1:$PORT 直到可连接（默认 60s 超时）
  │ ④ 注册路由（见 §5 协议）
  │ ⑤ 打印 ✅ https://codeup.test，进入守护
  │    子进程退出 / SIGINT / SIGTERM / SIGHUP → 自动注销
  │    信号先显式转发给子进程组，清理时按组终止（含孙进程）
  │    心跳 30s：路由丢失（caddy reload/重启）则重注册
  ▼
浏览器 https://codeup.test
  → dnsmasq (.test → 127.0.0.1)
  → Caddy :443（on-demand TLS 逐域名证书，Host 头原样转发）
  → reverse_proxy 127.0.0.1:$PORT
```

数据流：devp ↔ Caddy Admin API（localhost:2019）；devp ↔ 子进程（父子 + 信号）；无本地状态文件（路由即状态，真相在 Caddy 配置里）。

## 4. Caddy 侧一次性改动

`Caddyfile` 新增两个站点块（`.local.yikuaigao.com` / `localhost` 部分不动）：

```caddyfile
# devp 动态注册的本地项目网关
*.test {
	tls {
		on_demand
		issuer internal
	}
	respond "devp: no route registered for {http.request.host}" 404
}

http://*.test {
	redir https://{host}{uri} permanent
}
```

- `on_demand`：不为 `*.test` 预签通配证书，而是按 SNI 为每个 `<name>.test` 单独签发。
  原因：`*.test` 是二级通配符（TLD 本身是 `test`），OpenSSL/浏览器的主机名校验会拒绝
  （实测 `curl` exit 60: no alternative certificate subject name matches）。
- 未配置 `ask` 端点：本机任意进程都能为任意 `.test` 名称取得被系统信任的证书。仅本机信任域，
  已接受（D21）。
- 通配站点同时负责：未注册域名的 404 兜底。
- 80 端口显式 `redir ... permanent`（301，D7）；Caddy 自动生成的跳转才是 308。
- `reverse_proxy` 不改 Host 头（应用内生成的绝对 URL 正确；应用侧需放行 host 校验，见 §8）。

## 5. 注册协议（关键，含实测结论）

Admin API 路由操作的实测结论（隔离实例验证，2026-10-21）：

| 尝试 | 结果 |
|---|---|
| `POST .../srv0/routes` 追加动态路由 | **失败**：排在 `*.test` 通配路由之后被遮蔽，请求落到兜底 404。Caddy 只在 Caddyfile 适配阶段排序，运行时追加不重排 |
| `POST .../srv0/routes/0`（期望按下标插入） | **失败**：语义仍是追加到末尾 |
| `PUT .../srv0/routes`（整数组替换） | **失败**：409 key already exists，PUT 不做 upsert |
| `GET .../srv0/routes` → 本地插入到首个通配 host 路由之前 → `PATCH` 回整个数组 | **成功**：精确域名路由先于通配兜底被匹配 |
| `DELETE /id/devp_<name>` | **成功**：按 `@id` 精确删除单个数组元素 |

因此：

- **注册** = `GET .../srv0/routes` → 定位首个 host 含通配符的路由 → 将新路由插入其前（无通配路由则追加末尾）→ `PATCH` 回完整数组 → 再 `GET /id/devp_<name>` 回读确认写入生效（不依赖 srv0 动态发现；回读失败视为注册失败）。
- **注销** = `DELETE /id/devp_<name>`（无需动数组）。
- **并发保护** = `flock ~/.local/state/devp/lock` 包住注册的 GET+PATCH 与注销的 DELETE（毫秒级）；心跳的每次注册尝试前检查 stop，持锁后再检查一次，保证进程退出时不会在注销之后把路由写回。
- 动态路由 JSON 形如：

```json
{
  "@id": "devp_<name>",
  "match": [{"host": ["<name>.test"]}],
  "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:<port>"}]}],
  "terminal": true
}
```

- **崩溃恢复**：devp 常驻期间每 30s `GET /id/devp_<name>`，404 则重走注册（应对 `caddy reload` / `systemctl --user restart caddy` 冲掉运行时配置）。副作用：caddy 重启后最长 30s 内已注册域名 404。

## 6. `devp` CLI 规格

Python stdlib 单文件（argparse / subprocess / signal / socket / threading / urllib / fcntl / json），安装 = 复制到 `~/.local/bin/devp`。

| 命令 | 行为 |
|---|---|
| `devp up <name> [--port N] [--ready-timeout S] -- CMD...` | 完整生命周期（§3）。`--port` 显式指定端口（应用不读 `PORT` 时用）；`--` 后的启动命令必填，缺失则报错 |
| `devp down <name>` | `DELETE /id/devp_<name>`，清理崩溃残留 |
| `devp ls` | 列出所有 `devp_*` 路由：`<name>.test → 127.0.0.1:<port>`，并探测上游可达性（通/不通标注） |
| `devp selftest` | 回归测试：起隔离 Caddy 实例（高位端口 + 独立 storage + admin 端口）跑完整 up/转发/注销流程，结束清理 |

行为细节：

- **name 校验**：单标签 `\A[a-z0-9][a-z0-9-]*\Z`（首字符 `[a-z0-9]`、后续 `[a-z0-9-]`，长度 ≤ 63，不得含点）。用 `\A…\Z` 而非 `^…$`，防止末尾换行绕过（D8）。
- **端口探测**：`socket.bind(('127.0.0.1', 0))` 取随机空闲端口后关闭；存在微小时隙竞争，子进程 bind 失败时以子进程错误呈现，用户重跑即可。显式 `--port` 必须在 `1..65535`，否则报错退出。
- **环境注入**：`PORT`（端口）。仅此一个，`HOST` 等按需二期。
- **进程管理**：子进程以 `start_new_session=True` 启动，成为独立 session/进程组 leader；devp 收到 SIGINT → 向子进程组转发 SIGINT，收到 SIGTERM/SIGHUP → 转发 SIGTERM。清理顺序固定为 `stop.set()` → 有界 join 心跳 → 注销路由（重试 3 次）→ 按进程组发 SIGTERM、宽限 5s、超时 SIGKILL 进程组。退出码：SIGTERM→143、SIGHUP→129、SIGINT→130；正常退出透传子进程退出码。子进程先退出则直接注销并以同码退出。
- **就绪判定**：TCP connect `127.0.0.1:$PORT` 成功即注册（不做 HTTP 探测；dev server 就绪语义各异，TCP 足够）。超时未就绪：杀子进程、不注册、报错并提示「应用可能未使用 $PORT，可用 --port 显式指定」。
- **重复注册**：`up` 前检查 `/id/devp_<name>`，已存在则报错退出（提示先 `devp down` 或换名），不覆盖。

## 7. 错误处理

| 场景 | 行为 |
|---|---|
| Admin API 不可达（caddy 未运行） | `up` 启动前预检失败，明确报错退出 |
| 注册 PATCH 失败 | 重试 3 次（退避 1s/2s），仍失败则杀子进程退出非零；PATCH 后回读确认 |
| 注销失败（caddy 重启中） | 重试 3 次（退避 1s/2s）后放弃并警告（残留可 `devp down` 清理） |
| SIGINT / SIGTERM / SIGHUP | 转发给子进程组 → 注销路由 → 按组终止子进程 → 退出码 130/143/129 |
| 注册/注销期间收到信号 | handler 置 stop 并抛 `SystemExit(128+signum)`，`finally` 完成清理；持锁后的 stop 复查阻止心跳写回 |
| 心跳时路由丢失 | 静默重注册；重注册失败仅警告，不影响子进程 |
| 应用崩溃 | 正常注销路由，退出码透传 |

## 8. 已知边界与取舍

- **应用侧接入成本**：启动命令需引用 `$PORT`。Next.js / CRA 原生读 `PORT`；Vite 需 `vite --port $PORT`，uvicorn 需 `uvicorn --port $PORT`，gunicorn 需 `--bind 127.0.0.1:$PORT`（gunicorn **不**原生读 `PORT`）。命令行参数中的 `$PORT` 必须包在 `sh -c '...'` 里（D23）。每个项目接入 = 改一行 dev 命令。
- **Host 校验**：Host 头为 `<name>.test`，有 host 校验的框架需放行（Vite `server.allowedHosts`、Django `ALLOWED_HOSTS`）。这是「应用内绝对 URL 正确」的必要代价。
- **caddy 重启窗口**：最长 30s 404（心跳周期）。
- **崩溃残留**：不做自动扫描清理（YAGNI）；`devp ls` 标注不可达 + `devp down` 手动清。
- **多服务项目**：一个项目多个服务就多次 `devp up`（如 `devp up codeup-api` + `devp up codeup-web`），各自独立域名。

## 9. 测试与验收

- `devp selftest`（隔离 Caddy 实例，不碰生产 443/2019）：
  1. on-demand 逐域名签发成功，且客户端使用沙箱 CA 做真实主机名校验（不带 `-k`）
  2. 注册后 `foo.test` 命中上游、未注册域名得兜底 404
  3. 注销后 `foo.test` 回落兜底 404
  4. `http://foo.test` 返回 301，Location 为 `https://foo.test/`（D7）
- 单元/集成测试：`python3 -m unittest discover -s tests -v`（含 name 校验、注册幂等、回读确认、重试退避、flock 跨进程互斥、SIGTERM/SIGINT 生命周期与孙进程清理）。
- 真实验收：codeup-manager 双域名并行——`devp up codeup-api -- sh -c 'uvicorn app:app --host 127.0.0.1 --port "$PORT"'` 与 `devp up codeup-web -- sh -c 'npm run dev -- --port "$PORT"'`，浏览器两个域名可用，Ctrl+C 后域名注销。
- README 更新：devp 章节（命令表、接入示例、Host 校验提示）。

## 10. 二期候选（本期不做）

- `/proc` 扫描兜底（应用不读 `PORT` 时自动发现其监听端口）
- `devp ls --prune` 自动清理不可达路由
- 注入 `DEVP_HOST` 等附加环境变量

## 实施裁决记录

| 编号 | 裁决 | 原因 |
|---|---|---|
| D7 | 80→443 显式重定向期望 301（非 308） | Caddyfile 的 `redir ... permanent` 映射为 301；308 只出现在 Caddy 自动生成的跳转 |
| D8 | `NAME_RE` 用 `\A…\Z`，拒绝末尾换行 | `$` 匹配末尾换行之前，`codeup\n` 会生成永不匹配的 host，且注册/注销 URL 不一致 |
| D10 | `build_route`/`deregister`/`route_exists` 入口校验 name | 把「name 已校验」从调用方约定变成结构性保证 |
| D13 | `register` 幂等：同名路由就地替换 | PATCH 已生效但响应丢失后重试，盲目插入会产生重复 `@id`，`DELETE` 删不干净 |
| D15 | 注销前有界 join 心跳线程 | 心跳可能正卡在 `register_retry`，DELETE 后写回会留下指向死端口的残留；持锁后 stop 复查进一步闭合 |
| D18 | `up` 必须用 `--` 分隔 | `nargs=REMAINDER` 会吞掉 name 之后的 `--port`；规格用法本就始终带 `--` |
| D19 | `ls` 区分「Admin 不可达」与「无路由」 | 否则 caddy 没跑时误报空状态，把故障引向错误排查方向 |
| D21 | TLS 改为 on-demand 逐域名签发 | `*.test` 是二级通配符，OpenSSL/浏览器主机名校验拒绝（实测 curl exit 60）；不配 `ask`，仅本机信任域 |
| D22 | 清理 storage 里遗留的通配证书缓存 | 旧 `wildcard_.test` 缓存会让 on-demand 复用通配证书、从不逐域名签发；迁移后 restart |
| D23 | `$PORT` 示例必须包 `sh -c` | devp 直接 `Popen(argv)`、不经 shell，`$PORT` 会被用户 shell 提前展开成空串 |
| D25 | 终审修复波次范围 | 覆盖 SIGTERM/SIGHUP、join 守卫、注销重试、flock/DELETE 竞态、进程组清理、文档一致性及高价值 Minor；进程组采用 `start_new_session` + 显式转发 |
