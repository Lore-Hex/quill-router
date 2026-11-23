from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.refill_claim import RefillClaim, decide_claim
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import CreditAccount


@pytest.mark.parametrize("backend", ["memory", "spanner"])
def test_refill_claim_store_contract(backend):
    store = InMemoryStore() if backend == "memory" else make_fake_store()[0]
    ws = store.create_workspace(
        owner_user_id="owner", name="workspace", trial_credit_microdollars=1
    ).id
    store.update_auto_refill_settings(
        ws, enabled=True, amount_microdollars=20_000_000, threshold_microdollars=5_000_000
    )
    store.set_stripe_customer(ws, customer_id="cus", payment_method_id="pm")
    original = replace(store.get_credit_account(ws))

    def claim(key, account=None):
        account = account or replace(store.get_credit_account(ws))
        return store.claim_auto_refill(account, candidate=key, payload_hash="payload")

    def result(key, status):
        store.record_auto_refill_outcome(ws, attempt_key=key, status=status)

    assert claim("one", original) == ("claimed", "one")
    assert claim("two", original)[1] is None
    result("one", "succeeded")
    result("one", "pending")  # The webhook beat the create response.
    assert store.get_credit_account(ws).last_auto_refill_status == "succeeded"
    assert claim("one") == ("already_attempted", None)
    assert claim("two") == ("claimed", "two")
    result("one", "succeeded")
    result(None, "failed:declined")  # Old unfenced webhook.
    assert store.get_credit_account(ws).last_auto_refill_status == "pending"
    result("two", "succeeded")
    assert claim("one") == ("already_attempted", None)
    stale = replace(store.get_credit_account(ws))
    assert store.credit_workspace_once(ws, 100_000_000, "topup")
    assert claim("three", stale) == ("above_threshold", None)


@pytest.mark.parametrize(
    ("age", "state", "payload", "expected"),
    [
        (1, "sending", "original", "pending"),
        (31, "sending", "original", "claimed"),
        (31, "sending", "changed", "reconcile_required"),
        (31, "accepted", "original", "reconcile_required"),
        (23 * 60, "sending", "original", "reconcile_required"),
    ],
)
def test_uncertain_create_only_replays_identical_request_with_original_key(
    age, state, payload, expected
):
    now = datetime.now(UTC)
    started = (now - timedelta(minutes=age)).isoformat()
    account = CreditAccount(
        "ws",
        auto_refill_enabled=True,
        auto_refill_threshold_microdollars=5,
        last_auto_refill_at=started,
        last_auto_refill_status="pending",
    )
    current = RefillClaim("original-key", "original", started, started, state)
    reason, dispatch = decide_claim(
        account,
        account,
        (1, 0, 0),
        current,
        candidate="other-key",
        used=False,
        payload_hash=payload,
        now=now,
    )
    assert reason == expected
    if dispatch:
        assert (dispatch.key, dispatch.opened_at, dispatch.dispatched_at) == (
            "original-key",
            started,
            now.isoformat(),
        )
