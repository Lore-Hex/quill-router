from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy" / "normalize_staged_traffic.sh"


def _run_normalizer(
    tmp_path: Path,
    traffic: list[dict[str, object]],
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    call_log = tmp_path / "calls.log"
    service_json = tmp_path / "service.json"
    service_json.write_text(json.dumps({"status": {"traffic": traffic}}))
    gcloud = stub_bin / "gcloud"
    gcloud.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$NORMALIZE_CALL_LOG"
if [[ " $* " == *" run services describe "* ]]; then
  cat "$NORMALIZE_SERVICE_JSON"
fi
"""
    )
    gcloud.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{stub_bin}:/bin:/usr/bin",
        "NORMALIZE_CALL_LOG": str(call_log),
        "NORMALIZE_SERVICE_JSON": str(service_json),
        "PROJECT_ID": "test-project",
        "SERVICE": "trusted-router",
    }
    run = subprocess.run(  # noqa: S603 - fixed local script and stubbed PATH
        ["/bin/bash", str(SCRIPT), "us-central1"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    calls = call_log.read_text().splitlines() if call_log.exists() else []
    return run, calls


def test_normalizer_restores_desired_traffic_to_the_actual_serving_revision(
    tmp_path: Path,
) -> None:
    run, calls = _run_normalizer(
        tmp_path,
        [
            {"percent": 100, "revisionName": "old-rev"},
            {"revisionName": "failed-rev", "tag": "staged-probe"},
        ],
    )

    assert run.returncode == 0, run.stderr
    assert any(
        "run services update-traffic trusted-router" in call
        and "--to-revisions=old-rev=100" in call
        for call in calls
    )


def test_normalizer_refuses_to_collapse_live_split_traffic(tmp_path: Path) -> None:
    run, calls = _run_normalizer(
        tmp_path,
        [
            {"percent": 90, "revisionName": "old-rev"},
            {"percent": 10, "revisionName": "new-rev"},
        ],
    )

    assert run.returncode != 0
    assert "expected exactly one 100%-traffic revision" in run.stderr
    assert not any("run services update-traffic" in call for call in calls)
