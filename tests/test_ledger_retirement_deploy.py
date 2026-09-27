"""The release interlock that retires the regional quota and spend-lease ledgers.

``scripts/deploy/ledger_retirement.sh`` is sourced by rollout.sh and by the
two release-step wrappers. ``ledger_retirement_gate`` runs before any revision
changes and refuses a ledger-less rollout while escrow could still need one;
``ledger_retire_workers`` removes the reconciler workers afterwards and records
the retirement durably. Both are executed for real here against a recording
``gc`` fake, the same way the regional-quota activation tests drive theirs.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
HELPER = ROOT / "scripts/deploy/regional_quota_rollout.sh"
LIBRARY = ROOT / "scripts/deploy/ledger_retirement.sh"
GATE = ROOT / "scripts/deploy/regional_quota_drain_gate.sh"
RETIRE = ROOT / "scripts/deploy/retire_ledger_workers.sh"

REGIONS = ("us-central1", "us-east4")
REGIONAL_SCHEDULE = "trusted-router-regional-quota-reconcile"
SPEND_SCHEDULE = "trusted-router-spend-lease-reconcile"
REGIONAL_JOB = "trusted-router-regional-quota-reconciler-7c4f22f"
SPEND_JOB = "trusted-router-spend-lease-reconciler-7c4f22f"
MARKER_URI = "gs://tr-deploy-mutex-quill-cloud-proxy/controls/ledger-retirement.json"
# Serving revisions were created an hour ago; drained evidence is from now.
REVISION_CREATED = "2026-09-27T11:00:00Z"
NOW = dt.datetime(2026, 9, 27, 12, 0, tzinfo=dt.UTC)

# The scripts source _lib.sh for PROJECT_ID, SERVICE, the region list, the
# Spanner ids and gc. The fake below replaces exactly that surface, so no real
# cloud call can leak through the recorded fake.
_PRELUDE = r'''
set -euo pipefail
PROJECT_ID=quill-cloud-proxy
SERVICE=trusted-router
RELEASE=abc12345
TR_PRIMARY_REGION=us-central1
TR_CONTROL_PLANE_REGIONS=us-central1,us-east4
SPANNER_INSTANCE_ID=trusted-router-nam6
SPANNER_DATABASE_ID=trusted-router
TR_LEDGER_RETIRE_RETRY_SLEEP_SECONDS=0
TR_LEDGER_RETIRE_EXECUTION_WAIT_ATTEMPTS=3
log() { echo "$*" >&2; }
gc() {
  printf '%s\n' "$*" >> "$FIXTURES/calls"
  local region="" job="" sql=""
  for argument in "$@"; do
    case "$argument" in
      --region=*) region="${argument#--region=}" ;;
      --location=*) region="${argument#--location=}" ;;
      --job=*) job="${argument#--job=}" ;;
      --sql=*) sql="${argument#--sql=}" ;;
    esac
  done
  case "$1 $2 $3" in
    'storage buckets describe')
      echo '{"lifecycle_config":{"rule":[{"action":{"type":"Delete"},"condition":{"age":1,"matchesPrefix":["locks/"]}}]}}' ;;
    'storage objects list')
      if [ -f "$FIXTURES/list-error" ]; then cat "$FIXTURES/list-error" >&2; return 1; fi
      objects='{"bucket":"tr-deploy-mutex-quill-cloud-proxy","name":"controls/regional-quota-issuance.txt"}'
      [ -f "$FIXTURES/marker" ] && objects="$objects,{\"bucket\":\"tr-deploy-mutex-quill-cloud-proxy\",\"name\":\"controls/ledger-retirement.json\"}"
      [ -f "$FIXTURES/targets" ] && objects="$objects,{\"bucket\":\"tr-deploy-mutex-quill-cloud-proxy\",\"name\":\"controls/ledger-retirement-targets.json\"}"
      echo "[$objects]" ;;
    'storage cat '*)
      case "$3" in
        *ledger-retirement-targets.json) file="$FIXTURES/targets" ;;
        *) file="$FIXTURES/marker" ;;
      esac
      if [ -f "$file" ]; then cat "$file"
      else echo 'ERROR: (gcloud.storage.cat) No URLs matched' >&2; return 1; fi ;;
    'storage cp '*)
      case "$4" in
        *ledger-retirement-targets.json) cp "$3" "$FIXTURES/targets" ;;
        *) cp "$3" "$FIXTURES/marker" ;;
      esac ;;
    'spanner databases execute-sql')
      if [ -f "$FIXTURES/spanner-error" ]; then echo "ERROR: (gcloud.spanner.databases.execute-sql) DEADLINE_EXCEEDED" >&2; return 1; fi
      case "$sql" in
        *regional_quota_lease_workspace_open*) key=workspace-open ;;
        *regional_quota_lease_open*) key=open ;;
        *tr_reservation*) key=reservations ;;
        *spend_lease_open*) key=spend ;;
        *) key=unknown ;;
      esac
      if [ -f "$FIXTURES/count-$key" ]; then cat "$FIXTURES/count-$key"; else echo 0; fi ;;
    'scheduler jobs describe')
      if [ -f "$FIXTURES/scheduler-error" ]; then cat "$FIXTURES/scheduler-error" >&2; return 1; fi
      if [ -f "$FIXTURES/scheduler-missing-$4" ]; then
        echo "ERROR: (gcloud.scheduler.jobs.describe) NOT_FOUND: Job not found." >&2; return 1
      fi
      if [ -f "$FIXTURES/scheduler-json-$4" ]; then cat "$FIXTURES/scheduler-json-$4"; else echo '{}'; fi ;;
    'scheduler jobs delete')
      touch "$FIXTURES/scheduler-missing-$4" ;;
    'run services describe')
      if [ -f "$FIXTURES/service-$region.json" ]; then cat "$FIXTURES/service-$region.json"
      else echo "ERROR: (gcloud.run.services.describe) NOT_FOUND: Service [trusted-router] could not be found." >&2; return 1; fi ;;
    'run revisions describe')
      cat "$FIXTURES/revision-$region.json" ;;
    'logging read '*)
      if [[ "$*" == *"regional_quota.reconcile_complete"* ]]; then cat "$FIXTURES/regional-evidence.json"
      else cat "$FIXTURES/spend-evidence.json"; fi ;;
    'run jobs list')
      if [ -f "$FIXTURES/jobs-list-error" ]; then echo "ERROR: (gcloud.run.jobs.list) PERMISSION_DENIED" >&2; return 1; fi
      if [ -f "$FIXTURES/jobs-unreachable" ]; then echo "WARNING: The following regions were unreachable: us-west1" >&2; fi
      if [ -f "$FIXTURES/jobs" ]; then cat "$FIXTURES/jobs"; fi ;;
    'run jobs executions')
      if [ -f "$FIXTURES/executions-$job" ]; then
        remaining="$(cat "$FIXTURES/executions-$job")"
        if [ "$remaining" -gt 0 ]; then
          echo "$((remaining - 1))" > "$FIXTURES/executions-$job"
          printf '%s-exec\t\n' "$job"
        fi
      fi
      printf '%s-done\t2026-09-27T12:00:00Z\n' "$job" ;;
    'run jobs delete')
      if [ -f "$FIXTURES/delete-error-$4" ]; then echo "ERROR: (gcloud.run.jobs.delete) PERMISSION_DENIED" >&2; return 1; fi
      # a deleted job vanishes from later listings
      if [ -f "$FIXTURES/jobs" ]; then
        awk -F'\t' -v gone="$4" '$2 != gone' "$FIXTURES/jobs" > "$FIXTURES/jobs.next"
        mv "$FIXTURES/jobs.next" "$FIXTURES/jobs"
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
    body = body.replace('source "${SCRIPT_DIR}/ledger_retirement.sh"', 'source "$LIBRARY"')
    assert 'source "${SCRIPT_DIR}/' not in body, "every sourced helper must be replaced"
    return body


def _run(tmp_path: Path, script: Path, *, extra: str = "") -> subprocess.CompletedProcess[str]:
    (tmp_path / "calls").write_text("")
    return subprocess.run(  # noqa: S603 - fixed shell, repository-owned script, local fake gc
        ["/bin/bash", "-c", _PRELUDE + extra + _script_body(script)],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "HELPER": str(HELPER),
            "LIBRARY": str(LIBRARY),
            "FIXTURES": str(tmp_path),
        },
        check=False,
    )


def _calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls").read_text().splitlines()


_OFF_MARKERS = {
    "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED": "false",
    "TR_SPEND_LEASE_ISSUANCE_ENABLED": "false",
    "TR_SPEND_LEASE_BINDING_ENABLED": "false",
    "TR_SPEND_LEASE_ADMISSION_ACCEPT": "false",
}


def _serving(tmp_path: Path, env_by_region: dict[str, dict[str, str]], *, created: str = REVISION_CREATED) -> None:
    for region, env in env_by_region.items():
        (tmp_path / f"service-{region}.json").write_text(json.dumps({
            "status": {"traffic": [{"revisionName": f"trusted-router-{region}-live", "percent": 100}]},
        }))
        (tmp_path / f"revision-{region}.json").write_text(json.dumps({
            "metadata": {"creationTimestamp": created},
            "spec": {"containers": [{"env": [{"name": name, "value": value} for name, value in env.items()]}]},
        }))


def _step_one_fleet(tmp_path: Path) -> None:
    # Every region serves a step-1 revision: issuance off, capability still on.
    _serving(tmp_path, {region: {**_OFF_MARKERS, "TR_REGIONAL_QUOTA_LEASES_ENABLED": "true",
                                 "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES": "us-central1=tr-quota-us-central1",
                                 "TR_SPEND_LEASE_BIGTABLE_APP_PROFILES": "us-central1=tr-spend-us-central1"}
                        for region in REGIONS})


def _step_two_fleet(tmp_path: Path) -> None:
    # Every region serves a step-2 revision: capability off, no profile maps.
    _serving(tmp_path, {region: {**_OFF_MARKERS, "TR_REGIONAL_QUOTA_LEASES_ENABLED": "false"} for region in REGIONS})


def _schedule(name: str, job: str, *, state: str = "ENABLED", region: str = "us-east4") -> str:
    return json.dumps({
        "state": state,
        "httpTarget": {
            "uri": f"https://{region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/quill-cloud-proxy/jobs/{job}:run",
            "httpMethod": "POST",
        },
    })


def _schedules(tmp_path: Path) -> None:
    (tmp_path / f"scheduler-json-{REGIONAL_SCHEDULE}").write_text(_schedule(REGIONAL_SCHEDULE, REGIONAL_JOB))
    (tmp_path / f"scheduler-json-{SPEND_SCHEDULE}").write_text(_schedule(SPEND_SCHEDULE, SPEND_JOB))


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


def _entries(lines: list[str], *, at: dt.datetime = NOW) -> list[dict[str, str]]:
    return [
        {"textPayload": line, "timestamp": (at - dt.timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        for index, line in enumerate(lines)
    ]


def _evidence(tmp_path: Path, regional: list[str], spend: list[str], *, at: dt.datetime = NOW) -> None:
    (tmp_path / "regional-evidence.json").write_text(json.dumps(_entries(regional, at=at)))
    (tmp_path / "spend-evidence.json").write_text(json.dumps(_entries(spend, at=at)))


def _drained(tmp_path: Path) -> None:
    _step_one_fleet(tmp_path)
    _schedules(tmp_path)
    _evidence(tmp_path, [_regional_line()] * 5, [_spend_line()] * 5)


def _retired_marker(tmp_path: Path, **overrides: str) -> None:
    marker = {
        "state": "retired",
        "project": "quill-cloud-proxy",
        "spanner_instance": "trusted-router-nam6",
        "spanner_database": "trusted-router",
        "completed_at": "2026-09-27T14:00:00Z",
    }
    marker.update(overrides)
    (tmp_path / "marker").write_text(json.dumps(marker))


# ---------------------------------------------------------------------------
# ledger_retirement_gate
# ---------------------------------------------------------------------------


def test_gate_passes_on_a_drained_step_one_fleet(tmp_path: Path) -> None:
    _drained(tmp_path)
    run = _run(tmp_path, GATE)
    assert run.returncode == 0, run.stderr
    assert "ledger escrow is drained" in run.stderr
    calls = _calls(tmp_path)
    assert sum("run revisions describe" in call for call in calls) == len(REGIONS)
    counts = [call for call in calls if call.startswith("spanner databases execute-sql")]
    assert len(counts) == 4
    assert all("--priority=low" in call and "--timeout=60s" in call for call in counts)
    assert not any("tr_settle_outbox" in call for call in counts)
    # The evidence filter names the schedule's exact target: project, location and job.
    logging = [call for call in calls if call.startswith("logging read")]
    assert len(logging) == 2
    assert f'resource.labels.location="us-east4" AND resource.labels.job_name="{REGIONAL_JOB}"' in logging[0]
    assert 'resource.labels.project_id="quill-cloud-proxy"' in logging[0]
    assert f'resource.labels.location="us-east4" AND resource.labels.job_name="{SPEND_JOB}"' in logging[1]


def test_gate_stands_down_on_a_recorded_retirement_only_while_the_fleet_still_honours_it(tmp_path: Path) -> None:
    _retired_marker(tmp_path)
    _step_two_fleet(tmp_path)
    run = _run(tmp_path, GATE)
    assert run.returncode == 0, run.stderr
    assert "recorded as complete and every serving revision still runs without the ledgers" in run.stderr
    # The marker waives the worker evidence only; Spanner is always read.
    assert sum(call.startswith("spanner") for call in _calls(tmp_path)) == 4
    assert not any(call.startswith("logging") for call in _calls(tmp_path))

    # Escrow created during a temporary rollback outlives the traffic
    # restoration; the fleet looks retired again, Spanner does not.
    (tmp_path / "count-reservations").write_text("2\n")
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "Spanner still holds 2 unsettled RegionalCredits reservations" in run.stderr
    (tmp_path / "count-reservations").unlink()

    # A traffic rollback restored a capability-on revision: the full gate is
    # back. The workers are gone, so Spanner decides (with the warnings).
    _serving(tmp_path, {"us-central1": {**_OFF_MARKERS, "TR_REGIONAL_QUOTA_LEASES_ENABLED": "false"},
                        "us-east4": {**_OFF_MARKERS, "TR_REGIONAL_QUOTA_LEASES_ENABLED": "true",
                                     "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES": "us-central1=tr-quota-us-central1"}})
    for schedule in (REGIONAL_SCHEDULE, SPEND_SCHEDULE):
        (tmp_path / f"scheduler-missing-{schedule}").write_text("")
    run = _run(tmp_path, GATE)
    assert run.returncode == 0, run.stderr
    assert "carries lease capability again; running the full gate" in run.stderr
    assert any(call.startswith("spanner") for call in _calls(tmp_path))
    (tmp_path / "count-open").write_text("1\n")
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "Spanner still holds 1 open regional lease index rows" in run.stderr


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param({"state": "started"}, id="incomplete"),
        pytest.param({"project": "another-project"}, id="other-project"),
        pytest.param({"spanner_instance": "other-instance"}, id="other-instance"),
        pytest.param({"spanner_database": "other-db"}, id="other-database"),
    ],
)
def test_gate_ignores_a_marker_that_does_not_record_this_retirement(tmp_path: Path, marker: dict[str, str]) -> None:
    _retired_marker(tmp_path, **marker)
    _drained(tmp_path)
    run = _run(tmp_path, GATE)
    assert run.returncode == 0, run.stderr
    assert "treating the ledgers as live" in run.stderr
    assert any(call.startswith("spanner") for call in _calls(tmp_path))


def test_gate_aborts_when_the_control_prefix_cannot_be_listed(tmp_path: Path) -> None:
    (tmp_path / "list-error").write_text("ERROR: (gcloud.storage.objects.list) PERMISSION_DENIED")
    _drained(tmp_path)
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "cannot list the control prefix" in run.stderr
    assert not any(call.startswith("spanner") for call in _calls(tmp_path))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED", "true"),
        ("TR_SPEND_LEASE_ISSUANCE_ENABLED", "true"),
        ("TR_SPEND_LEASE_BINDING_ENABLED", "true"),
        ("TR_SPEND_LEASE_ADMISSION_ACCEPT", "true"),
    ],
)
def test_gate_refuses_while_any_serving_revision_can_still_mint(tmp_path: Path, name: str, value: str) -> None:
    # A held region keeps serving its older revision; both ledgers' issuance,
    # binding and admission must be off on every one of them.
    _drained(tmp_path)
    env = {**_OFF_MARKERS, "TR_REGIONAL_QUOTA_LEASES_ENABLED": "true", name: value}
    _serving(tmp_path, {"us-central1": {**_OFF_MARKERS}, "us-east4": env})
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert f"us-east4 still serves {name}={value}" in run.stderr
    assert not any(call.startswith("spanner") for call in _calls(tmp_path))


def test_gate_accepts_an_absent_marker_because_config_defaults_it_off(tmp_path: Path) -> None:
    # A revision that never carried the switch runs config.py's default,
    # which is false for every marker the gate inspects.
    _drained(tmp_path)
    _serving(tmp_path, {"us-central1": {**_OFF_MARKERS}, "us-east4": {}})
    run = _run(tmp_path, GATE)
    assert run.returncode == 0, run.stderr
    assert "us-east4: TR_SPEND_LEASE_ISSUANCE_ENABLED is not set on the serving revision; it defaults to false" in run.stderr


def test_gate_refuses_an_unreadable_or_undated_serving_revision(tmp_path: Path) -> None:
    _drained(tmp_path)
    (tmp_path / "service-us-east4.json").unlink()
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "cannot read the serving revision in us-east4" in run.stderr

    _drained(tmp_path)
    (tmp_path / "revision-us-east4.json").write_text(json.dumps({"spec": {"containers": [{"env": []}]}}))
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "serving revision in us-east4 has no creation time" in run.stderr


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("open", "open regional lease index rows"),
        ("workspace-open", "open regional lease workspace index rows"),
        ("reservations", "unsettled RegionalCredits reservations"),
        ("spend", "unfinished spend-lease open rows"),
    ],
)
def test_gate_refuses_on_spanner_open_work(tmp_path: Path, key: str, label: str) -> None:
    # Spanner is the source of truth: an empty reconciler pass does not prove
    # that an unsettled RegionalCredits reservation (whose pending or dead
    # settle intent still needs the ledger) is gone, and the spend reconciler
    # only counts rows that are DUE now.
    _drained(tmp_path)
    (tmp_path / f"count-{key}").write_text("3\n")
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert f"Spanner still holds 3 {label}" in run.stderr
    assert not any(call.startswith("logging read") for call in _calls(tmp_path))


def test_gate_fails_closed_on_a_spanner_error_or_a_non_numeric_count(tmp_path: Path) -> None:
    _drained(tmp_path)
    (tmp_path / "spanner-error").write_text("")
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "cannot count open regional lease index rows" in run.stderr

    (tmp_path / "spanner-error").unlink()
    (tmp_path / "count-open").write_text("\n")
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "count is not a number" in run.stderr


def test_gate_relies_on_spanner_alone_once_a_schedule_is_gone_without_a_marker(tmp_path: Path) -> None:
    # A partial teardown (schedule deleted, marker never written) must not
    # block forever, but it is not proof either: Spanner still decides.
    _drained(tmp_path)
    (tmp_path / f"scheduler-missing-{REGIONAL_SCHEDULE}").write_text("")
    run = _run(tmp_path, GATE)
    assert run.returncode == 0, run.stderr
    assert "regional quota reconciler schedule is already gone without a retirement marker" in run.stderr
    logging = [call for call in _calls(tmp_path) if call.startswith("logging read")]
    assert len(logging) == 1 and SPEND_JOB in logging[0]

    (tmp_path / "count-reservations").write_text("1\n")
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "Spanner still holds 1 unsettled RegionalCredits reservations" in run.stderr


def test_gate_refuses_a_paused_or_unverifiable_schedule(tmp_path: Path) -> None:
    _drained(tmp_path)
    (tmp_path / f"scheduler-json-{REGIONAL_SCHEDULE}").write_text(_schedule(REGIONAL_SCHEDULE, REGIONAL_JOB, state="PAUSED"))
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "regional quota reconciler schedule is PAUSED; it must be draining" in run.stderr

    (tmp_path / f"scheduler-json-{REGIONAL_SCHEDULE}").write_text(json.dumps({"state": "ENABLED", "httpTarget": {"uri": "https://example.com/hook"}}))
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "does not target a Cloud Run job of this project" in run.stderr

    (tmp_path / "scheduler-error").write_text("ERROR: (gcloud.scheduler.jobs.describe) PERMISSION_DENIED")
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "cannot read schedule" in run.stderr


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
def test_gate_refuses_on_reconciler_evidence(tmp_path: Path, regional: list[str], spend: list[str], message: str) -> None:
    _drained(tmp_path)
    _evidence(tmp_path, regional, spend)
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert message in run.stderr


def test_gate_requires_evidence_after_the_newest_revision_plus_the_drain_interval(tmp_path: Path) -> None:
    # Passes recorded before the last issuance-off revision existed (or within
    # the drain interval after it) prove nothing about what that revision's
    # predecessor may have minted in its final minutes.
    _drained(tmp_path)
    _serving(tmp_path, {region: {**_OFF_MARKERS, "TR_REGIONAL_QUOTA_LEASES_ENABLED": "true"} for region in REGIONS},
             created="2026-09-27T11:55:00Z")
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "predates the drain cutoff 2026-09-27T12:05:00+00:00 (newest serving revision + 600s)" in run.stderr

    run = _run(tmp_path, GATE, extra="TR_LEDGER_DRAIN_INTERVAL_SECONDS=60\n")
    assert run.returncode == 0, run.stderr

    (tmp_path / "regional-evidence.json").write_text(json.dumps([{"textPayload": _regional_line()}] * 5))
    run = _run(tmp_path, GATE, extra="TR_LEDGER_DRAIN_INTERVAL_SECONDS=60\n")
    assert run.returncode != 0
    assert "completion has no timestamp" in run.stderr


def test_gate_ignores_unrelated_log_lines_and_refuses_malformed_evidence(tmp_path: Path) -> None:
    _drained(tmp_path)
    _evidence(tmp_path, [_regional_line()] * 5 + ["INFO:regional_quota.reconciler_start"], [_spend_line()] * 5)
    assert _run(tmp_path, GATE).returncode == 0

    (tmp_path / "regional-evidence.json").write_text("not json")
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "evidence is not JSON" in run.stderr

    (tmp_path / "regional-evidence.json").write_text(json.dumps(
        _entries(["regional_quota.reconcile_complete inspected=0"] * 5)
    ))
    run = _run(tmp_path, GATE)
    assert run.returncode != 0
    assert "completion lacks backlog,remaining,errors" in run.stderr


# ---------------------------------------------------------------------------
# ledger_retire_workers
# ---------------------------------------------------------------------------


def _workers(tmp_path: Path, extra: str = "") -> None:
    (tmp_path / "jobs").write_text(
        "us-central1\ttrusted-router-regional-quota-reconciler-0ae8c79\n"
        "us-central1\ttrusted-router-synthetic-us-central1\n"
        "us-central1\ttrusted-router-regional-quota-reconciler-5262dd97\n"
        f"us-east4\t{REGIONAL_JOB}\n"
        f"us-east4\t{SPEND_JOB}\n"
        "us-east4\ttrusted-router-trust-reconciler\n"
        + extra
    )


def test_retire_deletes_schedules_then_workers_and_records_the_retirement(tmp_path: Path) -> None:
    _step_two_fleet(tmp_path)
    _schedules(tmp_path)
    _workers(tmp_path)
    run = _run(tmp_path, RETIRE)
    assert run.returncode == 0, run.stderr
    calls = _calls(tmp_path)
    schedule_deletes = [call for call in calls if call.startswith("scheduler jobs delete")]
    assert schedule_deletes == [
        f"scheduler jobs delete {REGIONAL_SCHEDULE} --location=us-central1 --quiet",
        f"scheduler jobs delete {SPEND_SCHEDULE} --location=us-central1 --quiet",
    ]
    job_deletes = [call for call in calls if call.startswith("run jobs delete")]
    assert job_deletes == [
        "run jobs delete trusted-router-regional-quota-reconciler-0ae8c79 --region=us-central1 --quiet",
        "run jobs delete trusted-router-regional-quota-reconciler-5262dd97 --region=us-central1 --quiet",
        f"run jobs delete {REGIONAL_JOB} --region=us-east4 --quiet",
        f"run jobs delete {SPEND_JOB} --region=us-east4 --quiet",
    ]
    # Schedules go first so no new execution starts; each job's executions
    # are checked before its definition is deleted.
    assert calls.index(schedule_deletes[-1]) < calls.index(job_deletes[0])
    for delete in job_deletes:
        job = delete.split()[3]
        region = delete.split()[4]
        assert calls.index(f"run jobs executions list --job={job} {region} --format=value(metadata.name,status.completionTime)") < calls.index(delete)
    # Absence is proven project-wide before the marker is written.
    listings = [index for index, call in enumerate(calls) if call.startswith("run jobs list")]
    marker_write = next(
        index for index, call in enumerate(calls)
        if call.startswith("storage cp") and call.endswith("controls/ledger-retirement.json --quiet")
    )
    assert len(listings) == 2 and listings[-1] > calls.index(job_deletes[-1]) and listings[-1] < marker_write
    marker = json.loads((tmp_path / "marker").read_text())
    assert marker["state"] == "retired"
    assert marker["project"] == "quill-cloud-proxy" and marker["spanner_database"] == "trusted-router"
    assert marker["spanner_instance"] == "trusted-router-nam6"
    # Targets were recorded before the first schedule was deleted.
    targets = json.loads((tmp_path / "targets").read_text())
    assert targets == {"targets": sorted([REGIONAL_JOB, SPEND_JOB])}
    assert calls.index([c for c in calls if c.startswith("storage cp") and "targets" in c][0]) < calls.index(schedule_deletes[0])
    assert marker["workers_deleted"] == 4 and marker["release"] == "abc12345"
    assert "ledger reconciler workers retired (4 job(s) deleted)" in run.stderr


def test_retire_is_a_no_op_once_recorded(tmp_path: Path) -> None:
    _retired_marker(tmp_path)
    run = _run(tmp_path, RETIRE)
    assert run.returncode == 0, run.stderr
    assert "already recorded as complete" in run.stderr
    assert not any(call.startswith(("run ", "scheduler", "spanner")) for call in _calls(tmp_path))


def test_retire_defers_while_a_region_still_serves_capability_or_a_profile_map(tmp_path: Path) -> None:
    _serving(tmp_path, {"us-central1": {**_OFF_MARKERS, "TR_REGIONAL_QUOTA_LEASES_ENABLED": "false"},
                        "us-east4": {**_OFF_MARKERS, "TR_REGIONAL_QUOTA_LEASES_ENABLED": "true"}})
    _schedules(tmp_path)
    _workers(tmp_path)
    run = _run(tmp_path, RETIRE)
    assert run.returncode == 0, run.stderr
    assert "::warning::ledger workers kept" in run.stdout
    assert "us-east4 still serves TR_REGIONAL_QUOTA_LEASES_ENABLED=true" in run.stderr
    assert not any("delete" in call for call in _calls(tmp_path))
    assert not (tmp_path / "marker").exists()

    _serving(tmp_path, {"us-central1": {**_OFF_MARKERS, "TR_REGIONAL_QUOTA_LEASES_ENABLED": "false"},
                        "us-east4": {**_OFF_MARKERS, "TR_REGIONAL_QUOTA_LEASES_ENABLED": "false",
                                     "TR_SPEND_LEASE_BIGTABLE_APP_PROFILES": "us-central1=tr-spend-us-central1"}})
    run = _run(tmp_path, RETIRE)
    assert run.returncode == 0, run.stderr
    assert "us-east4 still serves TR_SPEND_LEASE_BIGTABLE_APP_PROFILES=us-central1=tr-spend-us-central1" in run.stderr
    assert not any("delete" in call for call in _calls(tmp_path))


def test_retire_fails_when_spanner_regressed_since_the_gate(tmp_path: Path) -> None:
    _step_two_fleet(tmp_path)
    _schedules(tmp_path)
    _workers(tmp_path)
    (tmp_path / "count-spend").write_text("2\n")
    run = _run(tmp_path, RETIRE)
    assert run.returncode != 0
    assert "Spanner still holds 2 unfinished spend-lease open rows" in run.stderr
    assert not any("delete" in call for call in _calls(tmp_path))


def test_retire_waits_for_running_executions_then_gives_up(tmp_path: Path) -> None:
    _step_two_fleet(tmp_path)
    _schedules(tmp_path)
    (tmp_path / "jobs").write_text(f"us-east4\t{SPEND_JOB}\n")
    (tmp_path / f"executions-{SPEND_JOB}").write_text("2")
    run = _run(tmp_path, RETIRE)
    assert run.returncode == 0, run.stderr
    calls = _calls(tmp_path)
    assert sum(call.startswith(f"run jobs executions list --job={SPEND_JOB}") for call in calls) == 3
    assert any(call.startswith(f"run jobs delete {SPEND_JOB}") for call in calls)

    (tmp_path / "marker").unlink()
    (tmp_path / "jobs").write_text(f"us-east4\t{SPEND_JOB}\n")
    (tmp_path / f"executions-{SPEND_JOB}").write_text("10")
    _schedules(tmp_path)
    run = _run(tmp_path, RETIRE)
    assert run.returncode != 0
    assert f"{SPEND_JOB} in us-east4 still has running executions after 3 checks" in run.stderr
    assert not any(call.startswith(f"run jobs delete {SPEND_JOB}") for call in _calls(tmp_path))
    assert not (tmp_path / "marker").exists()


def test_retire_fails_closed_on_list_or_delete_errors(tmp_path: Path) -> None:
    _step_two_fleet(tmp_path)
    _schedules(tmp_path)
    _workers(tmp_path)
    (tmp_path / "jobs-list-error").write_text("")
    run = _run(tmp_path, RETIRE)
    assert run.returncode != 0
    assert "cannot list Cloud Run jobs" in run.stderr
    assert not (tmp_path / "marker").exists()

    (tmp_path / "jobs-list-error").unlink()
    _schedules(tmp_path)
    (tmp_path / f"delete-error-{SPEND_JOB}").write_text("")
    run = _run(tmp_path, RETIRE)
    assert run.returncode != 0
    assert f"cannot delete {SPEND_JOB} in us-east4" in run.stderr
    assert not (tmp_path / "marker").exists()


def test_retire_refuses_an_incomplete_project_wide_listing(tmp_path: Path) -> None:
    # Unreachable regions come back as a warning with exit 0 and a partial
    # list; certifying absence from that would lose a worker.
    _step_two_fleet(tmp_path)
    _schedules(tmp_path)
    _workers(tmp_path)
    (tmp_path / "jobs-unreachable").write_text("")
    run = _run(tmp_path, RETIRE)
    assert run.returncode != 0
    assert "Cloud Run job listing is incomplete: WARNING: The following regions were unreachable: us-west1" in run.stderr
    assert not any(call.startswith("run jobs delete") for call in _calls(tmp_path))
    assert not (tmp_path / "marker").exists()


def test_retire_finds_exact_named_and_schedule_targeted_workers_project_wide(tmp_path: Path) -> None:
    # Deployers accept exact job names (TR_*_RECONCILER_JOB) and custom
    # locations; the schedules name their own targets. None of those match the
    # historical prefixes, and none live in the control-plane region list.
    _step_two_fleet(tmp_path)
    (tmp_path / f"scheduler-json-{REGIONAL_SCHEDULE}").write_text(_schedule(REGIONAL_SCHEDULE, "quota-custom-worker", region="europe-west4"))
    (tmp_path / f"scheduler-json-{SPEND_SCHEDULE}").write_text(_schedule(SPEND_SCHEDULE, SPEND_JOB, region="us-west1"))
    (tmp_path / "jobs").write_text(
        "europe-west4\tquota-custom-worker\n"
        f"us-west1\t{SPEND_JOB}\n"
        "southamerica-east1\tspend-explicit-worker\n"
        "southamerica-east1\ttrusted-router-synthetic-southamerica-east1\n"
    )
    extra = "TR_REGIONAL_QUOTA_RECONCILER_SCHEDULER_REGION=us-east4\nTR_SPEND_LEASE_RECONCILER_JOB=spend-explicit-worker\n"
    run = _run(tmp_path, RETIRE, extra=extra)
    assert run.returncode == 0, run.stderr
    calls = _calls(tmp_path)
    assert f"scheduler jobs describe {REGIONAL_SCHEDULE} --location=us-east4 --format=json" in calls
    assert f"scheduler jobs delete {REGIONAL_SCHEDULE} --location=us-east4 --quiet" in calls
    deletes = {(call.split()[3], call.split()[4]) for call in calls if call.startswith("run jobs delete")}
    assert deletes == {
        ("quota-custom-worker", "--region=europe-west4"),
        (SPEND_JOB, "--region=us-west1"),
        ("spend-explicit-worker", "--region=southamerica-east1"),
    }
    assert json.loads((tmp_path / "marker").read_text())["workers_deleted"] == 3


def test_retire_retry_after_an_interrupted_teardown_still_finds_every_worker(tmp_path: Path) -> None:
    # First run: schedules deleted, then a delete fails. The schedules that
    # named the workers - including a custom target that matches neither
    # prefix nor override - are gone; the retry must still find them all
    # through the targets recorded before the first deletion.
    _step_two_fleet(tmp_path)
    (tmp_path / f"scheduler-json-{REGIONAL_SCHEDULE}").write_text(_schedule(REGIONAL_SCHEDULE, "quota-custom-worker", region="us-west1"))
    (tmp_path / f"scheduler-json-{SPEND_SCHEDULE}").write_text(_schedule(SPEND_SCHEDULE, SPEND_JOB, region="us-west1"))
    (tmp_path / "jobs").write_text(f"us-west1\tquota-custom-worker\nus-west1\t{SPEND_JOB}\n")
    (tmp_path / "delete-error-quota-custom-worker").write_text("")
    run = _run(tmp_path, RETIRE)
    assert run.returncode != 0
    assert not (tmp_path / "marker").exists()
    assert (tmp_path / f"scheduler-missing-{REGIONAL_SCHEDULE}").exists()
    assert json.loads((tmp_path / "targets").read_text()) == {"targets": sorted(["quota-custom-worker", SPEND_JOB])}
    assert (tmp_path / "jobs").read_text() == f"us-west1\tquota-custom-worker\nus-west1\t{SPEND_JOB}\n"

    (tmp_path / "delete-error-quota-custom-worker").unlink()
    run = _run(tmp_path, RETIRE)
    assert run.returncode == 0, run.stderr
    assert "schedule trusted-router-regional-quota-reconcile is already gone" in run.stderr
    assert "deleted worker quota-custom-worker in us-west1" in run.stderr
    assert f"deleted worker {SPEND_JOB} in us-west1" in run.stderr
    assert (tmp_path / "jobs").read_text() == ""
    assert json.loads((tmp_path / "marker").read_text())["workers_deleted"] == 2
