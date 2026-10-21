from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_postgres_service_uses_digest_pinned_official_public_mirror() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    postgres = jobs["test"]["services"]["postgres"]
    assert postgres["image"] == (
        "mirror.gcr.io/library/postgres:17@sha256:"
        "2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"
    )
    assert "5432:5432" in postgres["ports"]
    assert "pg_isready" in postgres["options"]
    assert "continue-on-error" not in jobs["test"]


def test_ci_shard_matrices_cover_every_partition_with_bounded_jobs() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    for name in ("test", "test-post-cutover"):
        job = jobs[name]
        count = int(job["env"]["TEST_SHARDS"])
        assert job["strategy"]["matrix"]["shard"] == list(range(1, count + 1))
        assert job["timeout-minutes"] == 35
        assert job["strategy"]["fail-fast"] is False
        assert "continue-on-error" not in job
    assert jobs["coverage"]["needs"] == "test"


def test_ci_runs_python_suite_once_with_coverage() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    # Still one full coverage pass, now sharded. The focused P5 pass checks
    # live-object budgets that cannot safely collect in a mixed-suite worker.
    assert "-n 4 --dist loadgroup" in workflow
    assert "--cov=trusted_router" in workflow
    jobs = yaml.safe_load(workflow)["jobs"]
    full_commands = "\n".join(step.get("run", "") for name, job in jobs.items()
                              if name != "spanner-emulator" for step in job.get("steps", []))
    assert full_commands.count("--cov=trusted_router") == 1
    assert workflow.count("--cov=trusted_router") == 2

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


def test_ci_shards_record_durations_using_existing_split_weights() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    path = "durations-${{ matrix.shard }}.json"
    for job_name, clock in (("test", "default"), ("test-post-cutover", "post-cutover")):
        steps = jobs[job_name]["steps"]
        test_index = next(i for i, step in enumerate(steps)
                          if "uv run pytest" in step.get("run", ""))
        assert steps[test_index - 1]["run"] == f"cp .test_durations {path}"
        command = steps[test_index]["run"]
        assert f"--store-durations --durations-path={path} --clean-durations" in command
        assert "--splitting-algorithm least_duration" in command
        assert "--splits ${{ env.TEST_SHARDS }} --group ${{ matrix.shard }}" in command
        upload = next(step for step in steps
                      if step.get("with", {}).get("name")
                      == f"durations-{clock}-${{{{ matrix.shard }}}}")
        assert steps.index(upload) > test_index
        assert upload["if"] == "always()"
        assert upload["uses"] == "actions/upload-artifact@v4"
        assert upload["with"]["path"] == path
        assert upload["with"]["retention-days"] == 7


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
    # The native emulator and proof-oracle jobs run focused tests. Keep the
    # duplicate-full-pass guard for other jobs, including future additions,
    # and verify both focused exceptions separately below.
    commands = "\n".join(
        step.get("run", "")
        for name, job in jobs.items() if name not in {"spanner-emulator", "proof-oracle"}
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
    assert len(steps) == 3
    assert "tests/conformance -k spanner-emulator" in steps[0]["run"]
    assert "tests/conformance/test_spanner_schema.py" in steps[1]["run"]
    assert "tests/conformance/test_spanner_sql_acceptance.py" in steps[1]["run"]
    assert "tests/conformance/test_spanner_schema_audit.py" in steps[1]["run"]
    assert steps[1]["if"] == "${{ !cancelled() }}"
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in job["steps"])


def test_p5_object_budgets_run_in_a_fresh_package_only_session() -> None:
    import shlex

    from tests.shadow_v2_gate_cache import PACKAGE_MODULES

    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    step = next(step for step in jobs["spanner-emulator"]["steps"]
                if step.get("name") == "P5 package object budgets")
    assert shlex.split(step["run"]) == [
        "uv", "run", "pytest", "-q", "tests/test_async_settle_shadow_v2_gate*.py",
        "-m", "not proof_oracle", "-n", "2",
        "--cov=trusted_router", "--cov-report=", "--cov-fail-under=0",
    ]
    assert {path.name for path in ROOT.glob("tests/test_async_settle_shadow_v2_gate*.py")} == PACKAGE_MODULES
    assert step["if"] == "${{ !cancelled() }}"
    assert step["timeout-minutes"] == 12
    assert "continue-on-error" not in step


def test_provider_health_is_monitored_separately_from_release_correctness() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    monitor = (ROOT / ".github/workflows/provider-catalog-health.yml").read_text(
        encoding="utf-8",
    )
    deploy = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    refresh = (ROOT / ".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")
    assert ci.count('-m "not provider_health and not proof_oracle"') == 2
    # The price refresh runs no local suite: it publishes only after the exact
    # generated SHA passes ci.yml, whose shards exclude provider_health, so one
    # provider delisting a model cannot stop every other provider's prices.
    assert "uv run pytest" not in refresh
    assert "gh workflow run ci.yml" in refresh
    assert "schedule:" in monitor
    assert "workflow_dispatch:" in monitor
    assert "-m provider_health" in monitor
    # Every provider_health check, wherever it lives.
    assert "uv run pytest -q -m provider_health\n" in monitor
    assert "continue-on-error" not in monitor
    assert "provider-catalog-health.yml" not in deploy
    assert "--workflow ci.yml" in deploy


def test_proof_oracle_ci_split(request, tmp_path):
    import shlex

    import pytest

    from tests.proof_oracle_ci import check_proof_oracle_paths, proof_oracle_command

    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    job = jobs['proof-oracle']
    command = proof_oracle_command(job)
    assert command == ['uv', 'run', 'pytest', '-q', '-n', '4', '--dist', 'loadgroup',
                       '-m', 'proof_oracle', 'tests/test_async_settle_proof_oracle.py',
                       'tests/test_async_settle_shadow_v2_gate_window.py',
                       '--splits', '6', '--group', '${{', 'matrix.shard', '}}',
                       '--splitting-algorithm', 'least_duration']
    assert job['runs-on'] == 'ubuntu-24.04'
    assert job['timeout-minutes'] == 45
    assert job['strategy']['matrix'] == {'clock': ['default', 'post-cutover'], 'shard': [1, 2, 3, 4, 5, 6]}
    assert job['strategy']['fail-fast'] is False
    assert job['permissions'] == {'contents': 'read'}
    assert 'continue-on-error' not in job
    assert any(step.get('run') == 'uv sync --frozen' for step in job['steps'])
    assert all('google-github-actions/auth' not in step.get('uses', '') for step in job['steps'])
    assert all('continue-on-error' not in step for step in job['steps'])
    pin = next(step for step in job['steps'] if 'TR_LIFECYCLE_CLOCK_OVERRIDE=' in step.get('run', ''))
    existing_pin = next(step for step in jobs['test-post-cutover']['steps']
                        if 'TR_LIFECYCLE_CLOCK_OVERRIDE=' in step.get('run', ''))
    assert pin['run'] == existing_pin['run']
    assert pin['if'] == "matrix.clock == 'post-cutover'"
    for name in ('test', 'test-post-cutover'):
        commands = [shlex.split(step['run']) for step in jobs[name]['steps']
                    if 'uv run pytest' in step.get('run', '')]
        assert len(commands) == 1
        tokens = commands[0]
        assert tokens[tokens.index('-m') + 1] == 'not provider_health and not proof_oracle'
    check_proof_oracle_paths({item.path for item in request.session.items
                              if item.get_closest_marker('proof_oracle')})
    # Negative control: moving/adding a marked file cannot silently drop it.
    with pytest.raises(AssertionError, match='missing from dedicated CI job'):
        check_proof_oracle_paths({tmp_path / 'test_unrouted_oracle.py'})


def test_ci_and_production_use_the_same_python() -> None:
    # CI previously took whatever Python the runner image shipped; a 3.11 image
    # would silently skip PEP 695 cases that production (3.12) exercises.
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert pinned == "3.12"
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.splitlines()[0] == f"FROM python:{pinned}-slim-bookworm"
    refresh = (ROOT / ".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")
    assert f'python-version: "{pinned}"' in refresh

def test_every_ci_job_pins_the_ubuntu_24_04_runner_image():
    # ubuntu-latest moved to ubuntu-26.04 on 2026-10-20. On that image the bundle package's
    # tracemalloc guards measured ~7.95 MB against their 2 MB bound and the fastpath TLC model checks
    # failed, on main and on every PR; on 24.04 (system CPython 3.12.3, the interpreter the suite and
    # the local gates are calibrated on) they pass. Move the pin deliberately, re-measuring those guards.
    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    assert {name: job['runs-on'] for name, job in jobs.items()} == dict.fromkeys(jobs, 'ubuntu-24.04')


def test_ci_cancels_the_run_it_supersedes_on_a_branch_but_never_on_main():
    # 2026-10-20: 60 queued runs starved every main run's coverage job for hours; the deploy gate
    # needs its commit's run to conclude, so nothing shipped. One run per branch, newest wins.
    # Main is exempt: a later docs-only push starts no deploy, and cancelling the earlier commit's
    # run would make that commit's deploy gate refuse a valid rollout (review of #1921).
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    assert workflow['concurrency'] == {
        'group': "ci-${{ github.workflow }}-${{ github.ref }}-${{ github.ref == 'refs/heads/main' && github.sha || 'head' }}",
        'cancel-in-progress': "${{ github.ref != 'refs/heads/main' }}"}
