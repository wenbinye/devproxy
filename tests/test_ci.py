"""CI 配置的守护测试。

没有 CI 的仓库等于没有回归网：本地绿了，合并后照样能红。这里守住
「单元测试套件必须在 CI 里跑」这一条 —— workflow 文件被删掉或改了
命令时，测试立刻失败。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_exists():
    assert WORKFLOW.exists(), "缺少 GitHub Actions workflow：.github/workflows/ci.yml"


def test_ci_workflow_runs_the_unit_suite():
    """CI 跑的是**单元**套件 —— 真机集成测试需要真实 caddy，不进 CI。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "python -m pytest" in text
    assert "--ignore=tests/test_integration.py" in text


def test_ci_workflow_installs_runtime_dependencies():
    """依赖只有 typer + rich；CI 必须装齐，否则测试根本跑不起来。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "typer" in text
    assert "rich" in text
    assert "pytest" in text
