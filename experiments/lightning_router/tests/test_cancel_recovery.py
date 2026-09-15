"""Exercise the real LND wire adapter against both funding database backends."""
import base64
import uuid

import httpx
import pytest
from lightning_router.lnd import Lnd


@pytest.mark.parametrize("terminal", ["CANCELED", "SETTLED"])
def test_lost_cancel_response_preserves_exact_credit_and_binding(funding, raw_key, terminal):
    item = funding.create(raw_key, uuid.uuid4().hex, 1000, new=True)
    owner = funding.credentials.fingerprint(raw_key)
    row = funding.store.invoice(item["id"], owner)
    lnd_state = "OPEN"
    posts = 0

    def handle(request):
        nonlocal lnd_state, posts
        if request.method == "POST":
            assert request.url.path == "/v2/invoices/cancel"
            posts += 1
            lnd_state = terminal
            raise httpx.ReadTimeout("cancel committed; response lost")
        return httpx.Response(200, json={
            "r_hash": base64.b64encode(bytes.fromhex(row["payment_hash"])).decode(),
            "payment_request": row["bolt11"], "state": lnd_state,
            "creation_date": str(row["created_at"]), "expiry": "900",
            "value_msat": str(row["requested_msat"]),
            "amt_paid_msat": str(row["requested_msat"] if lnd_state == "SETTLED" else 0),
            "settle_index": "1" if lnd_state == "SETTLED" else "0",
        })

    funding.lnd = Lnd(httpx.Client(base_url="https://lnd.invalid", transport=httpx.MockTransport(handle)), "regtest")
    result = funding.refresh(row, cancel=True)
    assert result["state"] == terminal
    assert funding.refresh(row, cancel=True) == result
    assert posts == 1
    assert funding.store.active(owner) is None
    assert funding.store.invoice(row["id"], owner)["failure_code"] == ""
    if terminal == "SETTLED":
        assert result["credited"] is True
        assert funding.account(raw_key)["balance_microdollars"] == "10000000"
        assert len(funding.credits.balances) == len(funding.credits.payments) == 1
        assert funding.credits.payments[row["payment_hash"]][1] == 10_000_000
    else:
        assert result["account_created"] is False
        assert funding.credits.balances == funding.credits.payments == {}
