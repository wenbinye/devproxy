from __future__ import annotations

import time

import typer

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.cli._shared import EXIT_PRECONDITION
from devproxy.cli._shared import EXIT_USAGE
from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import domain_for
from devproxy.gateway.names import validate_name
from devproxy.gateway.routes import Route
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.runner import reachable
from devproxy.gateway.store import RouteStoreError


@app.command()
def expose(
    name: str = typer.Argument(..., help="Route name"),
    port: int = typer.Option(..., "--port", help="Port the upstream is already listening on"),
) -> None:
    """Expose an already-running service under a domain. Returns immediately; the route persists."""
    try:
        validate_name(name)
    except InvalidNameError as e:
        s.fail(str(e), EXIT_USAGE)
    if not (1 <= port <= 65535):
        s.fail(f"invalid port {port}: must be between 1 and 65535", EXIT_USAGE)

    paths, config, store, admin = s.build_context()
    s._require_caddyfile(paths)
    # expose registers a persistent live route, so the reload must really happen --
    # Caddy not running is a precondition failure (exit 3), not a write failure (exit 1)
    if not admin.is_alive():
        s.fail(
            "Caddy is not running (Admin API unreachable); run devproxy setup or start Caddy first",
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
        s.fail(str(e))
    if not reachable(port):
        s.err_console.print(
            f"⚠️  upstream 127.0.0.1:{port} is not responding right now "
            "(the route is registered and will work once it is up)"
        )
    s.console.print(f"✅ https://{domain}  (upstream 127.0.0.1:{port})")
    s.console.print(f"   Remove with: devproxy down {name}")
    s._print_trust_hint_if_needed(paths, config)
