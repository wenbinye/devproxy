# 本地 HTTP/HTTPS 开发服务器（Caddy + systemd 用户级服务）
# 查看所有命令: just --list

caddy_bin   := home_dir() / ".local" / "bin" / "caddy"
# 下载链接为 linux/amd64；arm64 机器请改为 arch=arm64
caddy_url   := "https://caddyserver.com/api/download?os=linux&arch=amd64"
service_dst := home_dir() / ".config" / "systemd" / "user" / "caddy.service"
user_name   := env_var_or_default('USER', 'yewenbin')

# 默认：显示命令列表
default:
    @just --list

# 一键安装：下载 caddy + 授权 80/443 + 注册自启动服务
setup: install grant enable
    @echo "完成。服务已启动并设为开机自启"

# 安装 caddy 二进制到 ~/.local/bin（无需 root）
install:
    mkdir -p {{home_dir()}}/.local/bin
    curl -fsSL "{{caddy_url}}" -o {{caddy_bin}}
    chmod +x {{caddy_bin}}
    {{caddy_bin}} version

# 授权绑定 80/443 低位端口（需 sudo；升级 caddy 二进制后需重跑）
grant:
    sudo setcap cap_net_bind_service=+ep {{caddy_bin}}
    getcap {{caddy_bin}}

# 注册 systemd 用户级服务并设置开机自启（含 enable-linger，未登录也运行）
enable:
    #!/usr/bin/env bash
    set -eu
    if [ ! -x "{{caddy_bin}}" ]; then
        echo "caddy 未安装，请先执行 just install" >&2
        exit 1
    fi
    mkdir -p "{{home_dir()}}/.config/systemd/user"
    cp caddy.service "{{service_dst}}"
    systemctl --user daemon-reload
    systemctl --user enable --now caddy
    sudo loginctl enable-linger {{user_name}}
    echo "已启用开机自启（linger 已开启）"

# 取消自启动并停止服务
disable:
    systemctl --user disable --now caddy
    sudo loginctl disable-linger {{user_name}}

# 启动服务
start:
    systemctl --user start caddy

# 停止服务
stop:
    systemctl --user stop caddy

# 重启服务
restart:
    systemctl --user restart caddy

# 修改 Caddyfile 后热加载（不断开现有连接）
reload:
    systemctl --user reload caddy

# 查看服务日志（实时跟踪）
logs:
    journalctl --user -u caddy -f

# 前台运行（调试用，不经 systemd；先 just stop 避免端口冲突）
run:
    {{caddy_bin}} run --config Caddyfile

# 校验 Caddyfile 语法
validate:
    {{caddy_bin}} validate --config Caddyfile

# 格式化 Caddyfile
fmt:
    {{caddy_bin}} fmt --overwrite Caddyfile

# 安装本地 CA 根证书到系统信任库（需 sudo；curl 不再告警）
trust: trust-system trust-nss

# 装入系统信任库（curl 等基于 OpenSSL 的工具）
trust-system:
    #!/usr/bin/env bash
    set -eu
    cert="{{home_dir()}}/.local/share/caddy/pki/authorities/local/root.crt"
    if [ ! -f "$cert" ]; then
        echo "根证书不存在，请先 just start 生成" >&2
        exit 1
    fi
    sudo cp "$cert" /usr/local/share/ca-certificates/caddy-local.crt
    sudo update-ca-certificates

# 装入 NSS 库（Chrome/Chromium 用；不读系统信任库，必须单独装）
trust-nss:
    #!/usr/bin/env bash
    set -eu
    cert="{{home_dir()}}/.local/share/caddy/pki/authorities/local/root.crt"
    nss="sql:{{home_dir()}}/.pki/nssdb"
    if [ ! -f "$cert" ]; then
        echo "根证书不存在，请先 just start 生成" >&2
        exit 1
    fi
    if ! command -v certutil >/dev/null 2>&1; then
        echo "certutil 未安装，请执行: sudo apt install libnss3-tools" >&2
        exit 1
    fi
    mkdir -p "{{home_dir()}}/.pki/nssdb"
    # 已存在则先删除，保证重复执行幂等
    certutil -d "$nss" -D -n "Caddy Local Authority" >/dev/null 2>&1 || true
    certutil -d "$nss" -A -n "Caddy Local Authority" -t "C,," -i "$cert"
    echo "已装入 NSS 库（Chrome/Chromium 需完全重启后生效）"

# 健康检查：systemd 状态与端口探测
status:
    @echo "systemd 状态: $(systemctl --user is-active caddy)"
    @echo "自启动: $(systemctl --user is-enabled caddy)"
    @printf "http://localhost  -> HTTP %s\n" "$(curl -s -o /dev/null -w '%{http_code}' http://localhost)"
    @printf "https://localhost -> HTTP %s\n" "$(curl -s -o /dev/null -w '%{http_code}' https://localhost)"
    @printf "https://nginx.local.yikuaigao.com -> HTTP %s\n" "$(curl -s -o /dev/null -w '%{http_code}' https://nginx.local.yikuaigao.com)"

# 安装 devp CLI 到 ~/.local/bin（修改 devp 脚本后需重跑）
install-devp:
    install -m 755 devp {{home_dir()}}/.local/bin/devp
    {{home_dir()}}/.local/bin/devp --help > /dev/null && echo "devp 已安装到 ~/.local/bin/devp"
