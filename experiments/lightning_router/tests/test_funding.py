import hashlib
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import ROUND_CEILING, Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from lightning_router.credentials import Credentials
from lightning_router.money import MAX_MSATS, MSATS_PER_BTC, btc, msats
from lightning_router.rates import Rate
from lightning_router.store import deposits
from sqlalchemy import select


def create(funding, key, cents=1000, new=True, request_id=None):
    return funding.create(key, request_id or uuid.uuid4().hex, cents, new=new)


def row(funding, invoice, key):
    return funding.store.invoice(invoice["id"], funding.credentials.fingerprint(key))


def test_payment_creates_balance_exactly_once(funding, raw_key):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    assert invoice["balance_msat"] == "0"
    funding.lnd.pay(record["payment_hash"], 10_000_123)
    for _ in range(3):
        result = funding.refresh(record)
        assert result["balance_msat"] == "10000123"
        assert result["balance_btc"] == "0.00010000123"
    with funding.store.transaction() as conn:
        assert len(conn.execute(select(deposits)).all()) == 1


def test_existing_key_topup_is_same_account(funding, raw_key):
    first = create(funding, raw_key)
    record = row(funding, first, raw_key)
    funding.lnd.pay(record["payment_hash"])
    funding.refresh(record)
    second = create(funding, raw_key, cents=500, new=False)
    record = row(funding, second, raw_key)
    funding.lnd.pay(record["payment_hash"])
    assert funding.refresh(record)["balance_msat"] == "15000000"


def test_unknown_existing_key_does_not_create_account(funding, raw_key):
    with pytest.raises(KeyError):
        create(funding, raw_key, new=False)
    assert funding.lnd.creates == 0


def test_lost_creation_response_recovers_same_invoice_without_new_rate(funding, raw_key):
    request_id = uuid.uuid4().hex
    funding.lnd.fail_after_create = True
    with pytest.raises(TimeoutError):
        create(funding, raw_key, request_id=request_id)
    funding.rates.fail = True
    invoice = create(funding, raw_key, request_id=request_id)
    assert funding.lnd.creates == 1
    assert invoice["state"] == "OPEN"


def test_replay_changed_usd_amount_rejected(funding, raw_key):
    request_id = uuid.uuid4().hex
    create(funding, raw_key, request_id=request_id)
    with pytest.raises(ValueError, match="different amount"):
        create(funding, raw_key, cents=500, request_id=request_id)


def test_second_open_invoice_is_rejected(funding, raw_key):
    create(funding, raw_key)
    with pytest.raises(ValueError, match="existing invoice"):
        create(funding, raw_key)
    assert funding.lnd.creates == 1


def test_cancel_payment_race_credits_original_key(funding, raw_key):
    first = create(funding, raw_key)
    record = row(funding, first, raw_key)
    funding.lnd.pay_during_cancel = True
    result = funding.refresh(record, cancel=True)
    assert result["state"] == "SETTLED"
    assert result["balance_msat"] == "10000000"


def test_cancellation_cannot_be_rebound_to_other_key(funding, raw_key):
    first = create(funding, raw_key)
    other = "sk-lr-v1-" + secrets.token_urlsafe(32)
    with pytest.raises(KeyError):
        funding.store.invoice(first["id"], funding.credentials.fingerprint(other))
    result = funding.refresh(row(funding, first, raw_key), cancel=True)
    assert result["balance_msat"] == "0"
    assert result["state"] == "CANCELED"


def test_background_worker_credits_after_browser_is_closed(funding, raw_key):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.pay(record["payment_hash"])
    assert funding.reconcile() == {"checked": 1, "failed": 0}
    assert funding.reconcile() == {"checked": 0, "failed": 0}
    assert funding.store.balance(record["key_hash"])["balance_msat"] == "10000000"


def test_concurrent_settlement_is_one_deposit(funding, raw_key):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.pay(record["payment_hash"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: funding.refresh(record), range(16)))
    assert funding.store.balance(record["key_hash"])["balance_msat"] == "10000000"


@pytest.mark.parametrize("state", ["OPEN", "ACCEPTED", "CANCELED"])
def test_unsettled_payment_is_not_money(funding, raw_key, state):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.rows[record["payment_hash"]] = replace(funding.lnd.rows[record["payment_hash"]],
                                                    state=state, amount_msat=10_000_000)
    assert funding.refresh(record)["balance_msat"] == "0"


@pytest.mark.parametrize("field,value", [("payment_hash", "e" * 64), ("amount_msat", 1), ("settle_index", 0), ("requested_msat", 1)])
def test_corrupt_settlement_never_credits(funding, raw_key, field, value):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.pay(record["payment_hash"])
    funding.lnd.rows[record["payment_hash"]] = replace(funding.lnd.rows[record["payment_hash"]], **{field: value})
    with pytest.raises(ValueError):
        funding.refresh(record)
    assert funding.store.balance(record["key_hash"])["balance_msat"] == "0"


def test_money_and_public_payload_are_key_free(funding, raw_key):
    invoice = create(funding, raw_key)
    assert raw_key not in str(invoice)
    assert not {"key_hash", "payment_hash", "preimage", "workspace_id", "email"} & invoice.keys()
    record = row(funding, invoice, raw_key)
    assert raw_key not in str(record)
    assert record["key_hash"] != hashlib.sha256(raw_key.encode()).hexdigest()


def test_reconciliation_failure_is_redacted_and_keeps_pending(funding, raw_key, caplog):
    create(funding, raw_key)
    funding.lnd.fail_after_create = True
    assert funding.reconcile()["failed"] == 1
    assert raw_key not in caplog.text
    assert "lost LND response" not in caplog.text
    assert "TimeoutError" in caplog.text
    assert len(funding.store.pending()) == 1


@pytest.mark.parametrize("bad", [True, 1.1, "1.5", "-1", "1e9", "NaN", "Infinity", "１２", -1, MAX_MSATS + 1])
def test_reject_noninteger_money(bad):
    with pytest.raises(ValueError):
        msats(bad)


@given(st.integers(min_value=0, max_value=MAX_MSATS))
def test_btc_display_roundtrips_exactly(amount):
    assert Decimal(btc(amount)) * MSATS_PER_BTC == amount


@given(st.integers(1, 100_000), st.integers(1, 100_000_000))
def test_usd_conversion_is_exact_whole_sat_ceiling(cents, dollars):
    result = Rate(Decimal(dollars), 0).invoice_msats(cents)
    expected = int((Decimal(cents) * 1_000_000 / dollars).to_integral_value(rounding=ROUND_CEILING)) * 1000
    assert result == expected
    assert result % 1000 == 0


@pytest.mark.parametrize("raw", ["sk-tr-v1-" + "a" * 43, "x", "sk-lr-v1-" + "a" * 42, "sk-lr-v1-" + "a" * 43 + "\n"])
def test_other_key_namespaces_rejected(raw):
    with pytest.raises(ValueError):
        Credentials(b"x" * 32).fingerprint(raw)
