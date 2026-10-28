"""Shared ledger contract, including the native migration-backed emulator gate."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.nyte_resale_helpers import call, inspect_position, listing, purchase
from trusted_router.storage_resale import REFUND_WINDOW, reference
from trusted_router.store_protocol import Store


@pytest.fixture(params=[True, False], ids=["sales-on", "sales-off"])
def resale_enabled(request) -> bool:
    return request.param


def refund_queue_ids(store: Store, queue: str) -> list[str]:
    cursor, ids, seen = None, [], set()
    while True:
        page = call(store, "pending", queue=queue, cursor=cursor)
        ids.extend(row.get("id", row.get("position")) for row in page["rows"])
        cursor = page["cursor"]
        if not cursor:
            return ids
        assert cursor not in seen
        seen.add(cursor)


def test_nyte_fifo_partial_fill_and_session_event_idempotency(store: Store, unique: str) -> None:
    first, second, buyer = (unique + s for s in ("1", "2", "buyer"))
    purchase(store, first, price=80)
    purchase(store, second)
    l1, l2 = listing(store, first), listing(store, second)
    result = purchase(store, buyer, lots=5, allocate=True)
    assert result["new_lots"] == 0
    assert len(result["fills"]) == 2
    for fid, lid, count, refund in zip(result["fills"], (l1, l2), (3, 2), (240, 190), strict=True):
        fill = call(store, "fill_claim", fill=fid, owner="worker")
        assert fill["fee_bps"] == 500
        assert (fill["listing"], fill["lots"], fill["fee_cents"], fill["refund_amount_cents"]) == (lid, count, count * 5, refund)
        call(store, "fill_finish", fill=fid, owner="worker", status="refunded", stripe_refund_id="re_" + fid)
    assert purchase(store, buyer, lots=5, allocate=True) == result
    # A different Stripe delivery ID for the same session also cannot allocate.
    assert call(store, "purchase", position={"session_id": buyer}, event_id="evt_retry_" + unique, allocate=True) == result
    for event_id in ("evt_" + buyer, "evt_retry_" + unique):
        with pytest.raises(ValueError, match="event already"):
            call(store, "purchase", position={"session_id": "different"}, event_id=event_id)
    next_buyer = purchase(store, unique + "next", lots=2, allocate=True)
    assert next_buyer["new_lots"] == 1
    remaining = call(store, "positions", holder=second)["positions"][0]
    assert (remaining["sold_lots"], remaining["listed_lots"]) == (3, 0)


def test_nyte_listing_purchase_and_withdraw_races(store: Store, unique: str) -> None:
    seller = unique + "seller"
    purchase(store, seller)
    with ThreadPoolExecutor(max_workers=2) as pool:
        def list_race(n: int) -> str:
            try:
                return listing(store, seller, request=str(n))
            except ValueError:
                return "unavailable"
        outcomes = list(pool.map(list_race, range(2)))
    assert outcomes.count("unavailable") == 1
    lid = next(item for item in outcomes if item != "unavailable")
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(purchase, store, unique + str(n), lots=2, allocate=True) for n in range(2)]
        withdrawal = pool.submit(call, store, "withdraw", position=seller, holder=seller, listing=lid, request_id="withdraw")
        results = [job.result() for job in jobs]
        withdrawal.result()
    sold = sum(2 - p["new_lots"] for p in results)
    assert 0 <= sold <= 3
    p = call(store, "positions", holder=seller)["positions"][0]
    assert p["sold_lots"] == sold and p["listed_lots"] == 0
    assert purchase(store, unique + "after", allocate=True)["new_lots"] == 3


def test_nyte_refund_window_first_write_and_single_use(store: Store, unique: str) -> None:
    # Old checkout notification is the first-write source, including imports.
    store.claim_reserved_notification(unique, "worker", now=100)
    p = call(store, "purchase", position={"session_id": unique, "holder": unique, "lots": 2,
             "grade": unique, "per_lot_cents": 100, "payment_intent": "pi_" + unique, "purchase_completed_at": None}, event_id=unique)
    assert p["purchase_completed_at"] == 100
    with pytest.raises(ValueError, match="Within 30 days"):
        call(store, "list", position=unique, holder=unique, lots=1, request_id="list", now=100 + REFUND_WINDOW)
    with pytest.raises(ValueError, match="window has closed"):
        call(store, "request_refund", position=unique, holder=unique, request_id="refund")
    call(store, "request_refund", position=unique, holder=unique, request_id="refund", now=100 + REFUND_WINDOW)
    with pytest.raises(ValueError, match="Lots unavailable"):
        listing(store, unique, lots=1)
    call(store, "token_create", key=unique, holder=unique, now=100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        def consume(n: int) -> bool:
            try:
                call(store, "token_consume", key=unique, session=reference(unique + str(n)), now=101)
                return True
            except ValueError:
                return False
        assert sorted(pool.map(consume, range(2))) == [False, True]
    call(store, "token_create", key=unique + "expired", holder=unique, now=100)
    with pytest.raises(ValueError, match="expired"):
        call(store, "token_consume", key=unique + "expired", session="never", now=1000)


def test_nyte_relist_goes_to_tail_and_refund_lease_fencing(store: Store, unique: str) -> None:
    first, second = unique + "first", unique + "second"
    purchase(store, first)
    purchase(store, second)
    old = listing(store, first)
    lid = listing(store, second)
    call(store, "withdraw", holder=first, position=first, listing=old, request_id="withdraw")
    listing(store, first, request="relist")
    p = purchase(store, unique + "buyer", lots=1, allocate=True)
    fid = p["fills"][0]
    assert call(store, "fill_claim", fill=fid, owner="one")["listing"] == lid
    assert not call(store, "fill_claim", fill=fid, owner="two")
    assert call(store, "fill_claim", fill=fid, owner="two", now=REFUND_WINDOW + 222)
    assert not call(store, "fill_finish", fill=fid, owner="one", status="refunded")
    call(store, "fill_finish", fill=fid, owner="two", status="retry")
    assert call(store, "fill_claim", fill=fid, owner="three")


def test_nyte_two_buyers_cannot_fill_same_lots(store: Store, unique: str) -> None:
    seller = unique + "seller"
    purchase(store, seller)
    listing(store, seller)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda suffix: purchase(store, unique + suffix, lots=2, allocate=True), ("one", "two")))
    assert sorted(p["new_lots"] for p in results) == [0, 1]
    position = call(store, "positions", holder=seller)["positions"][0]
    assert position["sold_lots"] == 3 and position["listed_lots"] == 0


def test_nyte_import_before_webhook_completes_timestamp_once(store: Store, unique: str) -> None:
    name = unique + "import"
    p = {"session_id": name, "holder": name, "lots": 2, "grade": unique,
         "per_lot_cents": 100, "payment_intent": "pi_" + unique, "purchase_completed_at": None}
    call(store, "purchase", position=p, event_id="backfill:" + name, backfill=True)
    store.claim_reserved_notification(name, "webhook", now=200)
    call(store, "purchase", position={**p, "purchase_completed_at": 999}, event_id="evt_" + name, allocate=True, fee_bps=500)
    result = call(store, "positions", holder=name)
    assert len(result["positions"]) == 1 and result["cursor"] is None
    assert result["positions"][0]["purchase_completed_at"] == 200


@pytest.mark.parametrize("state", [{"amount_refunded": 1}, {"disputed": True}])
def test_nyte_external_payment_change_blocks_existing_position_and_listings(store: Store, unique: str, state: dict) -> None:
    seller, other, buyer = (unique + name for name in ("seller", "other", "buyer"))
    original = purchase(store, seller)
    lid = listing(store, seller, lots=2)
    purchase(store, other)
    other_lid = listing(store, other)
    # Reimporting an existing position must reconcile before its early return.
    blocked = call(store, "purchase", position=original, event_id="backfill:" + seller,
                   backfill=True, payment_state=state)
    assert blocked["status"] == "needs_review" and blocked["listed_lots"] == 0
    rows = call(store, "positions", holder=seller)["positions"][0]["listing_rows"]
    assert rows[0]["id"] == lid and rows[0]["status"] == "withdrawn"
    with pytest.raises(ValueError, match="review"):
        listing(store, seller, lots=1, request="after-refund")
    # A stale retry of the original listing cannot advertise it as active.
    with pytest.raises(ValueError, match="review"):
        listing(store, seller, lots=2)
    result = purchase(store, buyer, lots=4, allocate=True)
    assert result["new_lots"] == 1 and len(result["fills"]) == 1
    assert call(store, "fill_claim", fill=result["fills"][0], owner="worker")["listing"] == other_lid
    assert call(store, "position", position=seller)["sold_lots"] == 0


def test_nyte_own_refund_before_worker_commit_is_exempt_but_external_excess_blocks(store: Store, unique: str) -> None:
    seller, buyer = unique + "seller", unique + "buyer"
    purchase(store, seller)
    listing(store, seller)
    p = purchase(store, buyer, lots=1, allocate=True)
    fid = p["fills"][0]
    evidence = {"id": "re_" + unique, "payment_intent": "pi_" + seller, "amount": 95,
                "status": "succeeded", "metadata": {"nyte_fill_id": fid}}
    # The charge webhook can precede fill_finish; the durable fill is sufficient.
    state = {"amount_refunded": 95, "refunds": [evidence]}
    for _ in range(2):
        result = call(store, "reconcile_payment", payment_intent="pi_" + seller, payment_state=state)
        assert result["status"] == "held" and result["listed_lots"] == 2
    # Duplicated metadata/evidence cannot conceal an extra external refund.
    state = {"amount_refunded": 96, "refunds": [evidence, evidence]}
    result = call(store, "reconcile_payment", payment_intent="pi_" + seller, payment_state=state)
    assert result["status"] == "needs_review" and result["listed_lots"] == 0
    assert purchase(store, unique + "after", lots=1, allocate=True)["new_lots"] == 1


def test_nyte_timestamped_import_finalizes_historical_before_webhook(store: Store, unique: str) -> None:
    seller, buyer = unique + "seller", unique + "buyer"
    purchase(store, seller)
    listing(store, seller)
    # An import finalizes as new inventory even if a webhook claimed it first.
    # A later webhook cannot retroactively allocate that completed import.
    store.claim_reserved_notification(buyer, "webhook", now=200)
    p = {"session_id": buyer, "holder": buyer, "lots": 2, "grade": unique[:12],
         "per_lot_cents": 100, "payment_intent": "pi_" + buyer, "purchase_completed_at": None}
    imported = call(store, "purchase", position=p, event_id="backfill:" + buyer, backfill=True)
    assert imported["purchase_completed_at"] == 200 and imported["allocation_status"] == "historical"
    result = call(store, "purchase", position={**p, "purchase_completed_at": 999},
                  event_id="evt_" + buyer, allocate=True, fee_bps=500)
    assert result["purchase_completed_at"] == 200 and result["allocation_status"] == "historical"
    assert result["new_lots"] == 2 and result["fills"] == []
    assert call(store, "purchase", position=p, event_id="evt_retry_" + buyer, allocate=True) == result
    assert call(store, "position", position=seller)["sold_lots"] == 0
    assert len(call(store, "positions", holder=buyer)["positions"]) == 1


def test_nyte_completed_legacy_notification_import_is_historical(store: Store, unique: str) -> None:
    seller, buyer = unique + "seller", unique + "buyer"
    purchase(store, seller)
    listing(store, seller)
    store.claim_reserved_notification(buyer, "legacy", now=200)
    store.finish_reserved_notification(buyer, "legacy", sent=True)
    p = {"session_id": buyer, "holder": buyer, "lots": 2, "grade": unique[:12],
         "per_lot_cents": 100, "payment_intent": "pi_" + buyer, "purchase_completed_at": None}
    imported = call(store, "purchase", position=p, event_id="backfill:" + buyer, backfill=True)
    assert imported["allocation_status"] == "historical"
    result = call(store, "purchase", position=p, event_id="evt_" + buyer, allocate=True)
    assert result["fills"] == [] and result["new_lots"] == 2
    assert call(store, "position", position=seller)["sold_lots"] == 0


@pytest.mark.parametrize("state", [{"amount_refunded": 100}, {"disputed": True}])
def test_nyte_adverse_events_require_a_mapped_position(store: Store, unique: str, state: dict, monkeypatch, resale_enabled: bool) -> None:
    from trusted_router.config import Settings
    from trusted_router.services import resale

    seller, buyer = unique + "seller", unique + "buyer"
    purchase(store, seller)
    listing(store, seller)
    monkeypatch.setattr(resale, "STORE", store)
    early = {"id": "evt_early_" + unique, "type": "charge.dispute.created" if state.get("disputed") else "refund.created",
             "data": {"object": {"id": "re_" + unique, "payment_intent": "pi_" + buyer, "amount": 100,
                                 "status": "succeeded", "metadata": {"kind": "reserved_tokens_2027"}}}}
    settings = Settings(environment="test", resale_enabled=resale_enabled)
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", lambda payment, **kw: {"id": payment, "metadata": {"kind": "other_product"}})
    assert not resale.reconcile_payment_event(early, settings)
    assert call(store, "payment", payment_intent="pi_" + buyer) == {}
    assert call(store, "reconcile_payment", payment_intent="pi_" + buyer, payment_state=state) == {}
    result = purchase(store, buyer, lots=1)
    assert result["status"] == "held" and not result.get("payment_review")
    # Once the mapping exists, the same signed event is actionable in either mode.
    assert resale.reconcile_payment_event(early, settings)
    result = call(store, "position", position=buyer)
    assert result["status"] == "needs_review" and result["payment_review"]
    assert result["fills"] == [] and result["new_lots"] == 1
    with pytest.raises(ValueError, match="review"):
        listing(store, buyer, lots=1)
    assert call(store, "position", position=seller)["sold_lots"] == 0
    assert purchase(store, buyer, lots=1, allocate=True)["status"] == "needs_review"


@pytest.mark.parametrize("path", ["webhook_replay", "historical_import"])
def test_nyte_old_purchase_checks_current_stripe_before_held(store: Store, unique: str, monkeypatch, path: str) -> None:
    from types import SimpleNamespace

    from tests.test_reserved_tokens import event
    from trusted_router.config import Settings
    from trusted_router.services import resale, reserved_tokens
    obj = event()["data"]["object"]
    obj.update(id="cs_" + unique, payment_intent="pi_" + unique)
    store.claim_reserved_notification(obj["id"], "old", now=100)
    store.finish_reserved_notification(obj["id"], "old", sent=True)
    monkeypatch.setattr(resale, "STORE", store)
    monkeypatch.setattr(reserved_tokens, "STORE", store)
    observed = []
    def retrieve(payment, **kwargs):
        observed.append(payment)
        return {"status": "succeeded", "latest_charge": {"paid": True, "amount_refunded": obj["amount_total"]}}
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", retrieve)
    monkeypatch.setattr(resale.stripe.Refund, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([])))
    settings = Settings(environment="test", resale_enabled=True, stripe_secret_key="sk_test_fixture")  # noqa: S106
    if path == "webhook_replay":
        reserved_tokens.handle_reservation_event({"id": "evt_" + unique, "type": "checkout.session.completed", "data": {"object": obj}}, settings)
    else:
        resale.record_purchase(obj, "backfill:" + obj["id"], None, settings, backfill=True)
    assert observed == [obj["payment_intent"]]
    p = call(store, "position", position=obj["id"])
    assert p["status"] == "needs_review" and p["allocation_status"] == "historical"
    with pytest.raises(ValueError, match="review"):
        call(store, "list", position=obj["id"], holder=p["holder"], lots=1, request_id="list")


@pytest.mark.parametrize("failure", ["failed", "canceled"])
def test_nyte_successful_refund_later_fails_without_reissuance(store: Store, unique: str, monkeypatch, failure: str, resale_enabled: bool) -> None:
    from unittest.mock import Mock

    from trusted_router.config import Settings
    from trusted_router.services import resale

    settings = Settings(environment="test", resale_enabled=resale_enabled)
    monkeypatch.setattr(resale, "STORE", store)
    alert = Mock()
    monkeypatch.setattr(resale, "alert_ops", alert)

    def queued_ids(queue: str = "refunds") -> list[str]:
        ids: list[str] = []
        cursor = None
        seen: set[str] = set()
        while True:
            page = call(store, "pending", queue=queue, cursor=cursor)
            ids.extend(row["id"] for row in page["rows"])
            cursor = page["cursor"]
            if not cursor:
                return ids
            assert cursor not in seen
            seen.add(cursor)

    before_refunds, before_ops = queued_ids(), queued_ids("refunds_needs_ops")
    seller, buyer = unique + "seller", unique + "buyer"
    purchase(store, seller)
    listing(store, seller)
    fid = purchase(store, buyer, lots=1, allocate=True)["fills"][0]
    call(store, "fill_claim", fill=fid, owner="original")
    call(store, "fill_finish", fill=fid, owner="original", status="refunded", stripe_refund_id="re_" + unique)
    assert queued_ids() == before_refunds
    refund = {"id": "re_" + unique, "payment_intent": "pi_" + seller, "amount": 95, "status": failure}
    for altered in ({"id": "re_unknown_" + unique}, {"payment_intent": "pi_other"}, {"amount": 94}, {"status": "succeeded"}):
        assert call(store, "refund_failed", refund={**refund, **altered}) == {}
    for _ in range(2):
        assert resale.reconcile_payment_event({"type": "refund.updated", "data": {"object": refund}}, settings)
        reopened = call(store, "refund_obligation", kind="resale_fill", id=fid)
        assert reopened["status"] == "needs_ops" and reopened["stripe_refund_id"] == refund["id"]
    assert queued_ids("refunds_needs_ops") == [*before_ops, fid]
    assert queued_ids() == before_refunds
    # A stale success writer cannot undo the adverse event, even with its old owner.
    assert call(store, "fill_finish", fill=fid, owner="original", status="refunded") == {}
    assert call(store, "fill_claim", fill=fid, owner="another", now=REFUND_WINDOW * 2) == {}
    monkeypatch.setattr(resale, "STORE", store)
    create = Mock(side_effect=AssertionError("Needs-ops fill issued another refund"))
    monkeypatch.setattr(resale.stripe.Refund, "create", create)
    resale.process_fill(fid, settings)
    create.assert_not_called()


@pytest.mark.parametrize("failure_first", [True, False])
@pytest.mark.parametrize("failure", ["failed", "canceled"])
def test_nyte_refund_failure_and_success_commit_are_order_independent(store: Store, unique: str, monkeypatch, failure_first: bool, failure: str, resale_enabled: bool) -> None:
    from unittest.mock import Mock

    from trusted_router.config import Settings
    from trusted_router.services import resale

    settings = Settings(environment="test", resale_enabled=resale_enabled)
    monkeypatch.setattr(resale, "STORE", store)
    alert = Mock()
    monkeypatch.setattr(resale, "alert_ops", alert)

    seller, buyer = unique + "seller", unique + "buyer"
    purchase(store, seller)
    listing(store, seller)
    fid = purchase(store, buyer, lots=1, allocate=True)["fills"][0]
    claimed = call(store, "fill_claim", fill=fid, owner="worker")
    assert claimed["status"] == "in_flight" and claimed["stripe_refund_id"] is None
    assert claimed["position_id"] == seller
    refund = {"id": "re_" + unique, "payment_intent": "pi_" + seller, "amount": 95,
              "status": failure, "metadata": {"fill_id": fid, "position_id": seller}}
    for invalid in ({"payment_intent": "pi_wrong"}, {"amount": 94}, {"metadata": {"fill_id": "unknown"}}):
        assert call(store, "refund_failed", refund={**refund, **invalid}) == {}

    def success():
        return call(store, "fill_finish", fill=fid, owner="worker", status="refunded", stripe_refund_id=refund["id"])

    if not failure_first:
        assert success()["status"] == "refunded"
    assert resale.reconcile_payment_event({"type": "refund.updated", "data": {"object": refund}}, settings)
    blocked = call(store, "refund_obligation", kind="resale_fill", id=fid)
    assert blocked["status"] == "needs_ops" and blocked["stripe_refund_id"] == refund["id"]
    assert success() == {}  # Includes a late duplicate success in the reverse ordering.
    assert resale.reconcile_payment_event({"type": "refund.updated", "data": {"object": refund}}, settings)
    repeated = call(store, "refund_obligation", kind="resale_fill", id=fid)
    assert repeated["history"][-1] == {"at": REFUND_WINDOW + 101, "action": "stale_write", "from": "needs_ops", "to": "refunded"}
    assert sum(row["to"] == "needs_ops" for row in repeated["history"]) == 1
    assert not call(store, "fill_claim", fill=fid, owner="later", now=REFUND_WINDOW * 3)
    monkeypatch.setattr(resale, "STORE", store)
    create = Mock(side_effect=AssertionError("Terminal fill created another refund"))
    monkeypatch.setattr(resale.stripe.Refund, "create", create)
    resale.process_fill(fid, settings)
    create.assert_not_called()


@pytest.mark.parametrize("kind", ["resale_fill", "full_refund"])
@pytest.mark.parametrize("failure_first", [True, False])
@pytest.mark.parametrize("by_metadata", [True, False])
@pytest.mark.parametrize("failure", ["failed", "canceled"])
def test_nyte_shared_refund_obligations_are_monotonic(store: Store, unique: str, monkeypatch, kind: str, failure_first: bool, by_metadata: bool, failure: str, resale_enabled: bool) -> None:
    from unittest.mock import Mock

    from trusted_router.config import Settings
    from trusted_router.services import resale

    settings = Settings(environment="test", resale_enabled=resale_enabled)
    monkeypatch.setattr(resale, "STORE", store)
    alert = Mock()
    monkeypatch.setattr(resale, "alert_ops", alert)

    seller = unique + "seller"
    purchase(store, seller)
    if kind == "resale_fill":
        listing(store, seller)
        key = purchase(store, unique + "buyer", lots=1, allocate=True)["fills"][0]
        call(store, "fill_claim", fill=key, owner="worker")
        amount = 95
    else:
        call(store, "request_refund", position=seller, holder=seller, request_id="full", now=101)
        key, amount = seller, 300
    metadata = {"kind": kind, "fill_id": key} if kind == "resale_fill" else {"kind": kind, "position_id": seller}
    refund = {"id": "re_" + unique, "payment_intent": "pi_" + seller, "amount": amount, "status": "succeeded"}

    def success():
        if kind == "resale_fill":
            return call(store, "fill_finish", fill=key, owner="worker", stripe_refund_id=refund["id"], status="refunded")
        return call(store, "refund_acknowledge", position=key, owner="worker", refund=refund)

    if not by_metadata:
        if kind == "resale_fill":
            call(store, "fill_finish", fill=key, owner="worker", stripe_refund_id=refund["id"], status="retry")
            call(store, "fill_claim", fill=key, owner="worker")
        else:
            call(store, "refund_acknowledge", position=key, owner="worker", refund={**refund, "status": "pending"})
    if not failure_first:
        assert success()["status"] == "refunded"
    event = {**refund, "status": failure, "metadata": metadata if by_metadata else {}}
    for altered in ({"payment_intent": "pi_wrong"}, {"amount": amount + 1}):
        assert call(store, "refund_lifecycle", refund={**event, **altered}) == {}
    for _ in range(2):
        assert resale.reconcile_payment_event({"type": "refund.updated", "data": {"object": event}}, settings)
        blocked = call(store, "refund_obligation", kind=kind, id=key)
        assert blocked["status"] == "needs_ops" and blocked["stripe_refund_id"] == refund["id"]
    assert alert.call_count == 2
    success()
    # Every lifecycle event, including stale success, uses the same rule.
    assert resale.reconcile_payment_event({"type": "refund.updated", "data": {"object": {**event, "status": "succeeded"}}}, settings)
    row = call(store, "refund_obligation", kind=kind, id=key)
    assert row["status"] == "needs_ops" and row["history"][-1]["action"] == "stale_write"
    assert sum(h["to"] == "needs_ops" for h in row["history"]) == 1

    def queue_ids(queue):
        cursor, ids, seen = None, [], set()
        while True:
            page = call(store, "pending", queue=queue, cursor=cursor)
            ids.extend(r.get("id", r.get("position")) for r in page["rows"])
            cursor = page["cursor"]
            if not cursor:
                return ids
            assert cursor not in seen
            seen.add(cursor)
    queue = "refunds" if kind == "resale_fill" else "full_refunds"
    assert key not in queue_ids(queue)
    assert queue_ids(queue + "_needs_ops").count(key) == 1
    monkeypatch.setattr(resale, "STORE", store)
    create = Mock(side_effect=AssertionError("Terminal obligation reissued"))
    monkeypatch.setattr(resale.stripe.Refund, "create", create)
    if kind == "resale_fill":
        resale.process_fill(key, settings)
    else:
        assert call(store, "position", position=key)["status"] == "needs_ops"
        monkeypatch.setattr(resale.stripe.Refund, "retrieve", lambda *a, **kw: refund)
        assert resale.acknowledge_full_refund(key, refund["id"], settings)["status"] == "needs_ops"
        assert resale.alert_ops.called
    create.assert_not_called()


@pytest.mark.parametrize("kind", ["resale_fill", "full_refund"])
@pytest.mark.parametrize("failure", ["failed", "canceled"])
def test_nyte_unbound_refund_failure_survives_acknowledgement(store: Store, unique: str, kind: str, monkeypatch, resale_enabled: bool, failure: str) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from trusted_router.config import Settings
    from trusted_router.services import resale

    settings = Settings(environment="test", resale_enabled=resale_enabled, stripe_secret_key="sk_test_contract")  # noqa: S106
    monkeypatch.setattr(resale, "STORE", store)
    alert, create = Mock(), Mock(side_effect=AssertionError("Unbound failure was reissued"))
    monkeypatch.setattr(resale, "alert_ops", alert)
    monkeypatch.setattr(resale.stripe.Refund, "create", create)
    seller = unique + "seller"
    purchase(store, seller)
    if kind == "resale_fill":
        listing(store, seller)
        key = purchase(store, unique + "buyer", lots=1, allocate=True)["fills"][0]
        amount = 95
    else:
        call(store, "request_refund", position=seller, holder=seller, request_id="full", now=101)
        key, amount = seller, 300
    refund = {"id": "re_" + unique, "payment_intent": "pi_" + seller, "amount": amount, "status": failure}
    # No metadata and no binding, only the existing payment-to-position mapping.
    assert resale.reconcile_payment_event({"type": "refund.updated", "data": {"object": refund}}, settings)
    assert resale.reconcile_payment_event({"type": "refund.updated", "data": {"object": {**refund, "status": "succeeded"}}}, settings)
    stale = {**refund, "status": "succeeded"}
    monkeypatch.setattr(resale.stripe.Refund, "retrieve", lambda *a, **kw: stale)
    if kind == "resale_fill":
        # The next worker finds the issued refund with a stale success status.
        metadata_row = {**stale, "metadata": {"kind": kind, "fill_id": key}}
        monkeypatch.setattr(resale.stripe.Refund, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([metadata_row])))
        resale.process_fill(key, settings)
        resale.process_fill(key, settings)
    else:
        assert resale.acknowledge_full_refund(key, refund["id"], settings)["status"] == "needs_ops"
        assert resale.acknowledge_full_refund(key, refund["id"], settings)["status"] == "needs_ops"
    row = call(store, "refund_obligation", kind=kind, id=key)
    assert row["status"] == "needs_ops" and row["stripe_refund_id"] == refund["id"]
    assert row["history"][-1]["action"] == "stale_write"
    queue = "refunds" if kind == "resale_fill" else "full_refunds"
    assert key not in refund_queue_ids(store, queue)
    assert refund_queue_ids(store, queue + "_needs_ops").count(key) == 1
    assert alert.called and refund["id"] in alert.call_args.args[2]
    create.assert_not_called()


@pytest.mark.parametrize("kind", ["resale_fill", "full_refund"])
def test_nyte_existing_metadata_obligation_reconciles_in_both_modes(store: Store, unique: str, monkeypatch, kind: str, resale_enabled: bool) -> None:
    from unittest.mock import Mock

    from trusted_router.config import Settings
    from trusted_router.services import resale

    settings = Settings(environment="test", resale_enabled=resale_enabled)
    monkeypatch.setattr(resale, "STORE", store)
    alert = Mock()
    monkeypatch.setattr(resale, "alert_ops", alert)

    seller = unique + "seller"
    purchase(store, seller)
    if kind == "resale_fill":
        listing(store, seller)
        key = purchase(store, unique + "buyer", lots=1, allocate=True)["fills"][0]
        call(store, "fill_claim", fill=key, owner="worker")
        amount, metadata = 95, {"kind": kind, "fill_id": key}
    else:
        key = seller
        call(store, "request_refund", position=key, holder=key, now=101, request_id="full")
        amount, metadata = 300, {"kind": kind, "position_id": key}
    monkeypatch.setattr(resale, "STORE", store)
    alert = Mock()
    monkeypatch.setattr(resale, "alert_ops", alert)
    refund = {"id": "re_" + unique, "payment_intent": "pi_" + seller, "amount": amount, "metadata": metadata, "status": "failed"}
    # The obligation exists, but the worker has not yet recorded its refund ID.
    assert resale.reconcile_payment_event({"type": "refund.failed", "data": {"object": refund}}, settings)
    row = call(store, "refund_obligation", kind=kind, id=key)
    assert row["status"] == "needs_ops" and row["stripe_refund_id"] == refund["id"]
    alert.assert_called_once()
    settings.resale_enabled = True
    create = Mock(side_effect=AssertionError("Re-enabling sales reissued a failed refund"))
    monkeypatch.setattr(resale.stripe.Refund, "create", create)
    if kind == "resale_fill":
        resale.process_fill(key, settings)
    else:
        monkeypatch.setattr(resale.stripe.Refund, "retrieve", lambda *a, **kw: {**refund, "status": "succeeded"})
        assert resale.acknowledge_full_refund(key, refund["id"], settings)["status"] == "needs_ops"
    create.assert_not_called()


@pytest.mark.parametrize("failure", ["failed", "canceled"])
def test_nyte_unmapped_refund_evidence_is_inert(store: Store, unique: str, monkeypatch, resale_enabled: bool, failure: str) -> None:
    from unittest.mock import Mock

    from trusted_router.config import Settings
    from trusted_router.services import resale

    settings = Settings(environment="test", resale_enabled=resale_enabled)
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", lambda payment, **kw: {"id": payment, "metadata": {"kind": "other_product"}})
    monkeypatch.setattr(resale, "STORE", store)
    alert = Mock()
    monkeypatch.setattr(resale, "alert_ops", alert)
    refund = {"id": "re_" + unique, "payment_intent": "pi_" + unique, "amount": 300, "status": failure,
              "metadata": {"kind": "full_refund", "position_id": unique}}
    assert not resale.reconcile_payment_event({"type": "refund.updated", "data": {"object": refund}}, settings)
    assert call(store, "refund_lifecycle", refund=refund) == {}
    # The unrelated event did not leave evidence that could poison a later binding.
    purchase(store, unique)
    call(store, "request_refund", position=unique, holder=unique, now=101, request_id="full")
    monkeypatch.setattr(resale.stripe.Refund, "retrieve", lambda *a, **kw: {**refund, "status": "succeeded"})
    assert resale.acknowledge_full_refund(unique, refund["id"], settings)["status"] == "refunded"
    assert unique not in refund_queue_ids(store, "full_refunds_needs_ops")
    alert.assert_not_called()


@pytest.mark.parametrize("path", ["checkout", "historical_import"])
@pytest.mark.parametrize("timing", ["before_mapping", "during_snapshot"])
@pytest.mark.parametrize("event_type", ["refund.created", "charge.refunded", "charge.dispute.created"])
def test_nyte_purchase_adverse_interleaving(store: Store, unique: str, monkeypatch, resale_enabled: bool, path: str, timing: str, event_type: str) -> None:
    from types import SimpleNamespace

    from tests.test_reserved_tokens import event
    from trusted_router.config import Settings
    from trusted_router.services import resale, reserved_tokens

    settings = Settings(environment="test", resale_enabled=resale_enabled)
    monkeypatch.setattr(resale, "STORE", store)
    obj = event()["data"]["object"]
    obj.update(id="cs_" + unique, payment_intent="pi_" + unique)
    holder = resale.holder_key(obj["customer_details"]["email"])
    seller = unique + "seller"
    purchase(store, seller, grade="A")
    lid = listing(store, seller)
    if path == "historical_import":
        store.claim_reserved_notification(obj["id"], "old", now=100)
        store.finish_reserved_notification(obj["id"], "old", sent=True)
    healthy = {"id": obj["payment_intent"], "metadata": {"kind": reserved_tokens.KIND},
               "status": "succeeded", "latest_charge": {"paid": True, "amount_refunded": 0}}
    adverse = {"type": event_type, "data": {"object": {
        "id": "re_" + unique, "payment_intent": obj["payment_intent"], "status": "succeeded",
        "paid": True, "amount": 1600, "amount_refunded": 1600,
    }}}
    monkeypatch.setattr(resale.stripe.Refund, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([])))
    snapshot_reads = []

    def retrieve(payment, **kwargs):
        assert payment == obj["payment_intent"]
        if kwargs.get("expand") and not snapshot_reads:
            # Inject one concurrent event; its repair may also read Stripe.
            # The provider snapshot is healthy, but is stale by final commit.
            pending = inspect_position(store, obj["id"])
            assert call(store, "payment", payment_intent=payment)["session_id"] == obj["id"]
            assert pending["status"] == ("needs_review" if timing == "before_mapping" else "pending")
            assert pending["fills"] == []
            with pytest.raises(ValueError, match="review"):
                call(store, "list", position=obj["id"], holder=holder, lots=1, request_id="pending-list")
            # Pending full refunds are now accepted for manual verification;
            # the dedicated repair contract covers that path.
            snapshot_reads.append(payment)
            if timing == "during_snapshot":
                assert resale.reconcile_payment_event(adverse, settings)
        return healthy

    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", retrieve)
    if timing == "before_mapping":
        # Model the old read-before-write interleaving / an already captured
        # healthy snapshot: the signed adverse event precedes any mapping.
        assert call(store, "payment", payment_intent=obj["payment_intent"]) == {}
        assert resale.reconcile_payment_event(adverse, settings)
        assert call(store, "payment", payment_intent=obj["payment_intent"]) == {}
    if path == "historical_import":
        monkeypatch.setattr(resale.stripe.Customer, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([{"id": "cus_old"}])))
        monkeypatch.setattr(resale.stripe.checkout.Session, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([obj])))
        resale.backfill_email(obj["customer_details"]["email"], holder, settings)
    else:
        # Exercise finalization even if the flag changes while work is in flight.
        # Disabled public checkout still bypasses this service (separate contract).
        resale.record_purchase(obj, "evt_" + unique, 100, settings)
    assert snapshot_reads == [obj["payment_intent"]]
    p = inspect_position(store, obj["id"])
    assert p["status"] == "needs_review" and p["payment_review"]
    assert p["fills"] == [] and p["purchase_completed_at"] == 100
    repaired_by_event = timing == "during_snapshot" and event_type == "refund.created"
    assert p["allocation_status"] == ("complete" if resale_enabled and path == "checkout" and not repaired_by_event else "historical")
    assert call(store, "position", position=seller)["sold_lots"] == 0
    with pytest.raises(ValueError, match="review"):
        call(store, "list", position=obj["id"], holder=holder, lots=1, request_id="after")
    call(store, "withdraw", position=seller, holder=seller, listing=lid, request_id="cleanup")


@pytest.mark.parametrize("path", ["checkout", "historical_import"])
def test_nyte_pending_purchase_retries_after_stripe_failure(store: Store, unique: str, monkeypatch, resale_enabled: bool, path: str) -> None:
    from tests.test_reserved_tokens import event
    from trusted_router.config import Settings
    from trusted_router.services import resale

    settings = Settings(environment="test", resale_enabled=resale_enabled)
    monkeypatch.setattr(resale, "STORE", store)
    obj = event()["data"]["object"]
    obj.update(id="cs_" + unique, payment_intent="pi_" + unique)
    if path == "historical_import":
        store.claim_reserved_notification(obj["id"], "old", now=100)
        store.finish_reserved_notification(obj["id"], "old", sent=True)
    seller = unique + "seller"
    purchase(store, seller, grade="A")
    lid = listing(store, seller)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Stripe unavailable")
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", unavailable)
    for timestamp in (100, 200):
        with pytest.raises(RuntimeError, match="unavailable"):
            resale.record_purchase(obj, "evt_" + unique, timestamp, settings, backfill=path == "historical_import")
        p = inspect_position(store, obj["id"])
        assert p["status"] == "pending" and p["fills"] == [] and p["purchase_completed_at"] == 100
        assert call(store, "payment", payment_intent=obj["payment_intent"])["session_id"] == obj["id"]
        assert call(store, "position", position=seller)["sold_lots"] == 0
        with pytest.raises(ValueError, match="review"):
            call(store, "list", position=obj["id"], holder=p["holder"], lots=1, request_id="pending")
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", lambda *a, **kw: {
        "status": "succeeded", "latest_charge": {"paid": True, "amount_refunded": 0},
    })
    result = resale.record_purchase(obj, "evt_" + unique, 300, settings, backfill=path == "historical_import")
    assert result["status"] == "held" and result["purchase_completed_at"] == 100
    assert bool(result["fills"]) == (resale_enabled and path == "checkout")
    assert resale.record_purchase(obj, "evt_" + unique, 400, settings, backfill=path == "historical_import") == result
    call(store, "withdraw", position=seller, holder=seller, listing=lid, request_id="cleanup")


@pytest.mark.parametrize("expanded", [True, False])
@pytest.mark.parametrize("event_type", ["refund.created", "charge.refunded", "charge.dispute.created"])
def test_nyte_unmapped_nonreservation_payment_stays_inert(store: Store, unique: str, monkeypatch, resale_enabled: bool, expanded: bool, event_type: str) -> None:
    from unittest.mock import Mock

    from trusted_router.config import Settings
    from trusted_router.services import resale

    monkeypatch.setattr(resale, "STORE", store)
    settings = Settings(environment="test", resale_enabled=resale_enabled)
    payment = {"id": "pi_" + unique, "metadata": {"kind": "other_product"}}
    retrieve = Mock(return_value=payment)
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", retrieve)
    command = Mock(wraps=resale.command)
    adverse = {"type": event_type, "data": {"object": {
        "id": "re_" + unique, "payment_intent": payment if expanded else payment["id"],
        "metadata": {"kind": "reserved_tokens_2027"}, "status": "succeeded", "paid": True,
        "amount": 300, "amount_refunded": 300,
    }}}
    with monkeypatch.context() as patch:
        patch.setattr(resale, "command", command)
        assert not resale.reconcile_payment_event(adverse, settings)
    assert [call.args[0] for call in command.call_args_list] == ["payment"]
    assert retrieve.call_count == (0 if expanded else 1)
    assert purchase(store, unique)["status"] == "held"  # No sticky evidence.


@pytest.mark.parametrize("failure", ["failed", "canceled"])
def test_nyte_early_refund_failure_retained_until_binding(store: Store, unique: str, monkeypatch, resale_enabled: bool, failure: str) -> None:
    from unittest.mock import Mock

    from trusted_router.config import Settings
    from trusted_router.services import resale

    monkeypatch.setattr(resale, "STORE", store)
    settings = Settings(environment="test", resale_enabled=resale_enabled)
    # Expanded PI metadata is authoritative; refund metadata is unnecessary.
    refund = {"id": "re_" + unique, "payment_intent": {"id": "pi_" + unique, "metadata": {"kind": "reserved_tokens_2027"}},
              "amount": 300, "status": failure}
    retrieve = Mock(side_effect=AssertionError("Expanded PaymentIntent needs no lookup"))
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", retrieve)
    assert resale.reconcile_payment_event({"type": "refund.updated", "data": {"object": refund}}, settings)
    retrieve.assert_not_called()
    assert call(store, "payment", payment_intent="pi_" + unique) == {}
    purchase(store, unique)
    call(store, "request_refund", position=unique, holder=unique, now=101, request_id="full")
    monkeypatch.setattr(resale.stripe.Refund, "retrieve", lambda *a, **kw: {**refund, "payment_intent": "pi_" + unique, "status": "succeeded"})
    alert, create = Mock(), Mock(side_effect=AssertionError("Failed refund reissued"))
    monkeypatch.setattr(resale, "alert_ops", alert)
    monkeypatch.setattr(resale.stripe.Refund, "create", create)
    assert resale.acknowledge_full_refund(unique, refund["id"], settings)["status"] == "needs_ops"
    assert refund_queue_ids(store, "full_refunds_needs_ops").count(unique) == 1
    assert resale.acknowledge_full_refund(unique, refund["id"], settings)["status"] == "needs_ops"
    assert alert.called
    create.assert_not_called()


@pytest.mark.parametrize("initial_enabled", [True, False], ids=["on-off-on", "off-on-on"])
@pytest.mark.parametrize("reconcile_path", ["replay", "import"])
@pytest.mark.parametrize("action", ["request_refund", "list"])
def test_nyte_completed_toggle_repairs_pending(store: Store, unique: str, monkeypatch, initial_enabled: bool, reconcile_path: str, action: str) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from tests.test_reserved_tokens import event
    from trusted_router.config import Settings
    from trusted_router.services import resale, reserved_tokens

    settings = Settings(environment="test", resale_enabled=initial_enabled)
    monkeypatch.setattr(resale, "STORE", store)
    monkeypatch.setattr(reserved_tokens, "STORE", store)
    monkeypatch.setattr(reserved_tokens, "get_email_service", lambda _: SimpleNamespace(send=lambda _: True))
    monkeypatch.setattr(resale, "retry_buyer_refunds", lambda *a: None)
    clock = [100]
    monkeypatch.setattr(reserved_tokens, "_utc_now", lambda: datetime.fromtimestamp(clock[0], UTC))
    data = event()
    obj = data["data"]["object"]
    data["id"] = "evt_" + unique
    obj.update(id="cs_" + unique, payment_intent="pi_" + unique)
    seller = unique + "seller"
    purchase(store, seller, grade="A")
    lid = listing(store, seller)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Stripe unavailable")
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", unavailable)
    if initial_enabled:
        with pytest.raises(RuntimeError, match="unavailable"):
            reserved_tokens.handle_reservation_event(data, settings)
        assert call(store, "position", position=obj["id"])["status"] == "pending"
    else:
        assert reserved_tokens.handle_reservation_event(data, settings)["recorded"]
        assert call(store, "position", position=obj["id"]) == {}
    settings.resale_enabled = not initial_enabled
    clock[0] = 1000  # The failed first attempt's notification lease has expired.
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", lambda *a, **kw: {
        "status": "succeeded", "latest_charge": {"paid": True, "amount_refunded": 0},
    })
    reserved_tokens.handle_reservation_event(data, settings)
    assert store.claim_reserved_notification(obj["id"], "probe", now=1100) == ("sent", 100)
    settings.resale_enabled = True
    clock[0] = 2000
    if reconcile_path == "replay":
        reserved_tokens.handle_reservation_event(data, settings)
    else:
        holder = resale.holder_key(obj["customer_details"]["email"])
        monkeypatch.setattr(resale.stripe.Customer, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([{"id": "cus_old"}])))
        monkeypatch.setattr(resale.stripe.checkout.Session, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([obj])))
        resale.backfill_email(obj["customer_details"]["email"], holder, settings)
    p = call(store, "position", position=obj["id"])
    assert (p["status"], p["allocation_status"], p["purchase_completed_at"]) == ("held", "historical", 100)
    assert p["fills"] == [] and p["new_lots"] == p["lots"]
    assert call(store, "position", position=seller)["sold_lots"] == 0
    result = call(store, action, position=obj["id"], holder=p["holder"], lots=1,
                  now=101 if action == "request_refund" else 100 + REFUND_WINDOW + 1, request_id=unique + action)
    assert result["status"] == ("refund_requested" if action == "request_refund" else "active")
    if action == "list":
        call(store, "withdraw", position=obj["id"], holder=p["holder"], listing=result["listing_id"], request_id=unique + "cleanup-buyer")
    call(store, "withdraw", position=seller, holder=seller, listing=lid, request_id="cleanup-seller")


def test_nyte_purchase_completion_ordering_matrix(store: Store, unique: str, monkeypatch) -> None:
    from datetime import UTC, datetime
    from itertools import product
    from types import SimpleNamespace

    from tests.test_reserved_tokens import event
    from trusted_router.config import Settings
    from trusted_router.services import resale, reserved_tokens

    settings = Settings(environment="test")
    monkeypatch.setattr(resale, "STORE", store)
    monkeypatch.setattr(reserved_tokens, "STORE", store)
    monkeypatch.setattr(reserved_tokens, "get_email_service", lambda _: SimpleNamespace(send=lambda _: True))
    monkeypatch.setattr(resale, "retry_buyer_refunds", lambda *a: None)
    clock, provider_ok = [100], [True]
    monkeypatch.setattr(reserved_tokens, "_utc_now", lambda: datetime.fromtimestamp(clock[0], UTC))

    def retrieve(*args, **kwargs):
        if not provider_ok[0]:
            raise RuntimeError("Stripe unavailable")
        return {"status": "succeeded", "latest_charge": {"paid": True, "amount_refunded": 0}}
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", retrieve)
    cases = list(product((True, False), repeat=4))
    for index, (create_enabled, read_ok, completion_enabled, replay_enabled) in enumerate(cases):
        case = f"{unique}-{index}"
        data = event()
        obj = data["data"]["object"]
        data["id"] = "evt_" + case
        obj.update(id="cs_" + case, payment_intent="pi_" + case)
        seller = case + "seller"
        purchase(store, seller, grade="A")
        lid = listing(store, seller)
        completed = False

        def deliver(enabled: bool, stripe_ok: bool, at: int, *, obj=obj, data=data, case=case, seller=seller) -> None:
            nonlocal completed
            settings.resale_enabled, provider_ok[0], clock[0] = enabled, stripe_ok, at
            before = inspect_position(store, obj["id"])
            before_fills = before.get("fills", [])
            if enabled and not stripe_ok:
                with pytest.raises(RuntimeError, match="unavailable"):
                    reserved_tokens.handle_reservation_event(data, settings)
            else:
                reserved_tokens.handle_reservation_event(data, settings)
                after = inspect_position(store, obj["id"])
                if enabled:
                    assert after["status"] == "held" and after["allocation_status"] != "pending", (case, at)
                if completed:
                    assert after.get("fills", []) == before_fills, (case, at, "allocated after notification")
                completed = True
            p = inspect_position(store, obj["id"])
            assert len(p.get("fills", [])) <= 1, case
            assert call(store, "position", position=seller)["sold_lots"] <= 2, case

        deliver(create_enabled, read_ok, 100)
        deliver(completion_enabled, True, 1000)
        deliver(replay_enabled, True, 2000)
        # A disabled replay intentionally keeps #2125's no-ledger path. Once
        # enabled, every interrupted/imported position must be finalized.
        deliver(True, True, 3000)
        p = inspect_position(store, obj["id"])
        should_allocate = create_enabled and (read_ok or completion_enabled)
        assert p["allocation_status"] == ("complete" if should_allocate else "historical"), case
        assert p["status"] == "held" and p["purchase_completed_at"] == 100, case
        assert p["new_lots"] == (0 if should_allocate else 2), case
        assert len(p["fills"]) == int(should_allocate), case
        assert call(store, "position", position=seller)["sold_lots"] == (2 if should_allocate else 0), case
        # Further imports/replays preserve the completed allocation and timestamp.
        assert resale.record_purchase(obj, "backfill:" + case, None, settings, backfill=True) == p
        deliver(True, True, 4000)
        assert inspect_position(store, obj["id"]) == p
        call(store, "withdraw", position=seller, holder=seller, listing=lid, request_id="cleanup")


@pytest.mark.parametrize("notified", [True, False])
@pytest.mark.parametrize("backfill", [True, False])
@pytest.mark.parametrize("allocate", [True, False])
@pytest.mark.parametrize("adverse", [True, False])
def test_nyte_finalization_invariant_repairs_pending(store: Store, unique: str, notified: bool, backfill: bool, allocate: bool, adverse: bool) -> None:
    seller, buyer = unique + "seller", unique + "buyer"
    purchase(store, seller)
    listing(store, seller)
    p = {"id": buyer, "session_id": buyer, "holder": buyer, "lots": 2, "grade": unique[:12],
         "per_lot_cents": 100, "payment_intent": "pi_" + buyer, "purchase_completed_at": None}
    store.claim_reserved_notification(buyer, "webhook", now=200)
    call(store, "purchase_prepare", position=p, event_id="evt_" + buyer)
    if adverse:
        call(store, "reconcile_payment", payment_intent=p["payment_intent"], payment_state={"disputed": True})
    if notified:
        store.finish_reserved_notification(buyer, "webhook", sent=True)
    result = call(store, "purchase", position=p, event_id="finish_" + buyer, backfill=backfill,
                  allocate=allocate, fee_bps=500, payment_state={"unpaid": False, "amount_refunded": 0})
    eligible = allocate and not backfill and not notified
    assert result["status"] == ("needs_review" if adverse else "held")
    assert result["allocation_status"] == ("complete" if eligible else "historical")
    assert result["purchase_completed_at"] == 200
    assert bool(result.get("payment_review")) == adverse
    assert len(result["fills"]) == int(eligible and not adverse)
    assert result["new_lots"] == (0 if eligible and not adverse else 2)
    assert call(store, "purchase", position=p, event_id="replay_" + buyer, allocate=True, fee_bps=500) == result
    assert call(store, "position", position=seller)["sold_lots"] == (2 if eligible and not adverse else 0)


@pytest.mark.parametrize("backfill", [True, False], ids=["import", "replay"])
def test_nyte_completed_legacy_held_pending_is_repaired(store: Store, unique: str, monkeypatch, backfill: bool) -> None:
    from trusted_router import storage_resale

    buyer = unique + "buyer"
    p = {"id": buyer, "session_id": buyer, "holder": buyer, "lots": 2, "grade": unique[:12],
         "per_lot_cents": 100, "payment_intent": "pi_" + buyer, "purchase_completed_at": 100}
    original_execute = storage_resale.execute

    def old_import(read, write, command):
        def legacy_write(kind, key, row):
            if kind == "nyte_position" and key == buyer:
                row = {**row, "status": "held", "allocation_status": "pending"}
            write(kind, key, row)
        return original_execute(read, legacy_write, command)
    # Seed the exact persisted pre-fix shape through each backend transaction.
    with monkeypatch.context() as patch:
        patch.setattr(storage_resale, "execute", old_import)
        call(store, "purchase_prepare", position=p, event_id="old_" + buyer)
    assert call(store, "position", position=buyer)["allocation_status"] == "pending"
    store.claim_reserved_notification(buyer, "old", now=100)
    store.finish_reserved_notification(buyer, "old", sent=True)
    repaired = call(store, "purchase", position=p, event_id="repair_" + buyer,
                    backfill=backfill, allocate=True, fee_bps=500, payment_state={"unpaid": False})
    assert repaired["status"] == "held" and repaired["allocation_status"] == "historical"
    assert repaired["purchase_completed_at"] == 100 and repaired["new_lots"] == 2 and repaired["fills"] == []
    assert call(store, "request_refund", position=buyer, holder=buyer, now=101, request_id="refund")["status"] == "refund_requested"


@pytest.mark.parametrize("recovery", ["holder", "sweep", "request_refund"])
@pytest.mark.parametrize("provider", ["healthy", "unavailable", "adverse_race"])
def test_nyte_interrupted_checkout_repairs_without_replay(store: Store, unique: str, monkeypatch, recovery: str, provider: str) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import Mock

    from tests.test_reserved_tokens import event
    from trusted_router.config import Settings
    from trusted_router.routes.resale import HolderRequest, handle
    from trusted_router.services import resale, reserved_tokens

    settings = Settings(environment="test", resale_enabled=True, stripe_secret_key="sk_test")  # noqa: S106
    monkeypatch.setattr(resale, "STORE", store)
    monkeypatch.setattr(reserved_tokens, "STORE", store)
    monkeypatch.setattr(reserved_tokens, "get_email_service", lambda _: SimpleNamespace(send=lambda _: True))
    monkeypatch.setattr(resale, "retry_buyer_refunds", lambda *a: None)
    clock = [100]
    monkeypatch.setattr(resale, "time", SimpleNamespace(time=lambda: clock[0]))  # patch only resale's clock; a global time.time patch breaks gRPC deadlines
    monkeypatch.setattr(reserved_tokens, "_utc_now", lambda: datetime.fromtimestamp(clock[0], UTC))
    data = event()
    obj = data["data"]["object"]
    data["id"] = "evt_" + unique
    obj.update(id="cs_" + unique, payment_intent="pi_" + unique)
    holder = resale.holder_key(obj["customer_details"]["email"])
    seller = unique + "seller"
    purchase(store, seller, grade="A")
    lid = listing(store, seller)
    retrieve = Mock(side_effect=RuntimeError("Stripe unavailable"))
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", retrieve)
    with pytest.raises(RuntimeError, match="unavailable"):
        reserved_tokens.handle_reservation_event(data, settings)
    assert inspect_position(store, obj["id"])["status"] == "pending"
    settings.resale_enabled, clock[0] = False, 1000
    with monkeypatch.context() as patch:
        # Exactly #2125 on disabled completion, including a broken ledger.
        patch.setattr(type(store), "reserved_resale", Mock(side_effect=AssertionError("Disabled checkout touched resale")))
        assert reserved_tokens.handle_reservation_event(data, settings)["recorded"]
    assert store.claim_reserved_notification(obj["id"], "probe", now=1001) == ("sent", 100)
    assert inspect_position(store, obj["id"])["status"] == "pending"
    settings.resale_enabled = True
    alert, create = Mock(), Mock(side_effect=AssertionError("Manual full refund must not be issued"))
    monkeypatch.setattr(resale, "alert_ops", alert)
    monkeypatch.setattr(resale.stripe.Refund, "create", create)

    def current(*args, **kwargs):
        if provider == "unavailable":
            raise RuntimeError("Stripe still unavailable")
        if provider == "adverse_race":
            # Retained after the healthy snapshot, before its conditional commit.
            resale.command("reconcile_payment", settings=settings, payment_intent=obj["payment_intent"], payment_state={"disputed": True})
        return {"status": "succeeded", "latest_charge": {"paid": True, "amount_refunded": 0}}
    retrieve.side_effect = current
    retrieve.reset_mock()
    # Issue an authenticated session without a link/backfill or webhook replay.
    resale.command("token_create", key=unique, holder=holder)
    resale.command("token_consume", key=unique, session=reference(unique))
    if recovery == "sweep":
        # The background job works even with the public feature disabled.
        settings.resale_enabled = False
        resale.command("repair_cursor", cursor=None)
        totals = {"checked": 0, "repaired": 0, "pending": 0}
        for _ in range(10000):
            stats = resale.sweep_pending_positions(settings, limit=1)
            assert stats["checked"] <= 1
            for key in totals:
                totals[key] += stats[key]
            if resale.command("repair_cursor").get("cursor") is None:
                break
        else:
            pytest.fail("Repair sweep did not finish its bounded pages")
        assert retrieve.called  # No holder access has occurred.
        assert totals["repaired"] >= int(provider != "unavailable")
        settings.resale_enabled = True
    elif recovery == "holder":
        result = handle(HolderRequest(action="positions", session=unique), settings)
        view = next(p for p in result["positions"] if p["session_id"] == obj["id"])
        assert view["status"] == {"healthy": "held", "unavailable": "pending", "adverse_race": "needs_review"}[provider]
        assert view["refund_request_allowed"] == (provider != "adverse_race")
    else:
        if provider == "adverse_race":
            with pytest.raises(ValueError, match="unavailable"):
                handle(HolderRequest(action="request_refund", session=unique, position=obj["id"], request_id=unique), settings)
            # A rejected decision rolls back; an ordinary view persists repair.
            handle(HolderRequest(action="positions", session=unique), settings)
        else:
            assert handle(HolderRequest(action="request_refund", session=unique, position=obj["id"], request_id=unique), settings)["status"] == "refund_requested"
    repaired = inspect_position(store, obj["id"])
    assert repaired["allocation_status"] == "historical" and repaired["purchase_completed_at"] == 100
    assert repaired["fills"] == [] and repaired["new_lots"] == repaired["lots"]
    assert inspect_position(store, seller)["sold_lots"] == 0
    if provider == "adverse_race":
        assert repaired["status"] == "needs_review" and repaired["payment_review"]
    else:
        assert handle(HolderRequest(action="request_refund", session=unique, position=obj["id"], request_id=unique), settings)["status"] == "refund_requested"
        assert obj["id"] in refund_queue_ids(store, "full_refunds")
        assert alert.called
        assert inspect_position(store, obj["id"])["payment_verification_pending"] == (provider == "unavailable")
    create.assert_not_called()
    call(store, "withdraw", position=seller, holder=seller, listing=lid, request_id="cleanup")


@pytest.mark.parametrize("notified", [True, False])
def test_nyte_repair_checks_legacy_session_and_sweep_age(store: Store, unique: str, monkeypatch, notified: bool) -> None:
    from unittest.mock import Mock

    from trusted_router.config import Settings
    from trusted_router.services import resale

    settings = Settings(environment="test")
    monkeypatch.setattr(resale, "STORE", store)
    p = {"id": unique, "session_id": unique, "holder": unique, "lots": 2, "grade": unique[:12],
         "per_lot_cents": 100, "payment_intent": "pi_" + unique, "purchase_completed_at": 100}
    call(store, "purchase_prepare", position=p, event_id=unique, now=100)
    if notified:
        store.claim_reserved_notification(unique, "worker", now=100)
        store.finish_reserved_notification(unique, "worker", sent=True)
    session = Mock(return_value={"id": unique, "payment_intent": p["payment_intent"], "status": "complete", "payment_status": "paid"})
    payment = Mock(return_value={"status": "succeeded", "latest_charge": {"paid": True, "amount_refunded": 0}})
    monkeypatch.setattr(resale.stripe.checkout.Session, "retrieve", session)
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", payment)
    assert not resale.command("repair_position", settings=settings, position=unique, now=399)["repair_performed"]
    session.assert_not_called()
    payment.assert_not_called()
    repaired = resale.command("repair_position", settings=settings, position=unique, now=400)
    assert repaired["repair_performed"] and repaired["allocation_status"] == "historical" and repaired["status"] == "held"
    assert session.call_count == int(not notified)
    assert payment.call_count == 1
    assert not resale.command("repair_position", settings=settings, position=unique, now=401)["repair_performed"]
    assert payment.call_count == 1
    # Repaired historical inventory is listable after the original deadline.
    assert resale.command("list", settings=settings, position=unique, holder=unique, lots=1,
                          request_id=unique, now=101 + REFUND_WINDOW)["status"] == "active"


@pytest.mark.parametrize("decision", ["list", "withdraw", "allocation", "ops_queue", "worker"])
def test_nyte_all_position_decisions_repair_legacy_rows(store: Store, unique: str, monkeypatch, decision: str) -> None:
    from trusted_router import storage_resale
    from trusted_router.config import Settings
    from trusted_router.services import resale

    settings = Settings(environment="test")
    monkeypatch.setattr(resale, "STORE", store)
    seller, buyer = unique + "seller", unique + "buyer"
    purchase(store, seller)
    lid = listing(store, seller)
    fill = purchase(store, buyer, lots=1, allocate=True)["fills"][0]
    store.claim_reserved_notification(seller, "worker", now=100)
    store.finish_reserved_notification(seller, "worker", sent=True)
    original = storage_resale.execute

    def legacy(read, write, command):
        def old_write(kind, key, row):
            if kind == "nyte_position" and key == seller:
                row = {**row, "allocation_status": "pending"}
            write(kind, key, row)
        return original(read, old_write, command)
    with monkeypatch.context() as patch:
        patch.setattr(storage_resale, "execute", legacy)
        call(store, "purchase_prepare", position={"session_id": seller}, event_id="legacy_" + unique)
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", lambda *a, **kw: {
        "status": "succeeded", "latest_charge": {"paid": True, "amount_refunded": 0},
    })
    if decision in {"list", "withdraw"}:
        # Make room for another listing before seeding the legacy shape again.
        if decision == "list":
            resale.command("withdraw", settings=settings, position=seller, holder=seller, listing=lid, request_id="prepare")
            with monkeypatch.context() as patch:
                patch.setattr(storage_resale, "execute", legacy)
                call(store, "purchase_prepare", position={"session_id": seller}, event_id="legacy2_" + unique)
        resale.command(decision, settings=settings, position=seller, holder=seller, listing=lid, lots=1,
                       request_id=unique, now=REFUND_WINDOW + 101)
    elif decision == "allocation":
        resale.command("purchase", settings=settings, position={"id": unique, "session_id": unique, "holder": unique,
                       "lots": 1, "grade": seller[:12], "per_lot_cents": 100, "payment_intent": "pi_" + unique,
                       "purchase_completed_at": 100}, event_id=unique, allocate=True, fee_bps=500)
    elif decision == "ops_queue":
        cursor = None
        while True:
            page = resale.command("pending", settings=settings, cursor=cursor)
            if any(row["id"] == fill for row in page["rows"]):
                break
            cursor = page["cursor"]
            assert cursor is not None
    else:
        resale.command("fill_claim", settings=settings, fill=fill, owner="worker")
    p = inspect_position(store, seller)
    assert p["allocation_status"] == "historical" and p["status"] == "held" and p["purchase_completed_at"] == 100
    assert p["sold_lots"] == (2 if decision == "allocation" else 1)
    call(store, "withdraw", position=seller, holder=seller, listing=lid, request_id="cleanup")
