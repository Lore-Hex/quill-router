from __future__ import annotations

import copy
import io
import json
import subprocess
import zipfile
from dataclasses import asdict
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest
import yaml

from scripts.deploy import release_train as train
from scripts.deploy import superseded_deploy

ROOT = Path(__file__).resolve().parents[1]
A, B, C, D = (letter * 40 for letter in "abcd")


def release(sha=A, run_id=1, attempt=1):
    return train.Release(sha, sha[:9], train.IMAGE + sha[0] * 64, run_id, attempt)


def run(sha=A, run_id=1, **changes):
    return {"id": run_id, "head_sha": sha, "head_branch": "main", "event": "push",
            "run_attempt": 1, "status": "pending", "conclusion": None,
            "repository": {"full_name": train.REPO}, "path": ".github/workflows/deploy.yml", **changes}


class Evidence(train.GitHub):
    def __init__(self):
        super().__init__()
        self.head = D
        self.order = [A, B, C, D]
        self.runs = {1: run(A, 1), 2: run(B, 2), 3: run(C, 3)}
        self.job_rows = {i: [{"name": n, "conclusion": "success"}
                             for n in ("gate-on-ci", "build-image")] for i in self.runs}
        self.green = {A, B, C}
        self.incompatible = set()
        self.artifacts = {}
        self.after_refresh = None

    def json(self, *args):
        if args == ("api", f"repos/{train.REPO}/git/ref/heads/main"):
            return {"object": {"sha": self.head}}
        assert args[:2] == ("run", "list")
        return [{"databaseId": i, "status": r["status"], "conclusion": r["conclusion"],
                 "headSha": r["head_sha"], "headBranch": r["head_branch"], "event": r["event"],
                 "displayTitle": r.get("display_title", "")} for i, r in reversed(self.runs.items())]

    def run(self, run_id):
        return copy.deepcopy(self.runs[run_id])

    def jobs(self, run_id):
        return copy.deepcopy(self.job_rows[run_id])

    def relation(self, base, head):
        if base not in self.order or head not in self.order:
            return "diverged"
        left, right = self.order.index(base), self.order.index(head)
        return "identical" if left == right else "ahead" if left < right else "behind"

    def ci(self, commit):
        train.require(commit in self.green, "selected commit does not have green CI")
        return [A, B, C, D].index(commit) + 100

    def compatible(self, base, candidate):
        return candidate not in self.incompatible

    def artifact(self, source, kind):
        if (source["id"], kind) in self.artifacts:
            raw = self.artifacts[source["id"], kind]
            if isinstance(raw, Exception):
                raise raw
            return copy.deepcopy(raw)
        assert kind == "prepared"
        return {"schema_version": 1, "release": asdict(release(source["head_sha"], source["id"]))}

    def refresh(self):
        if self.after_refresh:
            self.after_refresh()


class IncidentEvidence(Evidence):
    def __init__(self):
        super().__init__()
        self.head = A
        self.runs[1]["head_repository"] = {"full_name": train.REPO}
        self.runs[2] = run(A, 2, event="workflow_dispatch", display_title=f"Emergency main {A}")
        self.job_rows[1][1]["run_attempt"] = 1
        self.job_rows[1][1]["steps"] = [{"name": "Build image via Cloud Build", "conclusion": "success"}]
        self.has_push = True
        self.job_rows[2] = [
            {"name": "scope", "conclusion": "success",
             "steps": [{"name": "Validate incident promotion", "conclusion": "success"}]},
            {"name": "rollout / gate-on-ci", "status": "pending", "conclusion": None},
        ]

    def json(self, *args):
        if "deploy.yml/runs?" in args[-1]:
            assert f"head_sha={A}" in args[-1]
            source = 2 if "event=workflow_dispatch" in args[-1] else 1
            if source == 1 and not self.has_push:
                return {"total_count": 0, "workflow_runs": []}
            return {"total_count": 1, "workflow_runs": [self.run(source)]}
        return super().json(*args)


def test_incident_reuses_exact_push_digest_and_normal_release_can_yield():
    api = IncidentEvidence()
    assert train.incident_build(api, A) == (release(), 100)
    assert train.queued_incident(api, A)


def test_no_push_uses_only_a_real_build_in_the_incident_dispatch():
    api = IncidentEvidence()
    api.has_push = False
    with pytest.raises(train.NeedsIncidentBuild):
        train.incident_build(api, A)
    api.runs[2]["head_repository"] = {"full_name": train.REPO}
    builder = copy.deepcopy(api.job_rows[1][1])
    api.job_rows[2].append(builder)
    assert train.incident_build(api, A, dispatch=api.runs[2]) == (release(A, 2), 100)
    builder["steps"][0]["conclusion"] = "skipped"
    with pytest.raises(train.Refused, match="actually build"):
        train.incident_build(api, A, dispatch=api.runs[2])


def test_incident_workflow_still_requires_ci_and_the_same_production_queue():
    entry = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())["jobs"]
    rollout = entry["rollout"]
    for gate in ("gate-on-ci", "build-image", "scope"):
        assert f"needs.{gate}.result == 'success'" in rollout["if"]
    assert rollout["uses"] == "./.github/workflows/deploy-production.yml"
    assert rollout["with"]["hotfix"] == "${{ inputs.hotfix == true }}"
    validation = entry["scope"]["steps"][0]
    assert validation["env"]["ALLOWED"] == (
        "${{ github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' && "
        "!inputs.hotfix && !inputs.track_main && !inputs.website_only }}")
    for step in entry["build-image"]["steps"][1:]:
        assert step["if"] == "steps.reuse.outputs.reuse != 'true'"
    child = yaml.safe_load((ROOT / ".github/workflows/deploy-production.yml").read_text())
    assert child["concurrency"] == {"group": "deploy-trusted-router", "queue": "max", "cancel-in-progress": False}
    jobs = child["jobs"]
    assert jobs["select-release"]["env"]["INCIDENT_SOURCE"] == "${{ needs.build-image.outputs.source }}"
    assert {"admit-cloud", "build-image"} == set(jobs["select-release"]["needs"])
    for gate in ("migrate-schema", "confirm-regions-ready", "confirm-still-on-main"):
        assert gate in jobs["deploy"]["needs"]


@pytest.mark.parametrize(("allowed", "incident", "ok"), [
    ("true", "INC-123", True), ("false", "INC-123", False), ("true", "X\nfoo", False),
    ("true", "X$(echo oops)", False), ("true", "A" * 65, False),
])
def test_incident_request_validation_executes(tmp_path, allowed, incident, ok):
    step = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())["jobs"]["scope"]["steps"][0]
    summary = tmp_path / "summary"
    result = subprocess.run(  # noqa: S603 -- fixed workflow step, no network
        ["/bin/bash", "-c", step["run"]], capture_output=True,
        env={"INCIDENT": incident, "ALLOWED": allowed, "GITHUB_STEP_SUMMARY": str(summary),
             "GITHUB_ACTOR": "operator", "GITHUB_SHA": A})
    assert (result.returncode == 0) is ok
    assert summary.exists() is ok


@pytest.mark.parametrize("defect", ["head", "ci", "fork", "pr", "failure", "job", "attempt", "manifest", "sha"])
def test_incident_fails_closed_on_bad_build_evidence(defect):
    api = IncidentEvidence()
    if defect == "head":
        api.head = B
    elif defect == "ci":
        api.green.clear()
    elif defect == "fork":
        api.runs[1]["head_repository"]["full_name"] = "other/quill-router"
    elif defect == "pr":
        api.runs[1]["event"] = "pull_request"
    elif defect == "failure":
        api.runs[1]["conclusion"] = "failure"
    elif defect == "job":
        api.job_rows[1][1]["conclusion"] = "in_progress"
    elif defect == "attempt":
        api.job_rows[1][1]["run_attempt"] = 2
    elif defect == "manifest":
        api.artifacts[1, "prepared"] = {"release": {**asdict(release()), "image": "registry:mutable"}}
    else:
        api.runs[1]["head_sha"] = B
    with pytest.raises(train.Refused):
        train.incident_build(api, A)


@pytest.mark.parametrize("defect", ["wrong-title", "failed-validation", "not-yet-queued", "cancelled"])
def test_normal_controller_never_yields_to_an_unverified_or_stopped_request(defect):
    api = IncidentEvidence()
    if defect == "wrong-title":
        api.runs[2]["display_title"] = f"Deploy revision {A}"
    elif defect == "failed-validation":
        api.job_rows[2][0]["steps"][0]["conclusion"] = "failure"
    elif defect == "not-yet-queued":
        api.job_rows[2].pop()
    else:
        api.runs[2]["status"] = "completed"
    assert not train.queued_incident(api, A)


def test_incident_preflight_selection_and_drift(tmp_path, monkeypatch):
    api = IncidentEvidence()
    monkeypatch.setattr(train, "GitHub", lambda: api)
    monkeypatch.chdir(tmp_path)
    env = {"EMERGENCY_INCIDENT": "INC-123", "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "2",
           "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": A, "GITHUB_OUTPUT": str(tmp_path / "out")}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["release_train", "incident-preflight"])
    train.main()
    assert (tmp_path / "out").read_text() == f"sha={release().tag}\nimage={release().image}\nsource=1/1\n"
    monkeypatch.setattr("sys.argv", ["release_train", "select"])
    monkeypatch.setenv("PREPARED_TAG", release().tag)
    monkeypatch.setenv("PREPARED_IMAGE", release().image)
    monkeypatch.setenv("INCIDENT_SOURCE", "1/2")
    with pytest.raises(train.Refused, match="changed during admission"):
        train.main()
    assert not (tmp_path / "release.json").exists()
    monkeypatch.setenv("INCIDENT_SOURCE", "1/1")
    train.main()
    receipt = json.loads((tmp_path / "release.json").read_text())
    assert (receipt["incident"], receipt["ci_run"], receipt["controller_run"], receipt["deploy"]) == (
        "INC-123", 100, 2, True)
    assert receipt["release"] == asdict(release())


def test_batches_every_ready_commit_at_admission():
    api = Evidence()
    result = train.select(api, api.runs[1], release())
    assert result["release"] == asdict(release(C, 3))
    assert result["controller_commit"] == A
    assert result["deploy"] and result["ci_run"] == 102


@pytest.mark.parametrize("unready", ["red", "image", "gate", "workflow", "artifact", "pinned", "side-branch"])
def test_unready_newest_does_not_hold_back_ready_batch(unready):
    api = Evidence()
    if unready == "red":
        api.green.remove(C)
    elif unready in {"image", "gate"}:
        api.job_rows[3][1 if unready == "image" else 0]["conclusion"] = ""
    elif unready == "workflow":
        api.incompatible.add(C)
    elif unready == "artifact":
        api.artifacts[3, "prepared"] = train.Refused("expired")
    elif unready == "pinned":
        api.runs[3]["event"] = "workflow_dispatch"
    else:
        api.runs[3]["head_branch"] = "feature"
    assert train.select(api, api.runs[1], release())["release"]["commit"] == B


def test_preparation_order_cannot_move_train_backwards():
    api = Evidence()
    api.runs = {1: api.runs[1], 3: api.runs[3], 2: api.runs[2]}
    assert train.select(api, api.runs[1], release())["release"]["commit"] == C


def test_new_arrival_after_freeze_waits_for_next_train():
    api = Evidence()
    api.after_refresh = lambda: api.green.add(D)
    assert train.select(api, api.runs[1], release())["release"]["commit"] == C


@pytest.mark.parametrize("change", ["force-push", "red"])
def test_revalidate_frozen_commit_before_returning(change):
    api = Evidence()
    if change == "force-push":
        api.after_refresh = lambda: setattr(api, "head", "f" * 40)
    else:
        api.after_refresh = lambda: api.green.remove(C)
    with pytest.raises(train.Refused):
        train.select(api, api.runs[1], release())


def test_controller_removed_from_main_fails_closed():
    api = Evidence()
    api.order.remove(A)
    with pytest.raises(train.Refused, match="left main"):
        train.select(api, api.runs[1], release())


def test_manual_pin_keeps_requested_commit():
    api = Evidence()
    pinned = run(event="workflow_dispatch")
    assert train.select(api, pinned, release())["release"]["commit"] == A


def test_explicit_track_main_dispatch_batches():
    api = Evidence()
    tracked = run(event="workflow_dispatch", display_title=f"Deploy latest main {A}")
    assert train.select(api, tracked, release())["release"]["commit"] == C


def test_hotfix_cannot_be_an_automatic_train():
    api = Evidence()
    with pytest.raises(train.Refused, match="green CI"):
        train.select(api, api.runs[1], release(), hotfix=True)
    api.green.clear()
    result = train.select(api, run(event="workflow_dispatch"), release(), hotfix=True)
    assert result["release"]["commit"] == A and result["ci_run"] == 0


def delivered(api, controller=A, selected=C, run_id=4):
    api.runs[run_id] = run(controller, run_id, status="completed", conclusion="success")
    api.job_rows[run_id] = [{"name": "rollout / " + n, "conclusion": "success"}
                            for n in (*superseded_deploy._DELIVERY_JOBS, "select-release")]
    api.artifacts[run_id, "selected"] = {"release": asdict(release(selected, 3)), "deploy": True}


def test_later_queued_runs_do_not_repeat_delivered_batch():
    api = Evidence()
    delivered(api)
    result = train.select(api, api.runs[2], release(B, 2))
    assert result["release"]["commit"] == C and result["deploy"] is False


def test_legacy_delivery_also_prevents_downgrade():
    api = Evidence()
    delivered(api, controller=C)
    api.job_rows[4] = [j for j in api.job_rows[4] if j["name"] != "rollout / select-release"]
    assert train.select(api, api.runs[1], release())["deploy"] is False


def test_partial_rollout_is_not_delivery():
    api = Evidence()
    delivered(api)
    next(j for j in api.job_rows[4] if j["name"] == "rollout / finalize-cloud")["conclusion"] = "failure"
    assert train.select(api, api.runs[1], release())["deploy"] is True


def public_delivery(api, sha=C):
    api.runs[4] = run(sha, 4, status="completed", conclusion="success")
    api.job_rows[4] = [{"name": "public-docs / deploy-public-docs", "conclusion": "success",
                        "steps": [{"name": "Record public-only delivery", "conclusion": "success"}]}]
    api.artifacts[4, "public"] = {"release": asdict(release(sha, 4))}


def test_public_delivery_is_floor_not_api_delivery():
    api = Evidence()
    api.runs.pop(3)
    public_delivery(api)
    result = train.select(api, api.runs[1], release())
    assert result["release"] == asdict(release(C, 4))
    assert result["deploy"] is True


def test_public_floor_cannot_bypass_workflow_compatibility():
    api = Evidence()
    api.incompatible.add(C)
    public_delivery(api)
    with pytest.raises(train.Refused, match="own controller"):
        train.select(api, api.runs[1], release())


def test_public_receipt_missing_fails_closed():
    api = Evidence()
    public_delivery(api)
    api.artifacts[4, "public"] = train.Refused("receipt missing")
    with pytest.raises(train.Refused, match="receipt missing"):
        train.select(api, api.runs[1], release())


def test_public_fallback_is_not_a_delivery():
    api = Evidence()
    public_delivery(api)
    api.job_rows[4][0]["steps"][0]["conclusion"] = "skipped"
    api.artifacts[4, "public"] = train.Refused("must not read fallback receipt")
    assert train.select(api, api.runs[1], release())["deploy"] is True


def test_unknown_public_outcome_is_not_ignored():
    api = Evidence()
    public_delivery(api)
    api.job_rows[4][0]["steps"] = []
    with pytest.raises(train.Refused, match="outcome is unavailable"):
        train.select(api, api.runs[1], release())


def test_missing_completed_train_receipt_refuses_instead_of_downgrading():
    api = Evidence()
    delivered(api)
    api.artifacts[4, "selected"] = train.Refused("receipt unavailable")
    with pytest.raises(train.Refused, match="receipt unavailable"):
        train.select(api, api.runs[1], release())


def test_unreadable_history_fails_closed(monkeypatch):
    api = Evidence()
    delivered(api)
    monkeypatch.setattr(api, "jobs", lambda _: (_ for _ in ()).throw(train.Refused("unknown")))
    with pytest.raises(train.Refused, match="history is incomplete"):
        train.select(api, api.runs[1], release())


def test_history_reads_overlap_but_results_keep_listing_order(monkeypatch):
    api = Evidence()
    width = train.HISTORY_WORKERS
    assert width == 4
    barrier = Barrier(width, timeout=5)
    later_read = Event()
    guard = Lock()
    active = peak = 0
    seen = []
    for offset, sha in enumerate([A, B, C, D] * 2):
        delivered(api, controller=sha, selected=sha, run_id=10 + offset)
    original = api.jobs

    def overlap(run_id):
        nonlocal active, peak
        with guard:
            seen.append(run_id)
            active += 1
            peak = max(peak, active)
        if run_id < 10 + width:
            barrier.wait()
        if run_id == 10:
            assert later_read.wait(5)
        if run_id == 10 + width:
            # A slow first lookup must not hold up the fifth read.
            later_read.set()
        with guard:
            active -= 1
        return original(run_id)

    monkeypatch.setattr(api, "jobs", overlap)
    history = [{"databaseId": i} for i in range(10, 18)]
    assert train.read_deliveries(api, history, D) == ([A, B, C, D] * 2, [])
    assert sorted(seen) == list(range(10, 18)) and peak == width


def test_one_bad_receipt_among_successes_blocks_selection_before_refresh():
    api = Evidence()
    for i in range(10, 18):
        delivered(api, run_id=i)
    api.artifacts[12, "selected"] = train.Refused("missing receipt")
    api.after_refresh = lambda: pytest.fail("must not promote with incomplete history")
    with pytest.raises(train.Refused, match="missing receipt"):
        train.select(api, api.runs[1], release())


def test_unreadable_successor_can_wait_without_blocking_verified_base(monkeypatch):
    api = Evidence()
    monkeypatch.setattr(api, "jobs", lambda _: (_ for _ in ()).throw(train.Refused("unknown")))
    assert train.select(api, api.runs[1], release())["release"]["commit"] == A


def test_failed_builds_do_not_trigger_expensive_history_reads(monkeypatch):
    api = Evidence()
    for source in api.runs.values():
        source.update(status="completed", conclusion="failure")
    monkeypatch.setattr(api, "jobs", lambda _: pytest.fail("failed builds cannot be deliveries or candidates"))
    assert train.select(api, api.runs[1], release())["release"]["commit"] == A


def rest_job(job_id=1, **changes):
    return {"id": job_id, "run_id": 4, "name": f"job-{job_id}",
            "status": "completed", "conclusion": "success", "run_attempt": 1, **changes}


def test_job_evidence_uses_complete_direct_latest_listing_and_existing_cache(monkeypatch):
    rows = [rest_job(i) for i in range(1, 102)]
    # A failed-job-only rerun retains successful jobs from the first attempt.
    rows[-1]["run_attempt"] = 2
    pages = [{"total_count": 101, "jobs": rows[:100]}, {"total_count": 101, "jobs": rows[100:]}]
    calls = []
    api = train.GitHub(deadline=1234)

    def lookup(args, deadline):
        calls.append((args, deadline))
        return copy.deepcopy(pages)

    monkeypatch.setattr(superseded_deploy, "_gh_json", lookup)
    assert api.jobs(4) == rows
    assert api.jobs(4) == rows
    assert calls == [(["api", "--paginate", "--slurp",
                      f"repos/{train.REPO}/actions/runs/4/jobs?filter=latest&per_page=100"], 1234)]
    api.refresh()
    assert api.jobs(4) == rows
    assert len(calls) == 2


@pytest.mark.parametrize("pages", [
    None, {}, [], [None], [{}], [{"total_count": True, "jobs": []}],
    [{"total_count": -1, "jobs": []}], [{"total_count": "1", "jobs": [rest_job()]}],
    [{"total_count": 1, "jobs": None}], [{"total_count": 1, "jobs": []}],
    [{"total_count": 0, "jobs": [rest_job()]}],
    [{"total_count": 2, "jobs": [rest_job()]}, {"total_count": 3, "jobs": [rest_job(2)]}],
    [{"total_count": 2, "jobs": [rest_job(), rest_job()]}],
    [{"total_count": 1, "jobs": [None]}],
    *[{"total_count": 1, "jobs": [rest_job(**fields)]} for fields in (
        {"id": True}, {"id": 0}, {"id": "1"}, {"run_id": 5}, {"run_id": True},
        {"run_id": "4"}, {"name": ""}, {"name": None}, {"status": None},
        {"conclusion": {}},
    )],
])
def test_job_evidence_refuses_incomplete_or_invalid_pages(monkeypatch, pages):
    # Each parametrized malformed row is a page, not a full paginated response.
    if isinstance(pages, dict) and "total_count" in pages:
        pages = [pages]
    api = train.GitHub()
    monkeypatch.setattr(api, "json", lambda *args: copy.deepcopy(pages))
    with pytest.raises(train.Refused):
        api.jobs(4)


@pytest.mark.parametrize(("rows", "green"), [
    ([{"databaseId": 7, "status": "completed", "conclusion": "success", "event": "push"}], True),
    ([{"databaseId": 7, "status": "completed", "conclusion": "success", "event": "workflow_dispatch"}], True),
    # A newer green pull_request run beside a red push run: refused.
    ([{"databaseId": 9, "status": "completed", "conclusion": "success", "event": "pull_request"},
      {"databaseId": 7, "status": "completed", "conclusion": "failure", "event": "push"}], False),
    # A lone pull_request run is no CI evidence for a release.
    ([{"databaseId": 9, "status": "completed", "conclusion": "success", "event": "pull_request"}], False),
    # A newer red pull_request run does not refuse a green push run.
    ([{"databaseId": 9, "status": "completed", "conclusion": "failure", "event": "pull_request"},
      {"databaseId": 7, "status": "completed", "conclusion": "success", "event": "push"}], True),
    (None, False), ([], False),
])
def test_ci_evidence_counts_push_and_dispatch_runs_only(monkeypatch, rows, green):
    api = train.GitHub()
    calls = []

    def lookup(*args):
        calls.append(args)
        return copy.deepcopy(rows)

    monkeypatch.setattr(api, "json", lookup)
    if green:
        assert api.ci("a" * 40) == 7
    else:
        with pytest.raises(train.Refused):
            api.ci("a" * 40)
    [args] = calls
    assert "--limit" not in args
    assert "event" in args[args.index("--json") + 1].split(",")


@pytest.mark.parametrize("run_id", [True, 0, -1, "4", None])
def test_job_evidence_refuses_invalid_run_identity_before_lookup(monkeypatch, run_id):
    api = train.GitHub()
    monkeypatch.setattr(api, "json", lambda *args: pytest.fail("invalid ID reached GitHub"))
    with pytest.raises(train.Refused, match="run ID"):
        api.jobs(run_id)


@pytest.mark.parametrize("outcome", ["success", "failure", "skipped", None])
@pytest.mark.parametrize("manual", [False, True])
def test_direct_job_evidence_preserves_release_decisions_and_retained_attempts(monkeypatch, outcome, manual):
    api = Evidence()
    delivered(api)
    api.job_rows[4][-2]["conclusion"] = outcome
    if manual:
        api.runs[1]["event"] = "workflow_dispatch"
    expected = train.select(api, api.runs[1], release())

    def direct_jobs(run_id):
        rows = [rest_job(i + 1, run_id=run_id, name=row["name"],
                         conclusion=row["conclusion"], run_attempt=2 if i else 1)
                for i, row in enumerate(api.job_rows[run_id])]
        direct = train.GitHub()
        monkeypatch.setattr(direct, "json", lambda *args: [{"total_count": len(rows), "jobs": rows}])
        return direct.jobs(run_id)

    monkeypatch.setattr(api, "jobs", direct_jobs)
    assert train.select(api, api.runs[1], release()) == expected


@pytest.mark.parametrize("invalid", ["version", "run", "attempt", "commit", "duplicate", "expired", "oversized"])
def test_prepared_manifest_rejects_bad_identity(monkeypatch, invalid):
    api = train.GitHub()
    source = run()
    item = {"id": 99, "name": "prepared-release-1", "expired": False, "size_in_bytes": 500,
            "workflow_run": {"id": 1, "head_sha": A}}
    metadata = {"total_count": 1, "artifacts": [item]}
    raw = {"schema_version": 1, "release": asdict(release())}
    if invalid == "version":
        raw["schema_version"] = True
    elif invalid in {"run", "attempt"}:
        raw["release"]["build_" + invalid] = 2
    elif invalid == "commit":
        raw["release"].update(commit=B, tag=B[:9])
    elif invalid == "duplicate":
        metadata["artifacts"].append(dict(item))
        metadata["total_count"] = 2
    elif invalid == "expired":
        item["expired"] = True
    else:
        item["size_in_bytes"] = train.MAX_ARTIFACT_BYTES + 1
    monkeypatch.setattr(api, "json", lambda *args: metadata)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=zipped(raw)))
    with pytest.raises(train.Refused):
        api.artifact(source, "prepared")


@pytest.mark.parametrize("field,value", [("controller_run", 2), ("controller_attempt", 2),
                                         ("controller_commit", B)])
def test_selected_receipt_is_bound_to_controller(monkeypatch, field, value):
    api = train.GitHub()
    metadata = {"total_count": 1, "artifacts": [{"id": 99, "name": "selected-release-1", "expired": False,
                "size_in_bytes": 500, "workflow_run": {"id": 1, "head_sha": A}}]}
    raw = {"schema_version": 1, "controller_run": 1, "controller_attempt": 1, "controller_commit": A,
           "release": asdict(release(C, 3)), "deploy": True}
    raw[field] = value
    monkeypatch.setattr(api, "json", lambda *args: metadata)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=zipped(raw)))
    with pytest.raises(train.Refused, match="receipt identity"):
        api.artifact(run(), "selected")


def test_cli_freezes_and_publishes_one_identity(monkeypatch, tmp_path):
    api = Evidence()
    monkeypatch.setattr(train, "GitHub", lambda: api)
    monkeypatch.chdir(tmp_path)
    for key, value in {"GITHUB_SHA": A, "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1",
                       "PREPARED_TAG": A[:9], "PREPARED_IMAGE": release().image,
                       "GITHUB_OUTPUT": str(tmp_path / "outputs")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["release_train", "prepare"])
    train.main()
    assert json.loads((tmp_path / "release.json").read_text())["release"] == asdict(release())
    monkeypatch.setattr("sys.argv", ["release_train", "select"])
    train.main()
    receipt = json.loads((tmp_path / "release.json").read_text())
    assert receipt["controller_commit"] == A and receipt["release"] == asdict(release(C, 3))
    assert (tmp_path / "outputs").read_text() == (
        f"commit={C}\nsha={C[:9]}\nimage={release(C, 3).image}\nproceed=true\n"
    )


def test_cli_refuses_inputs_different_from_prepared_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(train, "GitHub", Evidence)
    monkeypatch.chdir(tmp_path)
    for key, value in {"GITHUB_SHA": A, "GITHUB_RUN_ID": "1", "PREPARED_TAG": B[:9],
                       "PREPARED_IMAGE": release().image}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["release_train", "select"])
    with pytest.raises(train.Refused, match="inputs differ"):
        train.main()
    assert not (tmp_path / "release.json").exists()


@pytest.mark.parametrize("field,value", [
    ("commit", "main"), ("tag", "f" * 9), ("image", train.IMAGE.replace("@sha256:", ":latest")),
    ("image", "attacker.invalid/image@sha256:" + "a" * 64), ("build_run", True), ("build_attempt", 0),
])
def test_release_identity_is_strict(field, value):
    raw = asdict(release())
    raw[field] = value
    with pytest.raises(train.Refused):
        train.Release.parse(raw)


def zipped(raw, filename="release.json", extra=False):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(filename, json.dumps(raw))
        if extra:
            archive.writestr("other.json", "{}")
    return data.getvalue()


@pytest.mark.parametrize("filename,extra,raw", [
    ("../release.json", False, {}), ("release.json", True, {}), ("release.json", False, []),
    ("release.json", False, {"x": "x" * train.MAX_MANIFEST_BYTES}),
])
def test_manifest_archive_rejects_unexpected_contents(filename, extra, raw):
    with pytest.raises(train.Refused):
        train.manifest_from_zip(zipped(raw, filename, extra))


def test_prepared_artifact_provenance_and_inherited_attempt(monkeypatch):
    api = train.GitHub()
    source = run(run_attempt=2)
    metadata = {"total_count": 1, "artifacts": [{"id": 99, "name": "prepared-release-1",
                "expired": False, "size_in_bytes": 500, "workflow_run": {"id": 1, "head_sha": A}}]}
    monkeypatch.setattr(api, "json", lambda *args: metadata)
    raw = {"schema_version": 1, "release": asdict(release())}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=zipped(raw)))
    assert api.artifact(source, "prepared") == raw
    metadata["artifacts"][0]["workflow_run"]["head_sha"] = B
    with pytest.raises(train.Refused, match="another run or commit"):
        api.artifact(source, "prepared")


def gh_error(stderr):
    return subprocess.CalledProcessError(1, ["gh"], output=b"", stderr=stderr.encode())


SERVER_ERROR = gh_error("gh: Server Error (HTTP 502)\n")
PREPARED = {"schema_version": 1, "release": asdict(release())}


def scripted_gh(monkeypatch, *answers):
    """Answer each gh call with the next stdout, or fail with it; return the calls and retry waits.

    A CalledProcessError raises under check=True and is an exit code otherwise.
    The deadline's clock starts at 0 and moves only with the waits.
    """
    calls, waits, queue = [], [], list(answers)

    def fake(argv, **kwargs):
        calls.append(argv)
        answer = queue.pop(0)
        if isinstance(answer, subprocess.CalledProcessError):
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(answer.returncode, argv, b"", answer.stderr)
            return subprocess.CompletedProcess(argv, answer.returncode, "", answer.stderr.decode())
        if isinstance(answer, Exception):
            raise answer
        return subprocess.CompletedProcess(argv, 0, answer, "")

    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(superseded_deploy, "_monotonic", lambda: sum(waits))
    monkeypatch.setattr(superseded_deploy, "_sleep", waits.append)
    return calls, waits


def prepared_download(monkeypatch, *answers, deadline=600.0):
    api = train.GitHub(deadline=deadline)
    metadata = {"total_count": 1, "artifacts": [{"id": 99, "name": "prepared-release-1",
                "expired": False, "size_in_bytes": 500, "workflow_run": {"id": 1, "head_sha": A}}]}
    monkeypatch.setattr(api, "json", lambda *args: metadata)
    return (api, *scripted_gh(monkeypatch, *answers))


def test_artifact_download_retries_a_transient_server_error(monkeypatch):
    api, calls, waits = prepared_download(monkeypatch, SERVER_ERROR, zipped(PREPARED))
    assert api.artifact(run(), "prepared") == PREPARED
    assert calls == [["gh", "api", f"repos/{train.REPO}/actions/artifacts/99/zip"]] * 2 and waits == [1.0]


def test_artifact_download_fails_closed_after_bounded_retries(monkeypatch):
    # A fourth download would succeed: only the bound stops the retries.
    api, calls, waits = prepared_download(monkeypatch, *[SERVER_ERROR] * 3, zipped(PREPARED))
    with pytest.raises(train.Refused, match="download failed"):
        api.artifact(run(), "prepared")
    assert len(calls) == 3 and waits == [1.0, 2.0]


@pytest.mark.parametrize("failure", [
    gh_error("gh: API rate limit exceeded for installation ID 1. (HTTP 403)"),
    gh_error("gh: Too Many Requests (HTTP 429)"),
    gh_error("gh: Not Found (HTTP 404)"),
    subprocess.TimeoutExpired(["gh"], 60),
    OSError("gh missing"),
], ids=["rate-limit", "429", "404", "timeout", "no-gh"])
def test_artifact_download_never_retries_anything_but_a_server_error(monkeypatch, failure):
    api, calls, waits = prepared_download(monkeypatch, failure, zipped(PREPARED))
    with pytest.raises(train.Refused, match="download failed"):
        api.artifact(run(), "prepared")
    assert len(calls) == 1 and waits == []


def test_artifact_download_never_retries_an_unreadable_archive(monkeypatch):
    api, calls, waits = prepared_download(monkeypatch, b"<html>502 Bad Gateway</html>", zipped(PREPARED))
    with pytest.raises(train.Refused, match="unreadable release artifact"):
        api.artifact(run(), "prepared")
    assert len(calls) == 1 and waits == []


def test_artifact_download_retry_needs_time_before_the_deadline(monkeypatch):
    # 0.5 s past one call's timeout leaves no room for the 1 s wait and a whole call.
    api, calls, waits = prepared_download(monkeypatch, SERVER_ERROR, zipped(PREPARED),
                                          deadline=superseded_deploy.GH_TIMEOUT_SECONDS + 0.5)
    with pytest.raises(train.Refused, match="download failed"):
        api.artifact(run(), "prepared")
    assert len(calls) == 1 and waits == []


@pytest.mark.parametrize(("failures", "answers"), [(1, True), (3, False)])
def test_release_evidence_lookup_retries_a_transient_server_error(monkeypatch, failures, answers):
    api = train.GitHub(deadline=600.0)
    calls, waits = scripted_gh(monkeypatch, *[SERVER_ERROR] * failures, json.dumps({"object": {"sha": D}}))
    ref = f"repos/{train.REPO}/git/ref/heads/main"
    if answers:
        assert api.json("api", ref) == {"object": {"sha": D}}
    else:
        with pytest.raises(train.Refused, match="evidence unavailable"):
            api.json("api", ref)
    assert len(calls) == min(failures + 1, 3) and waits == [1.0, 2.0][:min(failures, 2)]


def test_all_release_jobs_use_one_frozen_full_commit():
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy-production.yml").read_text())
    jobs = workflow["jobs"]
    assert "admit-cloud" in jobs["select-release"]["needs"]
    assert workflow["concurrency"]["cancel-in-progress"] is False
    for name in ("confirm-regions-ready", "migrate-schema", "sync-runtime-secrets", "confirm-still-on-main",
                 "deploy", "public-surface-companion", "settle-drain-worker", "verify-cloud-complete"):
        job = jobs[name]
        assert "select-release" in job["needs"], name
        checkouts = [s for s in job["steps"] if s.get("uses") == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"]
        assert len(checkouts) == 1
        assert checkouts[0]["with"]["ref"] == "${{ needs.select-release.outputs.commit }}", name
    checkout = next(s for s in jobs["rollout-secondaries"]["steps"] if s.get("uses") == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1")
    assert checkout["with"]["ref"] == "${{ needs.deploy.outputs.commit }}"
    for name in ("confirm-regions-ready", "migrate-schema", "sync-runtime-secrets"):
        assert "needs.select-release.outputs.proceed == 'true'" in jobs[name]["if"]
    for name in ("sha", "image", "commit"):
        assert jobs["deploy"]["outputs"][name] == "${{ needs.select-release.outputs." + name + " }}"
    assert "select-release" in jobs["finalize-cloud"]["needs"]


def test_prepare_artifact_is_uploaded_after_build_before_admission():
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    steps = workflow["jobs"]["build-image"]["steps"]
    assert steps[-2]["run"] == "python3 -m scripts.deploy.release_train prepare"
    assert steps[-1]["uses"] == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    assert steps[-1]["with"]["if-no-files-found"] == "error"
    assert steps[-1]["with"]["name"] == "prepared-release-${{ github.run_attempt }}"
    assert set(workflow["jobs"]["rollout"]["needs"]) == {"gate-on-ci", "build-image", "scope", "public-docs"}
