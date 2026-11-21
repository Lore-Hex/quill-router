from __future__ import annotations

import json
import os
import tomllib
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


# test-post-cutover's shards and their count: eight for the whole suite, one
# when the scope job selected the test files a change reaches.
POST_CUTOVER_SHARDS = ("${{ fromJSON(needs.scope.outputs.post_cutover_tests == '' "
                       "&& '[1, 2, 3, 4, 5, 6, 7, 8]' || '[1]') }}")
POST_CUTOVER_SHARD_COUNT = "${{ needs.scope.outputs.post_cutover_tests == '' && '8' || '1' }}"


def job_env(job: dict) -> str:
    return str(job["env"]["TEST_SHARDS"])


def test_ci_shard_matrices_cover_every_partition_with_bounded_jobs() -> None:
    import re

    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    job = jobs["test"]
    count = int(job["env"]["TEST_SHARDS"])
    assert job["strategy"]["matrix"]["shard"] == list(range(1, count + 1))
    post = jobs["test-post-cutover"]
    assert post["strategy"]["matrix"]["shard"] == POST_CUTOVER_SHARDS
    assert post["env"]["TEST_SHARDS"] == POST_CUTOVER_SHARD_COUNT
    # Each branch of the two expressions agrees: every partition, either way.
    for shards, count_text in zip(re.findall(r"'(\[[^']*\])'", POST_CUTOVER_SHARDS),
                                  re.findall(r"'(\d+)'", POST_CUTOVER_SHARD_COUNT), strict=True):
        assert json.loads(shards) == list(range(1, int(count_text) + 1))
    from scripts.ci.commit_scope import HEAVY_SHARDS

    heavy = jobs["test-heavy"]
    assert heavy["strategy"]["matrix"]["shard"] == list(range(1, HEAVY_SHARDS + 1))
    assert heavy["env"]["TEST_SHARDS"] == str(HEAVY_SHARDS)
    for job in (jobs["test"], post, heavy):
        assert job["timeout-minutes"] == 35
        assert job["strategy"]["fail-fast"] is False
        assert "continue-on-error" not in job
    assert jobs["coverage"]["needs"] == ["test", "test-heavy", "scope"]
    # The floor counts exactly the shards each matrix makes.
    assert jobs["coverage"]["env"] == {"TEST_SHARDS": job_env(jobs["test"]), "HEAVY_SHARDS": str(HEAVY_SHARDS)}


def test_the_post_cutover_suite_runs_the_selected_tests_from_the_environment() -> None:
    # The list reaches the command through the environment, never through an
    # expression in the script, and commit_scope.py lists only safe paths.
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    post = jobs["test-post-cutover"]
    assert post["env"]["POST_CUTOVER_TESTS"] == "${{ needs.scope.outputs.post_cutover_tests }}"
    step = next(step for step in post["steps"] if "uv run pytest" in step.get("run", ""))
    assert step["run"].endswith("--clean-durations ${POST_CUTOVER_TESTS}")
    assert "outputs.post_cutover_tests" not in json.dumps(post["steps"])
    assert jobs["scope"]["outputs"]["post_cutover_tests"] == "${{ steps.scope.outputs.post_cutover_tests }}"


def test_ci_runs_python_suite_once_with_coverage() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    # Still one full coverage pass, sharded, and split between the eight
    # shards and the heavy lane, which run disjoint files (each test once:
    # test_the_heavy_lane_and_the_shards_partition_the_suite). The focused P5
    # pass checks live-object budgets that cannot safely collect in a
    # mixed-suite worker.
    assert "-n 4 --dist loadgroup" in workflow
    assert "--cov=trusted_router" in workflow
    jobs = yaml.safe_load(workflow)["jobs"]
    full_commands = {name: "\n".join(step.get("run", "") for step in job.get("steps", []))
                     for name, job in jobs.items() if name != "spanner-emulator"}
    assert {name for name, text in full_commands.items() if "--cov=trusted_router" in text} == {"test", "test-heavy"}
    assert all(text.count("--cov=trusted_router") <= 1 for text in full_commands.values())
    assert workflow.count("--cov=trusted_router") == 3

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


def test_coverage_measures_lines_through_sys_monitoring() -> None:
    # Coverage measures lines through sys.monitoring, set once in
    # pyproject.toml's [tool.coverage.run]. Before Python 3.14 that core
    # cannot measure branches. Asked to (branch = true, or --cov-branch on a
    # command), coverage falls back to its C tracer with only a warning,
    # bringing back about three times the plain suite's cost. timid, an
    # environment COVERAGE_CORE or COVERAGE_RCFILE, or another --cov-config
    # would also displace the setting.
    run = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["coverage"]["run"]
    assert run["core"] == "sysmon"
    assert "timid" not in run
    version = tuple(int(part) for part in (ROOT / ".python-version").read_text().strip().split(".")[:2])
    if version < (3, 14):
        assert run["branch"] is False
    opts = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"].get("addopts", [])
    # TOML allows addopts as one string or a list.
    addopts = opts if isinstance(opts, str) else " ".join(opts)
    for token in ("--cov-branch", "--cov-config"):
        assert token not in addopts, f"pytest addopts name {token}"
    workflows = sorted([*(ROOT / ".github/workflows").glob("*.yml"), *(ROOT / ".github/workflows").glob("*.yaml")])
    for path in workflows:
        text = path.read_text()
        for token in ("--cov-branch", "--cov-config", "COVERAGE_CORE", "COVERAGE_RCFILE"):
            assert token not in text, f"{path.name} names {token}"


def test_coverage_from_the_repository_root_resolves_to_sys_monitoring(monkeypatch) -> None:
    # coverage.py reads the first configuration it finds: COVERAGE_RCFILE or
    # .coveragerc, then .coveragerc.toml, setup.cfg, tox.ini and pyproject.toml.
    # A file ahead of pyproject.toml, even one with only a [report] section,
    # replaces all of pyproject's settings without a warning, and the run
    # falls back to the C tracer. So ask coverage itself what a run started
    # from the root would use.
    import coverage

    for name in ("COVERAGE_CORE", "COVERAGE_RCFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(ROOT)
    cov = coverage.Coverage()
    # The file whose settings coverage used: a setup.cfg or tox.ini without a
    # coverage section is read but not used, and is no displacement.
    assert os.path.realpath(cov.config.config_file) == os.path.realpath(ROOT / "pyproject.toml")
    assert cov.get_option("run:core") == "sysmon"
    assert cov.get_option("run:timid") is False
    version = tuple(int(part) for part in (ROOT / ".python-version").read_text().strip().split(".")[:2])
    if version < (3, 14):
        assert cov.get_option("run:branch") is False


def test_ci_shards_record_durations_using_existing_split_weights() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    path = "durations-${{ matrix.shard }}.json"
    # The heavy lane splits by the same weights as the shards and uploads
    # under durations-default-*, the default profile merge_shard_durations.py
    # reads (its shards and the eight run disjoint tests).
    for job_name, clock, artifact in (("test", "default", "default"), ("test-post-cutover", "post-cutover", "post-cutover"),
                                      ("test-heavy", "default", "default-heavy")):
        steps = jobs[job_name]["steps"]
        test_index = next(i for i, step in enumerate(steps)
                          if "uv run pytest" in step.get("run", ""))
        profile = ".test_durations" if clock == "default" else ".test_durations-post-cutover"
        assert steps[test_index - 1]["run"] == f"cp {profile} {path}"
        command = steps[test_index]["run"]
        assert f"--store-durations --durations-path={path} --clean-durations" in command
        assert "--splitting-algorithm least_duration" in command
        assert "--splits ${{ env.TEST_SHARDS }} --group ${{ matrix.shard }}" in command
        upload = next(step for step in steps
                      if step.get("with", {}).get("name")
                      == f"durations-{artifact}-${{{{ matrix.shard }}}}")
        assert steps.index(upload) > test_index
        assert upload["if"] == "always()"
        assert upload["uses"] == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
        assert upload["with"]["path"] == path
        assert upload["with"]["retention-days"] == 7


def test_default_oracle_uses_its_own_weights_without_filtering_cases() -> None:
    from scripts.ci.commit_scope import ORACLE_INPUTS, ORACLE_TESTS

    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["proof-oracle"]
    steps = job["steps"]
    index = next(i for i, step in enumerate(steps) if "uv run pytest" in step.get("run", ""))
    path = "durations-${{ matrix.shard }}.json"
    assert steps[index - 1]["run"] == f"cp .test_durations-oracle {path}"
    assert ".test_durations-oracle" in ORACLE_INPUTS
    command = steps[index]["run"]
    assert f"-m proof_oracle {' '.join(ORACLE_TESTS)}" in command
    assert f"--splits {len(job['strategy']['matrix']['shard'])} --group ${{{{ matrix.shard }}}}" in command
    assert f"--durations-path={path}" in command


def test_the_heavy_lane_and_the_shards_partition_the_suite() -> None:
    # Every test of the default suite runs once at the default clock: the
    # eight shards ignore exactly the files the heavy lane runs, both lists
    # read from commit_scope.HEAVY_TESTS when the job runs, with the same
    # markers, workers, grouping, split weights and coverage.
    import shlex

    from scripts.ci.commit_scope import HEAVY_SHARDS, PATH_SCOPED_JOBS, path_condition

    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    test, heavy = jobs["test"], jobs["test-heavy"]

    def suite(job: dict) -> list[str]:
        return shlex.split(next(step["run"] for step in job["steps"] if "uv run pytest" in step.get("run", "")))

    def env_step(job: dict, variable: str) -> str:
        return next(step["run"] for step in job["steps"] if f"{variable}=" in step.get("run", ""))

    assert "--ignore" in env_step(test, "HEAVY_IGNORE")
    assert 'echo "HEAVY_IGNORE=${ignored}" >> "$GITHUB_ENV"' in env_step(test, "HEAVY_IGNORE")
    assert "ignored=$(python3 scripts/ci/commit_scope.py heavy-tests --ignore)" in env_step(test, "HEAVY_IGNORE")
    assert "files=$(python3 scripts/ci/commit_scope.py heavy-tests --files)" in env_step(heavy, "HEAVY_FILES")
    assert 'echo "HEAVY_FILES=${files}" >> "$GITHUB_ENV"' in env_step(heavy, "HEAVY_FILES")
    for job in (test, heavy):
        # set -e: a failed listing fails the step instead of passing nothing.
        assert next(step["run"] for step in job["steps"] if "heavy-tests" in step.get("run", "")).startswith(
            "set -euo pipefail\n")
    shards, lane = suite(test), suite(heavy)
    assert "${HEAVY_IGNORE}" in shards and shards.count("${HEAVY_IGNORE}") == 1
    assert lane[-1] == "${HEAVY_FILES}" and not any(arg.startswith("tests/") for arg in lane)
    common = ["uv", "run", "pytest", "-q", "-n", "4", "--dist", "loadgroup",
              "-m", "not provider_health and not proof_oracle"]
    assert shards[:len(common)] == common and lane[:len(common)] == common
    for args in (shards, lane):
        assert "--splitting-algorithm" in args and args[args.index("--splitting-algorithm") + 1] == "least_duration"
        assert "--cov=trusted_router" in args and "--cov-report=" in args
        assert not any(arg.startswith("--cov-fail-under") for arg in args)
    assert heavy["needs"] == "scope" and heavy["if"] == path_condition(PATH_SCOPED_JOBS["test-heavy"])
    assert heavy["env"] == {"TEST_SHARDS": str(HEAVY_SHARDS)}
    # No databases or enclave source: none of the lane's files reads them.
    assert "services" not in heavy
    assert not any("quill-cloud-proxy" in json.dumps(step) for step in heavy["steps"])
    # The same checkout as the shards (a test that needs history skips alike).
    assert next(s for s in heavy["steps"] if "checkout" in s.get("uses", "")) == next(
        s for s in test["steps"] if "checkout" in s.get("uses", ""))
    uploads = {step["with"]["name"]: step for step in heavy["steps"] if "upload-artifact" in step.get("uses", "")}
    assert set(uploads) == {"durations-default-heavy-${{ matrix.shard }}", "coverage-heavy-${{ matrix.shard }}",
                            "test-failures-heavy-${{ matrix.shard }}"}
    # Its failure record, as the shards upload theirs: after the tests,
    # whatever their outcome, bounded, unable to fail the job, and named so
    # testmap.py's compare counts it against the lane's own result.
    from scripts.ci.testmap import HEAVY_FAILURES

    failures = uploads["test-failures-heavy-${{ matrix.shard }}"]
    shard_upload = next(s for s in test["steps"] if s.get("with", {}).get("name") == "test-failures-${{ matrix.shard }}")
    assert {k: v for k, v in failures.items() if k != "with"} == {k: v for k, v in shard_upload.items() if k != "with"}
    assert {**failures["with"], "name": None} == {**shard_upload["with"], "name": None}
    assert failures["with"]["name"].startswith(HEAVY_FAILURES)
    tests_step = next(s for s in heavy["steps"] if "uv run pytest" in s.get("run", ""))
    assert heavy["steps"].index(failures) > heavy["steps"].index(tests_step)
    assert uploads["coverage-heavy-${{ matrix.shard }}"]["with"]["path"] == "coverage-heavy-${{ matrix.shard }}.dat"
    assert any(step.get("run") == "mv .coverage coverage-heavy-${{ matrix.shard }}.dat" for step in heavy["steps"])
    # The coverage job downloads every coverage-* artifact, the lane's among them.
    download = next(s for s in jobs["coverage"]["steps"] if "download-artifact" in s.get("uses", ""))
    assert download["with"] == {"pattern": "coverage-*", "path": "shards"}


def test_the_heavy_tests_command_lists_the_lane(monkeypatch, capsys, tmp_path) -> None:
    from scripts.ci.commit_scope import HEAVY_TESTS, is_test_file, main

    assert list(HEAVY_TESTS) == sorted(set(HEAVY_TESTS))
    assert all(is_test_file(path) and (ROOT / path).is_file() for path in HEAVY_TESTS)
    monkeypatch.chdir(ROOT)
    assert main(["heavy-tests", "--ignore"]) == 0
    assert capsys.readouterr().out == " ".join(f"--ignore={path}" for path in HEAVY_TESTS) + "\n"
    assert main(["heavy-tests", "--files"]) == 0
    assert capsys.readouterr().out == " ".join(HEAVY_TESTS) + "\n"
    # Nothing to run is an error: no file arguments would run the whole suite.
    monkeypatch.chdir(tmp_path)
    assert main(["heavy-tests", "--files"]) == 1
    assert "none of commit_scope.HEAVY_TESTS exists" in capsys.readouterr().err


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

    # The default clock in two jobs that partition the suite (the eight
    # shards and the heavy lane), the post-cutover clock in one.
    assert commands.count("uv run pytest") == 3
    for name in ("test", "test-heavy", "test-post-cutover"):
        assert sum(step.get("run", "").count("uv run pytest") for step in jobs[name]["steps"]) == 1
    cutover = "\n".join(step.get("run", "") for step in jobs["test-post-cutover"]["steps"])
    assert "TR_LIFECYCLE_CLOCK_OVERRIDE=" in cutover
    # Derived from _RETIREMENTS at run time, never a hard-coded date that would
    # silently stop being in the future.
    assert "latest_scheduled_cutover" in cutover


def test_native_spanner_job_runs_both_focused_server_gates() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    job = jobs["spanner-emulator"]
    assert job["timeout-minutes"] == 40
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
    assert step["timeout-minutes"] == 22
    assert "continue-on-error" not in step


def test_provider_health_is_monitored_separately_from_release_correctness() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    monitor = (ROOT / ".github/workflows/provider-catalog-health.yml").read_text(
        encoding="utf-8",
    )
    deploy = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    refresh = (ROOT / ".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")
    assert ci.count('-m "not provider_health and not proof_oracle"') == 3
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


# proof-oracle's clock in ci.yml: the default one only. The on-demand
# workflow runs the post-cutover clock (tests/test_async_settle_proof_oracle.py
# holds that no endpoint the oracle settles changes between the two).
ORACLE_CLOCKS = ["default"]


def test_heavy_jobs_have_no_schedule_and_the_post_cutover_clock_runs_on_demand() -> None:
    # No scheduled run: ci.yml runs each heavy job on every event whose
    # changes reach it, pushes to main included, so no job skips because of
    # the event. post-cutover-daily.yml keeps the post-cutover suite, the
    # proof oracle's post-cutover clock and the model checks available by hand.
    import copy

    from scripts.ci.commit_scope import PUSH_SKIP_CONDITION, SKIPPED_ON_PUSH

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    ci = workflow["jobs"]
    # Only the post-cutover suite skips by event: on a push to main.
    assert PUSH_SKIP_CONDITION == "github.event_name != 'push'"
    assert {name for name, job in ci.items() if "github.event_name" in str(job.get("if", ""))} == SKIPPED_ON_PUSH
    assert all(str(ci[name]["if"]).startswith(f"{PUSH_SKIP_CONDITION} && ") for name in SKIPPED_ON_PUSH)
    assert ci["proof-oracle"]["strategy"]["matrix"]["clock"] == ORACLE_CLOCKS
    # The daily jobs inherit nothing from ci.yml's top level.
    assert "env" not in workflow and "defaults" not in workflow
    daily = yaml.safe_load((ROOT / ".github/workflows/post-cutover-daily.yml").read_text())
    # PyYAML reads the key `on` as True.
    triggers = daily.get("on", daily.get(True))
    # Dispatch only: no schedule, and no push or pull_request trigger. Not
    # named CI: deploy.yml's gate (`gh run list --workflow ci.yml`), the fast
    # path release (a workflow_run of "CI") and the price refresh read
    # ci.yml's runs only.
    assert set(triggers) == {"workflow_dispatch"}
    assert daily["name"] != "CI"
    assert daily["permissions"] == {"contents": "read"}
    jobs = daily["jobs"]
    assert set(jobs) == {"test-post-cutover", "proof-oracle", "proofs"}
    def comparable(job: dict) -> dict:
        # Everything but when the job runs and the oracle's clocks. ci.yml's
        # post-cutover job takes the whole suite's values when the scope job
        # selected no test files, and the on-demand job always has them.
        job = copy.deepcopy(job)
        job.pop("if", None)
        job.pop("needs", None)
        job.get("strategy", {}).get("matrix", {}).pop("clock", None)
        whole = {POST_CUTOVER_SHARDS: [1, 2, 3, 4, 5, 6, 7, 8], POST_CUTOVER_SHARD_COUNT: "8",
                 "${{ needs.scope.outputs.post_cutover_tests }}": ""}
        matrix = job.get("strategy", {}).get("matrix", {})
        if isinstance(matrix.get("shard"), str):
            matrix["shard"] = whole.get(matrix["shard"], matrix["shard"])
        for key, value in job.get("env", {}).items():
            if isinstance(value, str):
                job["env"][key] = whole.get(value, value)
        return job

    for name in jobs:
        # The same jobs (steps, env, services, runner, timeout, permissions,
        # matrix), so the on-demand run checks what a pull request's run
        # checks, and nothing can let it pass without running.
        assert comparable(jobs[name]) == comparable(ci[name]), name
        assert jobs[name]["if"] == "github.repository == 'Lore-Hex/quill-router'", name
        assert "needs" not in jobs[name], name
        # No needs context exists in the on-demand run, so nothing may read one.
        assert "needs." not in json.dumps(jobs[name]), name
        assert "continue-on-error" not in jobs[name], name
        assert all("continue-on-error" not in step for step in jobs[name]["steps"]), name
        assert "strategy" not in jobs[name] or jobs[name]["strategy"]["fail-fast"] is False, name
    assert jobs["proof-oracle"]["strategy"]["matrix"]["clock"] == ["post-cutover"]
    assert jobs["test-post-cutover"]["env"] == {"TEST_SHARDS": "8", "POST_CUTOVER_TESTS": ""}
    assert jobs["test-post-cutover"]["strategy"]["matrix"]["shard"] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_the_model_checks_run_check_sh_as_it_is() -> None:
    # The daily job is held to this one whole (above), so this pins both. A
    # run line that could not fail (`./proofs/check.sh || true`) or a
    # continue-on-error would pass every run whatever the specs say.
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["proofs"]
    steps = job["steps"]
    # Action names without their versions, which Dependabot moves.
    assert [str(step.get("uses", "")).split("@")[0] for step in steps] == [
        "actions/checkout", "actions/setup-java", ""]
    assert steps[-1]["run"] == "./proofs/check.sh"
    # Green runs took up to 26 minutes (ci.yml's comment). The required CI
    # summary needs this job, so reaching the limit holds a merge.
    assert job["timeout-minutes"] == 55
    # Nothing beside its name and command: an `if` on the step would skip it
    # and leave the job green, and a shell, env or working-directory could
    # change what the command does.
    assert set(steps[-1]) == {"name", "run"}
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in steps)


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
                       '--splitting-algorithm', 'least_duration', '--store-durations',
                       '--durations-path=durations-${{', 'matrix.shard', '}}.json', '--clean-durations']
    assert job['runs-on'] == 'ubuntu-24.04'
    assert job['timeout-minutes'] == 45
    assert job['strategy']['matrix'] == {'clock': ORACLE_CLOCKS, 'shard': [1, 2, 3, 4, 5, 6]}
    # The test files on its command are the roots commit_scope.py reads the
    # oracle's imports from.
    from scripts.ci.commit_scope import ORACLE_TESTS
    assert [arg for arg in command if arg.startswith('tests/')] == list(ORACLE_TESTS)
    assert job['strategy']['fail-fast'] is False
    assert job['permissions'] == {'contents': 'read'}
    assert 'continue-on-error' not in job
    assert any(step.get('run') == 'uv sync --frozen' for step in job['steps'])
    assert all('google-github-actions/auth' not in step.get('uses', '') for step in job['steps'])
    assert all('/.github/actions/gcp-auth@' not in step.get('uses', '') for step in job['steps'])
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
    short = {"scope", "coverage", "ci"}
    dedicated = ("${{ github.repository == 'Lore-Hex/quill-router' && github.ref == 'refs/heads/main' "
                 "&& (github.event_name == 'push' || github.event_name == 'workflow_dispatch') "
                 "&& 'tr-ci-short' || 'ubuntu-24.04' }}")
    assert short <= jobs.keys()
    for name, job in jobs.items():
        assert job['runs-on'] == (dedicated if name in short else 'ubuntu-24.04'), name


def test_ci_cancels_the_run_it_supersedes_on_a_branch_but_never_on_main():
    # 2026-10-20: 60 queued runs starved every main run's coverage job for hours; the deploy gate
    # needs its commit's run to conclude, so nothing shipped. One run per branch, newest wins.
    # Main is exempt: a later docs-only push starts no deploy, and cancelling the earlier commit's
    # run would make that commit's deploy gate refuse a valid rollout (review of #1921).
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    assert workflow['concurrency'] == {
        'group': "ci-${{ github.workflow }}-${{ github.ref }}-${{ github.ref == 'refs/heads/main' && github.sha || 'head' }}",
        'cancel-in-progress': "${{ github.ref != 'refs/heads/main' }}",
        'queue': "${{ github.ref == 'refs/heads/main' && 'max' || 'single' }}"}


def test_coverage_releases_cancelled_runs_without_accepting_partial_tests():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    coverage = jobs["coverage"]
    assert coverage["if"] == "${{ !cancelled() && needs.scope.outputs.data_only != 'true' }}"
    assert coverage["needs"] == ["test", "test-heavy", "scope"]
    assert "continue-on-error" not in coverage
    command = next(step["run"] for step in coverage["steps"]
                   if step.get("name") == "Combine and enforce the floor")
    assert '${{ needs.test.result }}' in command
    assert '!= "success"' in command
    assert "exit 1" in command
    assert "--fail-under=70" in command
    # The heavy lane's coverage counts when it ran; a skip (which the CI
    # summary accepts only with run_heavy=false) measures the eight alone,
    # and any other result fails the floor, as a failed shard does.
    flat = " ".join(command.split())
    assert 'case "${{ needs.test-heavy.result }}" in success) heavy="${HEAVY_SHARDS}" ;; skipped) heavy=0 ;; *)' in flat
    assert 'test "${shards}" -eq "${TEST_SHARDS}"' in command and 'test "${lane}" -eq "${heavy}"' in command
    assert "coverage-heavy-*) lane=$((lane + 1)) ;;" in command


ALWAYS_RUN_JOBS = {"scope", "lint", "production-import", "test", "browser", "ci"}


def test_data_only_commits_skip_only_the_jobs_that_read_no_catalog_data():
    # The hourly price refresh gates publication on this workflow's run of a
    # commit that touches only catalog data. On 2026-10-26 those 44-job runs
    # were among 19 queued runs while the organization's queue starved for two
    # hours. The scope job classifies the commit; a job skips only when its
    # `if` is the data-only condition, and every job that reads the data runs.
    from scripts.ci.commit_scope import (
        DATA_ONLY_CONDITION,
        PATH_SCOPED_JOBS,
        PUSH_SKIP_CONDITION,
        SKIPPED_FOR_DATA_ONLY,
        path_condition,
    )

    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    skipped = {name for name, job in jobs.items() if DATA_ONLY_CONDITION in str(job.get("if", ""))}
    assert skipped == SKIPPED_FOR_DATA_ONLY
    # The post-cutover suite reads the data: it is path-scoped, never skipped
    # for a data-only commit, and every data path sets its flag
    # (tests/test_commit_scope.py).
    assert set(jobs) - skipped == ALWAYS_RUN_JOBS | {"test-post-cutover", "test-heavy", "base-ci"}
    # base-ci runs only when the data-only skips rest on a base whose own
    # push run had not concluded (test_base_ci_*).
    assert jobs["base-ci"]["needs"] == "scope"
    assert jobs["base-ci"]["if"] == "needs.scope.outputs.base_pending == 'true'"
    for name in skipped:
        needs = jobs[name]["needs"]
        assert "scope" in ([needs] if isinstance(needs, str) else needs), name
        # `!cancelled()` would let a job run after a failed scope; coverage
        # keeps it (it reports beside a red shard) and guards with the
        # condition, every other job must skip when scope fails.
        if name in PATH_SCOPED_JOBS:
            assert jobs[name]["if"] == f"{DATA_ONLY_CONDITION} && {path_condition(PATH_SCOPED_JOBS[name])}", name
        elif name != "coverage":
            assert jobs[name]["if"] == DATA_ONLY_CONDITION, name
    for name in ALWAYS_RUN_JOBS:
        assert "if" not in jobs[name] or name == "ci", name
    assert jobs["test-post-cutover"]["needs"] == "scope"
    assert jobs["test-post-cutover"]["if"] == f"{PUSH_SKIP_CONDITION} && {path_condition('run_post_cutover')}"
    # The heavy lane reads the data too (all of src/): path-scoped only.
    assert jobs["test-heavy"]["needs"] == "scope"
    assert jobs["test-heavy"]["if"] == path_condition("run_heavy")
    # The jobs that read the published catalog: the suite under both clocks,
    # lint's catalog history check, the production import and the browser
    # smoke (which starts the app on the committed snapshot).
    commands = {name: "\n".join(step.get("run", "") for step in job.get("steps", []))
                for name, job in jobs.items()}
    assert {name for name, text in commands.items()
            if "not provider_health and not proof_oracle" in text} == {"test", "test-post-cutover", "test-heavy"}
    assert "update_model_changes.py --check" in commands["lint"]
    assert "import trusted_router.main" in commands["production-import"]
    assert "npm run test:browser" in commands["browser"]


def test_path_scoped_jobs_skip_only_on_their_own_flag():
    # Each heavy job that reads a known part of the tree carries one flag,
    # set by the scope job, in its `if`, and nothing else in ci.yml reads a
    # flag. summarize() accepts a job's skip only when that flag is `false`,
    # so a job and its condition must name the same flag.
    import re

    from scripts.ci.commit_scope import PATH_FLAGS, PATH_SCOPED_JOBS

    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    jobs = yaml.safe_load(workflow)["jobs"]
    readers = {name: re.findall(r"needs\.scope\.outputs\.(run_\w+)", str(job.get("if", "")))
               for name, job in jobs.items()}
    assert {name: flags for name, flags in readers.items() if flags} == {
        name: [flag] for name, flag in PATH_SCOPED_JOBS.items()}
    # Read only in an `if` (the summary reads the flags from NEEDS).
    assert set(re.findall(r"needs\.scope\.outputs\.(run_\w+)", workflow)) == set(PATH_FLAGS)
    assert workflow.count("needs.scope.outputs.run_") == len(PATH_SCOPED_JOBS)
    assert jobs["scope"]["outputs"] == {
        "data_only": "${{ steps.scope.outputs.data_only }}",
        "base": "${{ steps.scope.outputs.base }}",
        "base_pending": "${{ steps.scope.outputs.base_pending }}",
        "post_cutover_tests": "${{ steps.scope.outputs.post_cutover_tests }}",
        **{flag: f"${{{{ steps.scope.outputs.{flag} }}}}" for flag in PATH_FLAGS}}
    # Run unless the flag is exactly false: a missing output runs the job.
    for name, flag in PATH_SCOPED_JOBS.items():
        assert f"needs.scope.outputs.{flag} != 'false'" in jobs[name]["if"], name


def test_the_complete_matrix_is_one_dispatch_or_one_label_away():
    # A dispatch runs every job unless it asks for scope=paths, which only
    # the price refresh's publication gate does; a pull request labeled
    # ci:full runs every job. Adding a label starts no run: with
    # cancel-in-progress, a `labeled` trigger would cancel the pull
    # request's run on any label. The scope job reads the labels through the
    # API when it runs, so a push or a re-run after labeling honours it.
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["workflow_dispatch"] == {"inputs": {"scope": {
        "description": "full runs every job; paths skips a path-scoped job none of whose inputs changed",
        "type": "choice", "options": ["full", "paths"], "default": "full"}}}
    # GitHub's default activity types: opened, synchronize, reopened.
    assert triggers["pull_request"] == {"branches": ["main"]}
    assert "labeled" not in json.dumps(triggers)
    scope = workflow["jobs"]["scope"]
    assert scope["env"]["DISPATCH_SCOPE"] == "${{ inputs.scope }}"
    assert scope["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    # Not the event payload's labels, which a re-run would repeat.
    assert "github.event.pull_request.labels" not in json.dumps(scope)
    script = next(step for step in scope["steps"] if step.get("id") == "scope")["run"]
    flat = " ".join(script.split())
    assert ('if [ "${EVENT_NAME}" = workflow_dispatch ] && [ "${DISPATCH_SCOPE}" != paths ]; then'
            in script)
    assert ('elif [ "${EVENT_NAME}" = pull_request ]; then '
            "# The labels as they are now") in flat
    assert ('if ! labels=$(gh api "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels?per_page=100" '
            "\\ --jq '.[].name'); then full=\"the pull request's labels could not be read\"") in flat
    assert "elif grep -qxF 'ci:full' <<< \"${labels}\"; then" in script
    refresh = (ROOT / ".github/workflows/refresh-prices.yml").read_text()
    assert refresh.count("gh workflow run ci.yml") == 1
    dispatch = refresh[refresh.index("gh workflow run ci.yml"):]
    assert dispatch[:dispatch.index("echo")].split() == [
        "gh", "workflow", "run", "ci.yml", "\\", "--ref", '"${BRANCH}"', "\\",
        "--repo", '"${GITHUB_REPOSITORY}"', "\\", "-f", "scope=paths"]


def test_scope_classifies_the_commit_against_the_commit_it_supersedes():
    from scripts.ci.commit_scope import PATH_FLAGS

    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    scope = jobs["scope"]
    assert set(scope["outputs"]) == {"data_only", "base", "base_pending", "post_cutover_tests", *PATH_FLAGS}
    assert scope["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read"}
    assert "needs" not in scope
    step = next(step for step in scope["steps"] if step.get("id") == "scope")
    script = step["run"]
    assert 'push) base="${PUSH_BEFORE}" ;;' in script
    assert 'pull_request) base="${PR_BASE_SHA}" ;;' in script
    assert 'compare/main...${head}' in script and ".merge_base_commit.sha" in script
    assert scope["env"]["PUSH_BEFORE"] == "${{ github.event.before }}"
    assert scope["env"]["PR_BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    # No history is cloned: the base is fetched at depth 1 and the two trees
    # are compared. The classification is the committed script, not inline
    # shell, so tests/test_commit_scope.py can drive it.
    assert 'git fetch -q --depth=1 origin "${base}"' in script
    # --no-renames: a code file moved onto a data path must appear as the
    # deletion of its code path. --raw: modes and status reach the classifier.
    assert 'git -c core.quotePath=true diff --no-renames --raw "${base}" "${head}" > changed-paths.txt' in script
    assert 'if [ "$(python3 scripts/ci/commit_scope.py classify < changed-paths.txt)" = "true" ]' in script
    # The induction: data_only only when the base commit's own tree passed a
    # completed ci.yml run (push or dispatch; a pull_request run checks a
    # merge commit). The summary job makes that conclusion mean every job
    # that ran passed and every skip was legitimate.
    assert '"repos/${GITHUB_REPOSITORY}/actions/workflows/ci.yml/runs?head_sha=${base}&per_page=100"' in script
    validated = ('select(.status == "completed" and .conclusion == "success" and '
                 '(.event == "push" or .event == "workflow_dispatch"))')
    assert script.count(validated) == 2
    assert 'if [ "${validated:-0}" -gt 0 ]; then' in script
    # Twice: a validated base, or a dispatch whose base's own push run is
    # still running (base_pending, which base-ci then requires to succeed).
    assert script.count("data_only=true") == 2
    assert script.index("classify < changed-paths.txt") < script.index("data_only=true")
    assert script.count("base_pending=true") == 1
    assert ('[.workflow_runs[] | select(.status != "completed" and .event == "push")] | length'
            in script)
    assert 'if [ "${base_validated}" != true ] && [ "${EVENT_NAME}" = workflow_dispatch ]; then' in script
    assert script.count("data_only=false") == 1
    assert 'echo "data_only=${data_only}" >> "$GITHUB_OUTPUT"' in script
    # Every path that cannot be classified runs the complete matrix.
    for guard in ('[ -z "${base}" ]', '"0000000000000000000000000000000000000000"',
                  '[ "${base}" = "${head}" ]', "! git fetch"):
        assert guard in script, guard
    # The path-scoped jobs compare with REF: a pull request's base, or the
    # base when its own run passed, otherwise the newest of the latest
    # validated commits that is an ancestor of the base; none found, or REF
    # unfetchable, runs them all.
    assert ('if [ "${EVENT_NAME}" = pull_request ] || [ "${base_validated}" = true ] '
            '|| [ "${base_pending}" = true ]; then') in script
    assert '"repos/${GITHUB_REPOSITORY}/actions/workflows/ci.yml/runs?status=success&per_page=50"' in script
    assert '| .head_sha] | .[:10] | .[]' in script
    assert 'relation=$(gh api "repos/${GITHUB_REPOSITORY}/compare/${candidate}...${base}" --jq \'.status\' || true)' in script
    assert 'if [ "${relation}" = ahead ] || [ "${relation}" = identical ]; then' in script
    assert 'elif ! git fetch -q --depth=1 origin "${ref}"; then' in script
    assert 'python3 scripts/ci/commit_scope.py jobs --full < /dev/null | tee -a "$GITHUB_OUTPUT"' in script
    assert 'git -c core.quotePath=true diff --no-renames --raw "${ref}" "${head}" > scoped-paths.txt' in script
    # Two commands continue over a line: compare them with single spaces.
    flat = " ".join(script.split())
    # --text: a file .gitattributes marks binary still shows its lines;
    # no external diff driver or textconv rewrites them. Outside src/, all
    # of which runs the post-cutover suite whole.
    assert ("git -c core.quotePath=true diff --no-renames --no-color --text --no-ext-diff --no-textconv \\ "
            "-U0 \"${ref}\" \"${head}\" -- . ':(exclude)src' > scoped-lines.patch") in flat
    assert ('python3 scripts/ci/commit_scope.py jobs --event "${EVENT_NAME}" --lines scoped-lines.patch \\ '
            '< scoped-paths.txt | tee -a "$GITHUB_OUTPUT"') in flat
    assert "set -euo pipefail" in script


def test_summary_job_derives_the_run_conclusion_from_every_job():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    summary = jobs["ci"]
    assert summary["name"] == "CI"
    assert set(summary["needs"]) == set(jobs) - {"ci"}
    assert summary["if"] == "${{ !cancelled() }}"
    # NEEDS carries every job's result and the scope job's outputs (its
    # path-scoped flags among them).
    assert summary["env"] == {"NEEDS": "${{ toJSON(needs) }}",
                              "DATA_ONLY": "${{ needs.scope.outputs.data_only }}",
                              "EVENT_NAME": "${{ github.event_name }}"}
    assert [step["run"] for step in summary["steps"] if "run" in step] == [
        "python3 scripts/ci/commit_scope.py summarize"]
    assert "continue-on-error" not in summary


def test_data_only_paths_are_exactly_what_the_refresh_stages_minus_code_and_go_inputs():
    from scripts.ci.commit_scope import DATA_ONLY_PATHS

    workflow = yaml.safe_load((ROOT / ".github/workflows/refresh-prices.yml").read_text())
    step = next(step for step in workflow["jobs"]["refresh"]["steps"]
                if step.get("name") == "Commit, verify, and push if changed")
    script = step["run"]
    staged = script[script.index("git add \\"):script.index("if git diff --cached --quiet")]
    paths = staged.replace("\\", " ").split()[2:]
    assert len(paths) == 12
    # Parser code, Go alias vectors, and model/workload routing evidence
    # all require the complete matrix, even in a generated refresh commit.
    excluded = {"scripts/pricing/parsers/", "fastpath/testdata/model_alias_vectors_v1.json",
                "src/trusted_router/data/model_routing_evidence.json"}
    assert excluded <= set(paths)
    # The two staged directories admit only what the refresh writes in them:
    # one manifest per provider, and what scripts/generate_provider_og.py
    # writes (one PNG per provider and its manifest).
    og = (ROOT / "scripts/generate_provider_og.py").read_text()
    assert 'OUT_DIR = STATIC_DIR / "og" / "providers"' in og
    assert 'OUT_DIR / f"{facts.slug}.png"' in og
    assert 'MANIFEST_PATH = OUT_DIR / "manifest.json"' in og
    directory_patterns = {
        "src/trusted_router/data/provider_models/": {
            "src/trusted_router/data/provider_models/*.json"},
        "src/trusted_router/static/og/providers/": {
            "src/trusted_router/static/og/providers/*.png",
            "src/trusted_router/static/og/providers/manifest.json"},
    }
    expected: set[str] = set()
    for path in paths:
        if path in excluded:
            continue
        expected |= directory_patterns[path] if path.endswith("/") else {path}
    # Both ways: a pattern the refresh does not stage (frontend/src/*.ts) and
    # a staged file the classifier forgot both fail here.
    assert set(DATA_ONLY_PATHS) == expected
    assert len(DATA_ONLY_PATHS) == len(expected)


def test_required_checks_are_named_alike_everywhere() -> None:
    # main's required checks are written down in three places: the price
    # refresh posts one status per check before it pushes to main, the security
    # audit holds them as a minimum, and ci.yml produces them. A name missing
    # from the refresh's list stops every hourly push at the protected branch.
    import importlib.util
    import itertools
    import re

    refresh = (ROOT / ".github/workflows/refresh-prices.yml").read_text()
    loops = re.findall(r'^ *for context in ((?:"[^"\n]+" ?)+); do$', refresh, flags=re.MULTILINE)
    assert len(loops) == 1
    posted = re.findall(r'"([^"]+)"', loops[0])
    assert len(posted) == len(set(posted))

    spec = importlib.util.spec_from_file_location(
        "repo_security_coverage", ROOT / "scripts/security/check_repo_security_coverage.py")
    assert spec is not None and spec.loader is not None
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    assert set(posted) == audit.PROTECTED_REPOS["quill-router"]

    # The check names ci.yml's jobs report: a job's name, plus its matrix
    # values in parentheses. An axis written as an expression (the proof
    # oracle's clock list) is not a list here, so that job's names are not
    # derived; a required check from such a job would need its own expansion.
    produced = set()
    for key, job in yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"].items():
        name = job.get("name", key)
        matrix = {axis: values for axis, values in job.get("strategy", {}).get("matrix", {}).items()
                  if axis not in ("include", "exclude")}
        if not matrix:
            produced.add(name)
            continue
        if not all(isinstance(values, list) for values in matrix.values()):
            continue
        for combination in itertools.product(*matrix.values()):
            produced.add(f"{name} ({', '.join(str(value) for value in combination)})")
    assert set(posted) <= produced
    assert "CI" in posted


def test_a_passing_tests_temporary_directory_is_not_kept() -> None:
    # The deploy script tests copy the repository once per test, so a full run
    # writes tens of gigabytes under the system temporary directory. With
    # pytest's default every directory of the last three runs stays, and
    # several sessions running the suite on one machine fill the disk.
    options = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert options["tmp_path_retention_policy"] == "failed"
    assert options["tmp_path_retention_count"] == 3


# The per-test footprint map and the shadow selection (scripts/ci/testmap.py,
# scripts/ci/testmap_recorder.py, .github/workflows/testmap.yml). Shadow mode:
# nothing it produces decides what runs, and nothing it does can fail a ci.yml
# job or hold a deploy.


def _steps_named(job: dict, *words: str) -> list[dict]:
    return [step for step in job["steps"] if all(w.lower() in str(step.get("name", "")).lower() for w in words)]


def _commands(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_no_ci_yml_job_runs_a_test_under_the_recorder() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    jobs = yaml.safe_load(workflow)["jobs"]
    assert "TR_TESTMAP_OUT" not in _commands(workflow)
    assert "testmap_recorder" not in workflow
    # The only map steps are the coverage job's two report-only ones on a pull request.
    assert {name: [s.get("name") for s in job["steps"] if "testmap" in json.dumps(s)]
            for name, job in jobs.items() if "testmap" in json.dumps(job.get("steps", []))} == {
        "coverage": ["Shadow test selection (report only)", "Failures outside the shadow selection (report only)"]}
    # tests/conftest.py loads the recorder under that variable only, before
    # any application import.
    conftest = (ROOT / "tests/conftest.py").read_text()
    guard = 'if os.environ.get("TR_TESTMAP_OUT"):\n    import scripts.ci.testmap_recorder'
    assert conftest.count(guard) == 1
    assert conftest.index(guard) < conftest.index("import tests.lifecycle_freeze") < conftest.index("from trusted_router")


def test_the_shadow_selection_is_report_only_and_off_the_scope_job() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    scope, coverage, test = jobs["scope"], jobs["coverage"], jobs["test"]
    # Nothing waits on it: not in scope, which every path-scoped job needs.
    assert "testmap" not in json.dumps(scope)
    shadow = _steps_named(coverage, "shadow")
    assert [s["name"] for s in shadow] == ["Shadow test selection (report only)",
                                           "Failures outside the shadow selection (report only)",
                                           "Upload the shadow test selection"]
    for step in shadow:
        assert step["if"] == "github.event_name == 'pull_request'" and step["continue-on-error"] is True
        assert step["timeout-minutes"] == 5
        assert "id" not in step
    assert "testmap.py shadow" in shadow[0]["run"] and '--base "${PR_BASE_SHA}"' in shadow[0]["run"]
    assert shadow[0]["env"]["PR_BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert shadow[1]["run"] == ("python3 scripts/ci/testmap.py compare --selection selection.json.gz"
                                " --failures test-failures")
    # The comparison reads the test shards' failure records, never their logs:
    # a red run with a missing record says "unavailable", not zero.
    # The heavy lane's records count against its own result (testmap.py
    # tells them apart by their artifacts' test-failures-heavy- names).
    assert shadow[1]["env"] == {"TEST_RESULT": "${{ needs.test.result }}",
                                "HEAVY_RESULT": "${{ needs.test-heavy.result }}"}
    collect = next(s for s in coverage["steps"] if s.get("name") == "Collect every shard's failed tests")
    assert collect["if"] == "github.event_name == 'pull_request'" and collect["continue-on-error"] is True
    assert collect["uses"] == "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
    assert collect["with"] == {"pattern": "test-failures-*", "path": "test-failures"}
    assert coverage["steps"].index(collect) < coverage["steps"].index(shadow[1])
    # One step of the test job uploads the failure record: after the tests,
    # whatever their outcome, bounded, and unable to fail the job.
    uploads = [s for s in test["steps"] if "test-failures" in json.dumps(s)]
    assert len(uploads) == 1
    upload = uploads[0]
    tests_step = next(s for s in test["steps"] if "uv run pytest" in s.get("run", ""))
    assert test["steps"].index(upload) > test["steps"].index(tests_step)
    assert upload["if"] == "always()" and upload["continue-on-error"] is True
    assert upload["timeout-minutes"] == 5
    assert upload["with"]["name"] == "test-failures-${{ matrix.shard }}"
    assert upload["with"]["path"] == "${{ runner.temp }}/test-failures/failures.json"
    conftest = (ROOT / "tests/conftest.py").read_text()
    assert ('if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_TEMP"):\n'
            '    pytest_plugins = [*globals().get("pytest_plugins", []), "scripts.ci.failure_record"]') in conftest
    assert shadow[2]["with"]["name"] == "test-selection" and shadow[2]["with"]["path"] == "selection.json.gz"
    floor = next(s for s in coverage["steps"] if s.get("name") == "Combine and enforce the floor")
    assert all(coverage["steps"].index(s) < coverage["steps"].index(floor) for s in shadow)
    assert coverage["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read"}
    # What runs is unchanged: every shard, whatever the selection says.
    assert test["strategy"]["matrix"]["shard"] == list(range(1, int(test["env"]["TEST_SHARDS"]) + 1))
    assert "needs" not in test and "if" not in test


def _testmap() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/testmap.yml").read_text())


def test_the_map_has_its_own_workflow_that_nothing_gates_on() -> None:
    from scripts.ci import testmap

    workflow = _testmap()
    assert testmap.MAP_WORKFLOW == ".github/workflows/testmap.yml"
    assert workflow["name"] == "Test map" != "CI"
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "23 */3 * * *"}]
    assert workflow["concurrency"] == {"group": "testmap-${{ github.ref }}", "cancel-in-progress": False}
    assert workflow["permissions"] == {"contents": "read"}
    # No deploy gate, release script or required check can see it: they read
    # ci.yml's runs, the workflow named CI, or the required check names.
    readers = [path.relative_to(ROOT).as_posix()
               for path in [*(ROOT / ".github/workflows").glob("*.y*ml"), *(ROOT / "scripts").rglob("*.py"),
                            *(ROOT / "scripts").rglob("*.sh")]
               if "testmap.yml" in path.read_text(errors="replace") or "Test map" in path.read_text(errors="replace")]
    # ci.yml only names it where its pull request runs read the map (report only).
    assert sorted(readers) == [".github/workflows/ci.yml", ".github/workflows/testmap.yml",
                               "scripts/ci/testmap.py", "scripts/ci/testmap_recorder.py"]
    refresh = (ROOT / ".github/workflows/refresh-prices.yml").read_text()
    import re
    required = set(re.findall(r'"([^"]+)"', re.findall(r'for context in ((?:"[^"\n]+" ?)+); do', refresh)[0]))
    produced = {job.get("name", key) for key, job in workflow["jobs"].items()}
    produced |= {f"{job.get('name', key)} ({shard})" for key, job in workflow["jobs"].items()
                 for shard in job.get("strategy", {}).get("matrix", {}).get("shard", [])}
    assert not produced & required and not any(name in {"CI", "test", "lint"} for name in produced)


def test_every_map_job_and_step_is_bounded() -> None:
    for name, job in _testmap()["jobs"].items():
        assert isinstance(job["timeout-minutes"], int) and job["timeout-minutes"] <= 35, name
        for step in job["steps"]:
            assert isinstance(step.get("timeout-minutes"), int), (name, step.get("name", step.get("uses")))
    assert _testmap()["jobs"]["record"]["timeout-minutes"] == 35


def test_the_map_records_ci_yml_s_test_selection_in_eight_shards_and_merges_only_green() -> None:
    from scripts.ci import testmap

    jobs = _testmap()["jobs"]
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["test"]
    record = jobs["record"]
    assert jobs["changes"]["outputs"] == {"record": "${{ steps.decide.outputs.record }}"}
    decide = next(s for s in jobs["changes"]["steps"] if s.get("id") == "decide")
    assert "testmap.py needed" in decide["run"] and '>> "$GITHUB_OUTPUT"' not in decide["run"]
    assert 'tee -a "$GITHUB_OUTPUT"' in decide["run"]
    assert record["needs"] == "changes" and record["if"] == "needs.changes.outputs.record == 'true'"
    assert record["strategy"] == {"fail-fast": False, "matrix": {"shard": list(range(1, testmap.SHARDS + 1))}}
    assert record["env"] == ci["env"] and int(record["env"]["TEST_SHARDS"]) == testmap.SHARDS
    assert {name: {k: v for k, v in service.items()} for name, service in record["services"].items()} == {
        name: {k: v for k, v in service.items()} for name, service in ci["services"].items()}
    step = next(s for s in record["steps"] if "uv run pytest" in s.get("run", ""))
    assert step["env"] == {"TR_TESTMAP_OUT": "${{ runner.temp }}/testmap-raw"}
    assert '-m "not provider_health and not proof_oracle"' in step["run"]
    assert "--splits ${{ env.TEST_SHARDS }} --group ${{ matrix.shard }}" in step["run"]
    assert "--cov" not in step["run"] and "--store-durations" not in step["run"]
    upload = next(s for s in record["steps"] if s.get("with", {}).get("name", "").startswith("testmap-fragment"))
    assert upload["with"]["name"] == "testmap-fragment-${{ matrix.shard }}"
    merge = jobs["merge"]
    assert merge["needs"] == "record" and merge["if"] == "needs.record.result == 'success'"
    command = next(s["run"] for s in merge["steps"] if "testmap.py merge" in s.get("run", ""))
    assert f"--shards {testmap.SHARDS}" in command
    for flag in ('--commit "${GITHUB_SHA}"', '--ref "${GITHUB_REF}"', '--event "${GITHUB_EVENT_NAME}"',
                 '--run-id "${GITHUB_RUN_ID}"'):
        assert flag in command
    upload = next(s for s in merge["steps"] if s.get("with", {}).get("name") == testmap.ARTIFACT)
    assert upload["with"] == {"name": testmap.ARTIFACT, "path": "testmap.json.gz", "retention-days": 14}
    assert "continue-on-error" not in json.dumps(jobs)


def test_the_heavy_check_of_each_map_is_a_job_of_its_own_after_the_upload() -> None:
    # The heavy lane's drift check: each new map of main against the lane's
    # scope at the same commit. A red check names what the scope misses and
    # leaves the map usable (testmap.py's run_problem accepts a run whose only
    # failed job is this one, after the merge job that uploads the map). On
    # main it opens or updates the heavy lane's scope issue, and a pass closes
    # it, so the drift is seen without anyone watching this workflow.
    from scripts.ci import testmap

    workflow = _testmap()
    jobs = workflow["jobs"]
    merge, check = jobs["merge"], jobs["heavy-scope"]
    assert merge["name"] == testmap.MAP_JOB and check["name"] == testmap.HEAVY_CHECK_JOB
    # Main only: a map recorded on another branch is no map the selection
    # uses, and a run that skips this job keeps its own conclusion.
    assert check["needs"] == "merge"
    assert check["if"] == "needs.merge.result == 'success' && github.ref == 'refs/heads/main'"
    assert not any("testmap.py heavy" in step.get("run", "") for step in merge["steps"])
    download = next(step for step in check["steps"] if "download-artifact" in step.get("uses", ""))
    assert download["with"] == {"name": testmap.ARTIFACT}
    # Only this job may write issues; the workflow reads contents only.
    assert workflow["permissions"] == {"contents": "read"}
    assert check["permissions"] == {"contents": "read", "issues": "write"}
    # The token reaches the two issue steps only: not the check, which runs
    # the repository's code.
    assert "env" not in check
    # actions/checkout persists the job token in git config by default, where
    # the check could read it: this checkout must not.
    checkouts = [step for step in check["steps"] if "actions/checkout" in step.get("uses", "")]
    assert len(checkouts) == 1 and checkouts[0].get("with", {}).get("persist-credentials") is False
    runs = {step["name"]: step for step in check["steps"] if "run" in step}
    assert {name for name, step in runs.items() if "GH_TOKEN" in json.dumps(step)} == {
        "Open or update the heavy lane's scope issue", "Close the heavy lane's scope issue"}
    assert all(step.get("env") in (None, {"GH_TOKEN": "${{ github.token }}"}) for step in check["steps"])
    assert list(runs) == ["The heavy lane's scope covers what its tests ran",
                          "Open or update the heavy lane's scope issue", "Close the heavy lane's scope issue"]
    the_check, opens, closes = runs.values()
    assert the_check["run"] == "python3 scripts/ci/testmap.py heavy --map testmap.json.gz --report heavy-scope.md"
    assert the_check["id"] == "check" and "if" not in the_check
    assert opens["run"] == "python3 scripts/ci/testmap.py heavy-issue --outcome failure --report heavy-scope.md"
    assert opens["if"] == "failure() && steps.check.outcome != 'success'"
    assert closes["run"] == "python3 scripts/ci/testmap.py heavy-issue --outcome success"
    assert closes["if"] == "success()"
    # Nothing else may fail a run and keep its map.
    assert set(jobs) == {"changes", "record", "merge", "heavy-scope"}
