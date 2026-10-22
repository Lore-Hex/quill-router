"""A superseded deploy ships unless the run waiting to replace it can deploy at once."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/deploy.yml"


def _module():
    spec = importlib.util.spec_from_file_location(
        "superseded_deploy", ROOT / "scripts/deploy/superseded_deploy.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


superseded = _module()


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_the_script_and_the_trigger_list_the_same_deploy_paths() -> None:
    workflow = _workflow()
    # PyYAML reads the bare key `on` as the boolean True.
    trigger = workflow.get("on", workflow.get(True))
    assert tuple(trigger["push"]["paths"]) == superseded.DEPLOY_PATHS


def test_every_image_input_is_a_deploy_path() -> None:
    # A change to anything the image copies must start a deploy; otherwise a
    # superseded run would ship an image main no longer matches.
    sources: list[str] = []
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        parts = line.split()
        if parts and parts[0] == "COPY" and not any(part.startswith("--from") for part in parts):
            sources.extend(parts[1:-1])
    assert "src" in sources and "scripts/entrypoint.sh" in sources
    for source in sources:
        probe = source if (ROOT / source).is_file() else f"{source}/probe"
        assert superseded.touches_deploy_paths([probe]), source


@pytest.mark.parametrize(
    ("files", "relevant"),
    [
        (["docs/design/x.md"], False),
        (["tests/test_x.py", "clickhouse/023_x.sql"], False),
        (["src/trusted_router/main.py"], True),
        (["scripts/deploy/rollout.sh"], True),
        (["scripts/entrypoint.sh"], True),
        (["scripts/other_tool.sh"], False),
        (["frontend/src/app.ts"], True),
        (["frontend/package.json"], False),
        (["Dockerfile"], True),
        (["uv.lock"], True),
        ([".github/workflows/deploy.yml"], True),
        ([".github/workflows/ci.yml"], False),
        (["srcx/file.py"], False),
    ],
)
def test_touches_deploy_paths(files: list[str], relevant: bool) -> None:
    assert superseded.touches_deploy_paths(files) is relevant


def test_a_rename_counts_both_paths() -> None:
    compare = {
        "status": "ahead",
        "files": [{"filename": "docs/moved.py", "previous_filename": "src/trusted_router/moved.py"}],
    }
    assert superseded.changed_files(compare) == ["docs/moved.py", "src/trusted_router/moved.py"]
    assert superseded.may_deploy(compare) is False


@pytest.mark.parametrize(
    "compare",
    [
        {"status": "diverged", "files": [{"filename": "docs/x.md"}]},
        {"status": "behind", "files": []},
        {"status": "identical", "files": []},
        {"status": "ahead"},
        {"status": "ahead", "files": [{"filename": f"docs/{i}.md"} for i in range(300)]},
    ],
    ids=["diverged", "behind", "identical", "no-file-list", "truncated"],
)
def test_an_uncertain_comparison_never_deploys(compare: dict) -> None:
    assert superseded.changed_files(compare) is None
    assert superseded.may_deploy(compare) is False


def test_docs_only_changes_deploy() -> None:
    compare = {"status": "ahead", "files": [{"filename": "docs/a.md"}, {"filename": "tests/test_a.py"}]}
    assert superseded.may_deploy(compare) is True


def _fake_gh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    exit_code: int = 0,
    responses: dict[str, tuple[str, int]] | None = None,
) -> Path:
    """Put a fake `gh` on PATH and return its call log.

    `responses` maps a substring of the argv to (stdout, exit code); the first
    matching entry answers, and `stdout`/`exit_code` answer anything unmatched.
    The stdout `SLEEP` makes the fake hang for three seconds instead.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    table: dict[str, list[object]] = {key: list(value) for key, value in (responses or {}).items()}
    table[""] = [stdout, exit_code]
    (tmp_path / "gh.json").write_text(json.dumps(table))
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        'argv = " ".join(sys.argv[1:])\n'
        f'open({str(log)!r}, "a").write(argv + "\\n")\n'
        f'table = json.load(open({str(tmp_path / "gh.json")!r}))\n'
        "matches = [key for key in table if key and key in argv]\n"
        'out, code = table[matches[0] if matches else ""]\n'
        'if out == "SLEEP":\n'
        "    time.sleep(3)\n"
        "sys.stdout.write(out + '\\n')\n"
        "sys.exit(code)\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


SOURCE_MOVED = json.dumps({"status": "ahead", "files": [{"filename": "src/trusted_router/x.py"}]})
DESCENDS = json.dumps({"status": "ahead", "files": [{"filename": "src/trusted_router/x.py"}]})
THIS_RUN = {"databaseId": 1, "status": "in_progress", "headSha": "aaa", "headBranch": "main", "event": "push"}
# Newest first, as `gh run list` prints: the pending successor (a push run on main), then this run.
WAITING_RUN = json.dumps(
    [{"databaseId": 2, "status": "pending", "headSha": "bbb", "headBranch": "main", "event": "push"}, THIS_RUN]
)
# The three lookups a skip needs: has main moved on deploy paths, does the successor descend from BASE, is its CI green.
READY = {
    "compare/aaa...ccc": (SOURCE_MOVED, 0),
    "--workflow deploy.yml": (WAITING_RUN, 0),
    "compare/aaa...bbb": (DESCENDS, 0),
    "--workflow ci.yml --commit bbb": ('[{"status": "completed", "conclusion": "success"}]', 0),
}
ARGS = ["--repo", "o/r", "--base", "aaa", "--head", "ccc"]


def _ci(status: str, conclusion: str = "") -> str:
    return json.dumps([{"status": status, "conclusion": conclusion}])


def test_main_deploys_when_only_docs_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _fake_gh(
        tmp_path, monkeypatch, stdout=json.dumps({"status": "ahead", "files": [{"filename": "docs/a.md"}]})
    )
    assert superseded.main(["--repo", "Lore-Hex/quill-router", "--base", "aaa", "--head", "bbb"]) == 0
    assert log.read_text().strip() == "api repos/Lore-Hex/quill-router/compare/aaa...bbb"


def test_main_skips_when_the_waiting_main_push_run_has_green_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _fake_gh(tmp_path, monkeypatch, responses=READY)
    assert superseded.main(ARGS) == 1
    calls = log.read_text().splitlines()
    assert calls[0] == "api repos/o/r/compare/aaa...ccc"
    assert "run list --repo o/r --workflow deploy.yml" in calls[1]
    assert calls[2] == "api repos/o/r/compare/aaa...bbb"
    assert "run list --repo o/r --workflow ci.yml --commit bbb" in calls[3]


@pytest.mark.parametrize(
    "ci",
    [_ci("in_progress"), _ci("queued"), _ci("completed", "failure"), "[]"],
    ids=["running", "queued", "red", "no-ci-run"],
)
def test_main_deploys_when_the_waiting_run_cannot_deploy_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ci: str
) -> None:
    # The waiting run's gate would hold (or refuse) for an unknown time; shipping BASE now is progress.
    _fake_gh(tmp_path, monkeypatch, responses={**READY, "--workflow ci.yml --commit bbb": (ci, 0)})
    assert superseded.main(ARGS) == 0


@pytest.mark.parametrize("own_status", ["in_progress", "queued"])
def test_main_deploys_when_no_newer_run_is_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_status: str
) -> None:
    # The newer run was cancelled, or the listing shows only this run itself: skipping would leave main undeployed.
    runs = json.dumps([{**THIS_RUN, "status": own_status}])
    log = _fake_gh(tmp_path, monkeypatch, responses={**READY, "--workflow deploy.yml": (runs, 0)})
    assert superseded.main(ARGS) == 0
    assert "ci.yml" not in log.read_text()


@pytest.mark.parametrize(
    "waiting",
    [
        {"databaseId": 2, "status": "pending", "headSha": "bbb", "headBranch": "main", "event": "workflow_dispatch"},
        {"databaseId": 2, "status": "pending", "headSha": "bbb", "headBranch": "feature/x", "event": "push"},
    ],
    ids=["manual-dispatch", "side-branch"],
)
def test_main_deploys_when_the_waiting_run_cannot_advance_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waiting: dict
) -> None:
    # A dispatch (of any revision) or a side-branch run is not main's successor even with green CI.
    runs = json.dumps([waiting, THIS_RUN])
    log = _fake_gh(tmp_path, monkeypatch, responses={**READY, "--workflow deploy.yml": (runs, 0)})
    assert superseded.main(ARGS) == 0
    assert "ci.yml" not in log.read_text()


@pytest.mark.parametrize("status", ["diverged", "behind", "identical"])
def test_main_deploys_when_the_waiting_commit_does_not_descend_from_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    lineage = json.dumps({"status": status, "files": []})
    log = _fake_gh(tmp_path, monkeypatch, responses={**READY, "compare/aaa...bbb": (lineage, 0)})
    assert superseded.main(ARGS) == 0
    assert "ci.yml" not in log.read_text()


@pytest.mark.parametrize(
    "failing",
    ["--workflow deploy.yml", "compare/aaa...bbb", "--workflow ci.yml --commit bbb"],
    ids=["run-list-fails", "lineage-fails", "ci-lookup-fails"],
)
def test_main_deploys_when_a_successor_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    # "Unknown" reads as "not known to be ready": BASE ships at the cost of at most one extra rollout.
    _fake_gh(tmp_path, monkeypatch, responses={**READY, failing: ("", 1)})
    assert superseded.main(ARGS) == 0


@pytest.mark.parametrize(
    "hanging",
    ["--workflow deploy.yml", "compare/aaa...bbb", "--workflow ci.yml --commit bbb"],
    ids=["run-list-hangs", "lineage-hangs", "ci-lookup-hangs"],
)
def test_main_deploys_when_a_successor_lookup_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hanging: str, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(superseded, "GH_TIMEOUT_SECONDS", 0.3)
    _fake_gh(tmp_path, monkeypatch, responses={**READY, hanging: ("SLEEP", 0)})
    assert superseded.main(ARGS) == 0
    assert "timed out after 0.3 s" in capsys.readouterr().err


def test_main_skips_when_the_comparison_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The compare decides whether BASE is even on main; unknown there never deploys.
    monkeypatch.setattr(superseded, "GH_TIMEOUT_SECONDS", 0.3)
    _fake_gh(tmp_path, monkeypatch, responses={"compare/aaa...ccc": ("SLEEP", 0)})
    assert superseded.main(ARGS) == 1
    assert "timed out" in capsys.readouterr().err


@pytest.mark.parametrize(("status", "exit_code"), [("identical", 0), ("ahead", 0), ("behind", 1), ("diverged", 1)])
def test_ancestry_mode_deploys_only_while_base_is_on_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, exit_code: int
) -> None:
    compare = json.dumps({"status": status, "files": [{"filename": "src/trusted_router/x.py"}]})
    log = _fake_gh(tmp_path, monkeypatch, responses={"compare/aaa...ccc": (compare, 0)})
    assert superseded.main(["--mode", "ancestry", *ARGS]) == exit_code
    # Ancestry asks one question of one endpoint; it never consults runs or CI.
    assert log.read_text().strip() == "api repos/o/r/compare/aaa...ccc"


@pytest.mark.parametrize(("stdout", "exit_code"), [("", 1), ("not json", 0), ("SLEEP", 0)], ids=["gh-fails", "garbage", "hangs"])
def test_ancestry_mode_never_deploys_on_an_unknown_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, exit_code: int
) -> None:
    monkeypatch.setattr(superseded, "GH_TIMEOUT_SECONDS", 0.3)
    _fake_gh(tmp_path, monkeypatch, stdout=stdout, exit_code=exit_code)
    assert superseded.main(["--mode", "ancestry", *ARGS]) == 1


def test_base_on_main_reads_the_compare_status() -> None:
    assert superseded.base_on_main({"status": "identical"}) is True
    assert superseded.base_on_main({"status": "ahead"}) is True
    for compare in ({"status": "behind"}, {"status": "diverged"}, {}, None, "ahead"):
        assert superseded.base_on_main(compare) is False, compare


def _run(sha: str, status: str = "pending", branch: str = "main", event: str = "push") -> dict:
    return {"status": status, "headSha": sha, "headBranch": branch, "event": event}


def test_pending_successor_is_the_newest_waiting_main_push_other_than_this_run() -> None:
    runs = [_run("ccc"), _run("bbb", status="queued"), _run("aaa", status="in_progress")]
    assert superseded.pending_successor(runs, base="aaa") == "ccc"
    assert superseded.pending_successor([_run("zzz", status="completed")], base="aaa") is None
    assert superseded.pending_successor([_run("aaa", status="in_progress")], base="aaa") is None
    # GitHub may list the calling run as queued while this job runs; it is never its own successor.
    assert superseded.pending_successor([_run("aaa", status="queued")], base="aaa") is None
    # A dispatch or a side-branch run cannot advance main; the next eligible run still counts.
    assert superseded.pending_successor([_run("ddd", event="workflow_dispatch"), _run("ccc")], base="aaa") == "ccc"
    assert superseded.pending_successor([_run("ddd", branch="feature/x")], base="aaa") is None
    assert superseded.pending_successor({"not": "a list"}, base="aaa") is None


def test_ci_green_needs_a_completed_successful_run() -> None:
    assert superseded.ci_green([{"status": "completed", "conclusion": "success"}]) is True
    for runs in ([], [{"status": "in_progress", "conclusion": ""}], [{"status": "completed", "conclusion": "failure"}], "x"):
        assert superseded.ci_green(runs) is False, runs


@pytest.mark.parametrize(("stdout", "exit_code"), [("", 1), ("not json", 0)], ids=["gh-fails", "garbage"])
def test_main_skips_when_the_comparison_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, exit_code: int
) -> None:
    _fake_gh(tmp_path, monkeypatch, stdout=stdout, exit_code=exit_code)
    assert superseded.main(["--repo", "o/r", "--base", "aaa", "--head", "bbb"]) == 1


def test_the_workflow_asks_the_script_before_skipping() -> None:
    job = _workflow()["jobs"]["confirm-current-main"]
    checkout = job["steps"][0]
    assert checkout["uses"] == "actions/checkout@v7"
    assert checkout["with"]["sparse-checkout"] == "scripts/deploy/superseded_deploy.py"
    run = job["steps"][1]["run"]
    current = run.index('if [ "${latest_sha}" = "${GITHUB_SHA}" ]')
    script = run.index("python3 scripts/deploy/superseded_deploy.py")
    skip = run.index('echo "proceed=false" >> "$GITHUB_OUTPUT"')
    assert current < script < skip
    assert '--base "${GITHUB_SHA}" --head "${latest_sha}"' in run


def test_the_check_runs_before_the_lease_and_before_every_production_mutation() -> None:
    jobs = _workflow()["jobs"]
    # The check holds nothing: it needs CI and the image, never the lease.
    assert set(jobs["confirm-current-main"]["needs"]) <= {"gate-on-ci", "build-image"}
    admit = jobs["admit-cloud"]
    assert "confirm-current-main" in admit["needs"]
    assert "needs.confirm-current-main.outputs.proceed == 'true'" in admit["if"]
    # Everything that mutates production sits behind the lease, so a skipped
    # run migrates no schema, rotates no secret and ships no revision.
    for name in ("migrate-schema", "sync-runtime-secrets", "deploy"):
        assert "admit-cloud" in jobs[name]["needs"], name
    assert "needs.confirm-current-main.outputs.proceed == 'true'" in jobs["deploy"]["if"]


def test_ancestry_is_rechecked_after_admission_without_giving_up_the_lease() -> None:
    jobs = _workflow()["jobs"]
    still = jobs["confirm-still-on-main"]
    # After the lease and the pre-deploy mutations, right where the old check stood.
    assert {"admit-cloud", "migrate-schema", "sync-runtime-secrets"} <= set(still["needs"])
    run = still["steps"][1]["run"]
    assert "superseded_deploy.py --mode ancestry" in run
    assert '--base "${GITHUB_SHA}" --head "${latest_sha}"' in run
    assert 'echo "on_main=false" >> "$GITHUB_OUTPUT"' in run
    assert "confirm-still-on-main" in jobs["deploy"]["needs"]
    assert "needs.confirm-still-on-main.outputs.on_main == 'true'" in jobs["deploy"]["if"]
    # A refusal here must still release the lease: finalize-cloud waits for this job and runs on always().
    final = jobs["finalize-cloud"]
    assert "confirm-still-on-main" in final["needs"]
    assert "always()" in final["if"] and "needs.admit-cloud.result == 'success'" in final["if"]


def test_the_admit_wait_outlasts_a_gateway_rollout() -> None:
    job = _workflow()["jobs"]["admit-cloud"]
    acquire = next(step for step in job["steps"] if step.get("id") == "acquire_mutex")
    wait = int(acquire["env"]["TR_DEPLOY_WAIT_SECONDS"])
    # Gateway rollouts held the gcp lease for about 2 h 20 min (2026-10-01/02);
    # with trust-page verification and a runner-queue wait before the release,
    # one hold reached 2 h 56 min (2026-10-21/22).
    assert wait >= 4 * 3600
    # The job must not time out before the wait does, and the lease must last
    # longer than the job that holds it.
    assert job["timeout-minutes"] * 60 > wait
    assert int(acquire["env"]["TR_DEPLOY_MUTEX_TTL_SECONDS"]) > job["timeout-minutes"] * 60


@pytest.mark.parametrize(
    "workflow",
    [
        ".github/workflows/deploy.yml",
        ".github/workflows/deploy-aws-control-plane.yml",
        ".github/workflows/deploy-azure-control-plane.yml",
    ],
)
def test_every_workflow_wait_passes_the_coordinators_own_check(workflow: str) -> None:
    from scripts.deploy import cloud_rollout

    text = yaml.safe_load((ROOT / workflow).read_text())
    waits = [
        env["TR_DEPLOY_WAIT_SECONDS"]
        for job in text["jobs"].values()
        for env in [job.get("env", {})] + [step.get("env", {}) for step in job.get("steps", [])]
        if "TR_DEPLOY_WAIT_SECONDS" in env
    ]
    assert waits, workflow
    for wait in waits:
        # The value the coordinator will read; a wait it refuses fails every
        # deploy at admission, with no competing lease at all.
        assert cloud_rollout.admission_wait({"TR_DEPLOY_WAIT_SECONDS": wait}) == int(wait)


def test_no_other_workflow_can_evict_a_pending_deploy() -> None:
    # GitHub keeps one pending run per concurrency group and cancels the older
    # one when another arrives. Only deploys may queue behind a deploy, so the
    # pending one is always the newest deploy.
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        text = yaml.safe_load(path.read_text())
        groups = [text.get("concurrency")] + [job.get("concurrency") for job in text["jobs"].values()]
        names = [g["group"] if isinstance(g, dict) else g for g in groups if g]
        if path.name == "deploy.yml":
            assert names == ["deploy-trusted-router"]
        else:
            assert "deploy-trusted-router" not in names, path.name
