"""CI 配置的守护测试。

没有 CI 的仓库等于没有回归网：本地绿了，合并后照样能红。这里守住
「单元测试套件必须在 CI 里跑」以及「lint / 类型检查也必须在 CI 里跑」——
workflow 文件被删掉或改了命令时，测试立刻失败。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_exists():
    assert WORKFLOW.exists(), "缺少 GitHub Actions workflow：.github/workflows/ci.yml"


def test_ci_workflow_runs_the_unit_suite():
    """CI 跑的是**单元**套件 —— 真机集成测试需要真实 caddy，不进 CI。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pytest" in text
    assert "--ignore=tests/test_integration.py" in text


def test_ci_workflow_installs_dependencies_from_the_lock_file():
    """依赖按**锁文件**装。

    不锁版本意味着 CI 每次拿到不同的依赖组合 —— typer 0.27 起就不再依赖
    click（2026-10-26 就因此炸过一次），这类变动必须由锁文件与 --locked
    一起挡住。
    """
    assert (REPO_ROOT / "uv.lock").exists(), "锁文件必须提交"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "uv sync" in text
    assert "--locked" in text, "必须用 --locked：锁文件与 pyproject 不一致就失败"


def test_ci_workflow_runs_lint_and_type_check():
    """风格与类型检查也要在 CI 里跑，且是**阻断**的。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "ruff check" in text
    assert "ruff format --check" in text
    assert "mypy" in text
    assert "continue-on-error" not in text, "lint / 类型检查不得放宽为不阻断"


def test_ci_workflow_covers_macos():
    """macOS 也要跑：平台层两端实现不同，只跑 Linux 会漏掉一整边。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "macos-latest" in text
    assert "ubuntu-latest" in text


def test_ci_workflow_installs_certutil():
    """NSS 库的用例要在**真的** certutil 上跑（去重规则只有真 certutil 才有）。

    CI 不装 libnss3-tools 的话，那组用例会整组 skip —— 等于没有回归护栏。
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "libnss3-tools" in text


def test_publish_workflow_exists():
    """发版要有路：打 tag / 发 Release 时构建并上传 PyPI。"""
    assert (REPO_ROOT / ".github" / "workflows" / "publish.yml").exists()


def test_license_file_exists():
    """声明了 MIT 就得有许可证文本（否则法律上等于保留所有权利）。"""
    assert (REPO_ROOT / "LICENSE").exists(), "缺少 LICENSE 文件"
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "MIT"' in pyproject
