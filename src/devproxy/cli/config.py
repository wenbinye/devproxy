from __future__ import annotations

from devproxy.cli import _shared as s
from devproxy.cli import app
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.routes import load_table
from devproxy.gateway.routes import render_caddyfile


@app.command()
def config() -> None:
    """Show the configuration currently in effect."""
    paths, cfg, _, _ = s.build_context()
    s.console.print_json(
        data={
            "domain_suffix": cfg.domain_suffix,
            "dns_backend": cfg.dns_backend,
            "admin_addr": cfg.admin_addr,
            "http_port": cfg.http_port,
            "https_port": cfg.https_port,
            # The storage that is actually in effect
            # (config.toml > DEVPROXY_DATA_DIR > default)
            "storage": cfg.storage,
            "ready_timeout": cfg.ready_timeout,
            # Extra domain suffixes the user explicitly opted into in config.toml
            "allowed_domain_suffixes": list(cfg.allowed_domain_suffixes),
            "paths": {
                "config_dir": str(paths.config_dir),
                "state_dir": str(paths.state_dir),
                # The default directory only (affected by DEVPROXY_DATA_DIR alone) --
                # this is not the effective storage, so users don't think Caddy's data lives here
                "data_dir_default": str(paths.data_dir),
            },
        }
    )
    s.console.print(
        "[dim]`storage` is the Caddy storage path in effect (CA and certificates live "
        "there); `data_dir_default` is only the default directory and is overridden by "
        "`storage` in config.toml.[/dim]"
    )
    s.console.print("[dim]--- rendered route snippet ---[/dim]")
    try:
        snippet = render_caddyfile(load_table(paths.routes_json))
    except RouteTableError as e:
        s.fail(str(e))
    s.console.print(snippet)
