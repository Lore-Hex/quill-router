from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_runs_python_suite_once_with_coverage() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert (
        "uv run pytest -q -n 4 --dist loadgroup --cov=trusted_router "
        "--cov-report=term-missing --cov-fail-under=70"
    ) in workflow
    # Exactly one coverage pass. A second pass on the SAME clock doubles CI
    # time and delays every guarded rollout without checking any additional
    # behavior, which is why this file exists.
    assert workflow.count("--cov=trusted_router") == 1


def test_ci_runs_the_suite_again_past_every_scheduled_cutover() -> None:
    """The only sanctioned second pass: same tests, different clock.

    provider_lifecycle schedules effective-dated retirements, so a test written
    on the near side of one goes red at the announced minute on a pull request
    that did not touch it (CI run 31980690855, Wafer, 2026-08-17 00:00 UTC).
    The post-cutover job moves that failure onto the pull request that writes
    the test. It earns its runtime only if it actually moves the clock, so the
    override has to be there.
    """
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert workflow.count("uv run pytest") == 2
    assert "test-post-cutover:" in workflow
    assert "TR_LIFECYCLE_CLOCK_OVERRIDE=" in workflow
    # Derived from _RETIREMENTS at run time, never a hard-coded date that would
    # silently stop being in the future.
    assert "latest_scheduled_cutover" in workflow
