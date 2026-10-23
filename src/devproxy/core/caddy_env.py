"""单元环境变量的留档。

迁移时把遗留单元里的 `Environment=` 抄下来，后续 setup 重装单元要带上 ——
否则用户站点里的 `{$MINIKUBE_IP:…}` 会指向错误的网关。
"""
from __future__ import annotations

import json
from pathlib import Path

from devproxy.core.paths import Paths

def save_extra_env(paths: Paths, env: dict[str, str]) -> None:
    """把遗留单元的环境变量留档。

    用户站点里的 {$MINIKUBE_IP:…} 依赖它 —— 后续 setup 重装单元时必须带上，
    否则个人站点会指向错误的网关。
    """
    path = paths.caddy_env
    if not env:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(env, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_extra_env(paths: Paths) -> dict[str, str]:
    """读回遗留单元的环境变量（读不到或损坏时返回空）。"""
    try:
        data = json.loads(paths.caddy_env.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}
