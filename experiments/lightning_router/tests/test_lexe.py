import copy
import hashlib
import json
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import bolt11
import httpx
import pytest
from bolt11 import Bolt11, MilliSatoshi, TagChar, Tags
from lightning_router.errors import FundingReviewRequired
from lightning_router.lexe import PERMISSIONS, SCOPES, Lexe, satoshis
from lightning_router.store import Store, deposits, invoices
from sqlalchemy import select, update

WALLET = "a" * 64


class Node:
    """Real signed BOLT11s and the documented REST contract, no live funds."""
    def __init__(self):
        self.rows = {}
        self.preimages = {}
        self.lock = threading.Lock()
        self.creates = 0
        self.lose_create = False
        self.reject_create = False
        self.lose_cancel = False
        self.pay_on_cancel = False
        self.broken = False
        self.wallet_id = WALLET
        self.permissions = sorted(PERMISSIONS)
        self.expiry = int(time.time() * 1000) + 86400_000

    def handle(self, request):
        with self.lock:
            if self.broken:
                return httpx.Response(503)
            path = request.url.path
            if path == "/v2/node/client_info":
                return httpx.Response(200, json={"kind": "client_credentials", "scopes": sorted(SCOPES),
                                               "effective_permissions": self.permissions, "expires_at": self.expiry})
            if path == "/v2/node/node_info":
                return httpx.Response(200, json={"user_pk": self.wallet_id, "num_channels": 0})
            if path == "/v2/node/payment":
                data = self.rows.get(request.url.params["index"])
                return httpx.Response(200 if data else 404, json=data)
            if path == "/v2/node/updated_payments":
                assert request.url.params["limit"] == "100"
                return httpx.Response(200, json={"payments": list(self.rows.values()), "updated_index": None})
            body = json.loads(request.content)
            if path == "/v2/node/create_invoice":
                self.creates += 1
                if self.reject_create:
                    raise httpx.ReadTimeout("ambiguous failure before commit")
                created = int(time.time()) * 1000
                preimage = secrets.token_hex(32)
                payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
                index = f"{created:019d}-ln_{payment_hash}"
                tags = Tags()
                for tag, value in [(TagChar.payment_hash, payment_hash), (TagChar.payment_secret, "2" * 64),
                                   (TagChar.description, body["description"]), (TagChar.expire_time, body["expiration_secs"])]:
                    tags.add(tag, value)
                encoded = bolt11.encode(Bolt11("bc", created // 1000, tags, MilliSatoshi(satoshis(body["amount"]))), private_key="3" * 64)
                self.rows[index] = {"index": index, "hash": payment_hash, "invoice": encoded, "rail": "invoice", "kind": "invoice",
                                    "direction": "inbound", "status": "pending", "amount": body["amount"], "fees": "0",
                                    "created_at": created, "expires_at": created + body["expiration_secs"] * 1000,
                                    "personal_note": body["personal_note"]}
                self.preimages[index] = preimage
                if self.lose_create:
                    self.lose_create = False
                    raise httpx.ReadTimeout("lost committed response")
                return httpx.Response(200, json={"index": index})
            assert path == "/v2/node/cancel_payment"
            row = self.rows[body["index"]]
            if self.pay_on_cancel:
                self.pay(body["index"])
            elif row["status"] != "completed":
                row["status"] = "failed"
            if self.lose_cancel:
                raise httpx.ReadTimeout("lost cancel acknowledgement")
            return httpx.Response(200, json={})

    def pay(self, index, *, fee=123, extra=0):
        row = self.rows[index]
        gross = satoshis(row["amount"]) + extra
        row.update(status="completed", preimage=self.preimages[index], finalized_at=int(time.time() * 1000),
                   amount=str(Decimal(gross - fee) / 1000), fees=str(Decimal(fee) / 1000))


@pytest.fixture
def lexe(funding):
    node = Node()
    funding.lexe = Lexe(httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(node.handle)), WALLET)
    funding.new_invoice_backend = "lexe"
    return funding, node


def create(funding, key, request_id=None):
    result = funding.create(key, request_id or uuid.uuid4().hex, 100, new=True)
    return funding.store.invoice(result["id"], funding.credentials.fingerprint(key))


@pytest.mark.parametrize("backend", ["lnd", "lexe"])
@pytest.mark.parametrize("lost_ack", [False, True])
def test_credit_contract_is_identical_across_backends(lexe, raw_key, backend, lost_ack):
    funding, node = lexe
    funding.new_invoice_backend = backend
    row = create(funding, raw_key)
    assert not funding.credits.balances
    if backend == "lexe":
        node.pay(row["provider_index"])
    else:
        funding.lnd.pay(row["payment_hash"])
    funding.credits.fail_after_commit = lost_ack
    for _ in range(3):
        funding.reconcile()
    assert len(funding.credits.payments) == 1
    assert funding.account(raw_key)["balance_usd"] == "1.000000"
    with funding.store.transaction() as conn:
        receipt = conn.execute(select(deposits)).mappings().one()
        assert receipt["backend"] == backend
        assert receipt["provider_fee_msat"] == (123 if backend == "lexe" else 0)
        assert receipt["amount_msat"] == row["requested_msat"]


def test_committed_create_timeout_recovers_actual_identity(lexe, raw_key):
    funding, node = lexe
    node.lose_create = True
    request_id = uuid.uuid4().hex
    with pytest.raises(httpx.ReadTimeout):
        create(funding, raw_key, request_id)
    stored = funding.store.pending()[0]
    assert stored["payment_hash"] is None and stored["bolt11"] == ""
    assert not funding.credits.balances
    funding.rates.fail = True
    row = create(funding, raw_key, request_id)
    assert node.creates == 1
    assert row["payment_hash"] == next(iter(node.rows.values()))["hash"]
    assert row["provider_index"] == next(iter(node.rows))
    assert row["bolt11"]


def test_ambiguous_uncommitted_create_never_retries_or_provisions(lexe, raw_key):
    funding, node = lexe
    node.reject_create = True
    with pytest.raises(httpx.ReadTimeout):
        create(funding, raw_key)
    row = funding.store.pending()[0]
    with pytest.raises(RuntimeError, match="creation in progress"):
        funding._refresh(row, cancel=False)
    with funding.store.transaction() as conn:
        conn.execute(update(invoices).where(invoices.c.id == row["id"]).values(create_started_at=int(time.time()) - 61))
    row = funding.store.invoice(row["id"], row["key_hash"])
    for cancel in [False, True, True]:
        with pytest.raises(FundingReviewRequired, match="creation_ambiguous"):
            funding._refresh(row, cancel=cancel)
    assert node.creates == 1
    assert not funding.credits.balances
    assert funding.store.active(row["key_hash"]) == row["id"]


def test_creation_claim_is_durable_across_store_restart(lexe, raw_key):
    funding, node = lexe
    node.lose_create = True
    with pytest.raises(httpx.ReadTimeout):
        create(funding, raw_key)
    funding.store = Store(funding.store.engine.url)
    funding.store.pin_credentials(funding.credentials)
    funding.reconcile()
    assert node.creates == 1
    assert funding.store.pending()[0]["bolt11"]


def test_concurrent_creates_do_not_issue_duplicate_invoices(lexe, raw_key):
    funding, node = lexe
    request_id = uuid.uuid4().hex

    def run(_):
        try:
            return create(funding, raw_key, request_id)
        except (FundingReviewRequired, RuntimeError):
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(run, range(12)))
    assert node.creates == 1
    assert len({r["id"] for r in rows if r}) == 1
    assert len(node.rows) == 1


@pytest.mark.parametrize("race", [False, True])
@pytest.mark.parametrize("lost_ack", [False, True])
def test_cancel_and_payment_race(lexe, raw_key, race, lost_ack):
    funding, node = lexe
    row = create(funding, raw_key)
    node.pay_on_cancel, node.lose_cancel = race, lost_ack
    result = funding.refresh(row, cancel=True)
    assert result["state"] == ("SETTLED" if race else "CANCELED")
    assert result["credited"] is race
    assert len(funding.credits.payments) == int(race)


def test_mixed_backends_and_rollback_keep_original_routes(lexe, raw_key):
    funding, node = lexe
    other = "sk-tr-v1-" + secrets.token_urlsafe(32)
    funding.new_invoice_backend = "lnd"
    old = create(funding, raw_key)
    funding.new_invoice_backend = "lexe"
    new = create(funding, other)
    funding.new_invoice_backend = "lnd"
    funding.lnd.pay(old["payment_hash"])
    node.pay(new["provider_index"])
    assert funding.reconcile() == {"checked": 2, "failed": 0}
    assert len(funding.credits.payments) == 2
    assert funding.account(raw_key)["balance_usd"] == funding.account(other)["balance_usd"] == "1.000000"


def test_credit_outbox_recovers_even_if_lexe_is_down(lexe, raw_key):
    funding, node = lexe
    row = create(funding, raw_key)
    node.pay(row["provider_index"])
    funding.credits.fail_before_commit = True
    assert funding.reconcile()["failed"] == 1
    node.broken = True
    funding.credits.fail_before_commit = False
    assert funding.reconcile()["failed"] == 0
    assert funding.account(raw_key)["balance_usd"] == "1.000000"


@pytest.mark.parametrize("field,value", [
    ("hash", "f" * 64), ("direction", "outbound"), ("rail", "onchain"), ("kind", "offer"),
    ("personal_note", "another checkout"), ("index", "bad"), ("amount", "999"),
    ("fees", "0.0001"), ("status", "unknown"), ("created_at", 123), ("expires_at", 123),
    ("invoice", "lnbc100u1broken"), ("amount", 1000),
])
def test_invalid_receipt_never_credits(lexe, raw_key, field, value):
    funding, node = lexe
    row = create(funding, raw_key)
    node.rows[row["provider_index"]][field] = value
    with pytest.raises(FundingReviewRequired):
        funding.refresh(row)
    assert not funding.credits.balances and not funding.credits.payments


@pytest.mark.parametrize("field,value", [("preimage", "1" * 64), ("preimage", None), ("amount", "0"),
                                         ("amount", "999999999"), ("fees", "-1"), ("finalized_at", 1)])
def test_invalid_settlement_proof_fails_closed(lexe, raw_key, field, value):
    funding, node = lexe
    row = create(funding, raw_key)
    node.pay(row["provider_index"])
    node.rows[row["provider_index"]][field] = value
    with pytest.raises(FundingReviewRequired):
        funding.refresh(row)
    assert not funding.credits.balances


def test_wrong_wallet_or_privilege_or_expiry_fails_before_invoice(lexe, raw_key):
    funding, node = lexe
    funding.check_capacity = True
    for field, value in [("wallet_id", "b" * 64), ("permissions", [*PERMISSIONS, "pay_invoice"]), ("expiry", 1)]:
        previous = getattr(node, field)
        setattr(node, field, value)
        with pytest.raises(ValueError):
            create(funding, raw_key)
        setattr(node, field, previous)
    assert not node.rows and node.creates == 0


def test_jit_readiness_accepts_zero_channels(lexe):
    funding, _ = lexe
    assert funding.receiving_ready(100_000_000)


def test_duplicate_recovery_correlation_requires_review(lexe, raw_key):
    funding, node = lexe
    node.lose_create = True
    with pytest.raises(httpx.ReadTimeout):
        create(funding, raw_key)
    row = funding.store.pending()[0]
    funding.lexe.create(row)
    with pytest.raises(FundingReviewRequired, match="creation_ambiguous"):
        funding.lexe.recover(row)
    assert not funding.credits.balances


def test_fx_credit_is_preserved_and_fee_is_exact(lexe, raw_key):
    funding, node = lexe
    from lightning_router.rates import Rate
    funding.rates.current = lambda: Rate.from_spot(Decimal("76543.21"), int(time.time()))
    row = create(funding, raw_key)
    node.pay(row["provider_index"], fee=12345, extra=17)
    result = funding.refresh(row)
    expected = Rate(Decimal(row["usd_per_btc"]), row["created_at"]).credit_microdollars(row["requested_msat"] + 17)
    assert int(result["credit_microdollars"]) == expected
    assert result["fx_margin_bps"] == 1000
    saved = funding.store.invoice(row["id"], row["key_hash"])
    assert saved["provider_fee_msat"] == 12345


def test_settlement_fee_cannot_change_on_replay(lexe, raw_key):
    funding, node = lexe
    row = create(funding, raw_key)
    node.pay(row["provider_index"])
    funding.refresh(row)
    paid = funding.store.invoice(row["id"], row["key_hash"])
    with pytest.raises(ValueError, match="replay changed"):
        funding.store.observe(row["id"], state="SETTLED", payment_hash=row["payment_hash"], amount_msat=paid["amount_msat"],
                              settle_index=paid["settle_index"], now=int(time.time()), provider_fee_msat=124)


def test_deleted_provider_invoice_never_recreated(lexe, raw_key):
    funding, node = lexe
    row = create(funding, raw_key)
    node.rows.clear()
    with pytest.raises(FundingReviewRequired, match="invoice_missing"):
        funding.refresh(row)
    assert node.creates == 1


def test_stale_quote_never_creates_remote_invoice(lexe, raw_key):
    funding, node = lexe
    funding.store.prepare_checkout(funding.credentials.fingerprint(raw_key), funding.credentials.seal_pending_key(raw_key))
    row = funding.store.prepare(funding.credentials.fingerprint(raw_key), "a" * 32, "b" * 32, None, int(time.time()) - 1000,
                                requested_msat=1000000, usd_cents=100, usd_per_btc="100000", backend="lexe", wallet_id=WALLET)
    assert funding.refresh(row)["state"] == "CANCELED"
    assert node.creates == 0


def test_changed_provider_binding_refused(lexe, raw_key):
    funding, _ = lexe
    row = create(funding, raw_key)
    with pytest.raises(ValueError):
        funding.store.bind_provider_invoice(row["id"], "b" * 64, row["payment_hash"], row["provider_index"])


@pytest.mark.parametrize("value", [True, 1, "NaN", "-1", "0.0001", "1e3", " 1", "99999999999999999999"])
def test_exact_amount_parser_rejects_ambiguous_values(value):
    with pytest.raises(ValueError):
        satoshis(value)


def test_unknown_backend_and_wallet_fail_closed(lexe, raw_key):
    funding, _ = lexe
    row = create(funding, raw_key)
    with funding.store.transaction() as conn:
        conn.execute(update(invoices).where(invoices.c.id == row["id"]).values(backend="future"))
    with pytest.raises(FundingReviewRequired, match="unknown_backend"):
        funding.refresh(row)


def test_recovery_pagination_is_bounded(lexe, raw_key, monkeypatch):
    funding, node = lexe
    row = create(funding, raw_key)
    unrelated = copy.deepcopy(node.rows[row["provider_index"]])
    unrelated["personal_note"] = "not this checkout"
    count = 0

    def page(*args, **kwargs):
        nonlocal count
        count += 1
        return {"payments": [unrelated] * 100, "updated_index": f"u{count:019d}-ln_{'0' * 64}"}

    monkeypatch.setattr(funding.lexe, "request", page)
    with pytest.raises(FundingReviewRequired, match="creation_recovery_limit"):
        funding.lexe.recover(row)
    assert count == 10
