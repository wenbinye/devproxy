"""The command layer's shared seams (compatibility façade).

The implementation is split by concern across four modules:

- `_runtime`: Rich consoles, exit codes, `fail()`
- `_typer`: the `--` splitting group
- `_context`: config loading, platform/component assembly
- `_render`: tables, hint texts, result summaries

Command modules still only do `from devproxy.cli import _shared as s` and look up
`s.<name>` at **call time** (e.g. `s.build_context(...)`, `s._platform()`). The
names re-exported here are therefore a stable contract: patching
`devproxy.cli._shared.<name>` in tests must cover every command, so do not turn
these into direct imports inside command modules.

The leading underscores are part of that historical contract (existing tests and
docs patch these names); they do not mean the module is private.
"""

from __future__ import annotations

# This module is a stable re-export surface: the imported names are used by
# command modules and tests, so they must not be deleted as "unused" (ruff
# cannot see the `s.<name>` lookups that happen at call time).
# ruff: noqa: F401
import typer  # tests patch `_shared.typer.confirm`

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

# Component / gateway / lifecycle seams: command modules look these up as
# `s.<name>` at call time. These imports only load classes and functions; they
# never construct a platform or touch the real system.
from devproxy.components.caddy import CaddyComponent  # noqa: F401
from devproxy.components.certs import CertComponent  # noqa: F401
from devproxy.components.dns import DnsComponent  # noqa: F401
from devproxy.components.dnsmasq import DNSMASQ_CONF  # noqa: F401
from devproxy.components.dnsmasq import DnsmasqComponent  # noqa: F401
from devproxy.gateway.caddy import CaddyConfig  # noqa: F401
from devproxy.gateway.project import find_project_file  # noqa: F401
from devproxy.gateway.runner import UpRunner  # noqa: F401
from devproxy.gateway.runner import kill_pid_group  # noqa: F401
from devproxy.gateway.sync import build_sync_plan  # noqa: F401
from devproxy.gateway.sync import execute_sync_plan  # noqa: F401
from devproxy.lifecycle.migrate import default_dns_route  # noqa: F401
from devproxy.lifecycle.setup import apply_plan  # noqa: F401
from devproxy.lifecycle.setup import build_plan  # noqa: F401
from devproxy.lifecycle.uninstall import StepResult  # noqa: F401
from devproxy.lifecycle.uninstall import UninstallPlan  # noqa: F401
from devproxy.lifecycle.uninstall import UninstallTargets  # noqa: F401
from devproxy.lifecycle.uninstall import apply_plan as apply_uninstall_plan  # noqa: F401
from devproxy.lifecycle.uninstall import build_plan as build_uninstall_plan  # noqa: F401
