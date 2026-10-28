# 故障排查

## 常见症状


- **mise（或其它工具）报 "Error loading CA root certificate: failed to read PEM ... Permission denied"（`/etc/ssl/certs/caddy-local.pem`）**：
  这是前身 `devp` 脚本的残留 —— 它用 `cp` 把 CA 装成了
  `/usr/local/share/ca-certificates/caddy-local.crt`，源文件的 0600 权限也跟了过去，
  于是所有遍历信任库目录的工具都会在这份文件上失败。跑 `devproxy trust`：它会按名字
  认出这份残留、核对指纹后删除（update-ca-certificates 会顺带清掉失效的符号链接）。
  验证：`ls /usr/local/share/ca-certificates/` 里不再有 `caddy-local.crt`。
- **`devproxy up` 报 "conflict web ... sibling port changed (DEVPROXY_API_PORT: 49435 -> 51717)"**：
  兄弟路由重启后换了端口，那个应用还握着旧的。用 `devproxy down <名字>` 再 `devproxy up`
  重启它 —— 或者在 `devproxy.toml` 里把兄弟路由的端口写死，让它不再变动。
- **`devproxy up` 报 "route 'web' uses $DEVPROXY_API_PORT, but no route 'api' is declared in this file"**：
  命令引用了本文件没有声明的路由 —— devproxy 只导出本项目路由的端口。请把该路由声明在
  同一个文件里。
- **`devproxy up` 报「就绪超时」**：应用多半没读 `PORT`。改用 `sh -c '... $PORT ...'`，
  或用 `--port N` 显式指定端口（此时应用必须自己绑这个端口）。
- **`up` 报「Caddy 未运行（Admin API 不可达）」**（退出码 3）：先跑 `devproxy setup`，
  或 `systemctl --user start caddy` / `launchctl kickstart -k system/com.devproxy.caddy`。
- **`up` 报「主 Caddyfile 不存在」**（退出码 3）：还没 setup，先 `devproxy setup`。
- **curl 报证书错误 / Chrome 报 `net::ERR_CERT_AUTHORITY_INVALID`**：
  还没跑 `devproxy trust`，或 CA 是之后才生成的。顺序是：先 `up`（生成 CA）→ 再 `trust`。
  Chrome 只认 NSS 库，装完必须**完全退出并重启 Chrome**。
- **`doctor` 说「CA 根证书未被信任（缺少：系统信任库、NSS 库）」**：跑 `devproxy trust`。
  Linux 上若报缺 `certutil`，先 `sudo apt install libnss3-tools`。
- **macOS：`devproxy trust` 报 `SecTrustSettingsSetTrustSettings: The authorization was denied since no user interaction was possible`**：
  信任标志只能在**图形会话**里设，sudo 也不够 —— 要在**那台 Mac 自己的终端**里跑
  `devproxy trust`（走 SSH 会失败）。证书无论如何都会进
  `/Library/Keychains/System.keychain`，但没有信任标志就没人认它 ——
  此时 curl 仍 exit 60，`doctor` 仍报缺失。
- **Chrome 正常但 Firefox 仍报证书错误**：Firefox 有自己的证书库
  （`~/.mozilla/firefox/<profile>/cert9.db`），**不读** NSS 库也不读系统信任库。
  手动导入 CA，或在 `about:config` 打开 `security.enterprise_roots.enabled`，
  两种做法都要**完全退出并重启 Firefox**。详见上面「Firefox 要单独信任」一节。
- **升级后每条命令都报「配置项 domain_suffix 非法」**：新加的顶级域白名单挡住了你原来的
  `domain_suffix`。看上面「升级提示：既有用户可能突然报错」一节。
- **`doctor` 说「caddy 版本不符」**：setup 会替换为固定版本，重跑 `devproxy setup`。
- **改了 `config.toml` 的 `storage` 却没生效**：看上面「storage 留档」一节。
- **域名解析到别的 IP**：`doctor` 不查解析（它只做静态检查），自己查一次
  `resolvectl query <name>.test`（macOS 用 `dscacheutil -q host -a name <name>.test`）；
  确认路由文件在位之后，Linux 上必要时 `sudo systemctl restart systemd-resolved`。
- **`devproxy logs caddy` 打印了一堆东西但没有退出**：这是预期行为 —— 它是流式跟随，
  Ctrl-C 退出。
- **路由残留**（devproxy 被 `kill -9`）：`devproxy ls` 会把不可达的路由标出来，
  `devproxy down <name>` 清掉。
- **`devproxy logs dnsmasq`** 看 dnsmasq 的日志；Linux 是 journalctl，macOS 是
  `~/.local/state/devproxy/logs/dnsmasq.log`。
- **`devproxy run` 提示「caddy 服务仍在运行，80/443 已被占用」**：先按提示停掉服务
  （Linux `systemctl --user stop caddy`，macOS `launchctl bootout system/com.devproxy.caddy`），
  或者用 `--config` 指向一份用别的端口的 Caddyfile。

真机验证清单见 [verification-checklist.md](verification-checklist.md) ——
里面列了自动化测试覆盖不到、必须在真实机器上逐条跑的行为。

---
