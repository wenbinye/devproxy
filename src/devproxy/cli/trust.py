from __future__ import annotations

from rich.markup import escape
import typer

from devproxy.cli import app
from devproxy.cli._shared import EXIT_OK
from devproxy.cli._shared import EXIT_SUDO

from devproxy.cli import _shared as s


@app.command()
def trust(
    yes: bool = typer.Option(False, "--yes", help="非交互模式，不询问"),
) -> None:
    """把 CA 根证书装进系统信任库与 NSS 库。"""
    paths, config, _, _ = s.build_context()
    comp = s.CertComponent(s._platform(), paths, config)
    actions = comp.plan()
    if not actions:
        if not comp.ca_path.exists():
            s.console.print("尚无 CA 根证书（注册首条路由后由 Caddy 生成），无需变更。")
        else:
            s.console.print("CA 根证书已信任，无需变更。")
        raise typer.Exit(EXIT_OK)
    # 统一经 _shared._interactive() 判断，测试打桩才能覆盖所有命令
    if any(a.privileged for a in actions) and not yes and not s._interactive():
        s.err_console.print("非交互环境且未指定 --yes，拒绝执行需要提权的步骤。")
        raise typer.Exit(EXIT_SUDO)
    try:
        comp.apply(actions)
    except PermissionError as e:
        s.err_console.print(f"devproxy: {escape(str(e))}")
        raise typer.Exit(EXIT_SUDO)
    except RuntimeError as e:
        # 组件的 documented 契约只有 PermissionError / RuntimeError；
        # 其余异常是程序 bug，不能被伪装成运维错误
        s.fail(str(e))
    # 只做清理时不能说"已安装" —— 那是两回事，用户会以为 CA 被重装过
    commands = {a.command[0] for a in actions if a.command}
    if commands <= {"remove-stale-nss-ca"}:
        s.console.print("已清理 NSS 库中的陈旧 CA 根证书（Chrome 需完全退出重启后生效）")
    else:
        s.console.print("已安装 CA 根证书（Chrome 需完全退出重启后生效）")
