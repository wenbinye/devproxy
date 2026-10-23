"""单元环境变量的留档。

迁移时把遗留单元里的 `Environment=` 抄下来，后续 setup 重装单元要带上 ——
否则用户站点里的 `{$MINIKUBE_IP:…}` 会指向错误的网关。
"""
from __future__ import annotations

import json

from devproxy.core.paths import Paths, atomic_write_text


class CaddyEnvArchiveError(RuntimeError):
    """环境变量留档存在但读不懂。

    这不是“没有留档”：静默当成空会让重装的单元丢掉 `MINIKUBE_IP` 等变量，
    用户站点随之指向错误的网关，而且没有任何提示。构造/规划期必须中断，
    让用户先修好或明确删除这个文件。
    """


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


def _archive_error(path, exc: Exception) -> CaddyEnvArchiveError:
    """统一的“留档坏了”异常：带路径、后果与修复办法。"""
    return CaddyEnvArchiveError(
        f"环境变量留档无法解析：{path}（{exc}）。"
        f"迁移留档的 MINIKUBE_IP 等变量若丢失，用户站点会指向错误的网关。"
        f"请修复该文件（合法 JSON 对象），或在确认不再需要后删除它，再重新运行。"
    )


def load_extra_env(paths: Paths) -> dict[str, str]:
    """读回遗留单元的环境变量。

    文件不存在 → 空（正常情况，说明没有迁移留档）。文件存在但损坏/无权限
    → 抛 `CaddyEnvArchiveError`：静默当成空会丢环境变量且用户毫无感知。
    """
    path = paths.caddy_env
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise _archive_error(path, exc) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:  # UnicodeDecodeError 也是 ValueError 子类
        raise _archive_error(path, exc) from exc
    if not isinstance(data, dict):
        raise _archive_error(path, ValueError("顶层不是 JSON 对象"))
    return {str(k): str(v) for k, v in data.items()}
