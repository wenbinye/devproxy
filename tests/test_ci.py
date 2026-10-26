"""Guard tests for the CI configuration.

A repository without CI has no regression net: it can be green locally and still go
red after merging. These tests guard "the unit test suite must run in CI" and
"lint / type checking must also run in CI" -- if the workflow file is deleted or its
commands change, the tests fail immediately.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_exists():
    assert WORKFLOW.exists(), "missing GitHub Actions workflow: .github/workflows/ci.yml"


def test_ci_workflow_runs_the_unit_suite():
    """CI runs the **unit** suite -- real-machine integration tests need a real caddy and stay out of CI."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pytest" in text
    assert "--ignore=tests/test_integration.py" in text


def test_ci_workflow_installs_dependencies_from_the_lock_file():
    """Install dependencies from the **lock file**.

    Without pinning versions, CI would get a different dependency combination on every
    run -- from typer 0.27 on, click is no longer a dependency (that broke CI once, on
    2026-10-26). Such changes must be blocked by the lock file together with --locked.
    """
    assert (REPO_ROOT / "uv.lock").exists(), "the lock file must be committed"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "uv sync" in text
    assert "--locked" in text, "must use --locked: fail when the lock file and pyproject disagree"


def test_ci_workflow_runs_lint_and_type_check():
    """Style and type checks must also run in CI, and must be **blocking**."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "ruff check" in text
    assert "ruff format --check" in text
    assert "mypy" in text
    assert "continue-on-error" not in text, "lint / type checks must not be relaxed to non-blocking"


def test_ci_workflow_covers_macos():
    """macOS must run too: the two platform implementations differ, so Linux-only would miss one side."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "macos-latest" in text
    assert "ubuntu-latest" in text


def test_ci_workflow_installs_certutil():
    """The NSS library tests must run against a **real** certutil (only real certutil has the deduplication rules).

    If CI does not install libnss3-tools, that whole group is skipped -- which means no
    regression guard at all.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "libnss3-tools" in text


def test_publish_workflow_exists():
    """Releases need a path: build and upload to PyPI on tag / Release."""
    assert (REPO_ROOT / ".github" / "workflows" / "publish.yml").exists()


def test_license_file_exists():
    """Declaring MIT requires the license text (otherwise it legally means all rights reserved)."""
    assert (REPO_ROOT / "LICENSE").exists(), "missing LICENSE file"
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "MIT"' in pyproject
