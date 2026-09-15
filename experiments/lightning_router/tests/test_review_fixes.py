import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from lightning_router.app import create_app
from lightning_router.errors import QuoteUnavailable
from lightning_router.rates import Rate, Rates
from sqlalchemy import select, update


def test_rate_failure_has_short_negative_cache(monkeypatch):
    monkeypatch.setattr("lightning_router.rates.time.monotonic", lambda: 100)
    calls = []

    def fail(_):
        calls.append(1)
        raise httpx.ConnectError("private upstream detail")

    rates = Rates(httpx.Client(transport=httpx.MockTransport(fail)))
    for _ in range(5):
        with pytest.raises((httpx.ConnectError, QuoteUnavailable)):
            rates.current()
    assert len(calls) == 1


def test_limit_does_not_double_at_window_boundary(store):
    assert all(store.rate_limit("key:test", 899) for _ in range(10))
    assert not store.rate_limit("key:test", 900)
    assert store.rate_limit("key:test", 989)


def test_reused_node_settle_index_is_not_a_payment_identity(funding, raw_key):
    for _ in range(2):
        invoice = funding.create(raw_key, uuid.uuid4().hex, 100, new=True)
        row = funding.store.invoice(invoice["id"], funding.credentials.fingerprint(raw_key))
        funding.lnd.pay(row["payment_hash"])
        funding.lnd.rows[row["payment_hash"]] = replace(funding.lnd.rows[row["payment_hash"]], settle_index=1)
        assert funding.refresh(row)["credited"]
    assert len(funding.credits.payments) == 2


def test_missing_issued_invoice_is_not_recreated(funding, raw_key):
    invoice = funding.create(raw_key, uuid.uuid4().hex, 100, new=True)
    row = funding.store.invoice(invoice["id"], funding.credentials.fingerprint(raw_key))
    funding.lnd.rows.clear()
    funding.lnd.lookup = lambda _: None
    funding.reconcile()
    assert funding.lnd.creates == 1
    current = funding.store.invoice(row["id"], row["key_hash"])
    assert current["failure_code"] == "invoice_missing"
    assert not current["credited_at"]


def test_settled_aging_and_permanent_errors_are_visible(funding, raw_key, monkeypatch):
    from lightning_router.errors import FundingReviewRequired
    from lightning_router.store import invoices

    invoice = funding.create(raw_key, uuid.uuid4().hex, 100, new=True)
    row = funding.store.invoice(invoice["id"], funding.credentials.fingerprint(raw_key))
    funding.lnd.pay(row["payment_hash"])
    monkeypatch.setattr(funding.credits, "credit", lambda *args: (_ for _ in ()).throw(FundingReviewRequired("credit_rejected")))
    assert funding.reconcile()["failed"] == 1
    with funding.store.transaction() as conn:
        conn.execute(update(invoices).where(invoices.c.id == row["id"]).values(settled_at=int(time.time()) - 180))
    health = funding.store.delivery_health(int(time.time()))
    assert health["oldest_uncredited_seconds"] >= 180
    assert health["review_required"] == 1
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert raw_key not in response.text
        refreshed = client.post(f"/api/invoices/{row['id']}/refresh", headers={"Authorization": "Bearer " + raw_key}, json={})
        assert refreshed.status_code == 200
        assert refreshed.json()["attention_required"]


def test_retention_preserves_paid_and_unresolved_rows(funding, raw_key):
    from lightning_router.store import invoices

    canceled = funding.create(raw_key, uuid.uuid4().hex, 100, new=True)
    row = funding.store.invoice(canceled["id"], funding.credentials.fingerprint(raw_key))
    funding.refresh(row, cancel=True)
    paid = funding.create(raw_key, uuid.uuid4().hex, 100, new=True)
    paid_row = funding.store.invoice(paid["id"], row["key_hash"])
    funding.lnd.pay(paid_row["payment_hash"])
    funding.refresh(paid_row)
    open_invoice = funding.create(raw_key, uuid.uuid4().hex, 100, new=True)
    with funding.store.transaction() as conn:
        conn.execute(update(invoices).values(expires_at=1))
    result = funding.store.prune(int(time.time()), limit=1)
    assert result["invoices"] == 1
    with funding.store.transaction() as conn:
        remaining = set(conn.execute(select(invoices.c.id)).scalars())
    assert remaining == {paid["id"], open_invoice["id"]}


def test_health_does_not_claim_live_inference(funding):
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        data = client.get("/health").json()
    assert "inference_ready" not in data
    assert data["inference_configured"] is True


def test_max_quote_and_double_payment_fit_receipt_ceiling():
    from lightning_router.money import MAX_CREDIT_RECEIPT

    for spot in ("77577.655", "100000", "99999999"):
        rate = Rate.from_spot(Decimal(spot), 0)
        assert rate.credit_microdollars(2 * rate.invoice_msats(100000)) <= MAX_CREDIT_RECEIPT


@pytest.mark.parametrize("multiple", [1, 2, 3])
def test_real_limit_receipts_and_over_limit_review(funding, raw_key, multiple):
    from lightning_router.errors import FundingReviewRequired

    funding.rates.price = Decimal("77577.655")
    view = funding.create(raw_key, uuid.uuid4().hex, 100000, new=True)
    row = funding.store.invoice(view["id"], funding.credentials.fingerprint(raw_key))
    amount = row["requested_msat"] * multiple
    funding.lnd.pay(row["payment_hash"], amount)
    if multiple < 3:
        result = funding.refresh(row)
        assert result["credited"]
        assert int(result["balance_microdollars"]) > multiple * 1_000_000_000
        assert len(funding.credits.payments) == 1
    else:
        with pytest.raises(FundingReviewRequired, match="credit_amount_limit"):
            funding.refresh(row)
        row = funding.store.invoice(row["id"], row["key_hash"])
        assert row["amount_msat"] == amount
        assert row["credit_microdollars"] > 3_000_000_000
        assert row["failure_code"] == "credit_amount_limit"
        assert not funding.credits.balances
        assert funding.store.delivery_health(int(time.time()))["review_required"] == 1


def test_negative_cache_recovers_even_under_continued_requests(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("lightning_router.rates.time.monotonic", lambda: clock[0])
    calls = []

    def fetch(_):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": {"base": "BTC", "currency": "USD", "amount": "100000"}})

    rates = Rates(httpx.Client(transport=httpx.MockTransport(fetch)))
    with pytest.raises(httpx.HTTPStatusError):
        rates.current()
    for tick in (101, 102, 104):
        clock[0] = tick
        with pytest.raises(QuoteUnavailable):
            rates.current()
    clock[0] = 105
    assert rates.current().usd_per_btc == 90000
    assert len(calls) == 2


def test_rate_fetch_does_not_queue_other_threads():
    started, finish = Event(), Event()

    def fetch(_):
        started.set()
        assert finish.wait(5)
        return httpx.Response(200, json={"data": {"base": "BTC", "currency": "USD", "amount": "100000"}})

    rates = Rates(httpx.Client(transport=httpx.MockTransport(fetch)))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(rates.current)
        try:
            assert started.wait(5)
            with pytest.raises(QuoteUnavailable):
                rates.current()
        finally:
            finish.set()
        assert future.result(timeout=5).usd_per_btc == 90000


def test_credential_pin_survives_empty_database_and_rejects_rotation(funding):
    from lightning_router.credentials import Credentials

    funding.store.pin_credentials(funding.credentials)
    funding.store.pin_credentials(funding.credentials)
    with pytest.raises(ValueError, match="Checkout secret changed"):
        funding.store.pin_credentials(Credentials(b"different-test-secret" * 3))


def test_first_pin_checks_existing_recovery_material(funding, raw_key):
    from lightning_router.credentials import Credentials

    funding.create(raw_key, uuid.uuid4().hex, 100, new=True)
    with pytest.raises(ValueError, match="Checkout secret changed"):
        funding.store.pin_credentials(Credentials(b"different-test-secret" * 3))
    funding.store.pin_credentials(funding.credentials)


def test_transient_delivery_failure_recovers_without_duplicate(funding, raw_key):
    view = funding.create(raw_key, uuid.uuid4().hex, 100, new=True)
    row = funding.store.invoice(view["id"], funding.credentials.fingerprint(raw_key))
    funding.lnd.pay(row["payment_hash"])
    funding.credits.fail_before_commit = True
    assert funding.reconcile()["failed"] == 1
    current = funding.store.invoice(row["id"], row["key_hash"])
    assert current["failure_code"] == "credit_unavailable"
    assert current["next_attempt_at"] == 0
    funding.credits.fail_before_commit = False
    assert funding.reconcile()["failed"] == 0
    assert funding.refresh(row)["credited"]
    assert len(funding.credits.payments) == 1
    assert funding.store.delivery_health(int(time.time())) == {
        "uncredited_count": 0, "oldest_uncredited_seconds": 0, "review_required": 0}


def test_upgrade_preserves_rows_and_removes_node_index_uniqueness(store, funding, raw_key):
    from lightning_router.store import checkouts, invoices, limits, metadata
    from sqlalchemy import MetaData, inspect

    # Build the actual previous table shape, then upgrade twice.
    metadata.drop_all(store.engine)
    old = MetaData()
    old_checkout = checkouts.to_metadata(old)
    old_checkout._columns.remove(old_checkout.c.created_at)
    legacy = invoices.to_metadata(old)
    for name in ("settled_at", "failure_code", "failure_since", "next_attempt_at"):
        legacy._columns.remove(legacy.c[name])
    legacy.indexes.clear()
    from sqlalchemy import UniqueConstraint
    legacy.append_constraint(UniqueConstraint("settle_index", name="old_settle_unique"))
    limits.to_metadata(old)
    old.create_all(store.engine)
    # Insert through the old schema so the new fields are genuinely absent.
    hashed = funding.credentials.fingerprint(raw_key)
    with store.transaction() as conn:
        conn.execute(old.tables["lr_checkouts"].insert().values(key_hash=hashed, pending_key=funding.credentials.seal_pending_key(raw_key)))
        conn.execute(legacy.insert().values(id="a" * 32, key_hash=hashed, request_id="b" * 32,
            payment_hash="c" * 64, created_at=1, expires_at=901, requested_msat=1000,
            usd_cents=1, usd_per_btc="100000", state="CANCELED"))
    store.migrate()
    store.migrate()
    assert store.invoice("a" * 32, hashed)["state"] == "CANCELED"
    assert store.invoice("a" * 32, hashed)["fx_margin_bps"] == 0
    assert store.checkout(hashed)["created_at"] >= int(time.time()) - 60
    assert all(c["column_names"] != ["settle_index"] for c in inspect(store.engine).get_unique_constraints("lr_invoices"))
    test_reused_node_settle_index_is_not_a_payment_identity(funding, raw_key)


def _process_observe(url, invoice_id, payment_hash, amount, now):
    from lightning_router.store import Store
    local = Store(url)
    try:
        local.observe(invoice_id, state="SETTLED", payment_hash=payment_hash, amount_msat=amount,
                      settle_index=1, now=now)
    finally:
        local.engine.dispose()


def test_two_process_settlement_records_one_receipt(funding, raw_key):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    from lightning_router.store import deposits
    from sqlalchemy import func

    view = funding.create(raw_key, uuid.uuid4().hex, 100, new=True)
    row = funding.store.invoice(view["id"], funding.credentials.fingerprint(raw_key))
    url = funding.store.engine.url.render_as_string(hide_password=False)
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = [pool.submit(_process_observe, url, row["id"], row["payment_hash"], row["requested_msat"], int(time.time())) for _ in range(8)]
        for future in futures:
            future.result(timeout=30)
    with funding.store.transaction() as conn:
        assert conn.execute(select(func.count()).select_from(deposits)).scalar_one() == 1
    assert funding.refresh(row)["credited"]
    assert len(funding.credits.payments) == 1
