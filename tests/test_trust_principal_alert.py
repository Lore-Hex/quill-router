from unittest.mock import Mock

import pytest

from trusted_router.services import trust_recovery
from trusted_router.storage_models import AdverseTrustResult


def test_reconciliation_only_pages_for_applied_shortfall(monkeypatch: pytest.MonkeyPatch) -> None:
    notify = Mock(return_value=True)
    monkeypatch.setattr(trust_recovery, "ops_alert", notify)
    for outcome in ("applied", "replay", "replay", "stale", "illegal"):
        result = AdverseTrustResult(outcome, "workspace", 200_000_000, 195_210_000, 4_790_000)
        assert trust_recovery.alert_unrecovered_principal(result) is (outcome == "applied")
    assert not trust_recovery.alert_unrecovered_principal(AdverseTrustResult("applied", "workspace"))
    notify.assert_called_once_with(
        "trust.unrecovered_principal workspace_id=workspace unrecovered_micro=4790000",
        fingerprint=["trust.unrecovered_principal", "workspace"],
        tags={"workspace_id": "workspace"},
    )
