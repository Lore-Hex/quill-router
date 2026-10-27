"""Shared ledger contract, including the native migration-backed emulator gate."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.nyte_resale_helpers import call, listing, purchase
from trusted_router.storage_resale import REFUND_WINDOW, reference
from trusted_router.store_protocol import Store


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


def test_nyte_timestamped_import_still_allocates_after_webhook_claim(store: Store, unique: str) -> None:
    seller, buyer = unique + "seller", unique + "buyer"
    purchase(store, seller)
    listing(store, seller)
    # Critical interleaving: webhook persists completion time, then importer
    # writes the position, then webhook finally executes its allocation.
    store.claim_reserved_notification(buyer, "webhook", now=200)
    p = {"session_id": buyer, "holder": buyer, "lots": 2, "grade": unique[:12],
         "per_lot_cents": 100, "payment_intent": "pi_" + buyer, "purchase_completed_at": None}
    imported = call(store, "purchase", position=p, event_id="backfill:" + buyer, backfill=True)
    assert imported["purchase_completed_at"] == 200 and imported["allocation_status"] == "pending"
    result = call(store, "purchase", position={**p, "purchase_completed_at": 999},
                  event_id="evt_" + buyer, allocate=True, fee_bps=500)
    assert result["purchase_completed_at"] == 200 and result["allocation_status"] == "complete"
    assert result["new_lots"] == 0 and len(result["fills"]) == 1
    assert call(store, "purchase", position=p, event_id="evt_retry_" + buyer, allocate=True) == result
    assert call(store, "position", position=seller)["sold_lots"] == 2
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
