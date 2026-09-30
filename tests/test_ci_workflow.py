from __future__ import annotations

from pathlib import Path

import yaml

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
    """The second full-suite pass: same tests, different clock.

    provider_lifecycle schedules effective-dated retirements, so a test written
    on the near side of one goes red at the announced minute on a pull request
    that did not touch it (CI run 31980690855, Wafer, 2026-08-17 00:00 UTC).
    The post-cutover job moves that failure onto the pull request that writes
    the test. It earns its runtime only if it actually moves the clock, so the
    override has to be there.
    """
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    jobs = yaml.safe_load(workflow)["jobs"]
    # The native emulator job runs focused server tests, not another full-suite
    # pass. Keep the duplicate-full-pass guard for every other job, including
    # future jobs, and verify the focused exception separately below.
    commands = "\n".join(
        step.get("run", "")
        for name, job in jobs.items() if name != "spanner-emulator"
        for step in job.get("steps", [])
    )

    assert commands.count("uv run pytest") == 2
    for name in ("test", "test-post-cutover"):
        assert sum(step.get("run", "").count("uv run pytest") for step in jobs[name]["steps"]) == 1
    cutover = "\n".join(step.get("run", "") for step in jobs["test-post-cutover"]["steps"])
    assert "TR_LIFECYCLE_CLOCK_OVERRIDE=" in cutover
    # Derived from _RETIREMENTS at run time, never a hard-coded date that would
    # silently stop being in the future.
    assert "latest_scheduled_cutover" in cutover


def test_native_spanner_job_runs_both_focused_server_gates() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    job = jobs["spanner-emulator"]
    assert job["env"]["TR_CONFORMANCE_EMULATOR_SCHEMA"] == "1"
    assert job["env"]["SPANNER_EMULATOR_HOST"] == "127.0.0.1:9010"
    assert job["services"]["spanner"]["image"] == "gcr.io/cloud-spanner-emulator/emulator@sha256:c6f3402f2599684f295a0fdefb6fbbbfb18a0e43e309ff5456ccb452a4570a79"
    assert "bigtable" not in job["services"]
    steps = [step for step in job["steps"] if "uv run pytest" in step.get("run", "")]
    assert len(steps) == 2
    assert "tests/conformance -k spanner-emulator" in steps[0]["run"]
    assert "tests/conformance/test_spanner_schema.py" in steps[1]["run"]
    assert "tests/conformance/test_spanner_sql_acceptance.py" in steps[1]["run"]
    assert "tests/conformance/test_spanner_schema_audit.py" in steps[1]["run"]
    assert steps[1]["if"] == "${{ !cancelled() }}"
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in job["steps"])


def test_provider_health_is_monitored_separately_from_release_correctness() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    monitor = (ROOT / ".github/workflows/provider-catalog-health.yml").read_text(
        encoding="utf-8",
    )
    deploy = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    refresh = (ROOT / ".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")
    assert ci.count('-m "not provider_health"') == 2
    # The price refresh validates its catalog like CI does: a provider delisting
    # one model must not stop every other provider's prices from publishing.
    assert refresh.count('-m "not provider_health"') == 1
    assert "schedule:" in monitor
    assert "workflow_dispatch:" in monitor
    assert "-m provider_health" in monitor
    # Every provider_health check, wherever it lives.
    assert "uv run pytest -q -m provider_health\n" in monitor
    assert "continue-on-error" not in monitor
    assert "provider-catalog-health.yml" not in deploy
    assert "--workflow ci.yml" in deploy
