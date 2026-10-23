from __future__ import annotations

from dataclasses import replace
import sys

from rich.console import Console
from rich.markup import escape
from typer.core import TyperGroup
import typer

from devproxy.core.config import Config
from devproxy.core.config import DNS_BACKENDS
from devproxy.core.config import load_config
from devproxy.core.paths import Paths
from devproxy.core.paths import default_paths
from devproxy.gateway.caddy import CaddyAdmin
from devproxy.gateway.caddy import CaddyConfig
from devproxy.gateway.store import RouteStore
from devproxy.lifecycle.migrate import load_legacy_storage


# 命令模块经 `s.<名字>` 在调用时查找这些依赖（测试也在这里打桩）。
from devproxy.components.dnsmasq import DNSMASQ_CONF  # noqa: F401
from devproxy.gateway.runner import UpRunner  # noqa: F401
from devproxy.lifecycle.setup import apply_plan  # noqa: F401
from devproxy.lifecycle.setup import build_plan  # noqa: F401
from devproxy.lifecycle.migrate import default_dns_route  # noqa: F401
from devproxy.gateway.runner import kill_pid_group  # noqa: F401
from devproxy.components.certs import CertComponent  # noqa: F401
from devproxy.components.caddy import CaddyComponent  # noqa: F401
from devproxy.components.dns import DnsComponent  # noqa: F401
from devproxy.components.dnsmasq import DnsmasqComponent  # noqa: F401
console = Console()

err_console = Console(stderr=True)

EXIT_OK = 0

EXIT_FAIL = 1

EXIT_USAGE = 2

EXIT_PRECONDITION = 3

EXIT_SUDO = 4

EXIT_DECLINED = 4

TRUST_HINT = (
    "提示：首次注册路由（devproxy up / expose）会生成 CA，之后请运行 devproxy trust "
    "让 curl/Chrome 信任。"
)

TRUST_HINT_ROUTE = (
    "提示：CA 根证书已生成，但还没被信任 —— 现在 curl/Chrome 访问会报证书错误。"
    "请运行 devproxy trust 让它们信任它。"
)

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

def _print_trust_hint_if_needed(paths: Paths, config: Config) -> None:
    """CA 已生成但还没被信任时提示一次。

    CA 是**第一条路由注册时**才生成的 —— 第一次 up/expose 之后 curl 与
    Chrome 一定不信任这个证书。不说清楚，用户只会看到"浏览器报不安全"，
    根本不知道还差一步 `devproxy trust`。

    CA 还不存在（没什么可信任）或已被信任时都不提示 —— 后者否则每次
    运行都会冒出来，变成噪音。
    """
    comp = CertComponent(_platform(), paths, config)
    try:
        if comp.fully_trusted():
            return
    except Exception:  # noqa: BLE001 —— 提示失败不该影响主流程
        return
    if not comp.ca_path.exists():
        return
    console.print(TRUST_HINT_ROUTE)

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

def _global_scope_notes(plan) -> list[str]:
    """整机作用域的披露文案。

    变更清单的表格放不下这类话（Rich 会截断），而它恰恰是最该让用户看到的：
    `DNS=` 改的是**整台机器**的解析路径。组件通过可选的 `global_scope_note()`
    提供文案 —— 没有就说明这个组件不涉及整机。

    只要整机作用域真的成立就披露 —— 那台机器此刻就处在"整机解析指向用户级
    dnsmasq"的状态里，用户有权知道。两种情况算数：这次要写它（r.actions 非空），
    或者它早就写好了（组件状态正常）。
    """
    notes = []
    for r in plan.reports:
        fn = getattr(r.component, "global_scope_note", None)
        if not callable(fn):
            continue
        if not r.actions and not r.status.ok:
            # 既没在改、也没装好 —— 谈不上"整机解析已经指向 dnsmasq"。
            continue
        note = fn()
        if note:
            notes.append(note)
    return notes
