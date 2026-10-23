"""uninstall 编排：停进程 → 移除服务 → 清理 DNS → 卸载 CA → 删状态文件。

顺序是硬约束：残留子进程必须先收掉（它们还占着端口），服务单元次之，
DNS 路由再次之，CA 只按指纹精确匹配卸载，最后才删工具自己的状态文件。
`--purge` 的目录删除放在最后，避免备份先没了、dnsmasq 配置无法还原。

与 setup 一致：这里只做编排，具体系统调用全部经 Platform 协议。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import ssl
from dataclasses import dataclass, field
from pathlib import Path

from devproxy.components.certs import CertComponent
from devproxy.components.dns import STATE_FILE as DNS_STATE_FILE
from devproxy.components.dnsmasq import DNSMASQ_CONF
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.core.paths import atomic_write_text
from devproxy.gateway.routes import load_table
from devproxy.gateway.runner import kill_pid_group
from devproxy.platform.base import (
    RESOLVED_CONF,
    RESOLVER_DIR,
    SYSTEM_CA_DEST,
    SYSTEM_DNSMASQ_CONF,
    SYSTEM_KEYCHAIN,
    Action,
    Platform,
    file_fingerprints,
    nss_db_dir,
    pem_fingerprints,
)

# 与 CertComponent.ca_path 保持一致：Caddy 的默认 CA 落点。
CA_RELATIVE_PATH = Path("pki") / "authorities" / "local" / "root.crt"

# dnsmasq 配置备份的命名：dnsmasq.conf.<UTC 时间戳>[.<序号>].bak
_DNSMASQ_BACKUP_RE = re.compile(
    r"^dnsmasq\.conf\.(\d{8}T\d{6}Z)(?:\.(\d+))?\.bak$"
)

# `certutil -D` 明确表示条目不存在时的文案（与平台层的判据一致）。
_NSS_NOT_FOUND = (
    "could not find certificate named",
    "could not find cert:",
    "pr_file_not_found_error",
)

# `security delete-certificate` 找不到条目时的文案族。
_MACOS_NOT_FOUND = (
    "could not find",
    "could not be found",
    "not found",
    "unable to find",
    "no such",
)


@dataclass(frozen=True)
class UninstallTargets:
    """可被测试替换的系统路径。生产环境全部用默认值。"""

    resolved_conf: Path = RESOLVED_CONF
    system_dnsmasq_conf: Path = SYSTEM_DNSMASQ_CONF
    resolver_dir: Path = RESOLVER_DIR
    system_ca_dest: Path = SYSTEM_CA_DEST
    system_keychain: str = SYSTEM_KEYCHAIN
    nss_dir: Path = field(default_factory=nss_db_dir)
    dnsmasq_conf: Path = field(default_factory=lambda: DNSMASQ_CONF.expanduser())


@dataclass
class StepResult:
    """一条动作的执行结果。"""

    description: str
    ok: bool
    detail: str = ""


@dataclass
class UninstallPlan:
    """一次卸载的完整计划。

    `actions` 按执行顺序排列；`kept` 是要在输出里明确告诉用户"没动"的
    东西；`notes` 是带条件的说明（迁移留档、跳过原因等）。
    """

    platform: Platform | None = None
    targets: UninstallTargets = field(default_factory=UninstallTargets)
    actions: list[Action] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    migration_backup: Path | None = None
    # CA 因为「迁移前就存在」的启发式被保留 —— CLI 据此决定要不要问用户
    # 「要卸载它吗」。用结构化字段而不是在 notes 里找子串：文案一改就失效。
    ca_kept: bool = False

    @property
    def needs_sudo(self) -> list[Action]:
        return [a for a in self.actions if a.privileged]

    @property
    def is_noop(self) -> bool:
        return not self.actions


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def build_plan(
    platform: Platform,
    paths: Paths,
    config: Config,
    *,
    purge: bool,
    remove_ca: bool,
    dry_run: bool = False,
    targets: UninstallTargets | None = None,
) -> UninstallPlan:
    """计算卸载步骤。

    `dry_run=True` 时只做**文件系统**判断：不调用 `service_active` /
    `ca_trusted` / `nss_entries` 这类会起子进程的探测，保证预览没有
    任何副作用、也不需要 sudo。真实执行时才做精确探测。
    """
    plan = UninstallPlan(platform=platform, targets=targets or UninstallTargets())
    probe = not dry_run

    _plan_route_processes(plan, paths)
    _plan_services(plan, platform, probe=probe)
    _plan_dns(plan, platform, config)
    _plan_dnsmasq_restore(plan, paths)
    _plan_ca(plan, platform, paths, config, remove_ca=remove_ca, probe=probe)
    _plan_state_files(plan, paths)
    _plan_purge(plan, paths, config, purge=purge)
    _plan_kept(plan, paths, config, purge=purge)
    _plan_migration_hint(plan, paths, purge=purge)
    return plan


def _plan_route_processes(plan: UninstallPlan, paths: Paths) -> None:
    """读取 routes.json，为每条 mode=up 的记录计划一次带身份校验的清理。

    `proc_start` 缺失时**不**退化成按裸 pid 杀 —— pid 会被复用，宁可
    留下一个残留进程，也不能误杀无关进程组。
    """
    for route in load_table(paths.routes_json).routes:
        if route.mode != "up" or route.pid is None:
            continue
        if not route.proc_start:
            plan.notes.append(
                f"路由 {route.name} 缺少 proc_start，无法核实进程身份，"
                "已跳过清理（不会按裸 pid 杀进程）"
            )
            continue
        plan.actions.append(
            Action(
                description=(
                    f"终止路由 {route.name} 的残留子进程组"
                    f"（pid {route.pid}，带启动时刻校验）"
                ),
                kind="run",
                command=["kill-pid-group", str(route.pid), route.proc_start],
            )
        )


def _plan_services(plan: UninstallPlan, platform: Platform, *, probe: bool) -> None:
    """caddy、dnsmasq：单元存在或服务仍在跑时才计划移除。"""
    for name in ("caddy", "dnsmasq"):
        path = platform.service_unit_path(name)
        installed = path.exists() or path.is_symlink()
        active = False
        if probe:
            try:
                active = platform.service_active(name)
            except Exception:  # noqa: BLE001 —— 探测失败按"没在跑"处理，仍可清理单元
                active = False
        if installed or active:
            plan.actions.append(
                Action(
                    description=f"停止并移除 {name} 服务单元",
                    kind="run",
                    command=["remove-service", name],
                )
            )


def _plan_dns(plan: UninstallPlan, platform: Platform, config: Config) -> None:
    """删除 DNS 路由文件并让解析器重新加载。"""
    targets = plan.targets
    if platform.name == "mac":
        path = targets.resolver_dir / config.domain_suffix.lstrip(".")
        if path.exists() or path.is_symlink():
            plan.actions.append(
                Action(
                    description=f"删除 macOS 每域解析器文件 {path}",
                    kind="delete",
                    path=path,
                    privileged=True,
                )
            )
        return
    for path, service in (
        (targets.resolved_conf, "systemd-resolved"),
        (targets.system_dnsmasq_conf, "dnsmasq"),
    ):
        if not (path.exists() or path.is_symlink()):
            continue
        plan.actions.append(
            Action(
                description=f"删除 DNS 路由文件 {path}",
                kind="delete",
                path=path,
                privileged=True,
            )
        )
        plan.actions.append(
            Action(
                description=f"重启 {service} 使 DNS 路由失效",
                kind="run",
                command=["systemctl", "restart", service],
                privileged=True,
            )
        )


def _plan_dnsmasq_restore(plan: UninstallPlan, paths: Paths) -> None:
    """dnsmasq.conf 是增量合并的，不能删；有备份就还原到接管前的版本。"""
    conf = plan.targets.dnsmasq_conf
    backup = latest_dnsmasq_backup(paths.backup_dir)
    if backup is None:
        if conf.exists():
            plan.kept.append(f"{conf}（未找到改动前的备份，原样保留）")
        return
    try:
        backup_text = backup.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise RuntimeError(f"读取 dnsmasq 配置备份失败，拒绝继续: {backup}: {e}") from e
    current = None
    if conf.exists():
        try:
            current = conf.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = None
    if current == backup_text:
        plan.kept.append(f"{conf}（已还原为备份内容，未再改动）")
        return
    plan.actions.append(
        Action(
            description=f"从备份 {backup.name} 还原 dnsmasq 配置 {conf}",
            kind="write",
            path=conf,
            content=backup_text,
        )
    )


def _plan_ca(
    plan: UninstallPlan,
    platform: Platform,
    paths: Paths,
    config: Config,
    *,
    remove_ca: bool,
    probe: bool,
) -> None:
    """CA 卸载的两条规则：指纹精确匹配 + 迁移来的 CA 默认保留。"""
    ca = CertComponent(platform, paths, config).ca_path
    if not ca.exists():
        return
    archived = paths.caddy_storage.exists()
    if archived and not remove_ca:
        plan.ca_kept = True
        plan.notes.append(
            "检测到 storage 留档，CA 可能是你原有的，已保留；"
            "要卸请显式加 --remove-ca（或交互时回答「是」）。"
        )
        plan.kept.append(f"CA 根证书：{ca}（迁移前已存在，站点可能仍在使用）")
        return
    if archived and remove_ca:
        plan.notes.append("已选择强制卸载迁移前就存在的 CA。")

    if platform.name == "mac":
        trusted = _probe_ca_trusted(platform, ca) if probe else True
        if trusted is not False:
            plan.actions.append(
                Action(
                    description="按指纹从系统钥匙串移除 CA 根证书",
                    kind="run",
                    command=[
                        "remove-macos-ca",
                        str(ca),
                        plan.targets.system_keychain,
                    ],
                    privileged=True,
                )
            )
        return

    _plan_nss_ca(plan, platform, ca, probe=probe)
    _plan_system_ca(plan, platform, ca, probe=probe)


def _plan_nss_ca(
    plan: UninstallPlan, platform: Platform, ca: Path, *, probe: bool
) -> None:
    """NSS 库按证书内容（指纹）找条目，不按昵称。"""
    if not hasattr(platform, "nss_entries"):
        return
    entries: list[tuple[str, str]] | None = None
    if probe:
        try:
            entries = platform.nss_entries(ca, plan.targets.nss_dir)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 —— 判不了就退回"尝试清理"，apply 会复查
            entries = None
    if probe and entries == []:
        return
    if entries:
        for nickname in sorted({name for name, _flags in entries}):
            plan.actions.append(
                Action(
                    description=f"按指纹从 NSS 库移除 CA 条目「{nickname}」",
                    kind="run",
                    command=[
                        "remove-nss-ca",
                        str(ca),
                        str(plan.targets.nss_dir),
                        nickname,
                    ],
                )
            )
        return
    # 无法判定（库被锁/不可读）或 dry-run：给出一次尝试性清理，apply 复查。
    plan.actions.append(
        Action(
            description="按指纹从 NSS 库移除匹配的 CA 条目（Chrome/Chromium）",
            kind="run",
            command=["remove-nss-ca", str(ca), str(plan.targets.nss_dir)],
        )
    )


def _plan_system_ca(
    plan: UninstallPlan, platform: Platform, ca: Path, *, probe: bool
) -> None:
    """Linux 系统信任库：删暂存文件 + update-ca-certificates。"""
    dest = plan.targets.system_ca_dest
    if probe:
        trusted = _probe_ca_trusted(platform, ca)
        # 判不了（None）时保守地尝试；dest 还在也值得清理一次陈旧文件。
        needed = trusted is not False or dest.exists()
    else:
        # dry-run 不探测信任库，只按文件是否存在预估。
        needed = dest.exists() or dest.is_symlink()
    if not needed:
        return
    plan.actions.append(
        Action(
            description=f"按指纹从系统信任库移除 CA 并刷新（{dest}）",
            kind="run",
            command=["remove-linux-system-ca", str(ca), str(dest)],
            privileged=True,
        )
    )


def _plan_state_files(plan: UninstallPlan, paths: Paths) -> None:
    """删除工具生成的状态文件；用户的 sites.caddy / config.toml 不在这里。"""
    for path in (
        paths.caddyfile,
        paths.routes_json,
        paths.routes_caddy,
        paths.lock_path,
        paths.state_dir / DNS_STATE_FILE,
        paths.caddy_storage,
        paths.caddy_env,
    ):
        if path.exists() or path.is_symlink():
            plan.actions.append(
                Action(description=f"删除工具状态文件 {path}", kind="delete", path=path)
            )
    if paths.log_dir.exists() or paths.log_dir.is_symlink():
        plan.actions.append(
            Action(
                description=f"删除工具日志目录 {paths.log_dir}",
                kind="delete",
                path=paths.log_dir,
            )
        )


def _plan_purge(
    plan: UninstallPlan, paths: Paths, config: Config, *, purge: bool
) -> None:
    """只有在选择「连存储一起删」时才删：Caddy 存储、备份目录、下载的 caddy 二进制。"""
    if not purge:
        return
    storage = config.storage_path
    if storage.exists() or storage.is_symlink():
        plan.actions.append(
            Action(
                description=(
                    f"删除 Caddy 存储 {storage}（CA、证书与站点数据一并删除）"
                ),
                kind="delete",
                path=storage,
            )
        )
    if paths.backup_dir.exists() or paths.backup_dir.is_symlink():
        plan.actions.append(
            Action(
                description=(
                    f"删除备份目录 {paths.backup_dir}"
                    "（含迁移备份，删除后无法回滚）"
                ),
                kind="delete",
                path=paths.backup_dir,
            )
        )
    binary = config.caddy_bin_path
    if binary.exists() or binary.is_symlink():
        plan.actions.append(
            Action(
                description=f"删除下载的 caddy 二进制 {binary}",
                kind="delete",
                path=binary,
            )
        )


def _plan_kept(
    plan: UninstallPlan, paths: Paths, config: Config, *, purge: bool
) -> None:
    """明确列出默认不动的东西 —— 卸载不能"顺手"清掉用户资产。"""
    plan.kept.extend(
        [
            f"{paths.sites_caddy}（你的个人站点配置）",
            f"{paths.config_toml}（你的配置文件）",
            "loginctl enable-linger（整机级设置，其他用户服务可能依赖）",
        ]
    )
    if purge:
        return
    plan.kept.extend(
        [
            f"{config.storage_path}（Caddy 存储：CA 与证书；你自己的 Caddy 可能在用）",
            f"{config.caddy_bin_path}（可能是你自己安装的 caddy）",
            f"{paths.backup_dir}（回滚依据）",
        ]
    )


def _plan_migration_hint(plan: UninstallPlan, paths: Paths, *, purge: bool) -> None:
    """迁移过的机器：告诉用户原始文件在哪、怎么回滚。"""
    backup = latest_migration_backup(paths.backup_dir)
    if backup is None:
        return
    if purge:
        plan.notes.append(
            f"--purge 会删除迁移备份 {backup}，之后无法再用 "
            f"devproxy setup --rollback {backup} 还原。"
        )
        return
    plan.migration_backup = backup


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def apply_plan(plan: UninstallPlan) -> list[StepResult]:
    """逐条执行并返回结果；单条失败不阻断其余步骤。

    CLI 据结果决定退出码：有任何一个 ✗ 就是运行失败（1）。
    """
    platform = plan.platform
    if platform is None:
        raise RuntimeError("UninstallPlan 缺少 platform，无法执行")
    results: list[StepResult] = []
    for action in plan.actions:
        try:
            detail = _apply_one(platform, plan, action)
        except Exception as e:  # noqa: BLE001 —— 单步失败继续清理其余部分
            results.append(StepResult(action.description, False, str(e)))
        else:
            results.append(StepResult(action.description, True, detail))
    return results


def _apply_one(platform: Platform, plan: UninstallPlan, action: Action) -> str:
    if action.kind == "delete":
        return _delete_path(platform, action.path, action.privileged)
    if action.kind == "write":
        if action.path is None:
            raise RuntimeError("write 动作缺少 path")
        if action.privileged:
            platform.privilege().write(action.path, action.content)
        else:
            atomic_write_text(action.path, action.content)
        return ""
    if action.kind != "run":
        raise RuntimeError(f"未知动作类型: {action.kind}")
    command = list(action.command)
    if not command:
        raise RuntimeError("run 动作缺少 command")
    head = command[0]
    if head == "kill-pid-group":
        killed = kill_pid_group(int(command[1]), command[2] or None)
        return "已终止残留进程组" if killed else "进程已不在或身份不匹配，跳过"
    if head == "remove-service":
        platform.remove_service(command[1])
        return ""
    if head == "remove-nss-ca":
        nickname = command[3] if len(command) > 3 else None
        return _remove_nss_ca(platform, Path(command[1]), Path(command[2]), nickname)
    if head == "remove-linux-system-ca":
        return _remove_linux_system_ca(platform, Path(command[1]), Path(command[2]))
    if head == "remove-macos-ca":
        return _remove_macos_ca(platform, Path(command[1]), command[2])
    code, msg = (
        platform.privilege().run(command)
        if action.privileged
        else platform.run(command)
    )
    if code != 0:
        raise RuntimeError(msg or f"命令失败（返回码 {code}）")
    return ""


def _delete_path(
    platform: Platform, path: Path | None, privileged: bool
) -> str:
    """删除文件或工具自有目录。

    特权删除统一用 `rm -rf`：调用方只会把系统路径（DNS drop-in）放进来。
    非特权删除先区分符号链接，避免 `rmtree` 跟随链接删到别处。
    """
    if path is None:
        raise RuntimeError("delete 动作缺少 path")
    if privileged:
        code, msg = platform.privilege().run(["rm", "-rf", "--", str(path)])
        if code != 0:
            raise RuntimeError(f"删除 {path} 失败: {msg}")
        return ""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return ""
    if path.is_dir():
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
    return ""


def _remove_nss_ca(
    platform: Platform, ca: Path, nss: Path, nickname: str | None
) -> str:
    """按证书内容找到匹配条目并删除；昵称只作为定位手段，不作判据。"""
    probe = getattr(platform, "nss_entries", None)
    if probe is None:
        return "平台不提供 NSS 探测，跳过"
    entries = probe(ca, nss)
    if entries is None:
        raise RuntimeError(
            "无法判定 NSS 库中的 CA 条目（库可能被锁或不可读），拒绝静默跳过"
        )
    # 只删内容与当前 CA 完全相同的条目；plan 里带上的昵称只是缩小范围，
    # 执行前仍以指纹复查结果为准。
    targets = [
        (name, flags)
        for name, flags in entries
        if nickname is None or name == nickname
    ]
    if not targets:
        return "NSS 库中无匹配条目"
    removed: list[str] = []
    for name, _flags in targets:
        code, msg = platform.run(
            ["certutil", "-d", f"sql:{nss}", "-D", "-n", name]
        )
        if code != 0:
            remaining = probe(ca, nss)
            if remaining is not None and all(n != name for n, _f in remaining):
                continue
            if any(marker in (msg or "").lower() for marker in _NSS_NOT_FOUND):
                continue
            raise RuntimeError(f"删除 NSS 条目 {name} 失败: {msg}")
        removed.append(name)
    remaining = probe(ca, nss)
    if remaining is None:
        raise RuntimeError(
            "无法确认 NSS 库中的 CA 条目是否已删除（库可能被锁或不可读）"
        )
    if remaining:
        names = [name for name, _flags in remaining]
        raise RuntimeError(f"NSS 库里仍残留匹配 CA 的条目: {names}")
    return f"已移除 {len(removed)} 个 NSS 条目" if removed else "NSS 库中无匹配条目"


def _remove_linux_system_ca(platform: Platform, ca: Path, dest: Path) -> str:
    """只在 dest 里的证书与当前 CA 指纹完全一致时删除。"""
    expected = file_fingerprints(ca)
    if not expected:
        raise RuntimeError(f"无法读取 CA 证书（不是有效 PEM）: {ca}")
    text: str | None
    try:
        text = dest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = None
    if text is None:
        try:
            text = platform.privilege_read(dest)
        except Exception:  # noqa: BLE001 —— 读不到就当文件不存在
            text = None
    if text is None:
        return f"{dest} 不存在或不可读，跳过（未找到工具安装的 CA 文件）"
    if not (expected & pem_fingerprints(text)):
        return f"{dest} 中的证书与当前 CA 指纹不匹配，未删除"
    code, msg = platform.privilege().run(["rm", "-f", "--", str(dest)])
    if code != 0:
        raise RuntimeError(f"删除系统信任库文件 {dest} 失败: {msg}")
    code, msg = platform.privilege().run(["update-ca-certificates"])
    if code != 0:
        raise RuntimeError(f"update-ca-certificates 失败（CA 可能仍在信任库）: {msg}")
    return "已从系统信任库移除 CA 并刷新信任链"


def _remove_macos_ca(platform: Platform, ca: Path, keychain: str) -> str:
    """按 SHA-1 指纹删除系统钥匙串里的证书，不按昵称。"""
    try:
        text = ca.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise RuntimeError(f"读取 CA 证书失败: {ca}: {e}") from e
    try:
        der = ssl.PEM_cert_to_DER_cert(text)
    except ValueError as e:
        raise RuntimeError(f"CA 证书不是有效的 PEM: {ca}: {e}") from e
    fingerprint = hashlib.sha1(der).hexdigest().upper()
    code, msg = platform.privilege().run(
        ["security", "delete-certificate", "-Z", fingerprint, "-t", keychain]
    )
    if code == 0:
        return "已从系统钥匙串移除 CA"
    lowered = (msg or "").lower()
    if any(marker in lowered for marker in _MACOS_NOT_FOUND):
        return "系统钥匙串中未找到该 CA（已幂等跳过）"
    raise RuntimeError(f"从系统钥匙串删除 CA 失败: {msg}")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _probe_ca_trusted(platform: Platform, ca: Path) -> bool | None:
    """信任判定失败时返回 None（判不了），绝不谎报成"未信任"。"""
    try:
        return bool(platform.ca_trusted(ca))
    except Exception:  # noqa: BLE001 —— 探测失败不能带崩卸载计划
        return None


def latest_dnsmasq_backup(backup_dir: Path) -> Path | None:
    """备份目录里最新的一份 dnsmasq 配置备份。"""
    try:
        candidates = list(backup_dir.iterdir())
    except OSError:
        return None
    found: list[tuple[tuple[str, int], Path]] = []
    for path in candidates:
        match = _DNSMASQ_BACKUP_RE.match(path.name)
        if match is None or not path.is_file():
            continue
        key = (match.group(1), int(match.group(2) or 0))
        found.append((key, path))
    if not found:
        return None
    return max(found, key=lambda item: item[0])[1]


def latest_migration_backup(backup_dir: Path) -> Path | None:
    """最近的迁移备份目录（带 manifest.json 的子目录）。"""
    try:
        candidates = [
            path
            for path in backup_dir.iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        ]
    except OSError:
        return None
    return max(candidates, key=lambda path: path.name, default=None)
