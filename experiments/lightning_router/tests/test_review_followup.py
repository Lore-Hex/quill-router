import time
import uuid
from decimal import Decimal

import httpx
from lightning_router.rates import Rate, Rates
from lightning_router.store import checkouts, invoices
from sqlalchemy import update


def test_fresh_quote_cache_does_not_need_fetch_lock():
    rates = Rates(httpx.Client())
    rates._rate = Rate.from_spot(Decimal("100000"), int(time.time()))
    with rates._lock:
        assert rates.current() == rates._rate


def test_resumed_unfunded_checkout_survives_prune_during_quote_fetch(funding, raw_key, monkeypatch):
    hashed = funding.credentials.fingerprint(raw_key)
    funding.store.prepare_checkout(hashed, funding.credentials.seal_pending_key(raw_key))
    with funding.store.transaction() as conn:
        conn.execute(update(checkouts).where(checkouts.c.key_hash == hashed).values(created_at=0))

    def fetch():
        funding.store.prune(int(time.time()))
        return Rate(Decimal("100000"), int(time.time()))

    monkeypatch.setattr(funding.rates, "current", fetch)
    view = funding.create(raw_key, uuid.uuid4().hex, 100, new=True)
    assert view["state"] == "OPEN"
    row = funding.store.invoice(view["id"], hashed)
    funding.lnd.pay(row["payment_hash"])
    assert funding.refresh(row)["credited"]


def test_cancel_attempt_is_not_delayed_by_review_backoff(funding, raw_key):
    view = funding.create(raw_key, uuid.uuid4().hex, 100, new=True)
    row = funding.store.invoice(view["id"], funding.credentials.fingerprint(raw_key))
    funding.store.failed(row["id"], "invoice_invalid", int(time.time()), review=True)
    assert funding.refresh(row, cancel=True)["state"] == "CANCELED"


def test_unknown_checkout_age_from_old_worker_is_not_expired(funding, raw_key):
    hashed = funding.credentials.fingerprint(raw_key)
    funding.store.prepare_checkout(hashed, funding.credentials.seal_pending_key(raw_key))
    with funding.store.transaction() as conn:
        conn.execute(update(checkouts).where(checkouts.c.key_hash == hashed).values(created_at=0))
    funding.store.prune(int(time.time()))
    assert funding.store.checkout(hashed)["pending_key"] is not None


def test_stalled_delivery_has_stable_text_alert_trigger(funding, raw_key, caplog):
    view = funding.create(raw_key, uuid.uuid4().hex, 100, new=True)
    row = funding.store.invoice(view["id"], funding.credentials.fingerprint(raw_key))
    funding.lnd.pay(row["payment_hash"])
    funding.credits.fail_before_commit = True
    funding.reconcile()
    with funding.store.transaction() as conn:
        conn.execute(update(invoices).where(invoices.c.id == row["id"]).values(settled_at=int(time.time()) - 121))
    funding._last_health_log = 0
    funding.reconcile()
    assert "lightning.funding_stalled" in caplog.text
    assert raw_key not in caplog.text
    assert row["payment_hash"] not in caplog.text
