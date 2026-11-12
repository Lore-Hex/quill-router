"""A leftover drain tag is found before the first revision rolls out, not after.

staged_traffic.sh refuses a region whose `staged-drain` tag protects a revision
other than the serving one. The deploy workflow now asks that question of every
region under the reservation and before `deploy`, and releases the reservation
when the answer is no, since nothing has rolled out.

The answer is computed a second time here (scripts/deploy/drain_tag_preflight.py),
so the first group of tests runs the rollout's own `prepare_revision_drain`
against the same service documents and requires the two to agree.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy"
REGIONS = ("us-central1", "europe-west4", "us-east4", "southamerica-east1")
BASH = shutil.which("bash") or "/bin/bash"

FAKE_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_GCLOUD_LOG"
region=""
for arg in "$@"; do
  case "$arg" in --region=*) region="${arg#--region=}" ;; esac
done
case "$1 $2 $3" in
  "run services describe")
    [ -f "$FAKE_GCLOUD_DIR/$region.json" ] || { echo "no such service" >&2; exit 1; }
    cat "$FAKE_GCLOUD_DIR/$region.json" ;;
  "run revisions describe") cat "$FAKE_GCLOUD_DIR/revision.json" ;;
  *) exit 0 ;;
esac
"""


def _load() -> Any:
    sys.path.insert(0, str(DEPLOY))
    try:
        spec = importlib.util.spec_from_file_location("tr_drain_tag_preflight", DEPLOY / "drain_tag_preflight.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(DEPLOY))
    return module


preflight = _load()


def service(*traffic: dict[str, Any]) -> dict[str, Any]:
    return {"status": {"traffic": list(traffic)}}


def entry(revision: str, percent: int = 0, tag: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"revisionName": revision, "percent": percent}
    if tag is not None:
        item["tag"] = tag
    return item


# (name, document, the revision a leftover tag protects or None)
DOCUMENTS = [
    ("no tag", service(entry("new", 100)), None),
    ("tag on the serving revision", service(entry("new", 100), entry("new", 0, "staged-drain")), None),
    ("serving entry carries the tag", service(entry("new", 100, "staged-drain")), None),
    ("tag on a revision that no longer serves", service(entry("old", 0, "staged-drain"), entry("new", 100)), "old"),
    ("tag listed after the serving entry", service(entry("new", 100), entry("old", 0, "staged-drain")), "old"),
    ("another tag on another revision", service(entry("old", 0, "staged-probe"), entry("new", 100)), None),
    ("drain and probe tags together",
     service(entry("new", 100), entry("cand", 0, "staged-probe"), entry("old", 0, "staged-drain")), "old"),
]


@pytest.mark.parametrize("name,document,leftover", DOCUMENTS, ids=[d[0] for d in DOCUMENTS])
def test_leftover_revision(name: str, document: dict[str, Any], leftover: str | None) -> None:
    assert preflight.leftover_drain_revision(document) == leftover


@pytest.mark.parametrize("document,message", [
    (service(entry("a", 50), entry("b", 50, "staged-drain")), "exactly one 100%-traffic revision"),
    (service(entry("old", 0, "staged-drain")), "exactly one 100%-traffic revision"),
    (service(entry("new", 100), entry("a", 0, "staged-drain"), entry("b", 0, "staged-drain")), "more than one"),
    (service(entry("new", 100), {"percent": 0, "tag": "staged-drain"}), "empty revisionName"),
    ({"status": []}, "status is not an object"),
])
def test_a_document_that_cannot_answer_is_refused(document: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        preflight.leftover_drain_revision(document)


def _fake_cloud(tmp_path: Path, documents: dict[str, dict[str, Any]]) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(0o755)
    data = tmp_path / "cloud"
    data.mkdir()
    for region, document in documents.items():
        (data / f"{region}.json").write_text(json.dumps(document))
    (data / "revision.json").write_text(json.dumps({
        "metadata": {"annotations": {"autoscaling.knative.dev/minScale": "1"}},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }))
    log = tmp_path / "gcloud.log"
    log.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("TR_")}
    return {**env, "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "FAKE_GCLOUD_DIR": str(data), "FAKE_GCLOUD_LOG": str(log)}


def _calls(env: dict[str, str]) -> list[str]:
    return Path(env["FAKE_GCLOUD_LOG"]).read_text().splitlines()


def _rollout_guard() -> tuple[str, str]:
    """The rollout's own tag name and `prepare_revision_drain`, cut from staged_traffic.sh."""

    script = (DEPLOY / "staged_traffic.sh").read_text()
    tag = re.search(r'^DRAIN_TAG="([^"]+)"$', script, re.MULTILINE)
    function = re.search(r"^prepare_revision_drain\(\) \{\n.*?^\}\n", script, re.MULTILINE | re.DOTALL)
    assert tag is not None and function is not None
    return tag.group(1), function.group(0)


def test_the_tag_name_is_the_rollouts_own() -> None:
    assert preflight.DRAIN_TAG == _rollout_guard()[0] == "staged-drain"


@pytest.mark.parametrize("name,document,leftover", DOCUMENTS, ids=[d[0] for d in DOCUMENTS])
def test_agrees_with_the_rollouts_own_refusal(
    tmp_path: Path, name: str, document: dict[str, Any], leftover: str | None
) -> None:
    tag, function = _rollout_guard()
    env = _fake_cloud(tmp_path, {"europe-west4": document})
    serving = "new"
    program = "\n".join([
        "set -uo pipefail",
        f'source "{DEPLOY / "_cloud_run_revision_probe.sh"}"',
        'log() { echo "$*"; }',
        f'SERVICE=trusted-router REGION=europe-west4 PROJECT_ID=quill-cloud-proxy DRAIN_TAG="{tag}"',
        f'DRAIN_TAG_OWNED=0 OLD_REV="{serving}"',
        function,
        "prepare_revision_drain",
    ])
    run = subprocess.run([BASH, "-c", program], env=env, capture_output=True, text=True)  # noqa: S603
    refused = "refusing to replace outstanding drain tag" in run.stdout
    took_the_tag = any(f"--update-tags={tag}={serving}" in call for call in _calls(env))
    assert refused != took_the_tag, run.stdout + run.stderr
    assert refused == (leftover is not None)
    if refused:
        assert run.returncode != 0 and f"drain tag on {leftover};" in run.stdout


def _confirm(tmp_path: Path, documents: dict[str, dict[str, Any]], *regions: str,
             hold: str | None = None, **names: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    env = {**_fake_cloud(tmp_path, documents), **names}
    if hold is not None:
        env["TR_DEPLOY_HOLD_REGIONS"] = hold
    run = subprocess.run(  # noqa: S603
        [BASH, str(DEPLOY / "confirm_regions_ready.sh"), *regions], env=env, capture_output=True, text=True
    )
    return run, _calls(env)


def _ready() -> dict[str, dict[str, Any]]:
    return {region: service(entry(f"{region}-new", 100)) for region in REGIONS}


def test_every_region_ready_passes_and_only_reads(tmp_path: Path) -> None:
    run, calls = _confirm(tmp_path, _ready(), *REGIONS)
    assert run.returncode == 0, run.stdout + run.stderr
    assert calls == [
        f"run services describe trusted-router --region={region} --project=quill-cloud-proxy --format=json"
        for region in REGIONS
    ]
    assert "::error::" not in run.stdout


def test_a_leftover_tag_fails_and_names_the_region_revision_and_removal(tmp_path: Path) -> None:
    documents = _ready()
    documents["southamerica-east1"] = service(
        entry("sa-old", 0, "staged-drain"), entry("southamerica-east1-new", 100)
    )
    run, calls = _confirm(tmp_path, documents, *REGIONS)
    assert run.returncode == 1
    errors = [line for line in run.stdout.splitlines() if line.startswith("::error::")]
    # The whole line is pinned: an operator acts on every part of it.
    assert errors == [
        "::error::southamerica-east1: staged-drain is still on sa-old, but southamerica-east1-new serves. "
        "An earlier rollout's drain was interrupted there, and the staged rollout refuses a region in "
        "this state. Nothing has been rolled out by this run. Confirm southamerica-east1-new is healthy "
        "at 100%, remove the tag with the command below, then start the deploy again with Re-run all jobs "
        "(re-running only the failed jobs does not take the reservation again): "
        "gcloud run services update-traffic trusted-router --region southamerica-east1 "
        "--project quill-cloud-proxy --remove-tags staged-drain"
    ]
    # It reports; it never removes the tag itself.
    assert len(calls) == 4 and all(call.startswith("run services describe ") for call in calls)


def test_every_region_is_reported_in_one_run(tmp_path: Path) -> None:
    documents = _ready()
    documents["us-central1"] = service(entry("us-old", 0, "staged-drain"), entry("us-new", 100))
    documents["us-east4"] = service(entry("a", 50), entry("b", 50))
    del documents["southamerica-east1"]
    run, calls = _confirm(tmp_path, documents, *REGIONS)
    assert run.returncode == 1
    errors = [line.split(":")[4] for line in run.stdout.splitlines() if line.startswith("::error::")]
    assert errors == ["us-central1", "us-east4", "southamerica-east1"]
    assert "europe-west4: ready (europe-west4-new serves" in run.stdout
    assert len(calls) == 4


def test_a_region_that_cannot_answer_fails_on_its_own(tmp_path: Path) -> None:
    documents = _ready()
    documents["us-central1"] = service(entry("a", 50), entry("b", 50))
    run, calls = _confirm(tmp_path, documents, *REGIONS)
    assert run.returncode == 1
    errors = [line for line in run.stdout.splitlines() if line.startswith("::error::")]
    assert len(errors) == 1
    assert errors[0].startswith("::error::us-central1: cannot tell whether a rollout would be accepted: ")
    # The regions after it are still asked and reported.
    assert len(calls) == 4 and run.stdout.count(": ready (") == 3


def test_a_region_that_cannot_be_read_fails_on_its_own(tmp_path: Path) -> None:
    documents = _ready()
    del documents["us-central1"]
    run, calls = _confirm(tmp_path, documents, *REGIONS)
    assert run.returncode == 1
    errors = [line for line in run.stdout.splitlines() if line.startswith("::error::")]
    assert errors == ["::error::us-central1: could not read the service"]
    assert len(calls) == 4 and run.stdout.count(": ready (") == 3


def test_the_service_and_project_asked_are_the_ones_named(tmp_path: Path) -> None:
    documents = _ready()
    documents["us-east4"] = service(entry("east-old", 0, "staged-drain"), entry("us-east4-new", 100))
    run, calls = _confirm(tmp_path, documents, *REGIONS, SERVICE="other-service", PROJECT_ID="other-project")
    assert run.returncode == 1
    assert calls == [
        f"run services describe other-service --region={region} --project=other-project --format=json"
        for region in REGIONS
    ]
    assert run.stdout.rstrip().splitlines()[2].endswith(
        "gcloud run services update-traffic other-service --region us-east4 "
        "--project other-project --remove-tags staged-drain"
    )


def test_a_held_region_is_not_asked(tmp_path: Path) -> None:
    documents = _ready()
    documents["us-east4"] = service(entry("east-old", 0, "staged-drain"), entry("us-east4-new", 100))
    held, unheld = tmp_path / "held", tmp_path / "unheld"
    held.mkdir()
    unheld.mkdir()
    run, calls = _confirm(held, documents, *REGIONS, hold="us-east4")
    assert run.returncode == 0, run.stdout + run.stderr
    assert not any("--region=us-east4" in call for call in calls) and len(calls) == 3
    assert "::warning::us-east4 held by TR_DEPLOY_HOLD_REGIONS" in run.stdout
    # The same cloud without the hold is refused, so the hold is what passed it.
    refused, _ = _confirm(unheld, documents, *REGIONS)
    assert refused.returncode == 1


def test_without_arguments_it_asks_the_four_serving_regions(tmp_path: Path) -> None:
    run, calls = _confirm(tmp_path, _ready())
    assert run.returncode == 0
    assert [re.search(r"--region=(\S+)", call).group(1) for call in calls] == list(REGIONS)  # type: ignore[union-attr]


def _jobs() -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".github/workflows/deploy-production.yml").read_text())["jobs"]


def test_the_workflow_asks_under_the_reservation_and_before_any_revision() -> None:
    jobs = _jobs()
    job = jobs["confirm-regions-ready"]
    assert job["needs"] == ["admit-cloud"]
    assert job["if"] == "github.repository == 'Lore-Hex/quill-router'"
    assert "continue-on-error" not in job
    assert job["env"]["TR_DEPLOY_HOLD_REGIONS"] == jobs["deploy"]["env"]["TR_DEPLOY_HOLD_REGIONS"]
    assert (job["env"]["PROJECT_ID"], job["env"]["SERVICE"]) == (
        jobs["deploy"]["env"]["PROJECT_ID"], jobs["deploy"]["env"]["SERVICE"])
    runs = [step for step in job["steps"] if "run" in step]
    assert [step["run"] for step in runs] == [
        "bash scripts/deploy/confirm_regions_ready.sh " + " ".join(REGIONS)
    ]
    assert "if" not in runs[0] and "continue-on-error" not in runs[0]
    # It signs in the way the rollout's own first job does, and it cannot sit on the reservation.
    def sign_in(name: str) -> list[dict[str, Any]]:
        return [{key: step[key] for key in ("uses", "with") if key in step}
                for step in jobs[name]["steps"] if "google-github-actions/" in step.get("uses", "")]

    assert sign_in("confirm-regions-ready") == sign_in("admit-cloud") != []
    assert [step.get("uses") for step in job["steps"]].index("google-github-actions/setup-gcloud@v3") < (
        job["steps"].index(runs[0]))
    assert job["timeout-minutes"] <= 10

    # `deploy` waits for the answer, and a refusal skips it: its condition has no
    # status function, so a failed need is never overridden.
    deploy = jobs["deploy"]
    assert "confirm-regions-ready" in deploy["needs"]
    assert not re.search(r"always\(\)|failure\(\)|cancelled\(\)|success\(\)|\.result", deploy["if"])
    # Every job that changes a serving revision sits behind `deploy`.
    assert jobs["rollout-secondaries"]["needs"] == ["deploy"]
    for name in ("public-surface-companion", "settle-drain-worker"):
        assert "rollout-secondaries" in jobs[name]["needs"]
    assert "confirm-regions-ready" in jobs["finalize-cloud"]["needs"]


def test_the_rollout_asserts_the_reservation_before_it_runs_anything_else() -> None:
    # The refusal tells the operator that re-running only the failed jobs does
    # not take the reservation again. That holds because the first command
    # `deploy` runs is the reservation assert, which refuses a lease this run
    # no longer owns, before any step that changes a serving revision.
    deploy = _jobs()["deploy"]
    assert "continue-on-error" not in deploy
    steps = deploy["steps"]
    at = next(i for i, step in enumerate(steps) if "run" in step)
    assert steps[at]["run"] == "bash scripts/deploy/deploy_mutex.sh assert"
    assert "if" not in steps[at] and "continue-on-error" not in steps[at]
    # Only checkout and sign-in come before it, so nothing reaches production first.
    assert [step.get("uses", "").split("@")[0] for step in steps[:at]] == [
        "actions/checkout", "google-github-actions/auth", "google-github-actions/setup-gcloud"]


def test_the_regions_asked_are_the_regions_rolled_out() -> None:
    deploy = json.dumps(_jobs()["deploy"])
    assert f"regions=({' '.join(REGIONS)})" in deploy
    assert f"for region in {' '.join(REGIONS)}; do" in deploy
