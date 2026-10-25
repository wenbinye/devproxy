"""sync 的编排：比对现状 → 生成计划 → 执行。

分工：

- `gateway.project`：文件发现、解析、校验（纯函数，无副作用）
- `gateway.sync`（本模块）：计划（纯函数）+ 执行（有副作用）
- `cli.sync`：只做展示与退出码

**绝不自动重启**：路由表里已有同名路由、但声明变了（命令或端口）时，
只报告冲突，不杀进程、不重启 —— 换端口/换命令等于换了个服务，
该不该重启由人决定（工具给出的修复命令是 `devproxy down <name>` 再 sync）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from devproxy.core.logs import rotate_at_start
from devproxy.core.config import Config
from devproxy.core.paths import Paths
from devproxy.gateway.names import domain_for
from devproxy.gateway.project import MODE_EXPOSE
from devproxy.gateway.project import MODE_UP
from devproxy.gateway.project import ProjectFile
from devproxy.gateway.project import ProjectRoute
from devproxy.gateway.routes import Route
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.runner import kill_pid_group
from devproxy.gateway.runner import reachable
from devproxy.gateway.store import RouteStore
from devproxy.gateway.store import RouteStoreError

# 后台工作进程：它负责「启动应用 + 陪着应用 + 应用退出时注销路由」。
# 放在 cli 包里是因为它复用 cli._context.build_context（配置/storage
# 留档的解析只有一份实现）。这里只用模块路径字符串引用它 —— 不 import，
# 免得网关层反过来依赖 CLI 层。
WORKER_MODULE = "devproxy.cli._worker"
PAYLOAD_ENV = "DEVPROXY_UP_PAYLOAD"

# 工作进程注册路由前要经历「启动 → 就绪」，sync 在这之后额外留一点余量；
# 真正的超时由工作进程自己按 ready_timeout 兜底（它超时会退出并报错）。
_WAIT_MARGIN = 30.0


@dataclass
class SyncPlan:
    """现状与声明的差异。"""

    create_up: list[ProjectRoute] = field(default_factory=list)
    create_expose: list[ProjectRoute] = field(default_factory=list)
    skip: list[tuple[ProjectRoute, Route]] = field(default_factory=list)
    conflict: list[tuple[ProjectRoute, Route, str]] = field(default_factory=list)
    prune: list[Route] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.create_up or self.create_expose or self.prune)


@dataclass
class SyncOutcome:
    """一条路由的最终结果。"""

    name: str
    status: str  # created | skipped | conflict | failed | pruned
    detail: str = ""
    domain: str | None = None
    port: int | None = None
    log_path: Path | None = None
    hint: str = ""


def route_log_path(paths: Paths, name: str) -> Path:
    """后台应用的日志文件路径（stdout/stderr 都落在这里）。"""
    return paths.log_dir / "routes" / f"{name}.log"


def build_sync_plan(
    project: ProjectFile, existing: list[Route], prune: bool = False
) -> SyncPlan:
    """把声明与现状比对成计划。纯函数 —— 不碰文件系统。"""
    plan = SyncPlan()
    by_name = {r.name: r for r in existing}
    for declared in project.routes:
        current = by_name.get(declared.name)
        if current is None:
            if declared.mode == MODE_UP:
                plan.create_up.append(declared)
            else:
                plan.create_expose.append(declared)
            continue
        reason = conflict_reason(current, declared)
        if reason is None:
            plan.skip.append((declared, current))
        else:
            plan.conflict.append((declared, current, reason))
    if prune:
        # 只清理「本文件声明过、现在不再声明」的路由。
        # source 为 None 的路由（手工 up/expose 出来的）永远不碰。
        declared_names = {r.name for r in project.routes}
        plan.prune = [
            r
            for r in existing
            if r.source == str(project.path) and r.name not in declared_names
        ]
    return plan


def conflict_reason(existing: Route, declared: ProjectRoute) -> str | None:
    """现状与声明不一致的原因；一致时返回 None。"""
    if existing.mode != declared.mode:
        return f"模式不同（现状 {existing.mode}，期望 {declared.mode}）"
    if declared.port is not None and existing.port != declared.port:
        return f"端口不同（现状 {existing.port}，期望 {declared.port}）"
    if declared.mode == MODE_UP:
        current_cmd = list(existing.command or [])
        if current_cmd != list(declared.command or []):
            return f"启动命令不同（现状 {current_cmd}，期望 {declared.command}）"
    return None


def execute_sync_plan(
    project: ProjectFile,
    plan: SyncPlan,
    *,
    store: RouteStore,
    config: Config,
    paths: Paths,
) -> list[SyncOutcome]:
    """执行计划。返回逐条结果（顺序与声明一致，prune 的排在最后）。"""
    outcomes: dict[str, SyncOutcome] = {}

    def record(outcome: SyncOutcome) -> None:
        outcomes[outcome.name] = outcome

    # 1) up 模式：先把后台工作进程都拉起来（并行启动），再逐个等就绪。
    workers: list[tuple[ProjectRoute, subprocess.Popen, Path]] = []
    for declared in plan.create_up:
        log_path = route_log_path(paths, declared.name)
        try:
            proc = _spawn_worker(project, declared, log_path)
        except OSError as e:
            record(
                SyncOutcome(declared.name, "failed", f"无法启动后台进程：{e}")
            )
            continue
        workers.append((declared, proc, log_path))

    # 2) expose 模式：直接注册路由（不起进程）。
    for declared in plan.create_expose:
        try:
            route = register_expose(store, config, declared, str(project.path))
        except (RouteStoreError, RouteTableError) as e:
            record(SyncOutcome(declared.name, "failed", str(e)))
            continue
        detail = f"上游 127.0.0.1:{route.port}"
        if not reachable(route.port):
            detail += "（上游当前无响应，路由已注册）"
        record(
            SyncOutcome(
                declared.name,
                "created",
                detail,
                domain=route.domain,
                port=route.port,
            )
        )

    # 3) 等 up 模式的就绪。
    for declared, proc, log_path in workers:
        timeout = (declared.ready_timeout or config.ready_timeout) + _WAIT_MARGIN
        ok, reason = _wait_ready(store, declared.name, proc, timeout)
        if ok:
            route = store.get(declared.name)
            if route is None:
                # 就绪后立刻退出 —— 工作进程已经把路由注销了
                record(
                    SyncOutcome(
                        declared.name,
                        "failed",
                        "应用启动后立即退出（路由已被注销）",
                        log_path=log_path,
                    )
                )
                continue
            record(
                SyncOutcome(
                    declared.name,
                    "created",
                    f"端口 {route.port}",
                    domain=route.domain,
                    port=route.port,
                    log_path=log_path,
                )
            )
        else:
            tail = _log_tail(log_path)
            detail = f"{reason}：{tail}" if tail else reason
            record(
                SyncOutcome(declared.name, "failed", detail, log_path=log_path)
            )

    # 4) 跳过与冲突（只报告，不动手）。
    for declared, current in plan.skip:
        record(
            SyncOutcome(
                declared.name,
                "skipped",
                f"已是最最新（端口 {current.port}）",
                domain=current.domain,
                port=current.port,
            )
        )
    for declared, current, reason in plan.conflict:
        record(
            SyncOutcome(
                declared.name,
                "conflict",
                f"{reason}；现状：{_describe(current)}",
                domain=current.domain,
                port=current.port,
                hint=f"devproxy down {declared.name} 后重新运行 devproxy sync",
            )
        )

    # 5) prune：清理本文件里已删掉的声明。
    if plan.prune:
        try:
            prune_routes(store, plan.prune)
        except (RouteStoreError, RouteTableError) as e:
            for route in plan.prune:
                record(SyncOutcome(route.name, "failed", str(e)))
        else:
            for route in plan.prune:
                record(
                    SyncOutcome(
                        route.name,
                        "pruned",
                        "本文件已不再声明该路由",
                        domain=route.domain,
                    )
                )

    # 输出顺序：与声明一致（prune 掉的排在最后 —— 它们已不在声明里）
    ordered: list[SyncOutcome] = []
    for declared in project.routes:
        outcome = outcomes.get(declared.name)
        if outcome is not None:
            ordered.append(outcome)
    for route in plan.prune:
        outcome = outcomes.get(route.name)
        if outcome is not None:
            ordered.append(outcome)
    return ordered


def _describe(route: Route) -> str:
    """一句话描述路由现状（冲突报告用）。"""
    parts = [f"模式 {route.mode}", f"端口 {route.port}"]
    if route.command:
        parts.append(f"命令 {route.command}")
    if route.pid:
        parts.append(f"pid {route.pid}")
    return "，".join(parts)


def register_expose(
    store: RouteStore, config: Config, declared: ProjectRoute, source: str
) -> Route:
    """注册一条 expose 路由（上游端口来自声明）。"""
    port = int(declared.port or 0)
    route = Route(
        name=declared.name,
        domain=domain_for(declared.name, config.domain_suffix),
        port=port,
        mode=MODE_EXPOSE,
        upstream=f"127.0.0.1:{port}",
        source=source,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    store.mutate(lambda t: t.upsert(route))
    return route


def prune_routes(store: RouteStore, routes: list[Route]) -> None:
    """移除给定的路由，并把它们的子进程一并收掉（与 `devproxy down` 同语义）。"""
    names = [r.name for r in routes]

    def apply(table) -> None:
        for name in names:
            table.remove(name)

    store.mutate(apply)
    for route in routes:
        if route.pid:
            # 带上注册时记录的进程身份 —— pid 可能已被复用，身份不符不杀
            kill_pid_group(route.pid, route.proc_start)


def _spawn_worker(
    project: ProjectFile, declared: ProjectRoute, log_path: Path
) -> subprocess.Popen:
    """启动后台工作进程：它去跑应用并陪着应用。

    独立会话（start_new_session）—— sync 自己退出后它继续陪着应用；
    工作目录是**项目文件所在目录**（不是用户当前目录）。
    """
    payload = {
        "name": declared.name,
        "command": list(declared.command or []),
        "port": declared.port,
        "ready_timeout": declared.ready_timeout,
        "cwd": str(project.project_dir),
        "source": str(project.path),
        "log_path": str(log_path),
    }
    env = dict(os.environ)
    env[PAYLOAD_ENV] = json.dumps(payload)
    env["PYTHONPATH"] = _pythonpath()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 上一轮留下的日志挪成 .1：反复重启不会把日志堆成一条
    rotate_at_start(log_path)
    # stdout/stderr 交给 worker 自己写进日志文件（见 cli/_worker.py 的
    # _LogProxy）—— 那样 worker 的消息和应用的输出走同一个写入端，
    # 文件被轮转时不会有一半消息留在旧 inode 里。
    return subprocess.Popen(
        [sys.executable, "-m", WORKER_MODULE],
        env=env,
        cwd=str(project.project_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _pythonpath() -> str:
    """把本包的父目录放进 PYTHONPATH —— 后台进程必须能 import 到 devproxy。

    正常安装（pip/uv 安装到 venv）时这步是多余的；但从源码树直接跑
    （开发、测试）时 sys.executable 未必能找到这个包。
    """
    import devproxy

    root = str(Path(devproxy.__file__).resolve().parent.parent)
    existing = os.environ.get("PYTHONPATH", "")
    return f"{root}{os.pathsep}{existing}" if existing else root


def _wait_ready(
    store: RouteStore, name: str, proc: subprocess.Popen, timeout: float
) -> tuple[bool, str]:
    """等工作进程把路由注册上来。

    路由出现在表里 == 应用已就绪（工作进程是「就绪之后才注册」的）。
    工作进程自己先退出 = 启动失败（就绪超时、命令不存在……）。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            route = store.get(name)
        except RouteTableError:
            route = None
        if route is not None:
            return True, ""
        rc = proc.poll()
        if rc is not None:
            return False, f"后台进程已退出（退出码 {rc}）"
        if time.monotonic() >= deadline:
            return False, f"等待路由注册超时（{timeout:.0f}s）"
        time.sleep(0.15)


def _log_tail(path: Path, lines: int = 3) -> str:
    """取日志末尾几行 —— 后台进程失败时用它把原因带出来。"""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = [line.strip() for line in content.splitlines() if line.strip()]
    return " / ".join(tail[-lines:])
