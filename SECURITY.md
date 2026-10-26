# 安全策略

## 支持范围

| 版本 | 状态 |
|---|---|
| 0.1.x | ✅ 支持 |

## 报告安全问题

**请不要开公开 issue。** 用仓库托管平台提供的**私密漏洞报告**
（GitHub 是 Security Advisory 表单，Codeup 是「安全公告」），
或直接联系维护者（见仓库首页的联系方式）。

请在报告里写清：

- 受影响版本（`devproxy --version`）与操作系统；
- 复现步骤与**完整命令**；
- 期望的行为与实际的行为；
- 可能的利用方式（谁能触发、需要什么前提）。

我们会在 **7 天内**给出初步回应，确认后协调修复与披露。

## 这个工具的安全边界（给使用者看）

devproxy 的设计前提是「**只影响这台机器上的本地开发环境**」。它确实会动系统，
所以这些边界必须说清楚：

### 它会改什么

- 在**系统信任库**与 NSS 库 / 系统钥匙串里安装**自签 CA 根证书**；
- 写 DNS 解析路径（Linux 的 systemd-resolved drop-in、macOS 的 `/etc/resolver/`）
  —— Linux 的 resolved 后端下，drop-in 落在**全局段**，会影响这台机器**全部域名**
  的解析顺序（未注册到本工具的域名也会先问本机的 dnsmasq）；
- 安装并启用 **systemd user 单元 / launchd 作业**（macOS 的 Caddy 是 root 的
  LaunchDaemon，会绑定 80/443）；
- 下载并执行第三方二进制（Caddy）—— 有内建 sha256 校验，校验失败即中止。

### 它不做什么

- **不**把任何东西发到公网：路由表、配置、日志都在本机；
- **不**上传遥测；
- **不**读取或存储你的应用代码与密钥（只把应用的标准输出收进日志文件）。

### 已知需要注意的地方

- **CA 私钥**在 `~/.local/share/caddy/pki/authorities/local/`。谁能读这个目录，
  谁就能签发被这台机器信任的证书 —— 它的信任范围等同于你的用户账号。
- 装了 CA 之后，**本机浏览器与命令行工具会信任本机 Caddy 签发的证书**。
  用 `devproxy uninstall --remove-ca` 可以移除。
- `devproxy setup` 会**整机范围**地改 DNS 解析路径（见上）。共享机器上尤其要注意。
- 路由日志（`~/.local/state/devproxy/logs/routes/*.log`）里可能有你的应用打印的
  敏感信息。日志只在你自己的用户目录里，但别把它当保险箱。

### 遇到可疑情况

```bash
devproxy doctor        # 看当前装了哪些东西
devproxy uninstall     # 卸载：停服务、移除单元、删 DNS 路由（默认保留你的配置与 CA）
```

完整说明见 [docs/migration-and-uninstall.md](docs/migration-and-uninstall.md)。
