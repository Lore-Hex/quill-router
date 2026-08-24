from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_runs_python_suite_once_with_coverage() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    # Still one coverage pass, now sharded. A second pass on the SAME clock
    # doubles CI time and delays every guarded rollout without checking any
    # additional behavior, which is why this file exists.
    assert "-n 4 --dist loadgroup" in workflow
    assert "--cov=trusted_router" in workflow
    assert workflow.count("--cov=trusted_router") == 1

    # The shards collect coverage with NO threshold: each sees only its own
    # slice, so a per-shard floor would fail on a suite that covers far more.
    # The floor moved to the `coverage` job and must still be enforced exactly
    # once -- dropping it entirely would leave every assertion here passing.
    # Comment lines stripped first: the workflow EXPLAINS why a per-shard
    # --cov-fail-under would be wrong, and matching that prose would make this
    # assertion fail on the documentation rather than on the command.
    commands = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--cov-fail-under=70" not in commands
    assert commands.count("--fail-under=70") == 1
    assert "coverage combine" in workflow

    # --dist loadgroup is load-bearing: the conformance backends are
    # xdist_group-marked because the Spanner PG emulator rejects concurrent
    # DDL. Sharding is safe only because each matrix job gets its own service
    # containers.
    assert "--splits" in workflow and "--group" in workflow


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
