# DNS 与证书

## DNS

| 平台 | 机制 | dnsmasq 监听 |
|---|---|---|
| Linux（默认 `resolved`） | 写 `/etc/systemd/resolved.conf.d/devproxy.conf`，其中 `Domains=~.<后缀>` 把该后缀路由到 dnsmasq | `127.0.0.1:5353` |
| Linux（`system`） | 写 `/etc/dnsmasq.d/devproxy.conf`，由系统 dnsmasq 负责 | `127.0.0.1:5353` |
| macOS | 写 `/etc/resolver/<后缀>`（每域解析器文件），不动 `/etc/resolv.conf` | `127.0.0.1:5300` |

Linux 的 resolved drop-in 内容形如：

```ini
[Resolve]
DNS=127.0.0.1:5353
FallbackDNS=1.1.1.1 8.8.8.8
Domains=~.test
```

**注意**：drop-in 里的 `DNS=` 把这台机器的**全局** DNS 指到了用户级 dnsmasq ——
这是为了不占用 53 端口而做的取舍。落到实际语义上：

- `DNS=127.0.0.1:5353` 写在 resolved 的**全局段**，决定的是**这台机器全部域名**的
  解析都先问用户级 dnsmasq，**不只是 `*.test`**；
- `FallbackDNS=1.1.1.1 8.8.8.8` 是同一份 drop-in 里的**整机兜底**：dnsmasq 起不来、
  或它答不了的时候，全机解析退化为这两个公共解析器（而不是整机断网）；
- `Domains=~.<后缀>` 决定 dnsmasq 内部把哪个后缀解析到本机。

`Domains=~.<后缀>` 里的后缀**为什么只能是保留顶级域**，见
[「域名后缀只允许保留顶级域」](configuration.md#域名后缀只允许保留顶级域) ——
真实公共顶级域会让**整台机器**该后缀的解析都被劫持。

作为配套，工具保证 dnsmasq 的配置里**始终带 `no-resolv`**
（除非你自己写了 `resolv-file=`）：dnsmasq 因此**不会**把 `/etc/resolv.conf`
（resolved 机器上是 `127.0.0.53` 桩）当成上游，否则 resolved ↔ dnsmasq 互指，
普通域名的解析会在全机范围内失败。

dnsmasq 的配置是**增量合并**的：你的 `~/.config/dnsmasq/dnsmasq.conf` 里已有的
域名条目、`filter-AAAA` 等都会保留，工具只保证 `port=`、`address=/.<后缀>/`、
`listen-address=127.0.0.1`、`bind-interfaces`、`no-resolv` 与缺失的全局上游存在。
它**只监听回环地址**，不会在局域网里变成一个开放递归解析器。
## `doctor` 与 `setup` 各查什么

- **`devproxy doctor` 只做静态检查**：DNS 路由文件在不在、服务在不在跑、CA 在不在、
  单元是否指向本工具的 Caddyfile 与二进制。**它不做 DNS 解析** —— 不发起任何
  `resolvectl` / `dig` 查询。
- **「解析是否真的生效」由 `setup` 的自检阶段负责**：`setup` 执行完变更后会做 verify，
  真的解析一次 `foo.<后缀>` 并确认结果指向本机。注意这只发生在 **setup 确实做了变更**的时候；
  `setup` 走「所有组件已就绪」的 no-op 分支时，这次自检**不会**发生。

要立刻确认解析是否生效，可以自己查一次：

```bash
resolvectl query foo.test          # Linux
dscacheutil -q host -a name foo.test   # macOS
```
## 证书：精确站点块，不是通配符

每条路由渲染成**一个精确的站点块**：

```caddyfile
codeup.test {
	tls internal
	reverse_proxy 127.0.0.1:34567
}
```

**没有 `*.test` 通配块，也没有 on-demand TLS。** 由此得到一个明确的安全属性：

> **没有注册的域名拿不到任何证书** —— TLS 握手直接失败。

也就是说，不存在「本机任意进程都能为任意 `.test` 名字骗到一个被系统信任的证书」的情况。

信任 CA 需要装进不同的地方：

| 平台 | 装入位置 | 谁读它 |
|---|---|---|
| Linux | 系统信任库（`/usr/local/share/ca-certificates/` + `update-ca-certificates`） | curl / Go / Java 等 |
| Linux | NSS 库 `~/.pki/nssdb` | **Chrome / Chromium 只认 NSS 库** |
| macOS | 系统钥匙串 `/Library/Keychains/System.keychain` | Safari / Chrome / 自带 `curl` 都读（macOS 26 真机已验，见验证清单 E6） |
| 任意 | **Firefox 不读上面任何一处** | Firefox 只认自己 profile 里的 `cert9.db` |

两步是独立的：只装系统信任库，Chrome 照样报 `net::ERR_CERT_AUTHORITY_INVALID`；
只装 NSS 库，curl 又不认。Linux 上装 NSS 库需要 `certutil`
（`sudo apt install libnss3-tools`）。

**Linux 上装入 NSS 库后必须完全退出并重启 Chrome** —— 关窗口不算，要从托盘退出进程；
Chrome 只在启动时读取 NSS 库。

### Firefox 要单独信任（工具不替你装）

**Firefox 有自己的证书库**（`~/.mozilla/firefox/<profile>/cert9.db`）——
它既不读 NSS 库，也不读系统信任库。因此 `devproxy trust` 跑完之后，
**Chrome 正常了，Firefox 仍会报证书错误** —— 这不是工具坏了，是 Firefox 的设计。

两种解法（**都要完全退出并重启 Firefox** 才生效）：

1. **手动导入**：Firefox → 设置 → 隐私与安全 → 证书 → 查看证书 → 导入 →
   选 `<storage>/pki/authorities/local/root.crt`
   （默认 `~/.local/share/caddy/pki/authorities/local/root.crt`）；
2. **让 Firefox 也认系统信任库**：在 `about:config` 里打开
   `security.enterprise_roots.enabled`（布尔值改为 `true`）。

`devproxy doctor` 在检测到本机装了 Firefox 时会把这条提示打出来；
`devproxy trust` 装完之后也会提醒一句。**这是提示，不是缺陷** ——
Firefox 没装 CA 不影响 doctor 的体检结论（Chrome / curl 侧可能完全正常）。

CA 根证书的路径是 `<storage>/pki/authorities/local/root.crt`
（默认 `~/.local/share/caddy/pki/authorities/local/root.crt`）。

---
