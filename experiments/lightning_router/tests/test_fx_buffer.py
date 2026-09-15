import uuid
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from lightning_router.app import create_app
from lightning_router.rates import Rate, Rates
from sqlalchemy import select


def quote_source(spot="100000"):
    return Rates(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"data": {"base": "BTC", "currency": "USD", "amount": spot}},
    ))))


def test_live_quote_reserves_ten_percent_of_spot_value():
    rate = quote_source().current()
    assert rate.usd_per_btc == Decimal("90000")
    assert rate.fx_margin_bps == 1000
    assert rate.spot_usd_per_btc == Decimal("100000")
    assert rate.invoice_msats(1000) == 11_112_000
    assert rate.credit_microdollars(11_112_000) == 10_000_800


@given(cents=st.integers(1, 100_000), spot=st.integers(1000, 1_000_000))
def test_margin_rounding_never_underfunds_the_requested_credits(cents, spot):
    rate = Rate.from_spot(Decimal(spot), 0)
    amount = rate.invoice_msats(cents)
    credit = rate.credit_microdollars(amount)
    gross = Decimal(amount) * spot / 100_000
    assert amount % 1000 == 0
    assert credit >= cents * 10_000
    assert Decimal(credit) <= gross * Decimal("0.9")
    assert gross * Decimal("0.9") - credit < 1


@pytest.mark.parametrize("bps", [True, 1.5, -1, 10_000, "1000"])
def test_invalid_margin_is_rejected(bps):
    with pytest.raises(ValueError, match="margin"):
        Rate.from_spot(Decimal("100000"), 0, fx_margin_bps=bps)


def test_fee_applies_once_and_retries_do_not_reprice(funding, raw_key):
    funding.rates = quote_source()
    request_id = uuid.uuid4().hex
    invoice = funding.create(raw_key, request_id, 1000, new=True)
    record = funding.store.invoice(invoice["id"], funding.credentials.fingerprint(raw_key))
    assert record["fx_margin_bps"] == 1000
    assert Decimal(record["usd_per_btc"]) == Decimal("90000")
    # Older workers settle using only this stored rate, so rollback is safe.
    old_worker_rate = Rate(Decimal(record["usd_per_btc"]), record["created_at"])
    funding.rates = quote_source("200000")
    repeated = funding.create(raw_key, request_id, 1000, new=True)
    assert repeated["requested_msat"] == invoice["requested_msat"]
    assert repeated["invoice_spot_usd"] == "11.112000"
    funding.lnd.pay(record["payment_hash"], 20_000_000)
    funding.credits.fail_after_commit = True
    assert funding.reconcile()["failed"] == 1
    assert funding.reconcile()["failed"] == 0
    result = funding.refresh(record)
    assert result["balance_microdollars"] == "18000000"
    assert old_worker_rate.credit_microdollars(20_000_000) == 18_000_000
    assert len(funding.credits.payments) == 1


def test_legacy_invoice_still_gets_full_value_after_fee_launch(funding, raw_key):
    request_id = uuid.uuid4().hex
    invoice = funding.create(raw_key, request_id, 1000, new=True)
    record = funding.store.invoice(invoice["id"], funding.credentials.fingerprint(raw_key))
    funding.rates = quote_source()
    repeated = funding.create(raw_key, request_id, 1000, new=True)
    assert repeated["fx_margin_bps"] == 0
    assert repeated["requested_msat"] == "10000000"
    funding.lnd.pay(record["payment_hash"])
    assert funding.refresh(record)["balance_microdollars"] == "10000000"


def test_additive_migration_preserves_legacy_invoice_and_is_repeatable(funding, raw_key):
    from lightning_router.store import invoices

    invoice = funding.create(raw_key, uuid.uuid4().hex, 1000, new=True)
    with funding.store.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE lr_invoices DROP COLUMN fx_margin_bps")
    funding.store.migrate()
    funding.store.migrate()
    with funding.store.engine.begin() as connection:
        restored = dict(connection.execute(select(invoices)).mappings().one())
    assert restored["fx_margin_bps"] == 0
    assert restored["requested_msat"] == 10_000_000
    assert restored["id"] == invoice["id"]
    funding.rates = quote_source()
    funding.lnd.pay(restored["payment_hash"])
    assert funding.refresh(restored)["balance_microdollars"] == "10000000"


def test_quote_and_invoice_disclose_identical_buffer(funding, raw_key):
    funding.rates = quote_source()
    with TestClient(create_app(funding, rates=funding.rates, network="regtest", start_worker=False)) as client:
        quote = client.get("/api/quote?usd_cents=1000").json()
        response = client.post("/api/invoices", headers={"Authorization": "Bearer " + raw_key,
                               "Idempotency-Key": uuid.uuid4().hex}, json={"new_account": True, "usd_cents": 1000})
        assert response.status_code == 200
        invoice = response.json()
        for name in ("fx_margin_bps", "spot_usd_per_btc", "usd_per_btc", "invoice_spot_usd"):
            assert quote[name] == invoice[name]
        assert invoice["fx_margin_bps"] == 1000
        assert invoice["invoice_spot_usd"] == "11.112000"
        assert invoice["usd_amount"] == "10.00"
        assert invoice["account_created"] is False
        assert raw_key not in response.text


def test_stale_coinbase_rate_is_not_reused_when_refresh_fails(monkeypatch):
    now = [1000]
    monkeypatch.setattr("lightning_router.rates.time.time", lambda: now[0])
    calls = []

    def handler(_):
        calls.append(1)
        if len(calls) > 1:
            raise httpx.ConnectError("quote unavailable")
        return httpx.Response(200, json={"data": {"base": "BTC", "currency": "USD", "amount": "100000"}})

    rates = Rates(httpx.Client(transport=httpx.MockTransport(handler)))
    first = rates.current()
    now[0] += 59
    assert rates.current() is first
    now[0] += 1
    with pytest.raises(httpx.ConnectError):
        rates.current()
