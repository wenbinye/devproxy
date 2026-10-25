#!/usr/bin/env bash
# 在干净的 ubuntu 容器里端到端验证 devproxy（验证清单 F10 的可复现版本）。
#
# 用法（在 devproxy 仓库根目录）：
#   bash scripts/verify-clean-machine.sh
#
# 它做什么：
#   1. 用 `git archive HEAD` 打包**当前提交**的源码（不含 .git / .venv）；
#   2. 起一个干净的 ubuntu 容器（非 root 用户 + 免密 sudo，无 systemd）；
#   3. pip 安装，逐层验证：CLI 基础 → root 拒绝 → 裸机 setup 的优雅失败
#      → 手工补 systemd 的角色 → run 托管 → sync → trust → curl → down。
#
# 前置：docker 可用、能联网（拉镜像 + pip 拉 typer/rich + 下载 caddy）。
# 结束后容器自动删除，宿主机不受影响。

set -euo pipefail

CONTAINER=${CONTAINER:-dp-clean}
IMAGE=${IMAGE:-ubuntu:24.04}
DEVPROXY='~/.venv/bin/devproxy'   # 容器内 dev 用户的绝对路径（引号必须留着：
                                  # 波浪号要在**容器里**展开，不能在宿主机就展开）

PASS=0
FAIL=0

check() { # check <描述> <命令...>
    local desc="$1"; shift
    local out
    if out=$("$@" 2>&1); then
        echo "  ✔ $desc"; PASS=$((PASS+1)); return 0
    fi
    echo "  ✘ $desc"
    echo "$out" | head -3 | sed 's/^/      /'
    FAIL=$((FAIL+1))
    return 0   # 永远返回 0：单项失败不该中止整场验证（set -e）
}

expect_absent() { # expect_absent <描述> <不得出现的文字> <命令...>
    local desc="$1" banned="$2"; shift 2
    local out rc=0
    out=$("$@" 2>&1) || rc=$?   # set -e 下必须用 || 接住，赋值本身会带出退出码
    if [[ $rc -ne 0 && "$out" != *"$banned"* ]]; then
        echo "  ✔ $desc"; PASS=$((PASS+1)); return 0
    fi
    echo "  ✘ $desc（退出码 $rc，输出里不该出现「$banned」却出现了）"
    echo "    实际输出：$(echo "$out" | head -3 | tr '\n' ' ')"
    FAIL=$((FAIL+1))
    return 0
}

expect_fail() { # expect_fail <描述> <期望出现的文字> <命令...>
    local desc="$1" want="$2"; shift 2
    local out rc=0
    out=$("$@" 2>&1) || rc=$?   # set -e 下必须用 || 接住，赋值本身会带出退出码
    if [[ $rc -ne 0 && "$out" == *"$want"* ]]; then
        echo "  ✔ $desc"; PASS=$((PASS+1)); return 0
    fi
    echo "  ✘ $desc（退出码 $rc，输出里没找到「$want」）"
    echo "    实际输出：$(echo "$out" | head -3 | tr '\n' ' ')"
    FAIL=$((FAIL+1))
    return 0
}

runc() { # 在容器里以 dev 用户跑命令（登录 shell，PATH 干净）
    docker exec -u dev "$CONTAINER" bash -lc "$*"
}

cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -f /tmp/dp-src.tar
}
trap cleanup EXIT

echo "==> 1) 打包当前提交的源码（$(git rev-parse --short HEAD)）"
git archive HEAD > /tmp/dp-src.tar

echo "==> 2) 起干净容器（$IMAGE）"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" --hostname cleanbox "$IMAGE" sleep infinity >/dev/null
docker exec "$CONTAINER" bash -c "
    apt-get -qq update >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get -qq install -y \
        python3-venv python3-pip sudo curl ca-certificates dnsutils >/dev/null
    useradd -m -s /bin/bash dev
    echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev
    chmod 440 /etc/sudoers.d/dev
"

echo "==> 3) 安装 devproxy（干净机器，唯一前置是 python3-venv）"
docker cp /tmp/dp-src.tar "$CONTAINER":/tmp/src.tar >/dev/null
docker exec "$CONTAINER" bash -c "mkdir -p /opt/src && tar -xf /tmp/src.tar -C /opt/src && chown -R dev:dev /opt/src"
runc "python3 -m venv ~/.venv && ~/.venv/bin/pip install -q /opt/src"

echo "==> 4) Tier 1：CLI 基础"
runc "cd ~ && mkdir -p work/demo && cd work/demo && git init -q . 2>/dev/null; true"
check "config 读默认配置"       runc "$DEVPROXY config"
check "ls 空路由表"             runc "$DEVPROXY ls"
check "init 生成声明"           runc "cd ~/work/demo && $DEVPROXY init --name demo --command 'npm run dev'"
check "init 生成的东西能被解析" runc "cd ~/work/demo && grep -q 'name = \"demo\"' devproxy.toml"

echo "==> 5) Tier 2：root 拒绝 + 裸机 setup 的优雅失败"
expect_fail "sudo 运行被拒（退 3）" "请以普通用户运行" \
    runc "cd ~/work/demo && sudo $DEVPROXY setup --yes"
expect_fail "裸机 setup：setcap 缺失的提示可操作" "libcap2-bin" \
    runc "cd ~/work/demo && timeout 120 $DEVPROXY setup --yes"
expect_absent "裸机 setup：无 traceback" "Traceback" \
    runc "cd ~/work/demo && timeout 120 $DEVPROXY setup --yes"

echo "==> 6) Tier 3 准备：补包 + 手工顶替 systemd 的角色"
docker exec "$CONTAINER" bash -c "
    DEBIAN_FRONTEND=noninteractive apt-get -qq install -y libcap2-bin dnsmasq >/dev/null
    # 53 端口的 dnsmasq 当系统解析器（容器没有 resolved）：
    # .test 由工具配置接管，其余转发给 docker 的 DNS
    dnsmasq --port=53 --listen-address=127.0.0.1 --no-resolv \
        --address=/.test/127.0.0.1 --server=192.168.50.1 2>/dev/null || \
    dnsmasq --port=53 --listen-address=127.0.0.1 --no-resolv \
        --address=/.test/127.0.0.1 --server=127.0.0.11
    printf 'nameserver 127.0.0.1\n' > /etc/resolv.conf
    sleep 1
" || true
check "demo.test 经系统解析指向 127.0.0.1" \
    docker exec "$CONTAINER" bash -c "dig +short demo.test 2>/dev/null | grep -q 127.0.0.1"

echo "==> 7) Tier 3：setup 补齐产物 → run 托管 → sync → trust → curl"
runc "cd ~/work/demo && timeout 180 $DEVPROXY setup --yes" || true
check "caddy 二进制已下载"            docker exec "$CONTAINER" test -x /home/dev/.local/bin/caddy
check "setcap 已生效"                 docker exec "$CONTAINER" bash -c "getcap /home/dev/.local/bin/caddy | grep -q cap_net_bind_service"
check "主 Caddyfile 已生成"           docker exec "$CONTAINER" test -f /home/dev/.config/devproxy/Caddyfile
check "DNS 路由文件已写入"            docker exec "$CONTAINER" bash -c "grep -q address=/.test/ /etc/dnsmasq.d/devproxy.conf"

docker exec -u dev -d "$CONTAINER" bash -c "nohup $DEVPROXY run > /tmp/run.log 2>&1"
sleep 4
check "run：Caddy 在跑（Admin API）" \
    docker exec "$CONTAINER" bash -c "curl -s http://127.0.0.1:2019/config/ >/dev/null"
# 声明用 heredoc 写进容器 —— 避免 init --command 穿两层 shell 的转义地狱。
# 注意：caddy 没有站点块时**不监听** 80/443 —— 端口监听检查必须在 sync 之后做。
docker exec -i -u dev "$CONTAINER" bash -c 'cat > /home/dev/work/demo/devproxy.toml' <<'TOML'
[[routes]]
name = "demo"
command = ["sh", "-c", "python3 -m http.server $PORT"]
TOML
check "sync：应用起来、路由注册"      runc "cd ~/work/demo && timeout 60 $DEVPROXY sync --yes"
# 已知行为：sync 返回到 caddy 完成重绑之间有个短暂窗口（实测 ~2s），
# 所以这里带重试 —— 顺带把它记录为产品侧的已知竞态
check "run：80/443 在监听（setcap 生效，容忍重绑窗口）" \
    docker exec "$CONTAINER" bash -c "for i in 1 2 3 4 5 6 7 8 9 10; do (echo > /dev/tcp/127.0.0.1/443) 2>/dev/null && exit 0; sleep 1; done; exit 1"
check "首条路由触发 CA 生成"          docker exec "$CONTAINER" test -f /home/dev/.local/share/caddy/pki/authorities/local/root.crt

# trust：NSS 那步会因没有 certutil 失败（预期内）。
# 注意系统信任这步**可能被跳过** —— caddy 启动时也会借 sudo 装自己的根 CA，
# devproxy 见"已信任"就不重复装。所以不能断言 devproxy-local.crt 存在，
# 要断言的是性质：**系统信任库能验过这张 CA**。
runc "cd ~/work/demo && timeout 60 $DEVPROXY trust --yes" || true
check "trust：系统信任库能验过这张 CA" \
    runc "openssl verify -CApath /etc/ssl/certs ~/.local/share/caddy/pki/authorities/local/root.crt"

check "curl https://demo.test → 200（无 -k）" \
    docker exec "$CONTAINER" bash -c "curl -s -o /dev/null -w '%{http_code}' https://demo.test/ | grep -q 200"

check "down：注销后握手失败" \
    runc "cd ~/work/demo && $DEVPROXY down demo >/dev/null 2>&1; ! curl -s -o /dev/null https://demo.test/"

echo
echo "==> 结果：$PASS 通过 / $FAIL 失败"
if [[ $FAIL -gt 0 ]]; then
    echo "有失败项。容器保留（$CONTAINER），可进去排查："
    echo "  docker exec -u dev $CONTAINER bash -l"
    trap - EXIT
    exit 1
fi
echo "全部通过。容器已清理。"
