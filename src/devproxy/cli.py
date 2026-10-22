"""devproxy 命令行入口。"""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from typer.core import TyperGroup

from devproxy.components.caddy import CaddyComponent
from devproxy.components.certs import CertComponent
from devproxy.components.dns import DnsComponent
from devproxy.components.dnsmasq import DNSMASQ_CONF, DnsmasqComponent
from devproxy.config import (
    DNS_BACKENDS,
    Config,
    ConfigError,
    load_config,
    validate_config,
)
from devproxy.doctor import build_report, report_is_healthy
from devproxy.migrate import (
    LegacyState,
    MigrationError,
    default_dns_route,
    detect_legacy,
    load_legacy_storage,
    migrate,
    resolve_storage,
    rollback as do_rollback,
)
from devproxy.paths import Paths, default_paths
from devproxy.proxy.caddy import CaddyAdmin, CaddyConfig
from devproxy.proxy.names import InvalidNameError, domain_for, validate_name
from devproxy.proxy.routes import Route, RouteTableError, load_table, render_caddyfile
from devproxy.proxy.runner import (
    PreconditionError,
    UpError,
    UpRunner,
    kill_pid_group,
    reachable,
)
from devproxy.proxy.store import RouteStore, RouteStoreError
from devproxy.setup import apply_plan, build_plan

console = Console()
err_console = Console(stderr=True)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_PRECONDITION = 3
EXIT_SUDO = 4

TRUST_HINT = (
    "提示：首次注册路由（devproxy up / expose）会生成 CA，之后请运行 devproxy trust "
    "让 curl/Chrome 信任。"
)

# setup 结束时的示例命令。
# 必须用 sh -c 把 $PORT 推迟到子进程里展开：python3 -m http.server 不读 PORT
# 环境变量（它只认命令行上的端口参数），直接写 `python3 -m http.server` 会一直
# 绑在 8000 上 —— 用户照抄示例就会等满就绪超时才看到失败。
UP_EXAMPLE = "devproxy up demo -- sh -c 'python3 -m http.server $PORT'"


def fail(msg: str, code: int = EXIT_FAIL) -> None:
    """以统一形态输出错误并退出（Rich 渲染的一行错误，而非 traceback）。"""
    # escape：错误消息里可能带 [a-z0-9-] 这类字符集或用户输入，
    # 不转义会被 Rich 当成标记吞掉
    err_console.print(f"devproxy: {escape(msg)}")
    raise typer.Exit(code)


class DevProxyGroup(TyperGroup):
    """把 '--' 之后的参数原样切给子命令。

    Typer 默认会用自己的解析器处理子命令的所有参数，导致子命令的
    --port 之类被 devproxy 抢走。这里在解析前先切分，尾部参数以
    "trailing" 原样放进 ctx.obj，子命令自行取用。
    """

    def parse_args(self, ctx, args):
        has_sep = "--" in args
        if has_sep:
            i = args.index("--")
            head, tail = args[:i], args[i + 1:]
        else:
            head, tail = args, []
        data = ctx.ensure_object(dict)
        data["trailing"] = tail
        data["has_sep"] = has_sep
        return super().parse_args(ctx, head)


app = typer.Typer(
    cls=DevProxyGroup,
    help="本地开发环境一键部署与域名网关",
    no_args_is_help=True,
)


def _resolve_storage(config: Config, paths: Paths) -> Config:
    """沿用迁移留档的 storage，并把分歧说出来。

    留档（state/caddy-storage.json）是迁移时的决定：换了 storage 位置，
    Caddy 会重新生成 CA，浏览器信任与已有证书全部失效。所以它优先于
    config.toml —— 但**绝不能静默**：用户改了 config.toml 却被无声忽略，
    是最难查的一类问题。退出路径一并给出：删掉留档文件即可恢复配置。
    """
    storage = load_legacy_storage(paths)
    if storage is None:
        return config
    if storage != config.storage_path:
        err_console.print(
            f"devproxy: 沿用迁移留档的 storage 路径 {storage}"
            f"（config.toml 里是 {config.storage_path}）：留档优先，"
            f"config.toml 的 storage 当前不生效。"
            f"删除 {paths.caddy_storage} 即可恢复 config.toml 的设置。"
        )
    return replace(config, storage=str(storage))


def build_context(
    paths: Paths | None = None, config: Config | None = None
) -> tuple[Paths, Config, RouteStore, CaddyAdmin]:
    """组装依赖。测试可传入自定义 paths/config。"""
    paths = paths or default_paths()
    config = config or load_config(paths.config_toml)
    paths.ensure_files()
    # 迁移时沿用了遗留 Caddyfile 的 storage（CA 还在那儿）—— 别换回去，
    # 换了 Caddy 会重新生成 CA，浏览器信任与已有证书全部失效。
    config = _resolve_storage(config, paths)
    caddy = CaddyConfig(
        caddy_bin=config.caddy_bin_path,
        caddyfile=paths.caddyfile,
        admin_addr=config.admin_addr,
    )
    store = RouteStore(paths, config, caddy)
    admin = CaddyAdmin(base=config.admin_base)
    return paths, config, store, admin


def _require_caddyfile(paths: Paths) -> None:
    """up/expose 的前置条件：主 Caddyfile 必须已由 setup 生成。

    否则 RouteStore.mutate → CaddyConfig.reload() 会以“reload 失败”退出 1，
    把“还没 setup”误导成运行故障。按 §9.1 属于前置条件不满足（退出码 3）。
    """
    if not paths.caddyfile.exists():
        fail(
            f"主 Caddyfile 不存在: {paths.caddyfile}；请先运行 devproxy setup",
            EXIT_PRECONDITION,
        )


@app.command(context_settings={"ignore_unknown_options": True})
def up(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="路由名（\\[a-z0-9-]，单标签）"),
    port: int = typer.Option(None, "--port", help="显式端口（默认自动分配）"),
    ready_timeout: int = typer.Option(None, "--ready-timeout", help="就绪超时秒数"),
) -> None:
    """分配端口、启动命令并注册 https://<name>.test。"""
    data = ctx.ensure_object(dict)
    if not data.get("has_sep"):
        err_console.print("缺少 '--'：devproxy up <name> [选项] -- CMD...")
        raise typer.Exit(EXIT_FAIL)
    cmd = list(data.get("trailing") or [])
    if not cmd:
        err_console.print("缺少启动命令：-- 之后需要 CMD...")
        raise typer.Exit(EXIT_FAIL)

    paths, config, store, admin = build_context()
    _require_caddyfile(paths)
    runner = UpRunner(store, config, admin)

    def on_ready(domain: str, allocated: int) -> None:
        console.print(f"✅ https://{domain}  (端口 {allocated})")

    try:
        rc = runner.run(
            name,
            cmd,
            port=port,
            ready_timeout=ready_timeout,
            on_ready=on_ready,
        )
    except PreconditionError as e:
        # Caddy 未运行是前置条件不满足，不是运行失败 —— 退出码 3（§9.1）
        fail(str(e), EXIT_PRECONDITION)
    except (UpError, RouteStoreError, RouteTableError, InvalidNameError) as e:
        fail(str(e))
    raise typer.Exit(rc)


@app.command()
def expose(
    name: str = typer.Argument(..., help="路由名"),
    port: int = typer.Option(..., "--port", help="已在上游监听的端口"),
) -> None:
    """给已在运行的服务开一个域名。注册后立即返回，路由常驻。"""
    try:
        validate_name(name)
    except InvalidNameError as e:
        fail(str(e))
    if not (1 <= port <= 65535):
        fail(f"非法端口 {port}：必须在 1..65535 之间")

    paths, config, store, admin = build_context()
    _require_caddyfile(paths)
    # expose 注册的是常驻活路由，reload 必须真的发生 —— Caddy 未运行属于
    # 前置条件不满足（退出码 3），而不是写表失败的运行错误（退出码 1）
    if not admin.is_alive():
        fail(
            "Caddy 未运行（Admin API 不可达），请先执行 devproxy setup 或启动 Caddy",
            EXIT_PRECONDITION,
        )
    domain = domain_for(name, config.domain_suffix)
    route = Route(
        name=name,
        domain=domain,
        port=port,
        mode="expose",
        upstream=f"127.0.0.1:{port}",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    try:
        store.mutate(lambda t: t.upsert(route))
    except (RouteStoreError, RouteTableError) as e:
        fail(str(e))
    if not reachable(port):
        err_console.print(f"⚠️  上游 127.0.0.1:{port} 当前无响应（路由已注册，稍后可访问）")
    console.print(f"✅ https://{domain}  (上游 127.0.0.1:{port})")
    console.print(f"   移除: devproxy down {name}")


@app.command()
def down(name: str = typer.Argument(..., help="路由名")) -> None:
    """注销路由；若该路由的子进程还在，一并收掉。"""
    try:
        validate_name(name)
    except InvalidNameError as e:
        fail(str(e))
    _, _, store, _ = build_context()
    try:
        route = store.get(name)
    except RouteTableError as e:
        fail(str(e))
    if route is None:
        console.print(f"（{name} 未注册）")
        raise typer.Exit(EXIT_OK)
    try:
        store.mutate(lambda t: t.remove(name))
    except (RouteStoreError, RouteTableError) as e:
        fail(str(e))
    if route.pid:
        # 带上注册时记录的进程身份 —— pid 可能已被复用，身份不符不杀
        kill_pid_group(route.pid, route.proc_start)
    console.print(f"已注销 {name}")


@app.command()
def ls() -> None:
    """列出所有已注册路由。"""
    _, _, store, _ = build_context()
    try:
        routes = store.list()
    except RouteTableError as e:
        fail(str(e))
    if not routes:
        console.print("（无路由）")
        raise typer.Exit(EXIT_OK)
    table = Table(show_header=True, header_style="bold")
    table.add_column("NAME")
    table.add_column("DOMAIN")
    table.add_column("PORT")
    table.add_column("MODE")
    table.add_column("UPSTREAM")
    table.add_column("STATUS")
    for r in routes:
        ok = reachable(r.port)
        table.add_row(
            escape(r.name),
            escape(r.domain),
            str(r.port),
            escape(r.mode),
            escape(r.upstream),
            "● reachable" if ok else "⛔ 不可达",
        )
    console.print(table)


@app.command()
def config() -> None:
    """显示当前生效配置。"""
    paths, cfg, store, _ = build_context()
    console.print_json(
        data={
            "domain_suffix": cfg.domain_suffix,
            "dns_backend": cfg.dns_backend,
            "admin_addr": cfg.admin_addr,
            "http_port": cfg.http_port,
            "https_port": cfg.https_port,
            "storage": cfg.storage,
            "ready_timeout": cfg.ready_timeout,
            "paths": {
                "config_dir": str(paths.config_dir),
                "state_dir": str(paths.state_dir),
                "data_dir": str(paths.data_dir),
            },
        }
    )
    console.print("[dim]--- 渲染出的路由片段 ---[/dim]")
    try:
        snippet = render_caddyfile(load_table(paths.routes_json))
    except RouteTableError as e:
        fail(str(e))
    console.print(snippet)


@app.command()
def selftest() -> None:
    """隔离 Caddy 实例端到端自检（不碰生产 443/2019）。"""
    from devproxy.proxy.selftest import run_selftest

    _, config, _, _ = build_context()
    result = run_selftest(config.caddy_bin_path)
    for desc, ok in result.checks:
        console.print(f"  {'✅' if ok else '❌'} {desc}")
    if result.failed:
        err_console.print(f"devproxy: selftest 失败 {len(result.failed)} 项")
        raise typer.Exit(EXIT_FAIL)
    console.print(f"selftest 全部通过 ✅（{len(result.checks)} 项）")


def _platform():
    """按当前系统选平台实现（Linux / macOS）。"""
    from devproxy.platform import get_platform

    return get_platform()


def _components(paths, config, dns_backend=None, platform=None):
    """按配置（或显式覆盖）组装组件。

    dns_backend 缺省取 config.dns_backend —— setup 与 doctor 必须用同一个
    后端，否则 setup 装完、doctor 仍报"路由缺失"。
    """
    platform = platform or _platform()
    backend = dns_backend or config.dns_backend
    return [
        CaddyComponent(platform, paths, config),
        DnsmasqComponent(platform, paths, config),
        DnsComponent(platform, paths, config, backend=backend),
        CertComponent(platform, paths, config),
    ]


def _dns_backend(value: str | None, config: Config) -> str:
    """解析并校验 DNS 后端。非法值直接拒绝，别悄悄当成 auto。

    显式传空字符串（--dns-backend ""）是参数错误，不能因为
    `"" or ...` 的 falsy 语义悄悄退回配置值。
    """
    backend = config.dns_backend if value is None else value
    if backend not in DNS_BACKENDS:
        fail(
            f"dns_backend 必须是 {' / '.join(DNS_BACKENDS)}，当前值 {backend!r}",
            EXIT_USAGE,
        )
    return backend


def _status_cell(status) -> str:
    """表格里的“现状”列。

    ok 但 detail 是一条提示（如“尚无 CA 根证书…”）时必须如实展示 ——
    只写“✔ 就绪”会把“还需要 devproxy trust”这一关键信息藏起来。
    """
    if not status.ok:
        return "✗ " + escape(status.detail)
    if status.detail and "尚无" in status.detail:
        return "✔ " + escape(status.detail)
    return "✔ 就绪"


def _interactive() -> bool:
    return sys.stdin.isatty()


def _print_notes(notes: list[str]) -> None:
    """把迁移过程中的提示逐条打出来。"""
    for note in notes:
        console.print(f"  · {note}")


def _migrate_legacy(
    platform,
    paths,
    config,
    state: LegacyState,
    *,
    yes: bool,
    backend: str | None = None,
    notes: list[str] | None = None,
) -> None:
    """把遗留的 caddy 服务迁到本工具管理之下。

    迁移会改写用户的单元与 Caddyfile —— 非交互环境必须显式 --yes。
    """
    console.print("[yellow]检测到遗留的 caddy 服务（非本工具管理）[/yellow]")
    console.print(f"  单元: {state.unit_path}")
    console.print(f"  配置: {state.config_path}")
    console.print(f"  环境变量: {state.env or '无'}")
    console.print("将执行：备份 → 拆出个人站点到 sites.caddy → 改写单元 → 重启")
    if not yes and not sys.stdin.isatty():
        err_console.print("非交互环境且未指定 --yes，拒绝迁移。")
        raise typer.Exit(EXIT_SUDO)
    if not yes and _interactive():
        if not typer.confirm("即将迁移上述遗留环境（会改写单元与 Caddyfile），继续？", default=True):
            console.print("已取消。")
            raise typer.Exit(EXIT_OK)
    collected = list(notes or [])
    try:
        backup = migrate(
            platform,
            paths,
            config,
            state,
            dnsmasq_conf=DNSMASQ_CONF.expanduser(),
            dns_route=default_dns_route(platform, paths, config, backend),
            backend=backend,
            notes=collected,
        )
    except MigrationError as e:
        # 失败路径也要把提示打出来：用户得知道丢了哪些块、sites.caddy 有没有被覆盖
        _print_notes(collected)
        fail(str(e), EXIT_FAIL)
    _print_notes(collected)
    console.print(f"已备份到: {backup.directory}")
    console.print(f"回滚命令: devproxy setup --rollback {backup.directory}")


def _do_rollback(directory: Path) -> None:
    """按备份目录回滚。"""
    from devproxy.backup import BackupRestoreError, load_backup

    try:
        backup = load_backup(directory)
    except (OSError, ValueError) as e:
        fail(f"无法读取备份目录 {directory}: {e}", EXIT_FAIL)
    try:
        touched = do_rollback(backup, platform=_platform())
    except BackupRestoreError as e:
        # 部分条目失败时必须把"已还原了哪些"如实报出来 —— 异常一抛，
        # 用户就完全不知道机器现在是什么状态了。
        err_console.print(f"devproxy: 回滚部分失败（{escape(str(e))}）")
        for p in e.restored:
            console.print(f"  ✔ 已还原 {escape(str(p))}")
        for p, exc in e.failures:
            err_console.print(f"  ✗ {escape(str(p))}: {escape(str(exc))}")
        raise typer.Exit(EXIT_FAIL)
    console.print(f"已回滚 {len(touched)} 个文件：")
    for p in touched:
        console.print(f"  · {escape(str(p))}")
    # 单元换了但进程没重启的话，跑的还是迁移后的配置
    try:
        _platform().restart_service("caddy")
    except Exception:  # noqa: BLE001 —— 服务本来就不存在时不必报错
        pass


@app.command()
def setup(
    dns_backend: str = typer.Option(None, "--dns-backend", help="auto|resolved|system"),
    domain_suffix: str = typer.Option(None, "--domain-suffix"),
    no_trust: bool = typer.Option(False, "--no-trust", help="跳过证书信任"),
    yes: bool = typer.Option(False, "--yes", help="非交互模式，不询问"),
    rollback_dir: str = typer.Option(None, "--rollback", help="从指定备份目录回滚"),
) -> None:
    """部署本地开发环境（Caddy + dnsmasq + DNS 路由 + 证书信任 + 自启）。"""
    paths, config, _, _ = build_context()

    if rollback_dir:
        _do_rollback(Path(rollback_dir))
        raise typer.Exit(EXIT_OK)

    backend = _dns_backend(dns_backend, config)
    if domain_suffix:
        config = replace(config, domain_suffix=domain_suffix)
        try:
            # --domain-suffix 会直通 Caddyfile 与 /etc 下的 DNS 配置，
            # 与 config.toml 同等对待：必须过校验，不能绕过
            validate_config(config)
        except ConfigError as e:
            fail(str(e), EXIT_USAGE)

    platform = _platform()
    # 迁移必须先于组件流程：组件会读迁移留下的状态（单元环境变量、storage）
    state = detect_legacy(platform, paths)
    if state is not None:
        config, storage_note = resolve_storage(config, state)
        _migrate_legacy(
            platform,
            paths,
            config,
            state,
            yes=yes,
            backend=backend,
            notes=[storage_note] if storage_note else [],
        )

    components = _components(paths, config, backend, platform)
    if no_trust:
        components = [c for c in components if c.name != "cert"]

    plan = build_plan(components)

    table = Table(show_header=True, header_style="bold")
    table.add_column("组件")
    table.add_column("现状")
    table.add_column("将要执行")
    for r in plan.reports:
        table.add_row(
            r.name,
            _status_cell(r.status),
            "\n".join(escape(a.description) for a in r.actions) or "—",
        )
    console.print(table)

    if plan.is_noop:
        unhealthy = [r for r in plan.reports if not r.status.ok]
        if not unhealthy:
            console.print("所有组件已就绪，无需变更。")
            if any("尚无" in r.status.detail for r in plan.reports if r.status.ok):
                console.print(TRUST_HINT)
            raise typer.Exit(EXIT_OK)
        # 没有可执行的动作、但组件本身不健康 —— 不能谎报"已就绪"
        for r in unhealthy:
            err_console.print(f"devproxy: {r.name}: {escape(r.status.detail)}")
        raise typer.Exit(EXIT_PRECONDITION)

    if plan.needs_sudo:
        console.print(
            f"[yellow]以下步骤需要 sudo（共 {len(plan.needs_sudo)} 项）：[/yellow]"
        )
        for a in plan.needs_sudo:
            console.print(f"  · {escape(a.description)}")
        if not yes and not sys.stdin.isatty():
            err_console.print("非交互环境且未指定 --yes，拒绝执行需要提权的步骤。")
            raise typer.Exit(EXIT_SUDO)

    # 交互模式下的最后确认（§6.1）；--yes 或非交互环境直接跳过。
    if not yes and _interactive():
        prompt = (
            f"以上 {len(plan.needs_sudo)} 项需要 sudo，继续？"
            if plan.needs_sudo
            else "即将执行以上变更，继续？"
        )
        if not typer.confirm(prompt, default=True):
            console.print("已取消。")
            raise typer.Exit(EXIT_OK)

    results = apply_plan(plan)
    ok = True
    for name, status in results:
        mark = "✔" if status.ok else "✗"
        console.print(f"{mark} {name}: {escape(status.detail)}")
        if not status.ok:
            ok = False
            if status.hint:
                console.print(f"    修复: {escape(status.hint)}")
    if not ok:
        raise typer.Exit(EXIT_FAIL)
    console.print(f"完成。试试: {UP_EXAMPLE}")
    console.print(TRUST_HINT)


@app.command()
def doctor(
    dns_backend: str = typer.Option(None, "--dns-backend", help="auto|resolved|system"),
) -> None:
    """体检：检查所有前置条件。"""
    paths, config, _, _ = build_context()
    backend = _dns_backend(dns_backend, config)
    reports = build_report(_components(paths, config, backend))
    table = Table(show_header=True, header_style="bold")
    table.add_column("组件")
    table.add_column("状态")
    table.add_column("详情")
    table.add_column("修复")
    for r in reports:
        table.add_row(
            r.name,
            "✔ 就绪" if r.status.ok else "✗ 缺失",
            escape(r.status.detail),
            escape(r.status.hint),
        )
    console.print(table)
    if not report_is_healthy(reports):
        raise typer.Exit(EXIT_PRECONDITION)


@app.command()
def trust(
    yes: bool = typer.Option(False, "--yes", help="非交互模式，不询问"),
) -> None:
    """把 CA 根证书装进系统信任库与 NSS 库。"""
    paths, config, _, _ = build_context()
    comp = CertComponent(_platform(), paths, config)
    actions = comp.plan()
    if not actions:
        if not comp.ca_path.exists():
            console.print("尚无 CA 根证书（注册首条路由后由 Caddy 生成），无需变更。")
        else:
            console.print("CA 根证书已信任，无需变更。")
        raise typer.Exit(EXIT_OK)
    if any(a.privileged for a in actions) and not yes and not sys.stdin.isatty():
        err_console.print("非交互环境且未指定 --yes，拒绝执行需要提权的步骤。")
        raise typer.Exit(EXIT_SUDO)
    try:
        comp.apply(actions)
    except PermissionError as e:
        err_console.print(f"devproxy: {escape(str(e))}")
        raise typer.Exit(EXIT_SUDO)
    except Exception as e:  # noqa: BLE001 —— 面向用户的友好报错，不是 traceback
        fail(str(e))
    console.print("已安装 CA 根证书（Chrome 需完全退出重启后生效）")


@app.command()
def logs(
    service: str = typer.Argument("caddy", help="caddy|dnsmasq"),
) -> None:
    """跟踪服务日志。"""
    if service not in ("caddy", "dnsmasq"):
        # 参数错误按全局约定退出 2（§9.1），而不是运行失败 1
        err_console.print(f"devproxy: 未知服务 {service}（可选 caddy / dnsmasq）")
        raise typer.Exit(EXIT_USAGE)
    try:
        # 只读命令：不碰 build_context（它会在用户家目录里建文件）
        _platform().tail_service_log(service)
    except PermissionError as e:
        err_console.print(f"devproxy: {escape(str(e))}")
        raise typer.Exit(EXIT_SUDO)
    except OSError as e:
        # journalctl 不存在等：干净报错，不要把 FileNotFoundError 漏成 traceback
        err_console.print(f"devproxy: 无法读取 {service} 日志: {escape(str(e))}")
        raise typer.Exit(EXIT_FAIL)


def main() -> None:
    try:
        app()
    except (ConfigError, RouteStoreError, RouteTableError) as e:
        # 兜底：领域异常绝不能以 traceback 形态漏到终端
        err_console.print(f"devproxy: {escape(str(e))}")
        sys.exit(EXIT_FAIL)


if __name__ == "__main__":
    main()
