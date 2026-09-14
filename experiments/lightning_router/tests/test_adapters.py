import base64
import hashlib
import json
from decimal import Decimal

import httpx
import pytest
from lightning_router.lnd import Lnd
from lightning_router.rates import Rate, Rates

PREIMAGE = b"x" * 32
PAYMENT_HASH = hashlib.sha256(PREIMAGE).hexdigest()


def response_data(**values):
    return {"r_hash": base64.b64encode(bytes.fromhex(PAYMENT_HASH)).decode(),
            "payment_request": "lnbcrt100u1" + "q" * 180, "state": "OPEN",
            "value_msat": "10000000", "amt_paid_msat": "0", "settle_index": "0",
            "creation_date": "1900000000", "expiry": "900", **values}


def test_lnd_wire_contract_and_ambiguous_post_recovery():
    created = False
    posts = []
    def handler(request):
        nonlocal created
        assert request.headers["Grpc-Metadata-macaroon"] == "test-invoice-only"
        if request.method == "GET":
            assert request.url.path == "/v1/invoice/" + PAYMENT_HASH
            return httpx.Response(200, json=response_data()) if created else httpx.Response(404, json={"code": 5})
        body = json.loads(request.content)
        posts.append(body)
        assert base64.b64decode(body["r_preimage"]) == PREIMAGE
        assert body["value_msat"] == "10000000"
        assert "value" not in body
        created = True
        raise httpx.ReadTimeout("simulated lost response")
    client = httpx.Client(base_url="https://lnd.invalid", transport=httpx.MockTransport(handler),
                          headers={"Grpc-Metadata-macaroon": "test-invoice-only"})
    adapter = Lnd(client, "regtest")
    assert adapter.ensure(PREIMAGE, 10_000_000).payment_hash == PAYMENT_HASH
    assert adapter.ensure(PREIMAGE, 10_000_000).payment_hash == PAYMENT_HASH
    assert len(posts) == 1


@pytest.mark.parametrize("change", [
    {"r_hash": base64.b64encode(b"z" * 32).decode()},
    {"payment_request": "lnbc100u1" + "q" * 180},
    {"amt_paid_msat": 1.5}, {"state": "UNKNOWN"}, {"settle_index": True},
])
def test_invalid_lnd_response_fails_closed(change):
    client = httpx.Client(base_url="https://lnd.invalid", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=response_data(**change))))
    with pytest.raises(ValueError):
        Lnd(client, "regtest").lookup(PAYMENT_HASH)


def test_cancel_paid_race_is_not_a_false_cancellation():
    def handler(request):
        if request.method == "POST":
            assert request.url.path == "/v2/invoices/cancel"
            assert base64.b64decode(json.loads(request.content)["payment_hash"]).hex() == PAYMENT_HASH
            return httpx.Response(409, json={"message": "already settled"})
        return httpx.Response(200, json=response_data(state="SETTLED", amt_paid_msat="10000000", settle_index="9"))
    client = httpx.Client(base_url="https://lnd.invalid", transport=httpx.MockTransport(handler))
    assert Lnd(client, "regtest").cancel(PAYMENT_HASH).state == "SETTLED"


def test_missing_invoice_requires_grpc_not_found():
    client = httpx.Client(base_url="https://lnd.invalid", transport=httpx.MockTransport(
        lambda _: httpx.Response(404, json={"code": 7})))
    with pytest.raises(httpx.HTTPStatusError):
        Lnd(client, "regtest").lookup(PAYMENT_HASH)


def test_rate_cache_and_currency_validation():
    calls = []
    def handler(request):
        calls.append(request)
        assert "authorization" not in request.headers
        assert "Grpc-Metadata-macaroon" not in request.headers
        return httpx.Response(200, json={"data": {"base": "BTC", "currency": "USD", "amount": "98765.43"}})
    rates = Rates(httpx.Client(transport=httpx.MockTransport(handler)))
    assert rates.current().usd_per_btc == Decimal("98765.43")
    assert rates.current().usd_per_btc == Decimal("98765.43")
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", "100000001"])
def test_rate_not_finite_or_unbounded(value):
    with pytest.raises(ValueError):
        Rate(Decimal(value), 0)
