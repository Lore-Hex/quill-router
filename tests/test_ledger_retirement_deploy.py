"""The two release steps that retire the regional quota and spend-lease ledgers.

``regional_quota_drain_gate.sh`` runs before any revision changes and refuses
the capability-off rollout while escrow is open; ``retire_ledger_workers.sh``
removes the once-a-minute reconciler workers after the ramp. Both are executed
for real here against a recording ``gc`` fake, the same way the regional-quota
activation tests drive their helpers.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
HELPER = ROOT / "scripts/deploy/regional_quota_rollout.sh"
GATE = ROOT / "scripts/deploy/regional_quota_drain_gate.sh"
RETIRE = ROOT / "scripts/deploy/retire_ledger_workers.sh"

REGIONS = ("us-central1", "us-east4")

# The scripts source _lib.sh for PROJECT_ID, SERVICE, the region list and gc.
# The fake below replaces exactly that surface; nothing else from _lib.sh is
# needed, so a real cloud call can never leak through the recorded fake.
_PRELUDE = r'''
set -euo pipefail
PROJECT_ID=quill-cloud-proxy
SERVICE=trusted-router
TR_PRIMARY_REGION=us-central1
TR_CONTROL_PLANE_REGIONS=us-central1,us-east4
TR_LEDGER_RETIRE_RETRY_SLEEP_SECONDS=0
log() { echo "$*" >&2; }
_record() {
  printf '%s\n' "$*" >> "$FIXTURES/calls"
}
gc() {
  _record "$@"
  local region=""
  for argument in "$@"; do
    case "$argument" in
      --region=*) region="${argument#--region=}" ;;
    esac
  done
  case "$1 $2 $3" in
    'scheduler jobs describe')
      if [ -f "$FIXTURES/scheduler-error" ]; then cat "$FIXTURES/scheduler-error" >&2; return 1; fi
      if [ -f "$FIXTURES/scheduler-missing-$4" ]; then
        echo "ERROR: (gcloud.scheduler.jobs.describe) NOT_FOUND: Job not found." >&2; return 1
      fi
      echo ENABLED ;;
    'scheduler jobs delete') ;;
    'run services describe')
      if [ -f "$FIXTURES/service-$region.json" ]; then cat "$FIXTURES/service-$region.json"
      else echo "ERROR: (gcloud.run.services.describe) NOT_FOUND: Service [trusted-router] could not be found." >&2; return 1; fi ;;
    'run revisions describe')
      cat "$FIXTURES/revision-$region.json" ;;
    'logging read '*)
      if [[ "$*" == *"regional_quota.reconcile_complete"* ]]; then cat "$FIXTURES/regional-evidence.json"
      else cat "$FIXTURES/spend-evidence.json"; fi ;;
    'run jobs list')
      if [ -f "$FIXTURES/jobs-$region" ]; then cat "$FIXTURES/jobs-$region"; fi ;;
    'run jobs delete')
      if [ -f "$FIXTURES/delete-failures-$4" ]; then
        remaining="$(cat "$FIXTURES/delete-failures-$4")"
        if [ "$remaining" -gt 0 ]; then
          echo $((remaining - 1)) > "$FIXTURES/delete-failures-$4"
          echo "ERROR: (gcloud.run.jobs.delete) execution still running" >&2
          return 1
        fi
      fi ;;
    *) echo "unexpected cloud call: $*" >&2; return 90 ;;
  esac
}
'''


def _script_body(path: Path) -> str:
    body = path.read_text()
    body = body.replace('SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"', "SCRIPT_DIR=.")
    body = body.replace('source "${SCRIPT_DIR}/_lib.sh"', ":")
    body = body.replace('source "${SCRIPT_DIR}/regional_quota_rollout.sh"', 'source "$HELPER"')
    assert 'source "${SCRIPT_DIR}/' not in body, "every sourced helper must be replaced"
    return body


def _run(tmp_path: Path, script: Path) -> subprocess.CompletedProcess[str]:
    (tmp_path / "calls").write_text("")
    return subprocess.run(  # noqa: S603 - fixed shell, repository-owned script, local fake gc
        ["/bin/bash", "-c", _PRELUDE + _script_body(script)],
        text=True,
        capture_output=True,
        env={**os.environ, "HELPER": str(HELPER), "FIXTURES": str(tmp_path)},
        check=False,
    )


def _calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls").read_text().splitlines()


def _serving(tmp_path: Path, issuance: dict[str, str]) -> None:
    for region, marker in issuance.items():
        (tmp_path / f"service-{region}.json").write_text(json.dumps({
            "status": {"traffic": [{"revisionName": f"trusted-router-{region}-live", "percent": 100}]},
        }))
        (tmp_path / f"revision-{region}.json").write_text(json.dumps({
            "spec": {"containers": [{"env": [
                {"name": "TR_REGIONAL_QUOTA_LEASES_ENABLED", "value": "true"},
                {"name": "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED", "value": marker},
            ]}]},
        }))


def _regional_line(**overrides: int) -> str:
    values = {
        "inspected": 0, "reconciled": 0, "closed": 0, "errors": 0, "backlog": 0,
        "processed": 0, "remaining": 0, "completed": 0, "abandoned": 0, "budget_exhausted": 0,
    }
    values.update(overrides)
    rendered = " ".join(f"{key}={value}" for key, value in values.items())
    return f"INFO:trusted_router.regional_quota_reconcile_cli:regional_quota.reconcile_complete {rendered}"


def _spend_line(**overrides: int) -> str:
    values = {
        "candidates": 0, "open": 0, "recovered": 0, "bound": 0, "closed": 0,
        "deferred": 0, "errors": 0, "dead": 0,
    }
    values.update(overrides)
    rendered = " ".join(f"{key}={value}" for key, value in values.items())
    return f"INFO:__main__:spend_lease.reconcile_complete {rendered}"


def _evidence(tmp_path: Path, regional: list[str], spend: list[str]) -> None:
    (tmp_path / "regional-evidence.json").write_text(json.dumps([{"textPayload": line} for line in regional]))
    (tmp_path / "spend-evidence.json").write_text(json.dumps([{"textPayload": line} for line in spend]))


def _drained(tmp_path: Path) -> None:
    _serving(tmp_path, {region: "false" for region in REGIONS})
    _evidence(tmp_path, [_regional_line()] * 5, [_spend_line()] * 5)


# ---------------------------------------------------------------------------
# regional_quota_drain_gate.sh
# ---------------------------------------------------------------------------


def test_gate_passes_when_every_region_is_off_and_both_reconcilers_are_empty(tmp_path: Path) -> None:
    _drained(tmp_path)
    run = _run(tmp_path, GATE)
    assert run.returncode == 0, run.stderr
    assert "ledger escrow is drained: 5 consecutive empty passes" in run.stderr
    calls = _calls(tmp_path)
    assert sum("run revisions describe" in call for call in calls) == len(REGIONS)
    assert sum(call.startswith("logging read") for call in calls) == 2


def test_gate_is_a_no_op_once_the_reconciler_schedule_is_gone(tmp_path: Path) -> None:
    (tmp_path / "scheduler-missing-trusted-router-regional-quota-reconcile").write_text("")
    run = _run(tmp_path, GATE)
    assert run.returncode == 0, run.stderr
    assert "already retired" in run.stderr
    calls = _calls(tmp_path)
    assert calls == ["scheduler jobs describe trusted-router-regional-quota-reconcile --location=us-central1 --format=value(state)"]


def test_gate_fails_closed_when_the_schedule_cannot_be_read(tmp_path: Path) -> None:
    (tmp_path / "scheduler-error").write_text("ERROR: (gcloud.scheduler.jobs.describe) PERMISSION_DENIED")
    _drained(tmp_path)
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "cannot determine whether" in run.stderr
    assert not any("logging read" in call for call in _calls(tmp_path))


def test_gate_refuses_while_any_serving_revision_still_issues(tmp_path: Path) -> None:
    # A held region keeps serving its old revision; that is exactly the case
    # capability-off must wait for.
    _drained(tmp_path)
    _serving(tmp_path, {"us-central1": "false", "us-east4": "true"})
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "us-east4 still serves TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED=true" in run.stderr
    assert not any("logging read" in call for call in _calls(tmp_path))


def test_gate_refuses_a_missing_issuance_marker(tmp_path: Path) -> None:
    _drained(tmp_path)
    (tmp_path / "revision-us-east4.json").write_text(json.dumps({"spec": {"containers": [{"env": []}]}}))
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "us-east4 still serves TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED=missing" in run.stderr


def test_gate_refuses_an_unreadable_serving_revision(tmp_path: Path) -> None:
    _drained(tmp_path)
    (tmp_path / "service-us-east4.json").unlink()
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "cannot read the serving revision in us-east4" in run.stderr


@pytest.mark.parametrize(
    ("regional", "spend", "message"),
    [
        pytest.param(
            [_regional_line()] * 4 + [_regional_line(inspected=3, backlog=3)],
            [_spend_line()] * 5,
            "regional quota reconciler is not drained (inspected=3 backlog=3)",
            id="regional-backlog",
        ),
        pytest.param(
            [_regional_line()] * 5,
            [_spend_line()] * 4 + [_spend_line(open=1, dead=1)],
            "spend-lease reconciler is not drained (open=1 dead=1)",
            id="spend-open-and-dead",
        ),
        pytest.param(
            [_regional_line()] * 3,
            [_spend_line()] * 5,
            "regional quota reconciler has 3 recent completion(s); require 5",
            id="regional-too-few-passes",
        ),
        pytest.param(
            [_regional_line()] * 5,
            [],
            "spend-lease reconciler has 0 recent completion(s); require 5",
            id="spend-no-passes",
        ),
        pytest.param(
            [_regional_line(errors=2)] + [_regional_line()] * 4,
            [_spend_line()] * 5,
            "regional quota reconciler is not drained (errors=2)",
            id="regional-errors",
        ),
    ],
)
def test_gate_refuses_on_reconciler_evidence(
    tmp_path: Path, regional: list[str], spend: list[str], message: str,
) -> None:
    _serving(tmp_path, {region: "false" for region in REGIONS})
    _evidence(tmp_path, regional, spend)
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert message in run.stderr


def test_gate_ignores_unrelated_log_lines_and_refuses_malformed_evidence(tmp_path: Path) -> None:
    _serving(tmp_path, {region: "false" for region in REGIONS})
    # Five real lines plus noise the filter could have let through.
    regional = [_regional_line()] * 5 + ["INFO:regional_quota.reconciler_start"]
    _evidence(tmp_path, regional, [_spend_line()] * 5)
    assert _run(tmp_path, GATE).returncode == 0

    (tmp_path / "regional-evidence.json").write_text("not json")
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "evidence is not JSON" in run.stderr

    (tmp_path / "regional-evidence.json").write_text(json.dumps(
        [{"textPayload": "regional_quota.reconcile_complete inspected=0"}] * 5
    ))
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "completion lacks backlog,remaining,errors" in run.stderr


# ---------------------------------------------------------------------------
# retire_ledger_workers.sh
# ---------------------------------------------------------------------------


def test_retire_deletes_both_schedules_and_every_matching_worker(tmp_path: Path) -> None:
    (tmp_path / "jobs-us-central1").write_text(
        "trusted-router-regional-quota-reconciler-0ae8c79\n"
        "trusted-router-synthetic-us-central1\n"
        "trusted-router-regional-quota-reconciler-5262dd97\n"
    )
    (tmp_path / "jobs-us-east4").write_text(
        "trusted-router-regional-quota-reconciler-7c4f22f\n"
        "trusted-router-spend-lease-reconciler-7c4f22f\n"
        "trusted-router-trust-reconciler\n"
    )
    run = _run(tmp_path, RETIRE)
    assert run.returncode == 0, run.stderr
    calls = _calls(tmp_path)
    assert "scheduler jobs delete trusted-router-regional-quota-reconcile --location=us-central1 --quiet" in calls
    assert "scheduler jobs delete trusted-router-spend-lease-reconcile --location=us-central1 --quiet" in calls
    deletes = [call for call in calls if call.startswith("run jobs delete")]
    assert deletes == [
        "run jobs delete trusted-router-regional-quota-reconciler-0ae8c79 --region=us-central1 --quiet",
        "run jobs delete trusted-router-regional-quota-reconciler-5262dd97 --region=us-central1 --quiet",
        "run jobs delete trusted-router-regional-quota-reconciler-7c4f22f --region=us-east4 --quiet",
        "run jobs delete trusted-router-spend-lease-reconciler-7c4f22f --region=us-east4 --quiet",
    ]
    # The schedules go before any worker so no new execution can start.
    assert calls.index("scheduler jobs delete trusted-router-spend-lease-reconcile --location=us-central1 --quiet") < calls.index(deletes[0])
    assert "ledger reconciler workers retired (4 job(s) deleted)" in run.stderr


def test_retire_is_idempotent_when_everything_is_already_gone(tmp_path: Path) -> None:
    for scheduler in ("trusted-router-regional-quota-reconcile", "trusted-router-spend-lease-reconcile"):
        (tmp_path / f"scheduler-missing-{scheduler}").write_text("")
    run = _run(tmp_path, RETIRE)
    assert run.returncode == 0, run.stderr
    assert run.stderr.count("is already gone") == 2
    assert "ledger reconciler workers retired (0 job(s) deleted)" in run.stderr
    assert not any("delete" in call for call in _calls(tmp_path))


def test_retire_fails_closed_when_a_schedule_cannot_be_read(tmp_path: Path) -> None:
    (tmp_path / "scheduler-error").write_text("ERROR: (gcloud.scheduler.jobs.describe) PERMISSION_DENIED")
    run = _run(tmp_path, RETIRE)
    assert run.returncode != 0
    assert "cannot read schedule" in run.stderr
    assert not any("delete" in call for call in _calls(tmp_path))


def test_retire_waits_for_a_running_execution_then_gives_up(tmp_path: Path) -> None:
    (tmp_path / "jobs-us-east4").write_text("trusted-router-spend-lease-reconciler-7c4f22f\n")
    job = "trusted-router-spend-lease-reconciler-7c4f22f"
    (tmp_path / f"delete-failures-{job}").write_text("2")
    run = _run(tmp_path, RETIRE)
    assert run.returncode == 0, run.stderr
    assert sum(call.startswith(f"run jobs delete {job}") for call in _calls(tmp_path)) == 3

    (tmp_path / f"delete-failures-{job}").write_text("10")
    run = _run(tmp_path, RETIRE)
    assert run.returncode != 0
    assert f"could not delete {job} in us-east4 after 4 attempts" in run.stderr
    assert sum(call.startswith(f"run jobs delete {job}") for call in _calls(tmp_path)) == 4
