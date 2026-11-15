"""INC-2026-003 corrective action 6: Spanner DATA_READ audit records.

The intruder read the production database with tr-clickhouse's stolen metadata
token. These records exist to name that kind of caller, so the accounts a
stolen workload token would carry must never be exempt.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TF = ROOT / "infra/spanner_audit.tf"


def _exempted() -> list[str]:
    text = TF.read_text()
    block = re.search(r"spanner_serving_readers\s*=\s*\[(.*?)\]", text, re.S)
    assert block, "spanner_serving_readers list not found"
    return re.findall(r'"([^"]+)"', block.group(1))


def test_data_read_is_recorded_for_spanner() -> None:
    text = TF.read_text()
    assert 'resource "google_project_iam_audit_config" "spanner_data_read"' in text
    assert 'service = "spanner.googleapis.com"' in text
    assert 'log_type         = "DATA_READ"' in text


def test_the_clickhouse_and_compute_default_accounts_are_not_exempt() -> None:
    exempt = _exempted()
    assert exempt, "the exemption list should name the serving fleet"
    for member in exempt:
        assert "tr-clickhouse@" not in member, member
        assert "-compute@developer.gserviceaccount.com" not in member, member


def test_operators_and_ci_are_not_exempt() -> None:
    for member in _exempted():
        for recorded in ("tr-ops-local@", "tr-deploy@", "kertos-discovery@", "user:"):
            assert recorded not in member, member
