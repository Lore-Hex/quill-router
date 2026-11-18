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
        barrier.wait()
        if run_id == 10:
            assert later_read.wait(5)
        if run_id == 13:
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


@pytest.fixture
def retry_clock(monkeypatch):
    clock = SimpleNamespace(now=100.0, pauses=[])

    def pause(seconds):
        assert seconds > 0
        clock.pauses.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(superseded_deploy, "_monotonic", lambda: clock.now)
    monkeypatch.setattr(train.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(train, "_sleep", pause)
    return clock


@pytest.mark.parametrize("failures", [1, 2])
@pytest.mark.parametrize("answer", [{"evidence": True}, []])
def test_json_retry_recovers_and_caches(monkeypatch, retry_clock, failures, answer):
    api = train.GitHub()
    args = ("api", "repos/private/release-evidence")
    calls = []
    pause = train._sleep

    def unlocked_pause(seconds):
        assert not api.cache_lock.locked()
        pause(seconds)

    def lookup(actual, deadline):
        assert not api.cache_lock.locked()
        calls.append((actual, deadline))
        return None if len(calls) <= failures else answer

    monkeypatch.setattr(train, "_sleep", unlocked_pause)
    monkeypatch.setattr(superseded_deploy, "_gh_json", lookup)
    assert api.json(*args) == answer
    assert api.json(*args) == answer
    assert calls == [(list(args), api.deadline)] * (failures + 1)
    assert retry_clock.pauses == [1, 2][:failures]


def test_json_retry_exhaustion_refuses_without_caching(monkeypatch, retry_clock):
    api = train.GitHub()
    calls = []

    def lookup(args, deadline):
        calls.append((args, deadline))
        return None

    monkeypatch.setattr(superseded_deploy, "_gh_json", lookup)
    with pytest.raises(train.Refused, match="^GitHub release evidence unavailable$"):
        api.json("api", "repos/private/release-evidence")
    assert len(calls) == 3
    assert retry_clock.pauses == [1, 2]
    assert api.cache == {}


@pytest.mark.parametrize("subcommand", ["api", "run"])
def test_json_retry_stderr_only_names_subcommand(monkeypatch, retry_clock, capsys, subcommand):
    answers = iter([None, None, {}])
    monkeypatch.setattr(superseded_deploy, "_gh_json", lambda *args: next(answers))
    url = "https://api.github.com/repos/private/release-evidence"
    assert train.GitHub().json(subcommand, url, "--private-argument") == {}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        f"release_train: retrying gh {subcommand} (try 2 of 3)",
        f"release_train: retrying gh {subcommand} (try 3 of 3)",
    ]
    assert url not in captured.err and "--private-argument" not in captured.err


def fake_artifact_listing(monkeypatch):
    metadata = {"total_count": 1, "artifacts": [{"id": 99, "name": "prepared-release-1",
                "expired": False, "size_in_bytes": 500, "workflow_run": {"id": 1, "head_sha": A}}]}

    def lookup(args, deadline):
        assert args == ["api", f"repos/{train.REPO}/actions/runs/1/artifacts?per_page=100"]
        return metadata

    monkeypatch.setattr(superseded_deploy, "_gh_json", lookup)


@pytest.mark.parametrize("failures", [1, 2, 3])
@pytest.mark.parametrize("error", [OSError("unavailable"), subprocess.TimeoutExpired("gh", 10),
                                   subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")])
def test_artifact_download_retry(monkeypatch, retry_clock, capsys, failures, error):
    api = train.GitHub()
    fake_artifact_listing(monkeypatch)
    raw = {"schema_version": 1, "release": asdict(release())}
    calls = []

    def download(args, **kwargs):
        calls.append(args)
        assert kwargs == {"capture_output": True, "check": True,
                          "timeout": superseded_deploy._call_timeout(api.deadline)}
        if len(calls) <= failures:
            raise error
        return SimpleNamespace(stdout=zipped(raw))

    monkeypatch.setattr(subprocess, "run", download)
    if failures == 3:
        with pytest.raises(train.Refused, match="^release artifact download failed$") as exc:
            api.artifact(run(), "prepared")
        assert exc.value.__cause__ is error
    else:
        assert api.artifact(run(), "prepared") == raw
    attempts = min(failures + 1, 3)
    assert calls == [["gh", "api", f"repos/{train.REPO}/actions/artifacts/99/zip"]] * attempts
    assert retry_clock.pauses == [1, 2][:attempts - 1]
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        f"release_train: retrying gh api (try {attempt} of 3)" for attempt in range(2, attempts + 1)
    ]
    assert "repos/" not in captured.err and "/zip" not in captured.err


@pytest.mark.parametrize("invalid,reason", [
    ("archive", "unreadable release artifact"), ("version", "unknown release manifest version"),
    ("identity", "prepared release identity mismatch"),
])
def test_artifact_retry_keeps_download_validation(monkeypatch, retry_clock, invalid, reason):
    api = train.GitHub()
    fake_artifact_listing(monkeypatch)
    raw = {"schema_version": 2 if invalid == "version" else 1,
           "release": asdict(release(run_id=2 if invalid == "identity" else 1))}
    content = b"not a zip" if invalid == "archive" else zipped(raw)
    calls = []

    def download(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout=content)

    monkeypatch.setattr(subprocess, "run", download)
    with pytest.raises(train.Refused, match=f"^{reason}$"):
        api.artifact(run(), "prepared")
    assert len(calls) == 2
    assert retry_clock.pauses == [1]


@pytest.mark.parametrize("remaining,pauses,attempts", [(0.5, [0.5], 1), (2.5, [1, 1.5], 2)])
@pytest.mark.parametrize("kind", ["json", "artifact"])
def test_read_retry_respects_deadline(monkeypatch, retry_clock, capsys, remaining, pauses, attempts, kind):
    api = train.GitHub(deadline=retry_clock.now + remaining)
    calls = []
    json_calls = []
    original_json = superseded_deploy._gh_json

    def lookup(args, deadline):
        json_calls.append(args)
        return original_json(args, deadline)

    def fail(args, **kwargs):
        assert retry_clock.now < api.deadline
        assert kwargs["timeout"] <= api.deadline - retry_clock.now
        calls.append(args)
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fail)
    if kind == "artifact":
        fake_artifact_listing(monkeypatch)
        with pytest.raises(train.Refused, match="^release artifact download failed$"):
            api.artifact(run(), "prepared")
    else:
        monkeypatch.setattr(superseded_deploy, "_gh_json", lookup)
        with pytest.raises(train.Refused, match="^GitHub release evidence unavailable$"):
            api.json("api", "repos/private/release-evidence")
        assert len(json_calls) == attempts
    assert len(calls) == attempts
    assert retry_clock.pauses == pauses
    assert retry_clock.now == 100 + sum(pauses)
    retries = [line for line in capsys.readouterr().err.splitlines() if line.startswith("release_train:")]
    assert len(retries) == max(0, attempts - 1)

    # Starting a fresh reader after expiry must not pause or launch gh at all.
    retry_clock.now = api.deadline + 1
    expired = train.GitHub(deadline=api.deadline)
    json_calls.clear()
    if kind == "artifact":
        with pytest.raises(train.Refused, match="^release selection deadline exceeded$"):
            expired.artifact(run(), "prepared")
    else:
        with pytest.raises(train.Refused, match="^GitHub release evidence unavailable$"):
            expired.json("api", "repos/private/release-evidence")
        assert len(json_calls) <= 1
    assert len(calls) == attempts
    assert retry_clock.pauses == pauses
    assert retry_clock.now == api.deadline + 1
    assert "release_train: retrying" not in capsys.readouterr().err


@pytest.mark.parametrize("failures", [1, 3])
def test_history_jobs_retry_preserves_complete_ordered_evidence(monkeypatch, retry_clock, failures):
    api = train.GitHub()
    shas = [C, A, B, D, B, A, C, D]
    history = [{"databaseId": i} for i in range(10, 18)]
    calls = []
    guard = Lock()
    bad_url = f"repos/{train.REPO}/actions/runs/11/jobs?filter=latest&per_page=100"

    def lookup(args, deadline):
        with guard:
            calls.append(args)
            failed_reads = sum(call[-1] == bad_url for call in calls)
        if args[-1] == bad_url and failed_reads <= failures:
            return None
        if "/jobs?" in args[-1]:
            run_id = int(args[-1].split("/")[-2])
            rows = [rest_job(i + 1, run_id=run_id, name="rollout / " + name)
                    for i, name in enumerate(superseded_deploy._DELIVERY_JOBS)]
            return [{"total_count": len(rows), "jobs": rows}]
        if "/compare/" in args[-1]:
            return {"status": "ahead"}
        run_id = int(args[-1].split("/")[-1])
        return run(shas[run_id - 10], run_id)

    monkeypatch.setattr(superseded_deploy, "_gh_json", lookup)
    if failures == 3:
        with pytest.raises(train.Refused, match="^release history is incomplete$"):
            train.read_deliveries(api, history, D)
    else:
        assert train.read_deliveries(api, history, D) == (shas, [])
        assert {call[-1] for call in calls if "/jobs?" in call[-1]} == {
            f"repos/{train.REPO}/actions/runs/{i}/jobs?filter=latest&per_page=100" for i in range(10, 18)
        }
    assert sum(call[-1] == bad_url for call in calls) == min(failures + 1, 3)
    assert retry_clock.pauses == [1, 2][:failures]


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
