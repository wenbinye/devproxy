from __future__ import annotations

from devproxy.cli import app
from devproxy.gateway.routes import RouteTableError
from devproxy.gateway.routes import load_table
from devproxy.gateway.routes import render_caddyfile

from devproxy.cli import _shared as s

@app.command()
def config() -> None:
    """显示当前生效配置。"""
    paths, cfg, _, _ = s.build_context()
    s.console.print_json(
        data={
            "domain_suffix": cfg.domain_suffix,
            "dns_backend": cfg.dns_backend,
            "admin_addr": cfg.admin_addr,
            "http_port": cfg.http_port,
            "https_port": cfg.https_port,
            # 生效的 storage（config.toml > DEVPROXY_DATA_DIR > 默认值）
            "storage": cfg.storage,
            "ready_timeout": cfg.ready_timeout,
            # 额外放行的域名后缀（用户在 config.toml 里显式担责放行的）
            "allowed_domain_suffixes": list(cfg.allowed_domain_suffixes),
            "paths": {
                "config_dir": str(paths.config_dir),
                "state_dir": str(paths.state_dir),
                # 只是默认目录（仅 DEVPROXY_DATA_DIR 影响它）——
                # 不是生效的 storage，别让用户以为 Caddy 的数据在那儿
                "data_dir_default": str(paths.data_dir),
            },
        }
    )
    s.console.print(
        "[dim]storage 才是生效的 Caddy 存储路径（CA 与证书都在那儿）；"
        "data_dir_default 只是默认目录，被 config.toml 的 storage 覆盖时以 storage 为准。[/dim]"
    )
    s.console.print("[dim]--- 渲染出的路由片段 ---[/dim]")
    try:
        snippet = render_caddyfile(load_table(paths.routes_json))
    except RouteTableError as e:
        s.fail(str(e))
    s.console.print(snippet)
