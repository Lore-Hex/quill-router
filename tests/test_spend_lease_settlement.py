from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from trusted_router.app_markup_billing import (
    APP_MARKUP_PAYOUT_SETTLE_FIELD,
    app_markup_microdollars_from_charge,
    app_markup_owner_share_microdollars,
)
from trusted_router.services.spend_lease_settlement import (
    clamp_spend_lease_charge,
    derive_spend_lease_repair_amounts,
    mirror_finalized_spend_lease_best_effort,
)
from trusted_router.spend_lease_state import (
    FinalizationOutcome,
    MonetaryMismatchProof,
    SpendLeaseMonetaryMismatch,
)
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType


def _authorization(**overrides: Any) -> GatewayAuthorization:
    values: dict[str, Any] = {
        "id": "authorization-1",
        "workspace_id": "workspace-1",
        "key_hash": "key-1",
        "model_id": "model-1",
        "provider": "provider-1",
        "usage_type": UsageType.CREDITS,
        "estimated_microdollars": 900,
        "settlement": "spend_lease",
        "region": "us-central1",
        "spend_lease_id": "lease-1",
        "spend_lease_gen": 3,
        "spend_lease_allocated_micro": 700,
    }
    values.update(overrides)
    return GatewayAuthorization(**values)


def test_spend_lease_clamp_uses_minimum_of_allocation_and_estimate() -> None:
    assert clamp_spend_lease_charge(_authorization(), 1_200) == 700
    assert (
        clamp_spend_lease_charge(
            _authorization(estimated_microdollars=600, spend_lease_allocated_micro=700),
            1_200,
        )
        == 600
    )


def test_binding_facts_missing_falls_back_to_estimate_and_logs_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        charge = clamp_spend_lease_charge(
            _authorization(spend_lease_allocated_micro=None),
            1_200,
        )

    assert charge == 900
    assert "spend_lease.settle_binding_facts_missing" in caplog.text


def test_corrective_helper_rederives_markup_and_payout_from_capped_charge() -> None:
    authorization = _authorization(
        app_id="app-1",
        app_owner_user_id="owner-1",
        app_markup_basis_points=2_500,
    )

    repaired = derive_spend_lease_repair_amounts(
        authorization,
        1_200,
        {APP_MARKUP_PAYOUT_SETTLE_FIELD: 999_999},
    )

    markup = app_markup_microdollars_from_charge(700, 2_500)
    payout = app_markup_owner_share_microdollars(markup)
    assert repaired.actual_cost_micro == 700
    assert repaired.app_markup_micro == markup
    assert repaired.app_markup_payout_micro == payout
    assert repaired.settle_body[APP_MARKUP_PAYOUT_SETTLE_FIELD] == payout


class _Ledger:
    def __init__(self, *, mismatch: bool = False, failure: bool = False) -> None:
        self.mismatch = mismatch
        self.failure = failure
        self.observations: list[Any] = []
        self.quarantines: list[Any] = []
        self.allocation = SimpleNamespace(
            authorization_id="authorization-1",
            idempotency_scope="scope-1",
            request_fingerprint="fingerprint-1",
        )

    def get(self, _lease_id: str, *, region: str) -> Any:
        assert region == "us-central1"
        return SimpleNamespace(allocations=[self.allocation])

    def mirror(self, _lease_id: str, *, region: str, observation: Any) -> None:
        if self.failure:
            raise RuntimeError("ledger unavailable")
        self.observations.append(observation)
        if self.mismatch:
            raise SpendLeaseMonetaryMismatch(
                finalized_cost_microdollars=800,
                allocated_micro=700,
            )

    def quarantine(self, _lease_id: str, **kwargs: Any) -> None:
        self.quarantines.append(kwargs)


def test_eager_mirror_maps_settled_and_refunded_committed_outcomes() -> None:
    ledger = _Ledger()
    store = SimpleNamespace(_spend_lease_ledger=ledger)
    settled = _authorization(
        settled=True,
        finalization_outcome="settled",
        finalized_cost_microdollars=650,
    )
    refunded = _authorization(
        settled=True,
        finalization_outcome="refunded",
        finalized_cost_microdollars=0,
    )

    mirror_finalized_spend_lease_best_effort(store, settled)
    mirror_finalized_spend_lease_best_effort(store, refunded)

    assert [item.finalization_outcome for item in ledger.observations] == [
        FinalizationOutcome.SETTLED,
        FinalizationOutcome.REFUNDED,
    ]
    assert [item.finalized_cost_microdollars for item in ledger.observations] == [650, None]


def test_eager_mirror_monetary_mismatch_quarantines_with_proof() -> None:
    ledger = _Ledger(mismatch=True)
    store = SimpleNamespace(_spend_lease_ledger=ledger)

    mirror_finalized_spend_lease_best_effort(
        store,
        _authorization(
            settled=True,
            finalization_outcome="settled",
            finalized_cost_microdollars=800,
        ),
    )

    assert len(ledger.quarantines) == 1
    assert ledger.quarantines[0]["proof"] == MonetaryMismatchProof(800, 700)


def test_eager_mirror_failure_is_harmless_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = _Ledger(failure=True)
    with caplog.at_level(logging.ERROR):
        mirror_finalized_spend_lease_best_effort(
            SimpleNamespace(_spend_lease_ledger=ledger),
            _authorization(
                settled=True,
                finalization_outcome="settled",
                finalized_cost_microdollars=650,
            ),
        )

    assert "spend_lease.eager_mirror_failed" in caplog.text
