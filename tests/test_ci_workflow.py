from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_runs_python_suite_once_with_coverage() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert workflow.count("uv run pytest") == 1
    assert (
        "uv run pytest -q --cov=trusted_router --cov-report=term-missing "
        "--cov-fail-under=70"
    ) in workflow
