# 迁移、回滚与卸载

## 迁移与回滚

`devproxy setup` 会检测到**本工具未管理的遗留 caddy 服务**（手工写的
`caddy.service` + `Caddyfile`）。检测到之后，迁移按「**先备份，再动手**」的顺序执行：

1. 把**每一个将被改动的文件**备份到 `~/.local/state/devproxy/backups/<UTC时间戳>/`
   （目录里另有 `manifest.json` 与人可读的 `MANIFEST.txt`）；
2. 备份失败 → **一个文件都不改**，迁移中止；
3. 把遗留 Caddyfile 里的**个人站点块**搬到 `~/.config/devproxy/sites.caddy`
   （`.test` 相关的块由工具接管、会被丢弃，旧 Caddyfile 本身完整保留在备份里）；
4. 用 `caddy validate` 校验新配置（校验不过就不换单元）；
5. 改写服务单元、重启 caddy。

**有两种遗留环境会被工具直接拒绝迁移**，而不是无备份地硬来 —— 两种情况都以退出码 1 中止，
**一个文件都不改**：

- **`ExecStart=` 里解析不出 `--config`** —— 无法确认它用的是哪个 Caddyfile，猜错就会改错文件；
- **单元使用 `EnvironmentFile=`** —— 变量不在单元里，迁移无法把它们安全带到新单元。

修好单元后重跑 `setup` 即可：`--config` 写成 `--config <路径>` 或 `--config=<路径>` 都认；
`EnvironmentFile` 里的变量需要内联到 `Environment=`，或直接移除 `EnvironmentFile=`。

回滚：

```bash
devproxy setup --rollback ~/.local/state/devproxy/backups/2026-10-22T12-00-00Z
```

回滚会按备份逐个还原文件（原本不存在的文件会被删掉），部分条目失败时会把
「已还原了哪些」如实打印出来。

非交互环境里做迁移必须加 `--yes`，否则以退出码 4 拒绝（迁移会改写你的单元与配置，
不能不问一声就动手）。

---
## 卸载

```bash
devproxy uninstall --dry-run   # 先看它会做什么，不做任何变更、不需要 sudo
devproxy uninstall             # 交互式：逐项提问，最后再确认一次
devproxy uninstall --yes       # 非交互（CI / 脚本）必须加，否则退 4
```

**交互式会问什么**（这两个选择都有对应的 flag，给了就不再问）：

1. 「还要删除 Caddy 存储（CA 与证书）、迁移备份与下载的 caddy 二进制吗？」—— 默认**否**
2. 「要把 CA 从系统信任库卸载吗？」—— 只在 CA 属于「迁移前就存在」时问，默认**否**
3. 最后确认「即将执行以上卸载步骤，继续？」—— 默认**否**

答「是」之后工具会**重建并重画计划**，让你看清多出来的是哪几步，再进入最终确认。

**默认会做**（按这个顺序）：

1. 收掉由本工具启动、还挂着的子进程（按 `routes.json` 里的 pid + 启动时刻校验，不会误杀）
2. 停止并移除 `caddy`、`dnsmasq` 两个服务单元
3. 删除工具写入的 DNS 路由文件，并让解析器重新加载
4. 卸载 CA —— 但受下面两条规则约束
5. 删除工具生成的状态文件（`Caddyfile`、`routes.*`、`dns-route.json`、`caddy-storage.json`、`caddy-env.json`、`logs/`）

**默认保留**（不删）：

| 保留 | 原因 |
|---|---|
| `sites.caddy` | 你的个人站点 |
| `config.toml` | 你的配置 |
| `~/.local/share/caddy` | CA 与全部证书 —— 你自己另起的 caddy 可能也在用 |
| `~/.local/bin/caddy` | 可能是你自己装的 |
| `~/.config/dnsmasq/dnsmasq.conf` | 是**增量合并**的，原本就有。有改动前的备份就从最新备份还原，没有就原样保留并提示 |
| `backups/` | 回滚的唯一依据 |
| `loginctl enable-linger` | 整机级设置，你别的 user 服务可能依赖它 |

**CA 的两条规则**：

- **按指纹精确匹配**来删，不按昵称 —— 同一台机器上同名不同内容的根证书不会被误删；
- **迁移来的 CA 默认不卸载**：如果存在 `caddy-storage.json` 留档，说明这个 CA 在本工具接管**之前**就存在、
  你的站点在用。此时跳过并提示；确实要卸就显式加 `--remove-ca`。

**「连存储一起删」被选中时才额外删除**（交互答「是」，或显式 `--purge`）：
`~/.local/share/caddy`、`backups/`、`config.caddy_bin_path`。
注意最后一项默认是 `~/.local/bin/caddy` —— 如果你自己装过 caddy，这一步会把它一起删掉。

**迁移过的机器**：卸载不会自动还原遗留环境。原始 `caddy.service` 与 `Caddyfile` 在
`backups/<时间戳>/` 里，工具会在结尾把路径打出来；要回到迁移前的状态用
`devproxy setup --rollback <目录>`。备份默认保留，所以「先卸载、再回滚」也能恢复；
但 `--purge` 会把备份一起删掉，之后就来不及了。

`uninstall` 是幂等的：已经卸干净之后再跑一次会说「没有可卸载的东西」并成功退出。

---
