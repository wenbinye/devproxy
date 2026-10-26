"""依赖装配：配置加载、storage 留档解析、平台与组件构造。

这里只回答“命令要用的对象从哪来”，不做渲染，也不写用户可见的表格。
命令模块经 `_shared` 在调用时查找这些函数（测试在 `_shared` 上打桩）。
"""

from __future__ import annotations

from dataclasses import replace

from devproxy.cli._runtime import EXIT_PRECONDITION
from devproxy.cli._runtime import EXIT_USAGE
from devproxy.cli._runtime import err_console
from devproxy.cli._runtime import fail
from devproxy.core.config import DNS_BACKENDS
from devproxy.core.config import Config
from devproxy.core.config import load_config
from devproxy.core.paths import Paths
from devproxy.core.paths import default_paths
from devproxy.gateway.caddy import CaddyAdmin
from devproxy.gateway.caddy import CaddyConfig
from devproxy.gateway.store import RouteStore
from devproxy.lifecycle.migrate import load_legacy_storage


class StorageArchiveError(RuntimeError):
    """迁移 storage 留档存在但读不懂 —— 不能静默回退。"""


def _resolve_storage(config: Config, paths: Paths) -> tuple[Config, list[str]]:
    """沿用迁移留档的 storage，并返回需要告诉用户的警告。

    留档（state/caddy-storage.json）是迁移时的决定：换了 storage 位置，
    Caddy 会重新生成 CA，浏览器信任与已有证书全部失效。所以它优先于
    config.toml —— 但**绝不能静默**：用户改了 config.toml 却被无声忽略，
    是最难查的一类问题。

    留档“存在但读不懂”与“不存在”是两回事：前者说明磁盘满/进程中断
    留下了截断文件，此时回退到 config.toml 可能让 Caddy 换 storage、
    重新生成 CA，必须中止并让用户确认。查询与输出分离 —— 警告由调用方
    打印（CQS）。
    """
    archive = paths.caddy_storage
    if not archive.exists():
        return config, []
    storage = load_legacy_storage(paths)
    if storage is None:
        raise StorageArchiveError(
            f"迁移留档损坏，无法确认 Caddy 的 storage 路径：{archive}。"
            "为避免 Caddy 重建 CA（已有证书与浏览器信任会失效），已中止。"
            "请确认不再需要该留档后删除此文件，再重新运行。"
        )
    if storage == config.storage_path:
        return config, []
    warning = (
        f"devproxy: 沿用迁移留档的 storage 路径 {storage}"
        f"（config.toml 里是 {config.storage_path}）：留档优先，"
        f"config.toml 的 storage 当前不生效。"
        f"删除 {archive} 即可恢复 config.toml 的设置。"
    )
    return replace(config, storage=str(storage)), [warning]


def build_context(
    paths: Paths | None = None, config: Config | None = None
) -> tuple[Paths, Config, RouteStore, CaddyAdmin]:
    """组装依赖。测试可传入自定义 paths/config。

    **不建目录、不 touch 文件**：ls / config / doctor 这类只读命令也会
    调用这里，查询不该有写副作用（只读挂载或 HOME 不可写时更不该炸）。
    写路径自己保证目录存在（RouteStore.mutate、migrate），setup 在
    部署分支显式调用 `paths.ensure_files()`。
    """
    paths = paths or default_paths()
    config = config or load_config(paths.config_toml)
    try:
        config, warnings = _resolve_storage(config, paths)
    except StorageArchiveError as e:
        # 前置条件不满足：留档与配置的冲突必须由用户确认后解决
        fail(str(e), EXIT_PRECONDITION)
    for warning in warnings:
        err_console.print(warning)
    # 迁移时沿用了遗留 Caddyfile 的 storage（CA 还在那儿）—— 别换回去，
    # 换了 Caddy 会重新生成 CA，浏览器信任与已有证书全部失效。
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


def _platform():
    """按当前系统选平台实现（Linux / macOS）。

    平台构造会拒绝不合适的运行身份（例如 `sudo devproxy ...` 以 root
    运行）—— 那是前置条件不满足，翻译成可读信息 + 退出码 3，而不是
    让 RuntimeError 以 traceback 形态漏出去。
    """
    from devproxy.platform import get_platform

    try:
        return get_platform()
    except RuntimeError as e:
        fail(str(e), EXIT_PRECONDITION)


def _components(
    paths,
    config,
    dns_backend=None,
    platform=None,
    *,
    include_cert: bool = True,
):
    """按配置（或显式覆盖）组装组件。

    dns_backend 缺省取 config.dns_backend —— setup 与 doctor 必须用同一个
    后端，否则 setup 装完、doctor 仍报"路由缺失"。

    组件类与平台都在调用时经 `_shared` 查找：测试替换
    `_shared.CertComponent` / `_shared._platform` 必须对这里生效。
    """
    from devproxy.cli import _shared as s

    platform = platform or s._platform()
    backend = config.dns_backend if dns_backend is None else dns_backend
    components = [
        s.CaddyComponent(platform, paths, config),
        s.DnsmasqComponent(platform, paths, config),
        s.DnsComponent(platform, paths, config, backend=backend),
    ]
    if include_cert:
        components.append(s.CertComponent(platform, paths, config))
    return components


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


class _ElevationAware:
    """记录组件 apply 抛出的 PermissionError，其余行为原样透传。

    `lifecycle.setup.apply_plan` 为了“单组件失败不阻断”会把所有异常压成
    Status，异常类型随之丢失；CLI 需要据此区分“提权失败（退出码 4）”与
    普通运行失败（退出码 1）。这里只记录并**照旧抛出**，apply_plan 的
    既有语义不变。
    """

    def __init__(self, component, sink: list[tuple[str, PermissionError]]):
        self._component = component
        self._sink = sink

    def __getattr__(self, name):
        return getattr(self._component, name)

    def apply(self, actions) -> None:
        try:
            self._component.apply(actions)
        except PermissionError as e:
            self._sink.append((getattr(self._component, "name", "?"), e))
            raise


def track_elevation_errors(
    components: list,
) -> tuple[list, list[tuple[str, PermissionError]]]:
    """给组件套一层提权失败记录器，返回 (组件列表, 记录表)。"""
    errors: list[tuple[str, PermissionError]] = []
    return [_ElevationAware(c, errors) for c in components], errors


def ensure_backend_supported(backend: str, platform) -> None:
    """后端与平台不匹配时按**参数错误**拒绝。

    macOS 没有 systemd-resolved：DnsComponent.plan() 会抛 RuntimeError。
    提前在 CLI 层拦下，用户看到的是明确的参数错误（退出码 2），而不是
    “组件计算变更失败”或 traceback。
    """
    if backend == "resolved" and getattr(platform, "name", "") == "mac":
        fail(
            "macOS 不支持 resolved 后端（没有 systemd-resolved），请使用 auto 或 system",
            EXIT_USAGE,
        )
