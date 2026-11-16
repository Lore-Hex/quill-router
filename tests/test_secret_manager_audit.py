"""Secret Manager DATA_READ audit records (infra/secret_manager_audit.tf).

A secret read is the first use of a stolen credential. The records exist to
name that caller, so no account, serving or otherwise, may be exempt.
"""

from __future__ import annotations

import re
from pathlib import Path

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
