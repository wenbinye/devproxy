"""Linux 平台实现：systemd(user) / setcap / update-ca-certificates / NSS / resolved。"""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import ipaddress
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from devproxy.platform.base import (
    CA_NICKNAME,
    SYSTEM_TRUST_DIR,
    ElevatedSession,
    nss_db_dir,
)

# 系统信任库（活库）里可能存放 CA 的位置。
#
# - 汇总包：Debian/Ubuntu 的 /etc/ssl/certs/ca-certificates.crt、
#   RHEL 的 /etc/pki/tls/certs/ca-bundle.crt
# - 散列索引目录：/etc/ssl/certs 下的 *.0（主题散列软链）与 *.pem
#
# 刻意**不含**暂存目录 /usr/local/share/ca-certificates —— 那个文件存在
# 只说明有人放了它进去，不代表 update-ca-certificates 跑成功过。
TRUST_BUNDLE_FILES = (
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
)

# 子进程的默认超时（秒）：跨进程调用必须有界，漏传超时等于允许永久挂起。
DEFAULT_TIMEOUT = 30.0
# DNS 解析探测的上限（与 macOS 的 DNS_QUERY_TIMEOUT 对齐）：解析器挂起
# 不能把 setup 的自检拖死。
DNS_QUERY_TIMEOUT = 3.0
# systemd 是否作为 init 运行的判据（容器 / WSL 里没有）。
SYSTEMD_RUN_DIR = Path("/run/systemd/system")

# NSS 信任标志（`certutil -L` 的 Trust Attributes 列）—— **只认 SSL 列的 "C"**。
#
# 标志是三列三条目：`SSL,S/MIME,JAR/XPI`，本工具关心第一列（SSL）。
# 第一列（以及第二列）的字母语义：
#   "C" = 受信任的 CA，可签发 **服务端** 证书（SSL 列）/ 邮件证书（S/MIME 列）
#   "T" = 受信任的 CA，可签发 **客户端** 证书（SSL 列；Chromium 不拿它当锚点）
#   "P" = 受信任的对端（叶子），**不是** CA 锚点
#   "G" / "U" = 有效但不受信任，"," / "." / 空 = 完全没有信任
#
# 为什么只认第一列的 "C"：本工具的 CA 是给站点证书当 issuer 的根 CA，
# Chrome 要的正是这个锚点。实测（NSS 3.120，用 `certutil -V -u V` 验一条
# 由该 CA 签发的叶子链）：
#   C,, / C,C, / C,C,C → valid
#   T,, / P,, / ,,      → "issuer has been marked as not trusted by the user"
# 而 Chromium 的 TrustStoreNSS 只引用 CERTDB_TRUSTED_CA（即 "C"），
# CERTDB_TRUSTED_CLIENT_CA（"T"）根本没被引用，CERTDB_TRUSTED（"P"）只算
# "受信任叶子"。把 T / P 当已信任是**假阳性**。
NSS_TRUSTED_FLAGS = ("C",)
NSS_TRUSTED_COLUMN = 0

# `certutil -L` 的表格行：昵称列宽 55（超长会溢出，昵称与标志之间只剩一个空格），
# 昵称**可以含空格** —— 所以只能从行尾切标志，绝不能按"第一个空格"切昵称。
#
# 标志有**三列**（`SSL,S/MIME,JAR/XPI`）：`C,,`、`C,C,`、`C,C,C`、`,C,`、`,,`
# 都是合法形态。正则必须表达"两列逗号、三列内容"：
# `C,C,C` 用 `,{2,}` 那种写法根本匹配不上（没有连续两个逗号），
# 而真机上 `Caddy Local Authority` 恰恰就是 `C,C,C` —— 解析不出来就会把
# "已信任"判成"未信任"，再被 `-A -t C,,` 把另外两列静默抹掉。
#
# 注意 `C,C,C` 行尾**没有**补空格（`C,,` 行尾有两个空格），先 rstrip 再匹配。
#
# 正则只是**候选**：每个候选昵称都要再用 `certutil -L -n <昵称> -a` 实际取一次，
# 取不到就跳过 —— 解析有偏差时宁可漏判也不能误判。
_NSS_ROW_RE = re.compile(
    r"^(?P<name>.*?)[ ]+(?P<flags>[A-Za-z]*(?:,[A-Za-z]*){2})$"
)


def _pem_der_blocks(text: str) -> list[bytes]:
    """从 PEM 文本里取出全部证书的 DER 字节。

    只做 base64 解码，不做 X.509 解析 —— 判据是"同一份字节"，
    解析证书结构反而会引入额外的失败点。
    """
    blocks: list[bytes] = []
    buf: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("-----BEGIN CERTIFICATE"):
            inside = True
            buf = []
            continue
        if line.startswith("-----END CERTIFICATE"):
            inside = False
            try:
                blocks.append(base64.b64decode("".join(buf), validate=False))
            except (binascii.Error, ValueError):
                pass
            buf = []
            continue
        if inside:
            buf.append(line)
    return blocks


def _cert_fingerprint(der: bytes) -> str:
    """证书指纹：DER 的 sha256（与 `openssl x509 -fingerprint -sha256` 同义）。"""
    return hashlib.sha256(der).hexdigest().upper()


def _pem_fingerprints(text: str) -> set[str]:
    return {_cert_fingerprint(der) for der in _pem_der_blocks(text)}


def _store_candidates() -> list[Path]:
    """系统信任库里可能放着 CA 的文件（汇总包 + 散列索引目录）。

    只读、只列名字；任何列目录的错误都吞掉 —— 判据失败必须退化成
    "未信任"，绝不能让 doctor 抛异常。
    """
    files: list[Path] = list(TRUST_BUNDLE_FILES)
    try:
        if SYSTEM_TRUST_DIR.is_dir():
            for pattern in ("*.0", "*.pem", "*.crt"):
                files.extend(sorted(SYSTEM_TRUST_DIR.glob(pattern)))
    except OSError:
        pass
    return files


def ca_in_trust_store(ca: Path) -> bool:
    """CA 是否**按指纹**存在于系统信任库。永不抛异常。

    判据是"活库里有没有这份字节完全相同的证书"，而不是"openssl 能不能
    验通这条链"：openssl 按 **SUBJECT** 选锚点，信任库里存在多个同主题
    根时它会挑到另一个，报 error 18（self-signed certificate）——
    而 curl 其实完全正常。拿它当判据会让 setup 永远认为"没信任"，
    而重装同一个 CA 只会增加更多同主题根，永远修不好。
    """
    try:
        ca_text = ca.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    target = _pem_fingerprints(ca_text)
    if not target:
        # 不是可解析的 PEM 证书 —— 无法按指纹比对，判为未信任
        return False
    for path in _store_candidates():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if target & _pem_fingerprints(text):
            return True
    return False


def _nss_rows(listing: str) -> list[tuple[str, str]]:
    """解析 `certutil -L` 的表格 → [(昵称, 信任标志)]。

    表头两行与空行会被自然过滤掉：标志的形状是"三列、两列逗号"，
    表头里的 `Certificate Nickname … Trust Attributes` 没有逗号，
    `SSL,S/MIME,JAR/XPI` 虽然有两列逗号、但列内容含 `/`，都不满足
    `[A-Za-z]*(?:,[A-Za-z]*){2}`，匹配不上。
    """
    rows: list[tuple[str, str]] = []
    for raw in listing.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        match = _NSS_ROW_RE.match(line)
        if not match:
            continue
        name = match.group("name").rstrip()
        if not name:
            continue
        rows.append((name, match.group("flags")))
    return rows


def _nss_missing_cert(text: str) -> bool:
    """`certutil` 的输出是否在**明确**说"这个条目不存在"。

    只有这几种文案才算"不存在"：
    - `could not find certificate named "…"`（`-D` / `-L` 找不到昵称）
    - `could not find cert: …`
    - `PR_FILE_NOT_FOUND_ERROR`

    库被锁 / 不可读 / 损坏时的 `SEC_ERROR_BAD_DATABASE` 之类**不算** ——
    那种情况是"判不了"，必须往上报，绝不能当成"没有"。

    只匹配上面这三种**完整文案**：泛化的 `"not found"` 会把
    "could not find the module …" 这类加载失败也判成"条目不存在"，
    而本函数的返回值正是删除失败能否被当成幂等成功的关键。
    """
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "could not find certificate named",
            "could not find cert:",
            "pr_file_not_found_error",
        )
    )


def _nss_flags_trusted(flags: str) -> bool:
    """这条目的 **SSL 列** 是否表示"受信任的 CA"。

    只看第一列：第二列（S/MIME）的 "C" 只对邮件证书生效，
    对 HTTPS 没用；首字符是 "," 时整列无信任。
    """
    return bool(flags) and flags[NSS_TRUSTED_COLUMN] in NSS_TRUSTED_FLAGS


def _nss_repair_flags(flags: str) -> str:
    """把 SSL 列补成 "C"，**保留另外两列**原有的信任。

    `certutil -M -t "C,,"` 会把 S/MIME / JAR 两列一起清掉 —— 用户库里
    若有 `,C,` 这种"只信任邮件用途"的条目，那样补标志就是**改写**了
    用户不打算让我们碰的信任。`C,C,` 只动该动的那一列。
    """
    parts = flags.split(",")
    if len(parts) != 3:
        # 形态不认识时**原样返回**，让 `certutil -M` 把同一串写回去：
        # 重置成 "C,," 会抹掉未知形态里可能存在的 S/MIME / JAR 信任，
        # 那正是本函数要避免的"改写用户不打算让我们碰的信任"。
        # （组件层把返回值直接喂给 `-M`；这里不能返回 None —— 那会变成
        # 字符串 "None"，反而写坏标志。）
        return flags
    return ",".join(["C", parts[1], parts[2]])


def _nss_db_state(nss: Path) -> bool | None:
    """NSS 库是否存在：True / False（确定没有）/ None（判不了）。

    `Path.exists()` 会把 `stat()` 的 PermissionError 吞成 False ——
    "库存在但目录进不去"于是变成"库不存在、条目一定不存在"。删除失败
    就会被当成幂等成功吞掉（旧根还留在库里，工具却报已就绪），所以这里
    必须保留三态。
    """
    try:
        (nss / "cert9.db").stat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return True


def _fingerprints_of_file(path: Path) -> set[str] | None:
    """读文件并返回其中证书的指纹集合；读不了 / 不是 PEM 返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _pem_fingerprints(text)


def _nss_entry_matches(
    nickname: str,
    expected: set[str],
    nss: Path,
    cache: dict[str, str | None],
) -> bool:
    """该昵称下的证书内容是否与 `expected` 相同。

    表格解析只是候选 —— 解析有偏差时宁可漏判也不能误判。每个昵称的
    `certutil -L -n … -a` 结果在 `cache` 里只取一次。
    """
    if nickname not in cache:
        code, pem = _run(
            ["certutil", "-L", "-d", f"sql:{nss}", "-n", nickname, "-a"]
        )
        cache[nickname] = pem if code == 0 else None
    pem = cache[nickname]
    if pem is None:
        return False
    return bool(expected & _pem_fingerprints(pem))


def _run(
    argv: list[str], timeout: float | None = DEFAULT_TIMEOUT
) -> tuple[int, str]:
    """跑一个 argv。timeout 是**防挂死**，不是性能优化。

    没有超时的子进程调用会被卡在交互提示上的命令永久拖住，并**无限往
    stdout 里写** —— 实测 `certutil -N` 在密码提示上死循环，把 200GB
    写进了重定向文件、把 /home 撑到 100%。返回码 124 沿用 coreutils timeout
    的约定，调用方按"命令失败"处理即可。

    默认值因此是 `DEFAULT_TIMEOUT` 而不是 `None`：漏传超时是有界等待，
    不是无限等待。只有确实需要等用户输入的调用（sudo 密码路径）才显式
    传 `timeout=None`。

    `stdin=DEVNULL` 防止交互式探针抢占终端输入（`sudo` 从 `/dev/tty`
    读密码，不受影响）。
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 124, f"命令超时（{timeout}s）: {argv[0]}"
    except OSError as e:
        # 命令不存在（FileNotFoundError）、可执行位缺失（PermissionError）等
        # 都归一为“命令不可用”，让调用方按返回码处理而不是 traceback。
        return 127, f"命令不可用: {argv[0]}（{e}）"
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def _first_ip(text: str) -> str | None:
    """从 resolvectl / dig 输出里取第一个 A/AAAA 答案。"""
    for line in text.splitlines():
        for token in line.replace(",", " ").split():
            candidate = token.strip("[];()")
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                continue
    return None


# 平台自身单元测试的显式豁免：那些用例要把 _run 换成假的、路径指向
# tmp_path，覆盖的正是写操作的失败处理 —— 不需要护栏拦着。
_ALLOW_MUTATION = False


def _assert_may_mutate() -> None:
    """测试里禁止改动真实系统。

    平台对象一旦拿到真实 HOME，就能改写 ~/.config/systemd/user/ 下的单元、
    启停真实服务、甚至 sudo 写 /etc。隔离一旦漏掉一处，就会静默改掉用户机器
    （曾经真的覆盖过用户自己的 caddy.service）。

    护栏放在平台层而不是测试夹具里：只有平台知道哪些调用会动系统。
    只读探测（service_active / dns_query / http_ok …）不受影响。
    """
    if _ALLOW_MUTATION:
        return
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("DEVPROXY_NO_MUTATION"):
        raise RuntimeError("测试中禁止调用真实平台的写操作")


def render_systemd_unit(
    name: str,
    exec_start: list[str],
    exec_reload: list[str] | None,
    exec_stop: list[str] | None,
    env: dict[str, str],
    description: str = "",
    service_type: str | None = None,
) -> str:
    """生成 systemd user 单元。形态与现有的 caddy.service 保持一致。

    systemd 单元不做 shell 引号解析：参数里的空白会截断命令行。
    路径来自配置（用户可改），必须在这里拦住。

    service_type 默认按被拉起的程序判断：只有 caddy 会发 sd_notify，
    dnsmasq 这类程序硬套 Type=notify 会永远卡在 "start timed out"。
    """
    for label, args in (
        ("ExecStart", exec_start),
        ("ExecReload", exec_reload),
        ("ExecStop", exec_stop),
    ):
        for value in args or []:
            if any(c.isspace() for c in value):
                raise RuntimeError(f"{label} 参数不能包含空白字符: {value}")

    # Environment= 的值来自用户可改的配置（paths.caddy_env 的 JSON），
    # 与 argv 同属"外部输入"。换行会在 unit 里插入任意新行（例如伪造
    # ExecStartPre=），至少也会把 unit 写坏，因此和 argv 一样必须拦住。
    def _reject_newline(label: str, value: str) -> None:
        if "\n" in value or "\r" in value:
            raise RuntimeError(f"{label} 不能包含换行符: {value!r}")

    _reject_newline("Description", description)
    for key, value in env.items():
        _reject_newline("Environment 变量名", key)
        _reject_newline(f"Environment={key}", value)

    if service_type is None:
        program = Path(exec_start[0]).name if exec_start else ""
        service_type = "notify" if program.startswith("caddy") else "simple"
    lines = [
        "[Unit]",
        f"Description={description or f'{name} (managed by devproxy)'}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        f"Type={service_type}",
    ]
    for k, v in env.items():
        lines.append(f"Environment={k}={v}")
    lines.append(f"ExecStart={' '.join(exec_start)}")
    if exec_reload:
        lines.append(f"ExecReload={' '.join(exec_reload)}")
    if exec_stop:
        lines.append(f"ExecStop={' '.join(exec_stop)}")
    lines += [
        "TimeoutStopSec=5s",
        "LimitNOFILE=1048576",
        "Restart=on-failure",
        "RestartSec=2s",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


class LinuxElevated:
    """提权会话。构造时执行一次 `sudo -v` 完成预认证。

    `sudo -v` 是"无命令"调用：不匹配 sudoers 里的命令规范，
    于是落到默认的 `(ALL : ALL) ALL` —— 需要认证。而
    `(ALL) NOPASSWD: ALL` 只对**具体命令**免密，管不到 `sudo -v`。
    于是免密机器上 `sudo -v` 必然失败（无 TTY 时报
    "A terminal is required to authenticate"），而那台机器根本不需要密码。

    所以 `sudo -v` 失败**不能**直接判定"提权失败"：改用 `sudo -n true`
    探测免密能力 —— 它成功就说明这台机器不需要密码，
    后续特权命令一律自带 `-n`（绝不退化成会弹密码框的 `sudo <cmd>`）。
    """

    # 免密路径的标记。做成类属性：测试里用 __new__ 造出的会话
    # 没有实例属性，也必须能正常跑。
    _noninteractive = False

    def __init__(self) -> None:
        # 护栏也必须拦构造函数：直接 new 一个提权会话会真的跑 sudo -v。
        _assert_may_mutate()
        self._authenticate()

    def _authenticate(self) -> None:
        # `sudo -v` 可能要等用户输密码 —— 只有这条路径允许无超时。
        code, msg = _run(["sudo", "-v"], timeout=None)
        if code == 0:
            # 时间戳已预热，后续 `sudo <cmd>` 直接复用。
            return
        # 免密探测：`-n` 保证绝不等待输入（非交互环境会直接失败而不是挂住）。
        code_nopasswd, msg_nopasswd = _run(["sudo", "-n", "true"])
        if code_nopasswd == 0:
            # 免密可用。`sudo -v` 的预热在这台机器上不可用，
            # 后续命令必须自带 `-n` 才不会弹密码框。
            self._noninteractive = True
            return
        # 两条路都走不通 —— 错误信息要同时带上两条命令的输出，
        # 用户才能分辨"这台机器没免密"和"sudo 本身坏了"。
        raise PermissionError(
            f"提权失败：sudo -v: {msg}；sudo -n true: {msg_nopasswd}"
        )

    def run(self, argv: list[str]) -> tuple[int, str]:
        if self._noninteractive:
            return _run(["sudo", "-n", *argv])
        # 交互路径可能要等用户输入密码 —— 不设超时（其余调用一律有界）。
        return _run(["sudo", *argv], timeout=None)

    def write(self, path: Path, content: str) -> None:
        """通过系统临时目录中的文件 + 特权安装写入，避免 shell 引号问题。

        父目录不一定存在：`/etc/systemd/resolved.conf.d` 与 `/etc/dnsmasq.d`
        在干净机器上可能尚未创建（不归任何 dpkg 包所有）。先 `install -d`
        建目录，否则写入必然失败，setup 永远无法收敛。
        临时文件必须放在用户可写的系统临时目录（目标目录本身可能不可写），
        且无论成功失败都要清理。

        用 `install -m 644 -o root -g root` 而不是 `cp`：mkstemp 建出来的
        临时文件是 0600，`cp` 会把权限一并带过去，于是写进
        /usr/local/share/ca-certificates/ 与 /etc/systemd/resolved.conf.d/
        的文件变成 root:root 0600 —— 调用的普通用户自己都读不了，
        doctor 会永远报"文件读不到"。macOS 那条路径已经改成 0644，
        Linux 必须对齐。
        """
        code, msg = self.run(["install", "-d", str(path.parent)])
        if code != 0:
            raise PermissionError(f"创建目录 {path.parent} 失败: {msg}")
        fd, tmp_name = tempfile.mkstemp(prefix="devproxy-elev-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            code, msg = self.run(
                [
                    "install",
                    "-m",
                    "644",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    str(tmp),
                    str(path),
                ]
            )
            if code != 0:
                raise PermissionError(f"写入 {path} 失败: {msg}")
        finally:
            tmp.unlink(missing_ok=True)

    def read(self, path: Path) -> str:
        """读取特权文件。文件不存在抛 FileNotFoundError，其余失败抛 PermissionError。

        存在性用 `test -e` 的**返回码**判断，不解析 `cat` 的错误文案：
        GNU coreutils 的报错经 gettext 本地化，`LANG=zh_CN.UTF-8` 时
        输出是"没有那个文件或目录"，按 `"No such file"` 匹配会把缺失
        文件误抛成 PermissionError。
        """
        exists, _ = self.run(["test", "-e", str(path)])
        if exists != 0:
            raise FileNotFoundError(path)
        code, out = self.run(["cat", str(path)])
        if code != 0:
            raise PermissionError(f"读取 {path} 失败: {out}")
        return out

    def copy(self, src: Path, dest: Path) -> None:
        """复制文件（CA 装进系统信任库走这条路）。

        同样用 `install -m 644 -o root -g root`：`cp` 会把源文件的权限
        带过去，源文件若是 0600，目标就成了 root:root 0600。
        """
        code, msg = self.run(
            [
                "install",
                "-m",
                "644",
                "-o",
                "root",
                "-g",
                "root",
                str(src),
                str(dest),
            ]
        )
        if code != 0:
            raise PermissionError(f"复制 {src} → {dest} 失败：{msg}")


class LinuxPlatform:
    name = "linux"
    dns_port = 5353

    def __init__(self, user_home: Path | None = None) -> None:
        # 设计前提是"以普通用户运行，内部按需 sudo"。以 root 运行时
        # Path.home() 是 /root，unit 会装进 root 的家目录、systemctl --user
        # 操作 root 的 user manager —— 最坏会覆盖 root 家目录下同名的
        # caddy.service。必须在构造时就显式失败，而不是留到一堆
        # 令人困惑的 systemd 错误。
        if os.geteuid() == 0:
            raise RuntimeError(
                "请以普通用户运行 devproxy：需要特权的步骤由工具内部调用 sudo，"
                "以 root 运行会把服务装进 root 的家目录"
            )
        self.user_home = user_home or Path.home()
        self._elevated: LinuxElevated | None = None

    def privilege(self) -> ElevatedSession:
        _assert_may_mutate()
        if self._elevated is None:
            self._elevated = LinuxElevated()
        return self._elevated

    def run(self, argv: list[str]) -> tuple[int, str]:
        # 普通命令一律带超时；sudo 那条路径不加 —— 它可能要等用户输密码。
        return _run(argv, timeout=60.0)

    def privilege_read(self, path: Path) -> str | None:
        """非交互地读特权文件（`sudo -n`）。读不到返回 None。

        doctor 是只读命令，绝不能因为权限问题弹出密码框 —— 那既违反
        "只读体检"的承诺，也会让脚本里跑的 doctor 卡在密码提示上。
        `sudo -n` 在没有缓存凭据时立刻失败，绝不等待输入。
        """
        try:
            proc = subprocess.run(
                ["sudo", "-n", "cat", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def binary_version(self, binary: Path) -> str:
        if not binary.exists():
            return ""
        code, out = _run([str(binary), "version"])
        if code != 0:
            return ""
        return out.split()[0] if out else ""

    def service_unit_path(self, name: str) -> Path:
        return self.user_home / ".config" / "systemd" / "user" / f"{name}.service"

    def service_active(self, name: str) -> bool:
        code, out = _run(["systemctl", "--user", "is-active", name])
        return code == 0 and out.strip() == "active"

    def install_service(
        self,
        name: str,
        *,
        exec_start: list[str],
        exec_reload: list[str] | None = None,
        exec_stop: list[str] | None = None,
        env: dict[str, str],
        description: str = "",
    ) -> None:
        _assert_may_mutate()
        path = self.service_unit_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_systemd_unit(
                name, exec_start, exec_reload, exec_stop, env, description
            ),
            encoding="utf-8",
        )
        code, msg = _run(["systemctl", "--user", "daemon-reload"])
        if code != 0:
            raise RuntimeError(f"安装 {name} 后 systemctl daemon-reload 失败: {msg}")

    def enable_service(self, name: str) -> None:
        _assert_may_mutate()
        code, msg = _run(["systemctl", "--user", "enable", "--now", name])
        if code != 0:
            raise RuntimeError(f"systemctl enable --now {name} 失败: {msg}")

    def restart_service(self, name: str) -> None:
        _assert_may_mutate()
        code, msg = _run(["systemctl", "--user", "restart", name])
        if code != 0:
            raise RuntimeError(f"systemctl restart {name} 失败: {msg}")

    def tail_service_log(self, name: str) -> None:
        """流式跟随服务日志：继承终端 stdio（不捕获），Ctrl-C 自然传播。"""
        subprocess.run(["journalctl", "--user", "-u", name, "-f"])

    def enable_linger(self) -> tuple[int, str]:
        """启用 linger。

        与平台其他特权操作一致：先走 `sudo -n`（绝不弹密码框）。只有
        确实有控制终端时才回退到可能提示密码的 `sudo`；非交互环境
        直接返回可解释的错误码，绝不挂在密码提示上。
        """
        _assert_may_mutate()
        user = getpass.getuser()
        code, msg = _run(["sudo", "-n", "loginctl", "enable-linger", user])
        if code == 0:
            return code, msg
        if not sys.stdin.isatty():
            return code, f"{msg}（非交互环境，未尝试输入密码）"
        return _run(["sudo", "loginctl", "enable-linger", user], timeout=None)

    def linger_status(self) -> bool | None:
        """linger 的三态（**非协议方法**，组件用 getattr 探测）。

        - `True`：已启用，或本机根本不适用（没有 systemd / 没有 loginctl）；
        - `False`：确定未启用；
        - `None`：systemd 在，但探测失败 —— 判不了，调用方应显式处理，
          不能静默当成"已启用"。
        """
        if not SYSTEMD_RUN_DIR.is_dir():
            # 容器 / WSL / 非 systemd 宿主：linger 不适用。
            return True
        code, out = _run(
            ["loginctl", "show-user", getpass.getuser(), "-p", "Linger", "--value"]
        )
        if code == 127:
            # loginctl 不在 PATH（精简 systemd 环境）：linger 无从启用。
            return True
        if code != 0:
            # systemd 在、探测却失败（D-Bus 瞬时故障 / 权限）：判不了。
            return None
        return out.strip() == "yes"

    def linger_enabled(self) -> bool:
        """当前用户是否已启用 linger（只读探测）。

        `linger_status()` 返回"判不了"时按**未启用**处理（fail closed）：
        宁可让 plan 多出一条 `enable-linger` 动作（失败会显式报错），
        也不能静默跳过导致用户级服务在登出/重启后不再启动。
        """
        status = self.linger_status()
        return False if status is None else status

    def resolved_active(self) -> bool:
        code, out = _run(["systemctl", "is-active", "systemd-resolved"])
        if code == 0 and out.strip() == "active":
            return True
        # 部分环境没有 systemd-resolved 的 systemd 单元名，但 resolvectl
        # 能工作（例如容器）；以能否查询状态作为兜底判据。
        code, _ = _run(["resolvectl", "status"])
        return code == 0

    def dns_query(self, name: str) -> str | None:
        """优先走系统解析器（resolved），失败再直接问本机 dnsmasq。

        两条路径都必须有超时上限：`resolvectl query` 走 D-Bus，
        systemd-resolved 挂起时子进程不会自己返回，`verify()` 会被永久
        拖死。上限与 macOS 的 `DNS_QUERY_TIMEOUT` 保持一致。
        """
        code, out = _run(
            ["resolvectl", "query", name], timeout=DNS_QUERY_TIMEOUT
        )
        if code == 0:
            answer = _first_ip(out)
            if answer is not None:
                return answer
        code, out = _run(
            ["dig", "@127.0.0.1", "-p", str(self.dns_port), name, "+short"],
            timeout=DNS_QUERY_TIMEOUT,
        )
        if code == 0:
            return _first_ip(out)
        return None

    def http_ok(self, url: str) -> bool:
        # 本地回环探测必须绕过用户代理：curl 默认读 http_proxy/ALL_PROXY，
        # 公司代理配置会让发往 <dnsmasq 解析的 .test 域名> 的请求跑去公网，
        # verify() 于是假阴性。
        code, _ = _run(["curl", "--noproxy", "*", "-fsS", "--max-time", "3", url])
        return code == 0

    def ca_trusted(self, ca: Path) -> bool:
        """CA 是否**真的在系统信任库里**。

        权威判据是**指纹命中**（见 ca_in_trust_store）：活库里存在与该 CA
        字节完全相同的证书才算信任。

        只比对暂存目录（/usr/local/share/ca-certificates）里的文件是不够的：
        那个文件在 update-ca-certificates 没跑成功时也在那儿，系统却不认它。

        openssl 只作为**补充的肯定信号**：验通了说明信任链确实成立；
        它验不通可能只是上面的同主题歧义，绝不用来否定指纹命中。
        """
        # 指纹判定自己承诺"永不抛异常"（内部已把读取/解码失败转成
        # False）；这里不再包一层宽泛的 except —— 真出 bug 时应当暴露，
        # 而不是被吞掉后继续跑 openssl。
        if ca_in_trust_store(ca):
            return True
        code, _ = _run(
            ["openssl", "verify", "-CApath", str(SYSTEM_TRUST_DIR), str(ca)]
        )
        return code == 0

    def nss_db_path(self) -> Path:
        """本平台认定的 NSS 库路径（**非协议方法**）。

        组件层要拿它跟自己的 `nss_dir` 对一下：写入的库与复查的库必须是
        同一个 —— 否则"复查"这张安全网查的是另一个库，等于失效。
        """
        return nss_db_dir(self.user_home)

    def nss_entries(
        self, ca: Path, nss_dir: Path | None = None
    ) -> list[tuple[str, str]] | None:
        """NSS 库里与这张 CA **内容相同**的条目：[(昵称, 信任标志), ...]。

        按**内容**找，不按昵称找 —— NSS 按 (issuer, serial) 去重：
        同一张证书换个昵称再 `certutil -A`，会**静默无操作**（返回 0，
        但条目并不新增）。所以"这张 CA 装了没有"只能靠指纹比对。

        返回三态：
        - 列表：库读得到，已按内容筛过（可能为空）；
        - `[]`：库**确定**不存在，条目自然没有；
        - `None`：**判不了**（库被锁 / 目录不可读 / DB 损坏 / certutil 失败）
          —— 调用方不能把它当成"没有条目"。

        注意快路径：自有昵称那条命中且可信时**直接返回它一条**，
        不保证列出库里全部匹配项 —— 调用方只应把它当作"有没有被信任"的
        判据；要枚举全部匹配条目（例如为了补信任标志）时，前提本来就是
        "自有昵称不命中"，那时返回的是全量。

        `nss_dir` 允许显式指定库路径（默认本平台的 nss_db_path）：
        组件注入的库与平台默认库不一致时，复查必须查**写进去的那个**。

        这是**非协议方法**（组件层用 getattr 探测）：macOS 没有 NSS 库，
        不实现也不影响。
        """
        nss = self._resolve_nss_dir(nss_dir)
        state = _nss_db_state(nss)
        if state is False:
            return []
        if state is None:
            return None
        expected = _fingerprints_of_file(ca)
        if not expected:
            return []
        code, listing = _run(["certutil", "-L", "-d", f"sql:{nss}"])
        if code != 0:
            # `-L` 失败时无法区分"空库"与"读不了" —— 判不了，不能返回 []。
            return None
        rows = _nss_rows(listing)
        cache: dict[str, str | None] = {}

        # 快路径：自有昵称命中且已带信任标志 → 一次额外调用就够，不必全量枚举。
        for nickname, flags in rows:
            if nickname != CA_NICKNAME or not _nss_flags_trusted(flags):
                continue
            if _nss_entry_matches(nickname, expected, nss, cache):
                return [(nickname, flags)]
        return [
            (nickname, flags)
            for nickname, flags in rows
            if _nss_entry_matches(nickname, expected, nss, cache)
        ]

    def nss_stale_nickname(
        self, ca: Path, nickname: str, nss_dir: Path | None = None
    ) -> bool | None:
        """`nickname` 下有条目，且它的内容**不是**这张 CA（陈旧条目）。

        用途：工具自有昵称（`devproxy Local CA`）下若残留着**旧 CA**，
        浏览器会永久信任那个旧根 —— 若旧私钥还能拿到（备份、旧 storage），
        就等于给 HTTPS 中间人留了门。那个昵称归工具所有，可以安全删除。

        返回三态：True = 确定陈旧；False = 确定不陈旧（同名同内容，或
        条目确定不存在）；None = 判不了（库不可读、certutil 输出不是
        "找不到"、不是 PEM）。删除对象固定是工具自有昵称，不存在"误删
        用户条目"的风险；把"判不了"压成 False 会让陈旧根永久留在库里。
        """
        nss = self._resolve_nss_dir(nss_dir)
        state = _nss_db_state(nss)
        if state is False:
            return False
        if state is None:
            return None
        code, pem = _run(
            ["certutil", "-L", "-d", f"sql:{nss}", "-n", nickname, "-a"]
        )
        if code != 0:
            if _nss_missing_cert(pem):
                return False  # 这个昵称下确定没有条目
            return None  # 库被锁 / 损坏 —— 判不了
        stored = _pem_fingerprints(pem)
        expected = _fingerprints_of_file(ca)
        if not stored or not expected:
            return None  # 内容不是 PEM，无法比对
        return not (stored & expected)

    def nss_has_nickname(
        self, nickname: str, nss_dir: Path | None = None
    ) -> bool | None:
        """`nickname` 下是否有条目（不看内容）。**非协议方法**，三态。

        - `True` / `False`：确定有 / 确定没有；
        - `None`：**判不了**（库被锁、不可读、DB 损坏…）。

        为什么必须三态：`certutil -L -n` 在"条目不存在"和"库读不了"两种
        情况下**都**返回非零。把后者当"不存在"，删除失败就会被当成幂等成功
        吞掉（旧根还留在库里，工具却报已就绪）。实测库不可读时输出是
        `SEC_ERROR_BAD_DATABASE: security library: bad database.` ——
        与"找不到"完全是两种文案。
        """
        nss = self._resolve_nss_dir(nss_dir)
        state = _nss_db_state(nss)
        if state is False:
            return False  # 库确定不存在，条目自然没有
        if state is None:
            return None  # 目录/库访问不了 —— 判不了
        code, msg = _run(["certutil", "-L", "-d", f"sql:{nss}", "-n", nickname])
        if code == 0:
            return True
        if _nss_missing_cert(msg):
            return False
        return None  # 锁 / 权限 / DB 损坏 —— 判不了

    def nss_trust_flags(self, flags: str) -> str:
        """补 SSL 信任标志时用的完整三列标志字符串（**非协议方法**）。

        平台才知道标志串的列语义；组件只负责把结果喂给 `certutil -M`。
        """
        return _nss_repair_flags(flags)

    def nss_trusted(self, ca: Path) -> bool:
        """这张 CA 是否在 NSS 库里**且带 SSL 信任标志**。只读、非特权。

        两个要点：
        - 判据是**证书内容**，不是昵称。Caddy 重新生成内部 CA 后旧条目仍在，
          只按 nickname 判定会误报"已信任"；反过来，老工具（devp）装的
          "Caddy Local Authority"与本工具的昵称不同，只按自己的昵称查
          又会永远查不到 —— 工具因此永远认为 CA 没装上。
        - 光有证书**不算**信任：标志是 ",,"（无信任）时 Chrome 照样不认。
        """
        # `nss_entries` 已把读取/解析失败转成返回值（None/[]），这里不再
        # 包宽泛的 except —— 真出 bug 时应当暴露，而不是被吞成"未信任"。
        entries = self.nss_entries(ca) or []
        return any(_nss_flags_trusted(flags) for _nickname, flags in entries)

    def _resolve_nss_dir(self, nss_dir: Path | None) -> Path:
        """显式库路径优先；否则用本平台的默认库。"""
        return (
            Path(nss_dir).expanduser()
            if nss_dir is not None
            else self.nss_db_path()
        )

    def dns_route_installed(self, backend: str, suffix: str) -> bool:
        """Linux 的两个后端都是固定路径，不看域名后缀（macOS 才按后缀分文件）。"""
        if backend == "resolved":
            return Path("/etc/systemd/resolved.conf.d/devproxy.conf").exists()
        if backend == "system":
            return Path("/etc/dnsmasq.d/devproxy.conf").exists()
        return False
