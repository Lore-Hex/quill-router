from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy" / "verify_deployment.sh"


def _iso_at_age(minutes: int) -> str:
    value = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes)
    return value.isoformat().replace("+00:00", "Z")


def _status_payload(
    *,
    monitor_age_minutes: int | None = None,
    check_age_minutes: int | None = None,
) -> str:
    data: dict[str, Any] = {"overall_status": "up"}
    if monitor_age_minutes is not None:
        data["monitor_freshness"] = {
            "latest_sample_at": _iso_at_age(monitor_age_minutes),
        }
    if check_age_minutes is not None:
        data["current"] = {
            "checks": [{"created_at": _iso_at_age(check_age_minutes)}]
        }
    return json.dumps({"data": data})


def _run_verifier(
    tmp_path: Path,
    payload: str,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
url="${!#}"
if [[ "$*" == *"%{http_code}"* ]]; then
  case "$url" in
    */v1/chat/completions) printf '401' ;;
    *) printf '200' ;;
  esac
elif [[ "$url" == */status.json ]]; then
  printf '%s' "$VERIFY_STATUS_JSON"
fi
"""
    )
    fake_curl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["VERIFY_STATUS_JSON"] = payload
    return subprocess.run(  # noqa: S603 - checked-in script and test-owned fake curl.
        ["/bin/bash", str(SCRIPT), *args, "https://deployment.example"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_verify_deployment_without_expect_monitor_accepts_stale_data(
    tmp_path: Path,
) -> None:
    result = _run_verifier(
        tmp_path,
        _status_payload(monitor_age_minutes=90),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic monitor is" not in result.stdout


def test_verify_deployment_expect_monitor_accepts_fresh_monitor_freshness(
    tmp_path: Path,
) -> None:
    result = _run_verifier(
        tmp_path,
        _status_payload(monitor_age_minutes=5),
        "--expect-monitor",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS  synthetic monitor is fresh" in result.stdout


def test_verify_deployment_expect_monitor_falls_back_to_current_checks(
    tmp_path: Path,
) -> None:
    result = _run_verifier(
        tmp_path,
        _status_payload(check_age_minutes=10),
        "--expect-monitor",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS  synthetic monitor is fresh" in result.stdout


def test_verify_deployment_expect_monitor_rejects_stale_data(
    tmp_path: Path,
) -> None:
    result = _run_verifier(
        tmp_path,
        _status_payload(monitor_age_minutes=31),
        "--expect-monitor",
    )

    assert result.returncode == 1
    assert "FAIL  synthetic monitor is stale or missing" in result.stdout
