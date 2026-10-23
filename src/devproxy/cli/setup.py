"""setup 命令：只做顺序编排，具体动作拆给下面的小函数。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from rich.markup import escape
import typer

from devproxy.cli import app
from devproxy.cli._migrate import MigrateRequest
from devproxy.cli._migrate import _do_rollback
from devproxy.cli._migrate import _migrate_legacy
from devproxy.cli._shared import EXIT_DECLINED
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_PRECONDITION
from devproxy.cli._shared import EXIT_SUDO
from devproxy.cli._shared import EXIT_USAGE
from devproxy.cli._shared import UP_EXAMPLE
from devproxy.core.config import ConfigError
from devproxy.core.config import validate_config
from devproxy.lifecycle.migrate import detect_legacy
from devproxy.lifecycle.migrate import resolve_storage

from devproxy.cli import _shared as s


def _handle_rollback(raw: str | None) -> bool:
    """处理 --rollback；返回是否已进入回滚分支。

    回滚只依赖备份目录与平台，**不能先加载 config.toml** —— 配置损坏
    恰恰是最需要回滚的时刻，恢复路径不能依赖它要恢复的系统。

    空字符串不是“未提供”：脚本里变量未设置时 `--rollback "$DIR"` 会传
    空串，落到完整部署会写 /etc、重启服务，而用户以为自己在下发只读
    的回滚指令。按参数错误拒绝。
    """
    if raw is None:
        return False
    if not raw.strip():
        s.fail("--rollback 需要一个备份目录路径（收到空字符串）", EXIT_USAGE)
    _do_rollback(Path(raw))
    return True


def _apply_cli_overrides(
    config, dns_backend: str | None, domain_suffix: str | None
) -> tuple[object, str]:
    """把 CLI 覆盖折进配置并校验；返回 (配置, 生效的 DNS 后端)。"""
    backend = s._dns_backend(dns_backend, config)
    if domain_suffix is not None:
        config = replace(config, domain_suffix=domain_suffix)
        try:
            # --domain-suffix 会直通 Caddyfile 与 /etc 下的 DNS 配置，
            # 与 config.toml 同等对待：必须过校验，不能绕过
            validate_config(config)
        except ConfigError as e:
            s.fail(str(e), EXIT_USAGE)
    return config, backend


def _maybe_migrate(platform, paths, config, backend: str, yes: bool):
    """检测并迁移遗留环境；迁移必须先于组件流程。

    组件会读迁移留下的状态（单元环境变量、storage）。backend 先折进
    config，迁移与后续组件拿到的就是同一个后端。
    """
    config = replace(config, dns_backend=backend)
    state = detect_legacy(platform, paths)
    if state is None:
        return config
    config, storage_note = resolve_storage(config, state)
    _migrate_legacy(
        MigrateRequest(
            platform=platform,
            paths=paths,
            config=config,
            state=state,
            yes=yes,
            notes=[storage_note] if storage_note else [],
        )
    )
    return config


def _confirm_or_exit(plan, yes: bool) -> None:
    """提权披露、非交互拒绝与交互确认。"""
    if plan.needs_sudo:
        s.console.print(
            f"[yellow]以下步骤需要 sudo（共 {len(plan.needs_sudo)} 项）：[/yellow]"
        )
        for a in plan.needs_sudo:
            s.console.print(f"  · {escape(a.description)}")
        if not yes and not s._interactive():
            s.err_console.print("非交互环境且未指定 --yes，拒绝执行需要提权的步骤。")
            raise typer.Exit(EXIT_SUDO)

    # 交互模式下的最后确认（§6.1）；--yes 或非交互环境直接跳过。
    if not yes and s._interactive():
        prompt = (
            f"以上 {len(plan.needs_sudo)} 项需要 sudo，继续？"
            if plan.needs_sudo
            else "即将执行以上变更，继续？"
        )
        if not typer.confirm(prompt, default=True):
            s.console.print("已取消：什么都没改（未执行任何变更）。")
            raise typer.Exit(EXIT_DECLINED)


def _report_noop(plan, paths, config) -> None:
    """没有变更时：全就绪则提示信任状态，否则如实报不健康。"""
    unhealthy = [r for r in plan.reports if not r.status.ok]
    if not unhealthy:
        s.console.print("所有组件已就绪，无需变更。")
        # 只有 CA 已生成且尚未完全信任时才提示 trust；--no-trust 的
        # 场景下 CA 若已存在也会如实提示（用户本次显式跳过了信任）
        s._print_trust_hint_if_needed(paths, config)
        raise typer.Exit(EXIT_OK)
    # 没有可执行的动作、但组件本身不健康 —— 不能谎报"已就绪"
    s.print_noop_unhealthy(unhealthy)
    raise typer.Exit(EXIT_PRECONDITION)


@app.command()
def setup(
    dns_backend: str = typer.Option(None, "--dns-backend", help="auto|resolved|system"),
    domain_suffix: str = typer.Option(None, "--domain-suffix"),
    no_trust: bool = typer.Option(False, "--no-trust", help="跳过证书信任"),
    yes: bool = typer.Option(False, "--yes", help="非交互模式，不询问"),
    rollback_dir: str = typer.Option(None, "--rollback", help="从指定备份目录回滚"),
) -> None:
    """部署本地开发环境（Caddy + dnsmasq + DNS 路由 + 证书信任 + 自启）。"""
    if _handle_rollback(rollback_dir):
        return

    paths, config, _, _ = s.build_context()

    config, backend = _apply_cli_overrides(config, dns_backend, domain_suffix)
    platform = s._platform()
    s.ensure_backend_supported(backend, platform)
    # 参数都校验通过后才产生写副作用：部署分支需要目录与 import 目标，
    # 只读命令不再依赖 build_context 建文件（I1）
    paths.ensure_files()
    config = _maybe_migrate(platform, paths, config, backend, yes)

    components = s._components(
        paths, config, backend, platform, include_cert=not no_trust
    )
    # 记录 apply 阶段被 apply_plan 吞掉的 PermissionError，退出码才能区分
    # “需要提权”（4）与普通失败（1）
    components, elevation_errors = s.track_elevation_errors(components)

    plan = s.build_plan(components)
    s.render_plan(plan)
    # 整机作用域的披露：DNS= 与 FallbackDNS= 改的是**整台机器**的解析路径，
    # 表格里放不下也看不清 —— 单独打一遍，用户点确认之前就该看到。
    s.print_scope_notes(plan)

    if plan.is_noop:
        _report_noop(plan, paths, config)

    _confirm_or_exit(plan, yes)

    results = s.apply_plan(plan)
    if not s.print_apply_results(results):
        s.fail_for_apply_errors(elevation_errors)
    s.console.print(f"完成。试试: {UP_EXAMPLE}")
    s._print_trust_hint_if_needed(paths, config)
