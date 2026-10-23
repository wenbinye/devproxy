"""单元环境变量的留档。

迁移时把遗留单元里的 `Environment=` 抄下来，后续 setup 重装单元要带上 ——
否则用户站点里的 `{$MINIKUBE_IP:…}` 会指向错误的网关。
"""
from __future__ import annotations

import json
import warnings

from devproxy.core.paths import Paths, atomic_write_text


def save_extra_env(paths: Paths, env: dict[str, str]) -> None:
    """把遗留单元的环境变量留档。

    用户站点里的 {$MINIKUBE_IP:…} 依赖它 —— 后续 setup 重装单元时必须带上，
    否则个人站点会指向错误的网关。原子落盘：写一半的 JSON 比没有更危险。
    """
    path = paths.caddy_env
    if not env:
        path.unlink(missing_ok=True)
        return
    atomic_write_text(
        path, json.dumps(env, ensure_ascii=False, indent=2) + "\n"
    )


def _warn_archive_unusable(path, exc: Exception) -> None:
    """留档存在却读不懂时必须说出来 —— 静默当成空会丢环境变量。"""
    warnings.warn(
        f"环境变量留档不可读，已忽略：{path}（{exc}）。"
        f"若确认不再需要，可删除该文件；否则请修复它后重新运行。",
        UserWarning,
        stacklevel=3,
    )


def load_extra_env(paths: Paths) -> dict[str, str]:
    """读回遗留单元的环境变量。

    文件不存在 → 空（正常情况）。文件存在但损坏/无权限 → 发出显式警告后
    返回空：调用方是组件构造函数，不能因为留档损坏就带崩整个 setup，
    但绝不能像以前那样静默丢弃。
    """
    path = paths.caddy_env
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _warn_archive_unusable(path, exc)
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:  # UnicodeDecodeError 也是 ValueError 子类
        _warn_archive_unusable(path, exc)
        return {}
    if not isinstance(data, dict):
        _warn_archive_unusable(path, ValueError("顶层不是 JSON 对象"))
        return {}
    return {str(k): str(v) for k, v in data.items()}
