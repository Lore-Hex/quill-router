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
from lightning_router.money import (
    MAX_MICRODOLLARS,
    MAX_MSATS,
    MSATS_PER_BTC,
    btc,
    microdollars,
    msats,
    usd,
)
from lightning_router.rates import Rate
from lightning_router.store import deposits, invoices
from sqlalchemy import select, update


def create(funding, key, cents=1000, new=True, request_id=None):
    return funding.create(key, request_id or uuid.uuid4().hex, cents, new=new)


def row(funding, invoice, key):
    return funding.store.invoice(invoice["id"], funding.credentials.fingerprint(key))


def test_unpaid_checkout_does_not_provision_a_user_or_key(funding, raw_key):
    invoice = create(funding, raw_key)
    assert funding.credits.balances == {}
    assert funding.credits.payments == {}
    assert invoice["account_created"] is False
    with pytest.raises(KeyError):
        funding.account(raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.pay(record["payment_hash"])
    result = funding.refresh(record)
    assert result["account_created"] is True
    assert result["credited"] is True
    assert len(funding.credits.balances) == 1


def test_pending_recovery_key_is_encrypted_and_removed_after_binding(funding, raw_key):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    checkout = funding.store.checkout(record["key_hash"])
    assert checkout["credit_account_id"] is None
    assert raw_key.encode() not in checkout["pending_key"]
    assert funding.credentials.open_pending_key(checkout["pending_key"], record["key_hash"]) == raw_key
    funding.lnd.pay(record["payment_hash"])
    funding.reconcile()
    checkout = funding.store.checkout(record["key_hash"])
    assert checkout["credit_account_id"] is not None
    assert checkout["pending_key"] is None


@pytest.mark.parametrize("tamper", ["owner", "ciphertext", "secret"])
def test_pending_key_cannot_be_rebound_or_decrypted_with_other_secret(raw_key, tamper):
    from cryptography.exceptions import InvalidTag

    credentials = Credentials(b"a" * 32)
    owner = credentials.fingerprint(raw_key)
    sealed = credentials.seal_pending_key(raw_key)
    assert sealed != credentials.seal_pending_key(raw_key)
    if tamper == "owner":
        owner = "0" * 64
    elif tamper == "ciphertext":
        sealed = sealed[:-1] + bytes([sealed[-1] ^ 1])
    else:
        credentials = Credentials(b"b" * 32)
    with pytest.raises(InvalidTag):
        credentials.open_pending_key(sealed, owner)


def test_identity_ack_loss_replays_paid_checkout_without_an_extra_account(funding, raw_key, monkeypatch):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    original = funding.credits.resolve
    calls = []

    def resolve(raw, *, new):
        assert funding.store.invoice(record["id"], record["key_hash"])["state"] == "SETTLED"
        result = original(raw, new=new)
        calls.append(new)
        if len(calls) == 1:
            raise TimeoutError("lost identity acknowledgement")
        return result

    monkeypatch.setattr(funding.credits, "resolve", resolve)
    funding.lnd.pay(record["payment_hash"])
    assert funding.reconcile()["failed"] == 1
    assert funding.store.credit_account(record["key_hash"]) is None
    assert len(funding.credits.balances) == 1
    assert funding.credits.payments == {}
    assert funding.reconcile()["failed"] == 0
    assert calls == [True, True]
    assert len(funding.credits.balances) == len(funding.credits.payments) == 1
    assert funding.balance(record["key_hash"])["balance_usd"] == "10.000000"


def test_identity_backend_outage_never_turns_unpaid_invoice_into_account(funding, raw_key, monkeypatch):
    original = funding.credits.resolve

    def unavailable(*args, **kwargs):
        raise TimeoutError("identity unavailable")

    monkeypatch.setattr(funding.credits, "resolve", unavailable)
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    assert funding.reconcile()["failed"] == 0
    assert funding.credits.balances == {}
    funding.lnd.pay(record["payment_hash"])
    assert funding.reconcile()["failed"] == 1
    assert funding.credits.balances == {}
    assert funding.store.pending()[0]["state"] == "SETTLED"
    monkeypatch.setattr(funding.credits, "resolve", original)
    assert funding.reconcile()["failed"] == 0
    assert funding.account(raw_key)["balance_usd"] == "10.000000"


def test_rate_failure_does_not_create_account_or_invoice(funding, raw_key):
    funding.rates.fail = True
    with pytest.raises(RuntimeError):
        create(funding, raw_key)
    assert funding.credits.balances == {}
    assert funding.store.pending() == []
    assert funding.lnd.creates == 0


def test_payment_creates_balance_exactly_once(funding, raw_key):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    assert invoice["balance_microdollars"] == "0"
    funding.lnd.pay(record["payment_hash"], 10_000_123)
    for _ in range(3):
        result = funding.refresh(record)
        assert result["balance_microdollars"] == "10000123"
        assert result["balance_usd"] == "10.000123"
        assert result["credited"] is True
        assert "balance_btc" not in result
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
    assert funding.refresh(record)["balance_microdollars"] == "15000000"


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
    assert result["balance_microdollars"] == "10000000"


def test_cancellation_cannot_be_rebound_to_other_key(funding, raw_key):
    first = create(funding, raw_key)
    other = "sk-tr-v1-" + secrets.token_urlsafe(32)
    with pytest.raises(KeyError):
        funding.store.invoice(first["id"], funding.credentials.fingerprint(other))
    result = funding.refresh(row(funding, first, raw_key), cancel=True)
    assert result["balance_microdollars"] == "0"
    assert result["state"] == "CANCELED"


def test_background_worker_credits_after_browser_is_closed(funding, raw_key):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.pay(record["payment_hash"])
    assert funding.reconcile() == {"checked": 1, "failed": 0}
    assert funding.reconcile() == {"checked": 0, "failed": 0}
    assert funding.balance(record["key_hash"])["balance_microdollars"] == "10000000"


def test_concurrent_settlement_is_one_deposit(funding, raw_key):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.pay(record["payment_hash"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: funding.refresh(record), range(16)))
    assert funding.balance(record["key_hash"])["balance_microdollars"] == "10000000"


@pytest.mark.parametrize("state", ["OPEN", "ACCEPTED", "CANCELED"])
def test_unsettled_payment_is_not_money(funding, raw_key, state):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.rows[record["payment_hash"]] = replace(funding.lnd.rows[record["payment_hash"]],
                                                    state=state, amount_msat=10_000_000)
    assert funding.refresh(record)["balance_microdollars"] == "0"
    assert funding.credits.balances == {}


@pytest.mark.parametrize("field,value", [("payment_hash", "e" * 64), ("amount_msat", 1), ("settle_index", 0), ("requested_msat", 1)])
def test_corrupt_settlement_never_credits(funding, raw_key, field, value):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.pay(record["payment_hash"])
    funding.lnd.rows[record["payment_hash"]] = replace(funding.lnd.rows[record["payment_hash"]], **{field: value})
    with pytest.raises(ValueError):
        funding.refresh(record)
    assert funding.balance(record["key_hash"])["balance_microdollars"] == "0"


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


@pytest.mark.parametrize("raw", ["sk-lr-v1-" + "a" * 43, "x", "sk-tr-v1-" + "a" * 42, "sk-tr-v1-" + "a" * 43 + "\n"])
def test_other_key_namespaces_rejected(raw):
    with pytest.raises(ValueError):
        Credentials(b"x" * 32).fingerprint(raw)


def test_conversion_is_frozen_once_not_per_inference_or_balance_read(funding, raw_key):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.rates.price = Decimal("200000")
    funding.rates.fail = True
    funding.lnd.pay(record["payment_hash"])
    result = funding.refresh(record)
    assert result["credit_microdollars"] == "10000000"
    assert result["balance_usd"] == "10.000000"
    account_id = funding.store.credit_account(record["key_hash"])
    # An ordinary USD inference charge changes the canonical balance. No local
    # BTC balance is revalued, nor is a copy of the USD balance maintained here.
    funding.credits.balances[account_id] -= 123
    assert funding.account(raw_key)["balance_usd"] == "9.999877"


def test_credit_outage_keeps_settled_payment_for_background_delivery(funding, raw_key):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.pay(record["payment_hash"])
    funding.credits.fail_before_commit = True
    assert funding.reconcile() == {"checked": 0, "failed": 1}
    pending = funding.store.pending()
    assert len(pending) == 1
    assert pending[0]["state"] == "SETTLED"
    assert pending[0]["credit_microdollars"] == 10_000_000
    assert pending[0]["credited_at"] is None
    assert funding.account(raw_key)["balance_microdollars"] == "0"
    funding.credits.fail_before_commit = False
    funding.rates.fail = True
    assert funding.reconcile() == {"checked": 1, "failed": 0}
    assert funding.store.pending() == []
    assert funding.account(raw_key)["balance_usd"] == "10.000000"


def test_lost_credit_acknowledgement_does_not_double_fund(funding, raw_key):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.pay(record["payment_hash"])
    funding.credits.fail_after_commit = True
    assert funding.reconcile()["failed"] == 1
    assert funding.account(raw_key)["balance_microdollars"] == "10000000"
    assert len(funding.store.pending()) == 1
    assert funding.reconcile()["failed"] == 0
    assert funding.account(raw_key)["balance_microdollars"] == "10000000"
    assert len(funding.credits.payments) == 1


def test_crash_after_usd_credit_before_local_ack_is_recoverable(funding, raw_key, monkeypatch):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.pay(record["payment_hash"])
    original = funding.store.mark_credited
    def crash(*args):
        raise RuntimeError("local database unavailable")
    monkeypatch.setattr(funding.store, "mark_credited", crash)
    assert funding.reconcile()["failed"] == 1
    assert funding.account(raw_key)["balance_usd"] == "10.000000"
    monkeypatch.setattr(funding.store, "mark_credited", original)
    assert funding.reconcile()["failed"] == 0
    assert funding.account(raw_key)["balance_usd"] == "10.000000"


def test_existing_tr_key_does_not_need_previous_lightning_deposit(funding, raw_key):
    account_id = funding.credits.resolve(raw_key, new=True)
    funding.credits.balances[account_id] = 321
    assert funding.account(raw_key)["balance_usd"] == "0.000321"
    invoice = create(funding, raw_key, new=False)
    assert invoice["balance_microdollars"] == "321"


def test_revoked_key_is_not_authenticated_by_local_invoice_metadata(funding, raw_key):
    invoice = create(funding, raw_key)
    funding.lnd.pay(row(funding, invoice, raw_key)["payment_hash"])
    funding.refresh(row(funding, invoice, raw_key))
    account_id = funding.credits.resolve(raw_key, new=False)
    funding.credits.revoked.add(account_id)
    with pytest.raises(KeyError):
        funding.account(raw_key)
    with pytest.raises(KeyError):
        create(funding, raw_key, new=True)


def test_credit_account_binding_cannot_change(funding, raw_key):
    invoice = create(funding, raw_key)
    funding.lnd.pay(row(funding, invoice, raw_key)["payment_hash"])
    funding.refresh(row(funding, invoice, raw_key))
    with pytest.raises(ValueError, match="cannot change"):
        funding.store.bind_account(funding.credentials.fingerprint(raw_key), "another-workspace")


@pytest.mark.parametrize("bad", [True, 1.1, "1.5", "-1", "1e9", "NaN", "Infinity", "１２", -1, MAX_MICRODOLLARS + 1])
def test_usd_money_rejects_nonintegers(bad):
    with pytest.raises(ValueError):
        microdollars(bad)


@given(st.integers(min_value=0, max_value=MAX_MICRODOLLARS))
def test_usd_display_preserves_microdollars(amount):
    assert Decimal(usd(amount)) * 1_000_000 == amount


@given(st.integers(1, 100_000), st.integers(1, 100_000_000))
def test_whole_sat_invoice_never_credits_less_than_usd_target(cents, dollars):
    rate = Rate(Decimal(dollars), 0)
    assert rate.credit_microdollars(rate.invoice_msats(cents)) >= cents * 10_000


def test_submicrodollar_rounding_happens_once():
    rate = Rate(Decimal("98765.4321"), 0)
    assert rate.credit_microdollars(1000) == 987
    assert rate.credit_microdollars(2000) == 1975


def test_expired_unissued_quote_is_canceled_without_creating_invoice(funding, raw_key, monkeypatch):
    original = funding.lnd.ensure
    def unavailable(*args, **kwargs):
        raise TimeoutError("before LND create")
    monkeypatch.setattr(funding.lnd, "ensure", unavailable)
    with pytest.raises(TimeoutError):
        create(funding, raw_key)
    pending = funding.store.pending()[0]
    with funding.store.transaction() as conn:
        conn.execute(update(invoices).where(invoices.c.id == pending["id"]).values(expires_at=1))
    monkeypatch.setattr(funding.lnd, "ensure", original)
    assert funding.refresh(pending)["state"] == "CANCELED"
    assert funding.lnd.creates == 0
    assert funding.store.pending() == []
    assert funding.credits.balances == {}
    with pytest.raises(KeyError):
        funding.account(raw_key)


def test_delayed_lnd_creation_does_not_extend_fixed_fx_quote(funding, raw_key):
    invoice = create(funding, raw_key)
    record = row(funding, invoice, raw_key)
    funding.lnd.rows[record["payment_hash"]] = replace(funding.lnd.rows[record["payment_hash"]],
                                                     expires_at=record["expires_at"] + 3600)
    assert funding.refresh(record)["state"] == "CANCELED"
    assert funding.credits.balances == {}
    with pytest.raises(KeyError):
        funding.account(raw_key)
