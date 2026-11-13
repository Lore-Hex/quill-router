from __future__ import annotations

import copy
import io
import json
import subprocess
import zipfile
from dataclasses import asdict
from pathlib import Path
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


def test_all_release_jobs_use_one_frozen_full_commit():
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy-production.yml").read_text())
    jobs = workflow["jobs"]
    assert "admit-cloud" in jobs["select-release"]["needs"]
    assert workflow["concurrency"]["cancel-in-progress"] is False
    for name in ("confirm-regions-ready", "migrate-schema", "sync-runtime-secrets", "confirm-still-on-main",
                 "deploy", "public-surface-companion", "settle-drain-worker", "verify-cloud-complete"):
        job = jobs[name]
        assert "select-release" in job["needs"], name
        checkouts = [s for s in job["steps"] if s.get("uses") == "actions/checkout@v7"]
        assert len(checkouts) == 1
        assert checkouts[0]["with"]["ref"] == "${{ needs.select-release.outputs.commit }}", name
    checkout = next(s for s in jobs["rollout-secondaries"]["steps"] if s.get("uses") == "actions/checkout@v7")
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
    assert steps[-1]["uses"] == "actions/upload-artifact@v4"
    assert steps[-1]["with"]["if-no-files-found"] == "error"
    assert steps[-1]["with"]["name"] == "prepared-release-${{ github.run_attempt }}"
    assert set(workflow["jobs"]["rollout"]["needs"]) == {"gate-on-ci", "build-image"}
