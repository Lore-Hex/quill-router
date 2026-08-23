from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest


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
    : >"$STAGED_TAG_STATE"
    ;;
  *"status.traffic"*) cat "$STAGED_TAG_STATE" ;;
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
    for name in ("python3", "sleep"):
        stub = stub_bin / name
        stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        stub.chmod(0o755)
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
    assert any("--remove-tags=staged-probe" in call for call in calls)
    assert not any(call.startswith("curl ") for call in calls)
    assert "probe tag does not resolve to new-rev" in run.stdout


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
