"""Loopback-only browser test harness. Never imported by the deployed app."""
import secrets
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

import uvicorn
from lightning_router.app import create_app
from lightning_router.credentials import Credentials
from lightning_router.lookup import LookupGate
from lightning_router.rates import Rate
from lightning_router.reasoning import reasoning_profile
from lightning_router.service import Funding
from lightning_router.store import Store, limits
from sqlalchemy import delete

from tests.conftest import FakeCredits, FakeLnd, FakeRates

directory = tempfile.TemporaryDirectory(prefix="lr-browser-")
store = Store(f"sqlite:///{Path(directory.name) / 'ledger.db'}")
store.migrate()
funding = Funding(store, Credentials(secrets.token_bytes(32)), FakeLnd(), FakeRates(), FakeCredits())
funding.rates.current = lambda: Rate.from_spot(Decimal("100000"), 0)


class Catalog:
    def current(self):
        return [
            {"id": "deepseek/deepseek-flash", "name": "DeepSeek Flash", "context": 1048576, "output": 393216,
             "default_output": 65536, "reasoning": reasoning_profile({"id": "deepseek/deepseek-flash"}),
             "pricing": {"input_per_million": "0.042200", "output_per_million": "0.084400", "cached_input_per_million": "0.01"}},
            {"id": "kimi/kimi-k2.7", "name": "Kimi K2.7", "context": 262144, "output": 8192},
            {"id": "anthropic/claude-opus-4.8", "name": "Claude Opus 4.8",
             "reasoning": reasoning_profile({"id": "anthropic/claude-opus-4.8"})},
        ]


async def test_delay(self):
    pass


LookupGate.delay = test_delay
app = create_app(funding, rates=funding.rates, catalog=Catalog(), network="regtest", start_worker=False)


@app.post("/_test/reset-rate-limits")
def reset_rate_limits():
    # Each browser test is a separate user; the loopback peer must not share
    # a rate-limit budget across the entire suite. Production never imports us.
    with store.engine.begin() as connection:
        connection.execute(delete(limits))
    return {"ok": True}


@app.post("/_test/pay")
def pay():
    for row in funding.store.pending():
        funding.lnd.pay(row["payment_hash"])
    funding.reconcile()
    return {"ok": True}


@app.post("/_test/existing")
def existing():
    key = "sk-tr-v1-" + secrets.token_urlsafe(32)
    item = funding.create(key, uuid.uuid4().hex, 2000, new=True)
    row = funding.store.invoice(item["id"], funding.credentials.fingerprint(key))
    funding.lnd.pay(row["payment_hash"])
    funding.reconcile()
    return {"key": key}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8094, access_log=False)
