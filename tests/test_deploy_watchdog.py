from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_watchdog() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "watchdog.py"
    spec = importlib.util.spec_from_file_location("trusted_router_deploy_watchdog", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_watchdog_prefers_router_core_slo_region_status(monkeypatch) -> None:
    watchdog = _load_watchdog()
    payload = {
        "data": {
            "slo_classes": {
                "router_core": {
                    "current_by_region": {
                        "us-central1": {"status": "up"},
                        "europe-west4": {"status": "down"},
                    }
                }
            },
            # Fallback shape says the opposite; the SLO shape should win.
            "current": {
                "checks": [
                    {"target_region": "us-central1", "effective_status": "down"},
                    {"target_region": "europe-west4", "effective_status": "up"},
                ]
            },
        }
    }

    def fake_urlopen(_url: str, timeout: int) -> _FakeResponse:
        assert timeout == 10
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", fake_urlopen)

    assert watchdog.fetch_per_region(
        "https://trustedrouter.com/status.json",
        ["us-central1", "europe-west4"],
    ) == {"us-central1": "up", "europe-west4": "down"}


def test_watchdog_falls_back_to_current_checks_and_normalizes_degraded(monkeypatch) -> None:
    watchdog = _load_watchdog()
    payload = {
        "data": {
            "current": {
                "checks": [
                    {"target_region": "us-central1", "effective_status": "routing_degraded"},
                    {"target_region": "europe-west4", "effective_status": "up"},
                ]
            }
        }
    }

    def fake_urlopen(_url: str, timeout: int) -> _FakeResponse:
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", fake_urlopen)

    assert watchdog.fetch_per_region(
        "https://trustedrouter.com/status.json",
        ["us-central1", "europe-west4"],
    ) == {"us-central1": "degraded", "europe-west4": "up"}


def test_trust_degraded_is_preserved_not_folded_into_degraded(monkeypatch) -> None:
    """An attested gateway that cannot prove what it runs is BROKEN.

    trust_degraded used to normalize to "degraded", and rollback fires
    only on "down" — so an attestation regression could ship and never
    trigger a rollback. That is the one failure this product cannot
    afford to treat as churn.
    """
    watchdog = _load_watchdog()
    payload = {
        "data": {
            "current": {
                "checks": [
                    {"target_region": "eu-west-3", "effective_status": "trust_degraded"},
                    {"target_region": "us-central1", "effective_status": "up"},
                ]
            }
        }
    }

    def fake_urlopen(_url: str, timeout: int) -> _FakeResponse:
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", fake_urlopen)

    assert watchdog.fetch_per_region(
        "https://aws.trustedrouter.com/status.json",
        ["eu-west-3", "us-central1"],
    ) == {"eu-west-3": "trust_degraded", "us-central1": "up"}


def test_rollback_statuses_cover_down_and_trust_degraded() -> None:
    watchdog = _load_watchdog()
    assert "down" in watchdog.ROLLBACK_STATUSES
    assert "trust_degraded" in watchdog.ROLLBACK_STATUSES
    # Plain degraded stays out: synthetics flap during a rolling update.
    assert "degraded" not in watchdog.ROLLBACK_STATUSES
    assert "up" not in watchdog.ROLLBACK_STATUSES
    assert "unknown" not in watchdog.ROLLBACK_STATUSES


def test_normalize_maps_routing_degraded_but_not_trust_degraded() -> None:
    watchdog = _load_watchdog()
    assert watchdog.normalize_watchdog_status("routing_degraded") == "degraded"
    assert watchdog.normalize_watchdog_status("trust_degraded") == "trust_degraded"
    assert watchdog.normalize_watchdog_status("down") == "down"
    assert watchdog.normalize_watchdog_status("anything else") == "unknown"


def test_trust_degraded_ranks_with_down_in_severity() -> None:
    watchdog = _load_watchdog()
    assert watchdog.SEVERITY["trust_degraded"] == watchdog.SEVERITY["down"]
    assert watchdog.SEVERITY["trust_degraded"] > watchdog.SEVERITY["degraded"]


def _run_staged_probe(
    tmp_path: Path,
    *,
    console_code: str,
    session_code: str,
    resolved_tag_revision: str = "new-rev",
    remove_failures: int = 0,
    remove_always_fails: bool = False,
    remove_leaves_tag: bool = False,
    traffic_shift_failure_pct: int | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    call_log = tmp_path / "calls.log"
    call_log.write_text("")
    tag_state = tmp_path / "tag-state"
    tag_state.write_text("")
    remove_failures_state = tmp_path / "remove-failures"
    remove_failures_state.write_text(f"{remove_failures}\n")
    gcloud = stub_bin / "gcloud"
    gcloud.write_text(
        """#!/usr/bin/env bash
printf 'gcloud %s\\n' "$*" >>"$STAGED_CALL_LOG"
if [ -n "$STAGED_TRAFFIC_SHIFT_FAILURE_PCT" ] \
    && [[ " $* " == *" --to-revisions=new-rev=${STAGED_TRAFFIC_SHIFT_FAILURE_PCT},"* ]]; then
  exit 1
fi
case " $* " in
  *" --update-tags="*) printf '%s\\n' "$STAGED_RESOLVED_TAG_REV" >"$STAGED_TAG_STATE" ;;
  *" --remove-tags="*)
    remaining="$(cat "$STAGED_REMOVE_FAILURES_STATE")"
    if [ "$STAGED_REMOVE_ALWAYS_FAILS" = "1" ] || [ "$remaining" -gt 0 ]; then
      if [ "$remaining" -gt 0 ]; then
        printf '%s\\n' "$((remaining - 1))" >"$STAGED_REMOVE_FAILURES_STATE"
      fi
      exit 1
    fi
    if [ "$STAGED_REMOVE_LEAVES_TAG" != "1" ]; then
      : >"$STAGED_TAG_STATE"
    fi
    ;;
  *"status.traffic[?tag="*)
    printf '%s\\n' 'TEST ERROR: unsupported gcloud resource projection' >&2
    exit 64
    ;;
  *" --format=json"*)
    tagged_revision="$(cat "$STAGED_TAG_STATE")"
    if [ -n "$tagged_revision" ]; then
      printf '{"status":{"traffic":[{"percent":100,"revisionName":"old-rev"},{"percent":0,"revisionName":"%s","tag":"staged-probe"}]}}\\n' "$tagged_revision"
    else
      printf '%s\\n' '{"status":{"traffic":[{"percent":100,"revisionName":"old-rev"}]}}'
    fi
    ;;
  *"status.url"*) printf '%s\\n' 'https://trusted-router-hash-uc.a.run.app' ;;
esac
"""
    )
    curl = stub_bin / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
url="${*: -1}"
printf 'curl %s\\n' "$url" >>"$STAGED_CALL_LOG"
case "$url" in
  */console) code="$STAGED_CONSOLE_CODE" ;;
  */auth/session) code="$STAGED_SESSION_CODE" ;;
  *) exit 9 ;;
esac
if [ "$code" = "EMPTY" ]; then exit 7; fi
printf '%s' "$code"
[ "$code" != "000" ]
"""
    )
    sleep = stub_bin / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep.chmod(0o755)
    python3 = stub_bin / "python3"
    python3.write_text(
        """#!/usr/bin/env bash
if [ "$1" = "-c" ]; then
  exec "$STAGED_REAL_PYTHON" "$@"
fi
exit 0
"""
    )
    python3.chmod(0o755)
    gcloud.chmod(0o755)
    curl.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "staged_traffic.sh"
    env = {
        **os.environ,
        "PATH": f"{stub_bin}:/bin:/usr/bin",
        "PROJECT_ID": "test-project",
        "SERVICE": "trusted-router",
        "STAGED_CALL_LOG": str(call_log),
        "STAGED_CONSOLE_CODE": console_code,
        "STAGED_SESSION_CODE": session_code,
        "STAGED_RESOLVED_TAG_REV": resolved_tag_revision,
        "STAGED_TAG_STATE": str(tag_state),
        "STAGED_REMOVE_FAILURES_STATE": str(remove_failures_state),
        "STAGED_REMOVE_ALWAYS_FAILS": "1" if remove_always_fails else "0",
        "STAGED_REMOVE_LEAVES_TAG": "1" if remove_leaves_tag else "0",
        "STAGED_TRAFFIC_SHIFT_FAILURE_PCT": (
            "" if traffic_shift_failure_pct is None else str(traffic_shift_failure_pct)
        ),
        "STAGED_REAL_PYTHON": sys.executable,
        "TR_LEGACY_PROBE_RETRY_SECONDS": "0",
        "TR_PROBE_TAG_REMOVE_RETRY_SECONDS": "0",
        # This helper exercises a traffic ramp nested inside the workflow's
        # already-acquired deployment-mutex scope. Dedicated mutex tests cover
        # direct/manual acquisition against a generation-aware storage stub.
        "TR_DEPLOY_MUTEX_OPERATION": "watchdog-test-operation",
        "TR_DEPLOY_MUTEX_GENERATION": "1",
    }
    run = subprocess.run(  # noqa: S603 - fixed local script and stubbed PATH
        ["/bin/bash", str(script), "us-central1", "new-rev", "old-rev"],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    return run, call_log.read_text().splitlines()


def test_probe_tag_resolution_uses_json_and_unset_tag_resolves_to_empty(
    tmp_path: Path,
) -> None:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    call_log = tmp_path / "calls.log"
    service_json = tmp_path / "service.json"
    gcloud = stub_bin / "gcloud"
    gcloud.write_text(
        """#!/usr/bin/env bash
printf 'gcloud %s\\n' "$*" >>"$PROBE_CALL_LOG"
case " $* " in
  # Match real gcloud: this unsupported projection succeeds with empty output.
  # The call-log assertion below proves the implementation never takes it.
  *"status.traffic[?tag="*) exit 0 ;;
  *" --format=json"*) cat "$PROBE_SERVICE_JSON" ;;
  *) exit 9 ;;
esac
"""
    )
    gcloud.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{stub_bin}:/bin:/usr/bin",
        "PROBE_CALL_LOG": str(call_log),
        "PROBE_SERVICE_JSON": str(service_json),
    }
    command = (
        f"source {ROOT / 'scripts/deploy/_cloud_run_revision_probe.sh'}; "
        "cloud_run_probe_tag_revision trusted-router us-central1 test-project "
        "staged-probe"
    )

    service_json.write_text(
        json.dumps(
            {
                "status": {
                    "traffic": [
                        {"percent": 100, "revisionName": "old-rev"},
                        {
                            "percent": 0,
                            "revisionName": "new-rev",
                            "tag": "staged-probe",
                        },
                    ]
                }
            }
        )
    )
    resolved = subprocess.run(  # noqa: S603 - fixed shell with stubbed gcloud
        ["/bin/bash", "-c", command],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert resolved.returncode == 0, resolved.stderr
    assert resolved.stdout == "new-rev\n"
    calls = call_log.read_text().splitlines()
    assert calls == [
        "gcloud run services describe trusted-router --region=us-central1 "
        "--project=test-project --format=json"
    ]

    call_log.write_text("")
    service_json.write_text(
        json.dumps(
            {
                "status": {
                    "traffic": [{"percent": 100, "revisionName": "old-rev"}]
                }
            }
        )
    )
    unresolved = subprocess.run(  # noqa: S603 - fixed shell with stubbed gcloud
        ["/bin/bash", "-c", command],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert unresolved.returncode == 0, unresolved.stderr
    assert unresolved.stdout == ""
    assert call_log.read_text().splitlines() == calls


def _resolve_probe_tag(service_document: dict[str, object]) -> subprocess.CompletedProcess[str]:
    command = (
        f"source {ROOT / 'scripts/deploy/_cloud_run_revision_probe.sh'}; "
        "gcloud() { printf '%s\\n' \"$PROBE_SERVICE_JSON\"; }; "
        "cloud_run_probe_tag_revision trusted-router us-central1 test-project "
        "staged-probe"
    )
    return subprocess.run(  # noqa: S603 - fixed shell with in-process gcloud stub
        ["/bin/bash", "-c", command],
        capture_output=True,
        text=True,
        env={**os.environ, "PROBE_SERVICE_JSON": json.dumps(service_document)},
        timeout=10,
    )


def test_probe_tag_resolution_rejects_duplicate_tag_entries() -> None:
    resolved = _resolve_probe_tag(
        {
            "status": {
                "traffic": [
                    {"tag": "staged-probe", "revisionName": "first-rev"},
                    {"tag": "staged-probe", "revisionName": "second-rev"},
                ]
            }
        }
    )

    assert resolved.returncode != 0
    assert resolved.stdout == ""
    assert "more than one traffic entry" in resolved.stderr


def test_probe_tag_resolution_rejects_empty_revision_name() -> None:
    resolved = _resolve_probe_tag(
        {"status": {"traffic": [{"tag": "staged-probe", "revisionName": ""}]}}
    )

    assert resolved.returncode != 0
    assert resolved.stdout == ""
    assert "empty revisionName" in resolved.stderr


@pytest.mark.parametrize(
    ("console_code", "session_code", "expected_rc", "rolls_back"),
    (
        ("302", "401", 0, False),
        ("302", "200", 1, True),
        ("200", "401", 1, True),
        ("302", "403", 1, True),
        ("500", "401", 1, True),
        ("EMPTY", "401", 0, False),
        ("000", "401", 0, False),
    ),
)
def test_staged_legacy_probe_uses_regional_origin_and_classifies_results(
    tmp_path: Path,
    console_code: str,
    session_code: str,
    expected_rc: int,
    rolls_back: bool,
) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code=console_code,
        session_code=session_code,
    )

    assert run.returncode == expected_rc, run.stderr
    curl_calls = [call for call in calls if call.startswith("curl ")]
    assert curl_calls
    assert all(
        call.startswith(
            "curl https://staged-probe---trusted-router-hash-uc.a.run.app/"
        )
        for call in curl_calls
    )
    assert any(
        "--update-tags=staged-probe=new-rev" in call
        for call in calls
        if call.startswith("gcloud run services update-traffic")
    )
    assert any(
        "--format=json" in call
        for call in calls
        if call.startswith("gcloud run services describe")
    )
    assert not any("status.traffic[?tag=" in call for call in calls)
    assert not any("status.traffic[?" in call for call in calls)
    assert any("--format=json" in call for call in calls)
    rollback_calls = [
        call
        for call in calls
        if call.startswith("gcloud run services update-traffic")
        and "--to-revisions=old-rev=100" in call
    ]
    assert bool(rollback_calls) is rolls_back
    if console_code in {"EMPTY", "000"}:
        assert "inconclusive after bounded retries; continuing without rollback" in run.stdout


def test_staged_probe_reconciles_and_verifies_a_leftover_tag(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="302",
        session_code="401",
        resolved_tag_revision="old-rev",
    )

    assert run.returncode == 0, run.stderr
    assert any("--update-tags=staged-probe=new-rev" in call for call in calls)
    assert any("--format=json" in call for call in calls)
    assert not any("status.traffic[?tag=" in call for call in calls)
    assert any("--remove-tags=staged-probe" in call for call in calls)
    assert not any(call.startswith("curl ") for call in calls)
    assert "probe tag does not resolve to new-rev" in run.stdout
    assert "resolves to old-rev, expected new-rev" in run.stderr


def test_staged_probe_tag_cleanup_retries_a_transient_failure(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="302",
        session_code="401",
        remove_failures=1,
    )

    assert run.returncode == 0, run.stderr
    assert sum("--remove-tags=staged-probe" in call for call in calls) == 2


def test_staged_probe_tag_cleanup_permanent_failure_is_nonzero(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="302",
        session_code="401",
        remove_always_fails=True,
    )

    assert run.returncode != 0
    assert "probe tag staged-probe may still be addressable" in run.stderr
    assert (
        "gcloud run services update-traffic trusted-router --region=us-central1 "
        "--project=test-project --remove-tags=staged-probe --quiet"
    ) in run.stderr
    assert sum("--remove-tags=staged-probe" in call for call in calls) == 6


def test_staged_probe_tag_cleanup_verifies_a_successful_removal(
    tmp_path: Path,
) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="302",
        session_code="401",
        remove_leaves_tag=True,
    )

    assert run.returncode != 0
    assert "still resolves to new-rev after removal attempt" in run.stderr
    assert "probe tag staged-probe may still be addressable" in run.stderr
    assert sum("--remove-tags=staged-probe" in call for call in calls) == 6
    removal_index = next(
        index for index, call in enumerate(calls) if "--remove-tags=staged-probe" in call
    )
    assert any("--format=json" in call for call in calls[removal_index + 1 :])


def test_failed_traffic_shift_restores_the_old_desired_revision(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="302",
        session_code="401",
        traffic_shift_failure_pct=10,
    )

    assert run.returncode != 0
    traffic_calls = [
        call
        for call in calls
        if call.startswith("gcloud run services update-traffic")
        and "--to-revisions=" in call
    ]
    assert any("--to-revisions=new-rev=10,old-rev=90" in call for call in traffic_calls)
    assert any("--to-revisions=old-rev=100" in call for call in traffic_calls)
    assert "ROLLBACK" in run.stdout
    assert "traffic update to 10% failed" in run.stdout
