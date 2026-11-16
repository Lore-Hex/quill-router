"""Secret Manager DATA_READ audit records (infra/secret_manager_audit.tf).

A secret read is the first use of a stolen credential. The records exist to
name that caller, so no account, serving or otherwise, may be exempt.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TF = (ROOT / "infra/secret_manager_audit.tf").read_text()


def _resource() -> str:
    marker = 'resource "google_project_iam_audit_config" "secret_manager_data_read"'
    assert marker in TF
    return TF.split(marker, 1)[1]


def test_secret_reads_are_recorded() -> None:
    resource = _resource()
    assert 'service = "secretmanager.googleapis.com"' in resource
    assert re.findall(r'log_type\s*=\s*"([A-Z_]+)"', resource) == ["DATA_READ"]


def test_no_account_is_exempt() -> None:
    assert "exempted_members" not in _resource()


def test_the_config_is_for_this_service_alone() -> None:
    # google_project_iam_audit_config is authoritative per service. A config
    # for allServices here would replace the project's ADMIN_READ config and
    # record every service's data reads, the cost that ended the 2026-08-15
    # attempt (scripts/deploy/gcp_security_baseline_check.sh).
    assert re.findall(r'service\s*=\s*"([^"]+)"', TF) == ["secretmanager.googleapis.com"]


CHECKER = (ROOT / "scripts/deploy/gcp_security_baseline_check.sh").read_text()


def _audit_section_findings(tmp_path: Path, audit_configs: list[dict]) -> list[str]:
    """Run the daily baseline check's audit section against a project policy."""
    section = CHECKER.split('sec "audit log configuration"', 1)[1].split("# 2. CIS log-based metrics", 1)[0]
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"auditConfigs": audit_configs}))
    program = (
        "set -euo pipefail\n"
        'ok() { echo "OK $*"; }\n'
        'drift() { echo "DRIFT $*"; }\n'
        f"g() {{ cat {policy}; }}\n"
        "PROJECT=quill-cloud-proxy\n" + section
    )
    bash = shutil.which("bash")
    assert bash, "bash not found"
    done = subprocess.run(  # noqa: S603 - fixed argv, repo-local script section, stubbed gcloud
        [bash, "-c", program], capture_output=True, text=True, check=True
    )
    return [line for line in done.stdout.splitlines() if line.startswith("DRIFT")]


def _live_audit_configs() -> list[dict]:
    exempt = re.search(r'SPANNER_READ_EXEMPT_WANT="([^"]*)"', CHECKER)
    assert exempt, "SPANNER_READ_EXEMPT_WANT not found in the baseline checker"
    return [
        {"service": "allServices", "auditLogConfigs": [{"logType": "ADMIN_READ"}]},
        {
            "service": "spanner.googleapis.com",
            "auditLogConfigs": [{"logType": "DATA_READ", "exemptedMembers": exempt.group(1).split(",")}],
        },
        {"service": "secretmanager.googleapis.com", "auditLogConfigs": [{"logType": "DATA_READ"}]},
        {"service": "iam.googleapis.com", "auditLogConfigs": [{"logType": "DATA_READ"}]},
    ]


@pytest.mark.skipif(shutil.which("jq") is None, reason="the baseline checker needs jq")
def test_the_daily_baseline_check_accepts_the_applied_config_and_nothing_wider(tmp_path: Path) -> None:
    assert _audit_section_findings(tmp_path, _live_audit_configs()) == []

    missing = [c for c in _live_audit_configs() if c["service"] != "secretmanager.googleapis.com"]
    assert any("secretmanager.googleapis.com" in line for line in _audit_section_findings(tmp_path, missing))

    exempted = _live_audit_configs()
    exempted[2]["auditLogConfigs"][0]["exemptedMembers"] = [
        "serviceAccount:trusted-router-control-run@quill-cloud-proxy.iam.gserviceaccount.com"
    ]
    assert any("exemptedMembers" in line for line in _audit_section_findings(tmp_path, exempted))

    widened = _live_audit_configs()
    widened[2]["auditLogConfigs"].append({"logType": "DATA_WRITE"})
    assert any("secretmanager.googleapis.com" in line for line in _audit_section_findings(tmp_path, widened))

    another = _live_audit_configs() + [
        {"service": "storage.googleapis.com", "auditLogConfigs": [{"logType": "DATA_READ"}]}
    ]
    assert any("storage.googleapis.com" in line for line in _audit_section_findings(tmp_path, another))


IAM_TF = (ROOT / "infra/iam_audit.tf").read_text()


def test_iam_reads_are_recorded_for_this_service_alone() -> None:
    marker = 'resource "google_project_iam_audit_config" "iam_data_read"'
    assert marker in IAM_TF
    resource = IAM_TF.split(marker, 1)[1]
    assert re.findall(r'service\s*=\s*"([^"]+)"', IAM_TF) == ["iam.googleapis.com"]
    assert re.findall(r'log_type\s*=\s*"([A-Z_]+)"', resource) == ["DATA_READ"]
    assert "exempted_members" not in resource
    # The live config predates this file: it is adopted, never recreated.
    assert 'to = google_project_iam_audit_config.iam_data_read' in IAM_TF
    assert 'id = "quill-cloud-proxy iam.googleapis.com"' in IAM_TF


@pytest.mark.skipif(shutil.which("jq") is None, reason="the baseline checker needs jq")
def test_the_daily_baseline_check_expects_iam_data_read_and_nothing_wider(tmp_path: Path) -> None:
    assert _audit_section_findings(tmp_path, _live_audit_configs()) == []
    missing = [c for c in _live_audit_configs() if c["service"] != "iam.googleapis.com"]
    assert any("iam.googleapis.com" in line for line in _audit_section_findings(tmp_path, missing))
    widened = _live_audit_configs()
    widened[3]["auditLogConfigs"].append({"logType": "DATA_WRITE"})
    assert any("iam.googleapis.com" in line for line in _audit_section_findings(tmp_path, widened))
    exempted = _live_audit_configs()
    exempted[3]["auditLogConfigs"][0]["exemptedMembers"] = ["user:someone@example.com"]
    assert any("exemptedMembers" in line for line in _audit_section_findings(tmp_path, exempted))
