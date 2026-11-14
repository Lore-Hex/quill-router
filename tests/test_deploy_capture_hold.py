"""The rollback capture honours TR_DEPLOY_HOLD_REGIONS, as the rollout does.

`deploy`'s "Capture pre-deploy revisions" step records each region's sole
serving revision as its rollback target and refuses a region with none. A held
region's traffic is never moved, and a region is often held because its
traffic is split, so before this the hold that should let a deploy pass over
such a region failed the whole deploy here instead. These tests run the step's
own script against a fake gcloud.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-production.yml"
REGIONS = ("us-central1", "europe-west4", "us-east4", "southamerica-east1")
BASH = shutil.which("bash") or "/bin/bash"

FAKE_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_GCLOUD_LOG"
region=""
for arg in "$@"; do
  case "$arg" in --region=*) region="${arg#--region=}" ;; esac
done
[ -f "$FAKE_GCLOUD_DIR/$region.json" ] || { echo "no such service" >&2; exit 1; }
cat "$FAKE_GCLOUD_DIR/$region.json"
"""


def _capture_script() -> str:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    step = next(s for s in jobs["deploy"]["steps"] if s.get("id") == "prev")
    assert step["name"] == "Capture pre-deploy revisions (for rollback)"
    return step["run"]


def _serving(revision: str) -> dict[str, Any]:
    return {"status": {"traffic": [{"revisionName": revision, "percent": 100}]}}


SPLIT = {"status": {"traffic": [{"revisionName": "a", "percent": 50}, {"revisionName": "b", "percent": 50}]}}


def _run(tmp_path: Path, hold: str | None, documents: dict[str, dict[str, Any]]) -> tuple[int, str, dict[str, str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(0o755)
    data = tmp_path / "cloud"
    data.mkdir()
    for region, document in documents.items():
        (data / f"{region}.json").write_text(json.dumps(document))
    log = tmp_path / "gcloud.log"
    log.write_text("")
    output = tmp_path / "github_output"
    output.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("TR_")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", FAKE_GCLOUD_DIR=str(data),
               FAKE_GCLOUD_LOG=str(log), GITHUB_OUTPUT=str(output),
               SERVICE="trusted-router", PROJECT_ID="test-project")
    if hold is not None:
        env["TR_DEPLOY_HOLD_REGIONS"] = hold
    result = subprocess.run([BASH, "-c", _capture_script()], cwd=ROOT, env=env,  # noqa: S603
                            capture_output=True, text=True, timeout=60)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if line)
    return result.returncode, result.stdout + result.stderr, outputs, log.read_text().splitlines()


def _documents(**overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    docs = {region: _serving(f"{region}-serving") for region in REGIONS}
    docs.update({k.replace("_", "-"): v for k, v in overrides.items()})
    return docs


def test_a_held_region_with_split_traffic_is_passed_over(tmp_path: Path) -> None:
    code, text, outputs, calls = _run(tmp_path, "europe-west4", _documents(europe_west4=SPLIT))
    assert code == 0, text
    assert outputs == {
        "us_central1_revision": "us-central1-serving",
        "europe_west4_revision": "held",
        "us_east4_revision": "us-east4-serving",
        "southamerica_east1_revision": "southamerica-east1-serving",
    }
    assert not [call for call in calls if "--region=europe-west4" in call]
    assert "europe-west4 held by TR_DEPLOY_HOLD_REGIONS" in text


def test_an_unheld_region_with_split_traffic_still_fails_the_deploy(tmp_path: Path) -> None:
    code, text, outputs, _ = _run(tmp_path, None, _documents(europe_west4=SPLIT))
    assert code == 1
    assert "europe-west4 has no unambiguous 100%-traffic rollback revision" in text
    assert "europe_west4_revision" not in outputs


def test_the_hold_list_is_parsed_as_the_rollout_parses_it(tmp_path: Path) -> None:
    # Spaces around entries and several entries, read by the shared parser.
    code, text, outputs, calls = _run(tmp_path, " us-east4 , europe-west4", _documents(europe_west4=SPLIT, us_east4=SPLIT))
    assert code == 0, text
    assert outputs["europe_west4_revision"] == outputs["us_east4_revision"] == "held"
    assert len(calls) == 2


def test_all_holds_every_region_and_reads_none(tmp_path: Path) -> None:
    code, text, outputs, calls = _run(tmp_path, "all", {})
    assert code == 0, text
    assert outputs == {f"{r.replace('-', '_')}_revision": "held" for r in REGIONS}
    assert calls == []


def test_the_placeholder_is_never_used_as_a_revision() -> None:
    # The placeholder reaches the primary baseline step and the secondaries'
    # ramp. Each must check the hold before it uses the captured value.
    workflow = WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("- name: Resolve primary ramp baseline")
    baseline = workflow[start:workflow.index("\n      - name:", start + 1)]
    assert baseline.index("deploy_region_is_held us-central1") < baseline.index('"${SNAPSHOT_REVISION}"')

    ramp = (ROOT / "scripts" / "deploy" / "ramp_secondaries.sh").read_text(encoding="utf-8")
    body = re.search(r"^ramp_secondary\(\) \{\n.*?^\}\n", ramp, re.MULTILINE | re.DOTALL)
    assert body is not None
    text = body.group(0)
    held = text.index('if deploy_region_is_held "$region"; then')
    assert all(m.start() > held for m in re.finditer(r"\$\{?snapshot\b", text))
    rollback = re.search(r"^rollback_region\(\) \{\n.*?^\}\n", ramp, re.MULTILINE | re.DOTALL)
    assert rollback is not None
    first = rollback.group(0)
    assert first.index("deploy_region_is_held") < first.index("previous_revision")
