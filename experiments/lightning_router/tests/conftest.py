import hashlib
import os
import secrets
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from lightning_router.credentials import Credentials
from lightning_router.lnd import Invoice, Lnd
from lightning_router.rates import Rate, Rates
from lightning_router.service import Funding
from lightning_router.store import Store, metadata


class FakeRates(Rates):
    def __init__(self) -> None:
        self.fail = False

    def current(self) -> Rate:
        if self.fail:
            raise RuntimeError("rate unavailable")
        return Rate(Decimal("100000"), int(time.time()))


class FakeLnd(Lnd):
    def __init__(self) -> None:
        self.rows: dict[str, Invoice] = {}
        self.creates = 0
        self.fail_after_create = False
        self.pay_during_cancel = False

    def ensure(self, preimage: bytes, amount_msat: int) -> Invoice:
        payment_hash = hashlib.sha256(preimage).hexdigest()
        if payment_hash not in self.rows:
            self.creates += 1
            self.rows[payment_hash] = Invoice(
                payment_hash, "lnbcrt100u1" + "q" * 180, "OPEN", 0, 0,
                int(time.time()) + 900, amount_msat,
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
    return Funding(store, Credentials(b"test-only-secret-32-bytes-" + b"x" * 20), FakeLnd(), FakeRates())


@pytest.fixture
def raw_key() -> str:
    return "sk-lr-v1-" + secrets.token_urlsafe(32)
