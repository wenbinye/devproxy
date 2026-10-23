"""CA 根证书信任组件。

Linux 需要两处：系统信任库（curl / Go / Java 等）与 NSS 库（Chrome/Chromium）。
两者是**独立的两步**：只装系统信任库，Chrome 照样不认；只装 NSS 库，curl 又不认。
macOS 只需要系统钥匙串（由 Plan 3 的 macOS 平台实现提供）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.platform.base import (
    CA_NICKNAME,
    NSS_DIR,
    SYSTEM_CA_DEST,
    SYSTEM_KEYCHAIN,
    Action,
    Platform,
    Status,
)

CA_NAME = CA_NICKNAME


def _nss_missing_output(text: str) -> bool:
    """`certutil` 的输出是否在说"这个条目不存在"。

    平台没有 `nss_has_nickname` 可用时的兜底判据（真机实测文案：
    `certutil: could not find certificate named "x": …`）。
    """
    lowered = text.lower()
    return "could not find" in lowered or "not found" in lowered


# macOS 在"非图形界面会话"里拒绝修改系统信任设置时的系统原文。
# 不同版本措辞略有出入，按子串族匹配（真机 macOS 26 命中的是第一条）。
_MACOS_GUI_DENIED = (
    "no user interaction was possible",
    "user interaction is not allowed",
    "errauthorizationinteractionnotallowed",
)


def _macos_needs_gui(message: str) -> bool:
    low = (message or "").lower()
    return any(marker in low for marker in _MACOS_GUI_DENIED)


class CertComponent:
    name = "cert"

    def __init__(
        self,
        platform: Platform,
        paths: Paths,
        config: Config,
        system_dest: Path | None = None,
        nss_dir: Path | None = None,
    ):
        """system_dest / nss_dir 允许注入 —— 测试用 tmp_path，绝不写真实系统。"""
        self.platform = platform
        self.paths = paths
        self.config = config
        self.system_dest = Path(system_dest) if system_dest else SYSTEM_CA_DEST
        # **必须在这里展开 `~`**：NSS_DIR 是 `Path("~/.pki/nssdb")`，
        # pathlib 不认波浪号。不展开的话 `(nss_dir / "cert9.db").exists()`
        # 查的是「当前工作目录下的 ~/.pki/nssdb」—— 恒为 False，
        # 生产构造（不注入 nss_dir 的三处调用）下所有 NSS 判定都会瞎掉。
        # 只在这里展开一次，组件内部任何消费方拿到的都是绝对路径。
        self.nss_dir = (Path(nss_dir) if nss_dir else NSS_DIR).expanduser()

    @property
    def ca_path(self) -> Path:
        return self.config.storage_path / "pki" / "authorities" / "local" / "root.crt"

    # -- 平台判定（永不抛异常） ---------------------------------------------

    @staticmethod
    def _trusted(checker, ca: Path) -> bool:
        """check() 永不抛异常的前提：平台判定失败一律当作"没信任"。"""
        try:
            return bool(checker(ca))
        except Exception:  # noqa: BLE001 —— 判定异常不能带崩 doctor
            return False

    def _system_trusted(self) -> bool:
        checker = getattr(self.platform, "ca_trusted", None)
        if checker is None:
            return False
        return self._trusted(checker, self.ca_path)

    def _nss_trusted(self, ca: Path | None = None) -> bool:
        # 平台协议可能有变（Plan 3 的 macOS 实现）；属性访问本身也要在
        # try 之外兜住的话，缺方法会让 check() 抛异常 —— doctor 就炸了。
        checker = getattr(self.platform, "nss_trusted", None)
        if checker is None:
            return False
        return self._trusted(checker, ca or self.ca_path)

    def _nss_entries(
        self, ca: Path | None = None, nss_dir: Path | None = None
    ) -> list[tuple[str, str]]:
        """库里与这张 CA 内容匹配的条目：[(昵称, 信任标志), ...]。

        `nss_entries` 是**非协议方法**（macOS 没有 NSS 库）——
        取不到就退化成"只按自己的昵称处理"。
        """
        getter = getattr(self.platform, "nss_entries", None)
        if getter is None:
            return []
        target = ca or self.ca_path
        try:
            if nss_dir is None:
                raw = getter(target)
            else:
                try:
                    raw = getter(target, nss_dir=nss_dir)
                except TypeError:
                    # 平台的 nss_entries 不接受 nss_dir（旧实现 / 别的平台）：
                    # 退回它自己的库 —— 一致性已在上游用 nss_db_path 把过关。
                    raw = getter(target)
        except Exception:  # noqa: BLE001 —— 取不到就当没有
            return []
        entries: list[tuple[str, str]] = []
        for item in raw or []:
            try:
                nickname, flags = item
            except (TypeError, ValueError):
                continue
            entries.append((str(nickname), str(flags)))
        return entries

    def _nss_db_path(self) -> Path | None:
        """平台认定的 NSS 库路径（非协议方法）；没有这个方法返回 None。"""
        getter = getattr(self.platform, "nss_db_path", None)
        if getter is None:
            return None
        try:
            return Path(getter()).expanduser()
        except Exception:  # noqa: BLE001 —— 探测失败就当平台不提供
            return None

    def _nss_stale(self, ca: Path, nss_dir: Path) -> bool:
        """工具自有昵称下是否残留着**别的** CA（陈旧条目）。

        取不到这个能力就返回 False —— 宁可漏删也不能误删。
        """
        checker = getattr(self.platform, "nss_stale_nickname", None)
        if checker is None:
            return False
        try:
            return bool(checker(ca, CA_NAME, nss_dir=nss_dir))
        except TypeError:
            try:
                return bool(checker(ca, CA_NAME))
            except Exception:  # noqa: BLE001
                return False
        except Exception:  # noqa: BLE001
            return False

    def _nss_flags_for_repair(self, flags: str) -> str:
        """补 SSL 信任标志用的完整标志串（保留 S/MIME 与 JAR 两列）。"""
        helper = getattr(self.platform, "nss_trust_flags", None)
        if helper is None:
            return "C,,"
        try:
            return str(helper(flags))
        except Exception:  # noqa: BLE001
            return "C,,"

    def _nss_has_nickname(self, nss: Path, nickname: str) -> bool | None:
        """昵称下是否有条目；判不了返回 None（平台不支持这个探测也是 None）。

        **必须原样透传三态**：平台的 `None` 是"库读不了/被锁"，与
        "确定没有"（False）完全不是一回事 —— 把 None 压成 bool 会让
        "删除失败"重新变成"幂等成功"。
        """
        getter = getattr(self.platform, "nss_has_nickname", None)
        if getter is None:
            return None
        try:
            try:
                result = getter(nickname, nss_dir=nss)
            except TypeError:
                result = getter(nickname)
        except Exception:  # noqa: BLE001 —— 探测失败按"判不了"处理
            return None
        if result is None:
            return None
        return bool(result)

    def _stale_own_entry(self) -> bool:
        """工具自有昵称下是否残留着**别的** CA。判定失败一律当作没有。"""
        if not self.ca_path.exists():
            return False
        return self._nss_stale(self.ca_path, self.nss_dir)

    def fully_trusted(self) -> bool:
        """CA 是否已被系统信任库与 NSS 库**同时**信任。

        CA 还不存在时返回 False —— 没什么可信任的。
        """
        if not self.ca_path.exists():
            return False
        return self._system_trusted() and self._nss_trusted()

    @property
    def _is_macos(self) -> bool:
        return self.platform.name == "mac"

    def _system_label(self) -> str:
        """系统信任点的名字（报错与提示里用）。"""
        return "系统钥匙串" if self._is_macos else "系统信任库"

    # -- check ---------------------------------------------------------

    def check(self) -> Status:
        # CA 根证书是 Caddy 的内部 CA：只有在有站点用 tls internal 时才生成。
        # 首次部署时它还不存在 —— 这是"还没到那一步"，不是缺陷。
        if not self.ca_path.exists():
            return Status(
                ok=True, detail="尚无 CA 根证书（将在首条路由注册时由 Caddy 生成）"
            )
        missing = []
        if not self._system_trusted():
            missing.append(self._system_label())
        if not self._nss_trusted():
            missing.append("NSS 库")
        if missing:
            return Status(
                ok=False,
                detail=f"CA 根证书未被信任（缺少：{'、'.join(missing)}）",
                hint="devproxy trust",
            )
        if self._is_macos:
            return Status(ok=True, detail="CA 根证书已信任（系统钥匙串）")
        # 陈旧根属于"卫生问题"而不是"未就绪"：HTTPS 一切正常，所以 ok 仍是 True；
        # 但也不能谎称完全没问题 —— 警示**前置**到 detail 开头（doctor 表格会截断
        # 长字符串的末尾），同时放进 hint，让"修复"列直接给出动作。
        if self._stale_own_entry():
            return Status(
                ok=True,
                detail="NSS 库中工具昵称下残留陈旧 CA 根；CA 根证书已信任（系统信任库 + NSS 库）",
                hint="devproxy trust（清理陈旧 CA 根）",
            )
        return Status(ok=True, detail="CA 根证书已信任（系统信任库 + NSS 库）")

    # -- plan ----------------------------------------------------------

    def plan(self) -> list[Action]:
        """CA 还没生成时无事可做；否则按缺哪补哪。

        非特权的那步排在前面：用户若拒绝 sudo，NSS（Chrome 信任）仍能装上。
        """
        if not self.ca_path.exists():
            return []
        if self._is_macos:
            # macOS 没有 NSS 库：CA 只要进系统钥匙串（Safari/Chrome 都认它）。
            if self._system_trusted():
                return []
            return [
                Action(
                    description="把 CA 根证书加入系统钥匙串（Safari/Chrome 都会信任）",
                    kind="run",
                    command=["install-macos-ca", str(self.ca_path)],
                    privileged=True,
                )
            ]
        actions: list[Action] = []
        nss_trusted = self._nss_trusted()
        if not nss_trusted:
            actions.append(
                Action(
                    description="安装 CA 根证书到 NSS 库（Chrome/Chromium）",
                    kind="run",
                    command=["install-nss-ca", str(self.ca_path), str(self.nss_dir)],
                    # NSS 库在当前用户家目录，不需要 root
                    privileged=False,
                )
            )
        elif self._stale_own_entry():
            # CA 已信任、但工具昵称下残留旧 CA 根。这条必须由 plan() 产出 ——
            # 此场景下原本一切都"已就绪"、apply() 根本不会被调用，
            # 只写在安装路径里的清理代码永远执行不到。
            actions.append(
                Action(
                    description="清理 NSS 库中工具昵称下的陈旧 CA 根证书",
                    kind="run",
                    command=[
                        "remove-stale-nss-ca",
                        str(self.ca_path),
                        str(self.nss_dir),
                    ],
                    # 自有昵称归工具所有；NSS 库在用户家目录，不需要 root
                    privileged=False,
                )
            )
        if not self._system_trusted():
            actions.append(
                Action(
                    description="安装 CA 根证书到系统信任库并 update-ca-certificates",
                    kind="run",
                    command=[
                        "install-system-ca",
                        str(self.ca_path),
                        str(self.system_dest),
                    ],
                    privileged=True,
                )
            )
        return actions

    # -- apply ---------------------------------------------------------

    def apply(self, actions: list[Action]) -> None:
        """两个信任库互相独立：任一步失败，另一步仍要跑完。

        失败不在中途抛出，最后汇总成一条错误 —— 否则先跑的 NSS 步一旦
        失败（如缺 certutil），系统信任库就永远装不上，反之亦然。
        """
        failures: list[tuple[Action, Exception]] = []
        for action in actions:
            try:
                self._apply_one(action)
            except Exception as e:  # noqa: BLE001 —— 先收集，最后统一报
                failures.append((action, e))
        if failures:
            detail = "；".join(f"{a.description}（{e}）" for a, e in failures)
            message = f"{len(failures)} 个步骤失败：{detail}"
            succeeded = len(actions) - len(failures)
            if succeeded:
                message += f"；其余 {succeeded} 个步骤已执行"
            # 提权失败要保留类型：CLI 据此给退出码 4（需要提权），而不是 1
            if all(isinstance(e, PermissionError) for _, e in failures):
                raise PermissionError(message)
            raise RuntimeError(message)

    def _apply_one(self, action: Action) -> None:
        if action.command[:1] == ["install-macos-ca"]:
            # macOS：把 CA 加进系统钥匙串并标成信任根（-d 加到系统域，
            # -r trustRoot 让它对所有应用生效，包括 Safari/Chrome）。
            src = Path(action.command[1])
            code, msg = self.platform.privilege().run(
                [
                    "security",
                    "add-trusted-cert",
                    "-d",
                    "-r",
                    "trustRoot",
                    "-k",
                    SYSTEM_KEYCHAIN,
                    str(src),
                ]
            )
            if code != 0:
                if _macos_needs_gui(msg):
                    # 真机实测（macOS 26 + SSH）：证书其实**已经**进了系统钥匙串，
                    # 卡住的是 SecTrustSettingsSetTrustSettings —— 它要弹一个
                    # 系统授权框，而 SSH 会话没有图形上下文，于是直接拒绝。
                    # 这既不是权限不够（sudo 是成功的），也不是命令写错，
                    # 用户除了"去那台机器的图形界面点一下"别无他法。
                    # 分行写：Rich 会把超长的一整段折成难读的多行，
                    # 而"该运行什么命令"必须是完整、可复制的一行。
                    raise PermissionError(
                        "macOS 不允许在非图形界面会话里修改系统信任设置"
                        "（SSH 下弹不出授权框）。\n"
                        "请在那台 Mac 的「终端」窗口里运行：devproxy trust\n"
                        f"系统原文：{msg}"
                    )
                raise RuntimeError(f"安装 CA 到系统钥匙串失败: {msg}")
            return
        if action.command[:1] == ["install-system-ca"]:
            elevated = self.platform.privilege()
            src, dest = Path(action.command[1]), Path(action.command[2])
            code, msg = elevated.run(["install", "-d", str(dest.parent)])
            if code != 0:
                raise RuntimeError(f"创建信任库目录失败: {msg}")
            elevated.copy(src, dest)
            code, msg = elevated.run(["update-ca-certificates"])
            if code != 0:
                # 不检查返回码的话，CA 只是躺在暂存目录里，系统并不认它
                raise RuntimeError(
                    f"update-ca-certificates 失败（CA 未进入系统信任库）: {msg}"
                )
            return
        if action.command[:1] == ["remove-stale-nss-ca"]:
            src = Path(action.command[1])
            nss = Path(action.command[2]).expanduser()
            if shutil.which("certutil") is None:
                raise RuntimeError(
                    "缺少 certutil（Chrome 信任需 NSS 库）：sudo apt install libnss3-tools"
                )
            self._require_same_nss_db(nss)
            self._drop_stale_own_entry(src, nss)
            return
        if action.command[:1] == ["install-nss-ca"]:
            src = Path(action.command[1])
            nss = Path(action.command[2]).expanduser()
            # 探测只能看二进制在不在：certutil -V 需要 -n，真实机器上永远非零
            if shutil.which("certutil") is None:
                raise RuntimeError(
                    "缺少 certutil（Chrome 信任需 NSS 库）：sudo apt install libnss3-tools"
                )
            self._require_same_nss_db(nss)
            # 平台判定已信任 → 一个字节都不改，只做一次**定向**清理：
            # 自有昵称下若残留旧 CA，那个根会被浏览器永久信任，旧私钥若
            # 可恢复就是 HTTPS 中间人后门。那个昵称归工具所有，可以安全删。
            if self._nss_trusted(src):
                self._drop_stale_own_entry(src, nss)
                return
            nss.mkdir(parents=True, exist_ok=True)
            # 库还不存在时先建一个空库，否则 certutil -A 会失败
            if not (nss / "cert9.db").exists():
                code, msg = self.platform.run(
                    ["certutil", "-N", "-d", f"sql:{nss}", "--empty-password"]
                )
                if code != 0:
                    raise RuntimeError(f"创建 NSS 库失败: {msg}")

            # 库里与这张 CA 内容相同的条目（可能挂在我们不认识的昵称下）。
            # 判据是**内容**：NSS 按 (issuer, serial) 去重，换个昵称再装
            # 是装不进去的。
            entries = self._nss_entries(src, nss_dir=nss)
            if any(self._flags_trusted(flags) for _n, flags in entries):
                # 按内容查到的条目已经带 SSL 信任 → 同样什么都不改
                # （平台没有 nss_trusted 的内容判定时，靠这一步兜住）。
                self._drop_stale_own_entry(src, nss)
                return
            if entries:
                # 证书已在库里但没有 SSL 信任 → 只补标志，**不要 -A**：
                # 对已存在的证书，-A -t "C,," 会把这行改写成 "C,,"，
                # 把 S/MIME / JAR 两列原有的信任静默抹掉。
                # 顺手清掉自有昵称下的陈旧条目：补完标志这条 CA 就算信任了，
                # 再没人会回头看那个旧根。
                self._drop_stale_own_entry(src, nss)
                self._repair_flags(src, nss, entries)
                return
            # 库里没有这张 CA：先清掉自有昵称下的陈旧条目，再安装。
            self._drop_stale_own_entry(src, nss, unconditional=True)
            code, msg = self.platform.run(
                [
                    "certutil",
                    "-d",
                    f"sql:{nss}",
                    "-A",
                    "-n",
                    CA_NAME,
                    "-t",
                    "C,,",
                    "-i",
                    str(src),
                ]
            )
            if code != 0:
                raise RuntimeError(f"安装 NSS 证书失败: {msg}")
            # 复查：**-A 返回 0 不代表真的装上了**（对已存在的证书它是静默的）。
            if self._nss_trusted(src):
                return
            # 还是不行 → 库里有这张 CA 但缺 SSL 信任标志，补标志后再复查
            self._repair_flags(
                src, nss, self._nss_entries(src, nss_dir=nss), initial_output=msg
            )
            return
        raise RuntimeError(f"未知动作: {action.command}")

    # -- NSS 安装的辅助 -------------------------------------------------

    def _require_same_nss_db(self, nss: Path) -> None:
        """写入的库与平台复查的库必须是同一个。

        复查正是用来兜住 `certutil -A` 说谎的安全网；查错库 = 安全网失效
        （而且会静默地"安装成功"）。两者不一致时**显式报错**，绝不静默。
        """
        platform_db = self._nss_db_path()
        if platform_db is None:
            return  # 平台不提供这个能力（macOS / 旧实现）：无从校验
        if platform_db.resolve() != nss.resolve():
            raise RuntimeError(
                f"NSS 库路径不一致：组件写入 {nss}，平台复查 {platform_db}"
                "（复查会查错库，拒绝继续）"
            )

    @staticmethod
    def _flags_trusted(flags: str) -> bool:
        """标志串的 SSL 列是否受信任（"C" 开头）。"""
        return bool(flags) and flags[0] == "C"

    def _drop_stale_own_entry(
        self, ca: Path, nss: Path, *, unconditional: bool = False
    ) -> None:
        """删掉工具自有昵称下的**陈旧** CA 条目。

        `unconditional=True` 用在"库里没有当前 CA"的安装路径上：
        这时自有昵称下即便有条目也只可能是**别的** CA。

        `certutil -D` 的返回码**必须**检查：陈旧根会被浏览器永久信任，
        删除失败若被当成成功，旧根就留在库里而工具报"已就绪"。
        只有"条目确实不存在"（并发下已被别处删掉 / 本来就没有）才算幂等成功。

        两种真实的"假成功"都被挡住了：
        - 库被锁 / 不可读 → `-D` 非零，`nss_has_nickname` 返回 **None**（判不了）
          → 必须往上报，不能当"已经没有这条"；
        - 库**目录只读**时 `certutil -D` 返回 **0 却什么都没删**（实测）
          → 删除之后必须复验条目真的没了。
        """
        if not unconditional and not self._nss_stale(ca, nss):
            return
        code, msg = self.platform.run(
            ["certutil", "-d", f"sql:{nss}", "-D", "-n", CA_NAME]
        )
        if code != 0:
            has_probe = getattr(self.platform, "nss_has_nickname", None) is not None
            present = self._nss_has_nickname(nss, CA_NAME)
            if present is False:
                return  # 条目确实不存在 —— 目标状态已达成，幂等
            if not has_probe and _nss_missing_output(msg):
                # 平台不提供探测能力，只能看 certutil 的输出
                return
            if present is None and has_probe:
                raise RuntimeError(
                    "删除 NSS 库中的陈旧 CA 条目失败，且无法确认条目是否仍在"
                    f"（库可能被锁或不可读）: {msg}"
                )
            raise RuntimeError(f"删除 NSS 库中的陈旧 CA 条目失败: {msg}")
        # 返回 0 不代表真的删了：NSS 库目录只读时 certutil -D 静默无效（实测）。
        if self._nss_stale(ca, nss):
            raise RuntimeError(
                "删除 NSS 库中的陈旧 CA 条目未生效（certutil 返回成功但条目仍在，"
                f"库目录可能不可写）: {msg}"
            )

    def _repair_flags(
        self,
        ca: Path,
        nss: Path,
        entries: list[tuple[str, str]],
        initial_output: str = "",
    ) -> None:
        """给库里那些条目补 SSL 信任标志（保留 S/MIME / JAR 两列）。"""
        last_output = initial_output
        for nickname, flags in entries:
            code, msg = self.platform.run(
                [
                    "certutil",
                    "-M",
                    "-d",
                    f"sql:{nss}",
                    "-n",
                    nickname,
                    "-t",
                    self._nss_flags_for_repair(flags),
                ]
            )
            last_output = msg
            if code != 0:
                last_output = f"({code}) {msg}"
            if self._nss_trusted(ca):
                return
        # 复查仍否 —— 错误信息要带上**最后一次 -M** 的输出与库里的实际条目，
        # 只留 -A 的输出在成功返回 0 时是空的，等于什么都没说。
        detail = last_output or "certutil 无输出"
        raise RuntimeError(
            "安装 NSS 证书失败：NSS 库里找不到这张 CA 的 SSL 信任标志"
            f"（库中条目: {entries or '无'}；certutil: {detail}）"
        )

    # -- verify --------------------------------------------------------

    def _probe_url(self) -> str:
        """真实 HTTPS 探测的 URL：优先首条已注册路由，其次 localhost。

        用 localhost 去验一个只有 codeup.test 的机器会因“无 localhost 站点”
        而握手失败 —— 那验的是站点匹配，不是证书信任链。
        """
        try:
            data = json.loads(self.paths.routes_json.read_text(encoding="utf-8"))
            for route in data.get("routes") or []:
                domain = route.get("domain") if isinstance(route, dict) else None
                if isinstance(domain, str) and domain:
                    return f"https://{domain}"
        except (OSError, ValueError, AttributeError):
            pass
        return "https://localhost"

    def verify(self) -> Status:
        if not self.ca_path.exists():
            return Status(ok=True, detail="尚无 CA 根证书（跳过验证）")
        if not (self._system_trusted() and self._nss_trusted()):
            return Status(ok=False, detail="CA 仍未被完全信任", hint="devproxy trust")
        # openssl 验证信任链只是第一步；再发一次真实 HTTPS 请求确认端到端可用。
        url = self._probe_url()
        if not self.platform.http_ok(url):
            return Status(
                ok=False,
                detail=f"CA 已进信任库，但 {url} 仍握手失败",
                hint=(
                    "devproxy trust；launchctl print system/com.devproxy.caddy"
                    if self._is_macos
                    else "devproxy trust；systemctl --user status caddy"
                ),
            )
        return Status(ok=True, detail=f"CA 已信任，{url} 握手成功")
