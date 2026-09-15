import hashlib
import os
import secrets
import threading
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from lightning_router.credentials import Credentials
from lightning_router.lnd import Invoice, Lnd
from lightning_router.money import MAX_CREDIT_RECEIPT, microdollars
from lightning_router.rates import Rate, Rates
from lightning_router.service import Funding
from lightning_router.store import Store, metadata


class FakeRates(Rates):
    def __init__(self) -> None:
        self.fail = False
        self.price = Decimal("100000")

    def current(self) -> Rate:
        if self.fail:
            raise RuntimeError("rate unavailable")
        return Rate(self.price, int(time.time()))


class FakeCredits:
    """Test-only stand-in for the canonical USD ledger. Never deployed."""
    def __init__(self):
        self.balances = {}
        self.payments = {}
        self.revoked = set()
        self.fail_before_commit = False
        self.fail_after_commit = False
        self.lock = threading.Lock()

    def resolve(self, raw_key, *, new):
        account_id = hashlib.sha256(raw_key.encode()).hexdigest()
        with self.lock:
            if account_id in self.revoked:
                raise KeyError("Revoked key")
            if new:
                self.balances.setdefault(account_id, 0)
            if account_id not in self.balances:
                raise KeyError("Unknown key")
        return account_id

    def balance(self, account_id):
        with self.lock:
            return self.balances[account_id]

    def usage(self, raw_key):
        self.resolve(raw_key, new=False)
        return {"usage_usd": "1.234567", "byok_usage_usd": "0.000000", "reserved_usd": "0.000000",
                "limit_usd": None, "limit_remaining_usd": None}

    def credit(self, account_id, payment_hash, amount_microdollars):
        with self.lock:
            if self.fail_before_commit:
                raise TimeoutError("USD ledger unavailable")
            amount = microdollars(amount_microdollars)
            if not 0 < amount <= MAX_CREDIT_RECEIPT:
                raise ValueError("Funding credit amount rejected")
            previous = self.payments.get(payment_hash)
            if previous and previous != (account_id, amount):
                raise ValueError("Payment binding changed")
            if previous is None:
                self.balances[account_id] = microdollars(self.balances[account_id] + amount)
                self.payments[payment_hash] = (account_id, amount)
            if self.fail_after_commit:
                self.fail_after_commit = False
                raise TimeoutError("Lost USD commit response")


class FakeLnd(Lnd):
    def __init__(self) -> None:
        self.rows: dict[str, Invoice] = {}
        self.creates = 0
        self.fail_after_create = False
        self.pay_during_cancel = False

    def lookup(self, payment_hash: str) -> Invoice | None:
        if self.fail_after_create:
            self.fail_after_create = False
            raise TimeoutError("lost LND response")
        return self.rows.get(payment_hash)

    def ensure(self, preimage: bytes, amount_msat: int, *, expires_at: int) -> Invoice | None:
        payment_hash = hashlib.sha256(preimage).hexdigest()
        if payment_hash not in self.rows:
            if expires_at <= int(time.time()):
                return None
            self.creates += 1
            self.rows[payment_hash] = Invoice(
                payment_hash, "lnbcrt100u1" + "q" * 180, "OPEN", 0, 0,
                expires_at, amount_msat,
            )
        if self.fail_after_create:
            self.fail_after_create = False
            raise TimeoutError("lost LND response")
        return self.rows[payment_hash]

    def cancel(self, payment_hash: str) -> Invoice:
        if self.pay_during_cancel:
            self.pay(payment_hash)
        elif self.rows[payment_hash].state != "SETTLED":
            self.rows[payment_hash] = replace(self.rows[payment_hash], state="CANCELED")
        return self.rows[payment_hash]

    def pay(self, payment_hash: str, amount: int | None = None) -> None:
        row = self.rows[payment_hash]
        self.rows[payment_hash] = replace(row, state="SETTLED", amount_msat=amount or row.requested_msat,
                                         settle_index=len([r for r in self.rows.values() if r.state == "SETTLED"]) + 1)


@pytest.fixture(params=["sqlite", "postgres"] if os.environ.get("LR_TEST_POSTGRES_URL") else ["sqlite"])
def store(tmp_path: Path, request):
    url = f"sqlite:///{tmp_path / 'ledger.db'}"
    if request.param == "postgres":
        url = os.environ["LR_TEST_POSTGRES_URL"]
        parsed = urlsplit(url)
        if parsed.hostname != "127.0.0.1" or parsed.path != "/lightning_router_test":
            raise RuntimeError("Tests only permit the dedicated loopback test database")
    result = Store(url)
    if request.param == "postgres":
        metadata.drop_all(result.engine)
    result.migrate()
    yield result
    result.engine.dispose()


@pytest.fixture
def funding(store: Store) -> Funding:
    return Funding(store, Credentials(b"test-only-secret-32-bytes-" + b"x" * 20), FakeLnd(), FakeRates(), FakeCredits())


@pytest.fixture
def raw_key() -> str:
    return "sk-tr-v1-" + secrets.token_urlsafe(32)
