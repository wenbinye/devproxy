"""devproxy 命令行入口。"""

from __future__ import annotations

import sys
import time

import typer
from rich.console import Console
from rich.table import Table
from typer.core import TyperGroup

from devproxy.config import Config, ConfigError, load_config
from devproxy.paths import Paths, default_paths
from devproxy.proxy.caddy import CaddyAdmin, CaddyConfig
from devproxy.proxy.names import InvalidNameError, domain_for, validate_name
from devproxy.proxy.routes import Route, load_table, render_caddyfile
from devproxy.proxy.runner import UpError, UpRunner, kill_pid_group, reachable
from devproxy.proxy.store import RouteStore, RouteStoreError

console = Console()
err_console = Console(stderr=True)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PRECONDITION = 3


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


def build_context(
    paths: Paths | None = None, config: Config | None = None
) -> tuple[Paths, Config, RouteStore, CaddyAdmin]:
    """组装依赖。测试可传入自定义 paths/config。"""
    paths = paths or default_paths()
    config = config or load_config(paths.config_toml)
    paths.ensure_files()
    caddy = CaddyConfig(
        caddy_bin=config.caddy_bin_path,
        caddyfile=paths.caddyfile,
        admin_addr=config.admin_addr,
    )
    store = RouteStore(paths, config, caddy)
    admin = CaddyAdmin(base=config.admin_base)
    return paths, config, store, admin


@app.command(context_settings={"ignore_unknown_options": True})
def up(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="路由名（[a-z0-9-]，单标签）"),
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

    _, config, store, admin = build_context()
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
    except (UpError, RouteStoreError, InvalidNameError) as e:
        err_console.print(f"devproxy: {e}")
        raise typer.Exit(EXIT_FAIL) from e
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
        err_console.print(f"devproxy: {e}")
        raise typer.Exit(EXIT_FAIL) from e
    if not (1 <= port <= 65535):
        err_console.print(f"devproxy: 非法端口 {port}：必须在 1..65535 之间")
        raise typer.Exit(EXIT_FAIL)

    _, config, store, _ = build_context()
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
    except RouteStoreError as e:
        err_console.print(f"devproxy: {e}")
        raise typer.Exit(EXIT_FAIL) from e
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
        err_console.print(f"devproxy: {e}")
        raise typer.Exit(EXIT_FAIL) from e
    _, _, store, _ = build_context()
    route = store.get(name)
    if route is None:
        console.print(f"（{name} 未注册）")
        raise typer.Exit(EXIT_OK)
    try:
        store.mutate(lambda t: t.remove(name))
    except RouteStoreError as e:
        err_console.print(f"devproxy: {e}")
        raise typer.Exit(EXIT_FAIL) from e
    if route.pid:
        kill_pid_group(route.pid)
    console.print(f"已注销 {name}")


@app.command()
def ls() -> None:
    """列出所有已注册路由。"""
    _, _, store, _ = build_context()
    routes = store.list()
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
            r.name,
            r.domain,
            str(r.port),
            r.mode,
            r.upstream,
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
    console.print(render_caddyfile(load_table(paths.routes_json)))


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


def main() -> None:
    try:
        app()
    except ConfigError as e:
        err_console.print(f"devproxy: 配置错误：{e}")
        sys.exit(EXIT_FAIL)


if __name__ == "__main__":
    main()
