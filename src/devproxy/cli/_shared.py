"""命令层的公共接缝（兼容入口）。

实现按关注点拆在四个模块里：

- `_runtime`：Rich console、退出码、`fail()`
- `_typer`：`--` 切分组
- `_context`：配置加载、平台/组件装配
- `_render`：表格、提示文案、执行结果汇总

命令模块仍然只 `from devproxy.cli import _shared as s`，并在**调用时**
查找 `s.<名字>`（如 `s.build_context(...)`、`s._platform()`）。这里
转出的名字因此是稳定契约：测试对 `devproxy.cli._shared.<名字>` 打桩
必须能覆盖所有命令，不要改成命令模块里的直接 import。

名字里的下划线是历史契约的一部分（既有测试与文档都按这些名字打桩），
不代表模块私有。
"""

from __future__ import annotations

# 本模块是稳定的兼容转出口：导入的名字供命令模块与测试使用，
# 不能按“未使用”删掉（ruff 看不到 `s.<名字>` 这种调用时查找）。
# ruff: noqa: F401

import typer  # 测试经 `_shared.typer.confirm` 打桩

from devproxy.cli._context import StorageArchiveError
from devproxy.cli._context import _components
from devproxy.cli._context import _dns_backend
from devproxy.cli._context import _platform
from devproxy.cli._context import _require_caddyfile
from devproxy.cli._context import _resolve_storage
from devproxy.cli._context import build_context
from devproxy.cli._context import ensure_backend_supported
from devproxy.cli._context import track_elevation_errors
from devproxy.cli._render import TRUST_HINT_ROUTE
from devproxy.cli._render import UP_EXAMPLE
from devproxy.cli._render import _global_scope_notes
from devproxy.cli._render import _print_notes
from devproxy.cli._render import _print_trust_hint_if_needed
from devproxy.cli._render import _status_cell
from devproxy.cli._render import component_note
from devproxy.cli._render import fail_for_apply_errors
from devproxy.cli._render import firefox_notes
from devproxy.cli._render import print_apply_results
from devproxy.cli._render import print_noop_unhealthy
from devproxy.cli._render import print_scope_notes
from devproxy.cli._render import print_uninstall_kept
from devproxy.cli._render import print_uninstall_results
from devproxy.cli._render import render_plan
from devproxy.cli._render import render_uninstall_plan
from devproxy.cli._runtime import EXIT_DECLINED
from devproxy.cli._runtime import EXIT_FAIL
from devproxy.cli._runtime import EXIT_OK
from devproxy.cli._runtime import EXIT_PRECONDITION
from devproxy.cli._runtime import EXIT_SUDO
from devproxy.cli._runtime import EXIT_USAGE
from devproxy.cli._runtime import _interactive
from devproxy.cli._runtime import console
from devproxy.cli._runtime import err_console
from devproxy.cli._runtime import fail
from devproxy.cli._typer import DevProxyGroup

# 组件 / 网关 / 生命周期接缝：命令模块经 `s.<名字>` 在调用时查找。
# 这些导入只加载类与函数，不会构造平台或碰真实系统。
from devproxy.components.caddy import CaddyComponent  # noqa: F401
from devproxy.components.certs import CertComponent  # noqa: F401
from devproxy.components.dns import DnsComponent  # noqa: F401
from devproxy.components.dnsmasq import DNSMASQ_CONF  # noqa: F401
from devproxy.components.dnsmasq import DnsmasqComponent  # noqa: F401
from devproxy.gateway.runner import UpRunner  # noqa: F401
from devproxy.gateway.runner import kill_pid_group  # noqa: F401
from devproxy.lifecycle.migrate import default_dns_route  # noqa: F401
from devproxy.lifecycle.setup import apply_plan  # noqa: F401
from devproxy.lifecycle.setup import build_plan  # noqa: F401
from devproxy.lifecycle.uninstall import StepResult  # noqa: F401
from devproxy.lifecycle.uninstall import UninstallPlan  # noqa: F401
from devproxy.lifecycle.uninstall import UninstallTargets  # noqa: F401
from devproxy.lifecycle.uninstall import apply_plan as apply_uninstall_plan  # noqa: F401
from devproxy.lifecycle.uninstall import build_plan as build_uninstall_plan  # noqa: F401
