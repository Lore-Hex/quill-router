import base64
import hashlib
import json
import time
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
    assert adapter.ensure(PREIMAGE, 10_000_000, expires_at=int(time.time()) + 900).payment_hash == PAYMENT_HASH
    assert adapter.ensure(PREIMAGE, 10_000_000, expires_at=int(time.time()) + 900).payment_hash == PAYMENT_HASH
    assert len(posts) == 1
    assert 0 < int(posts[0]["expiry"]) <= 892


def test_expired_quote_never_creates_a_new_payable_invoice():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(404, json={"code": 5})
    adapter = Lnd(httpx.Client(base_url="https://lnd.invalid", transport=httpx.MockTransport(handler)), "regtest")
    assert adapter.ensure(PREIMAGE, 10_000_000, expires_at=int(time.time()) - 1) is None


def test_expired_quote_still_recovers_an_already_paid_invoice():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=response_data(state="SETTLED", amt_paid_msat="10000000", settle_index="9"))
    adapter = Lnd(httpx.Client(base_url="https://lnd.invalid", transport=httpx.MockTransport(handler)), "regtest")
    assert adapter.ensure(PREIMAGE, 10_000_000, expires_at=1).state == "SETTLED"


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


@pytest.mark.parametrize("state", ["CANCELED", "SETTLED"])
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_cancel_recovers_terminal_state_after_lost_response(state, failure):
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            raise failure("lost cancellation response")
        return httpx.Response(200, json=response_data(
            state=state, amt_paid_msat="10000000" if state == "SETTLED" else "0",
            settle_index="9" if state == "SETTLED" else "0"))
    client = httpx.Client(base_url="https://lnd.invalid", transport=httpx.MockTransport(handler))
    assert Lnd(client, "regtest").cancel(PAYMENT_HASH).state == state
    assert calls == ["POST", "GET"]


@pytest.mark.parametrize("state", ["OPEN", "ACCEPTED", None])
def test_ambiguous_cancel_never_reports_success_without_terminal_state(state):
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            raise httpx.ReadTimeout("lost cancellation response")
        return (httpx.Response(404, json={"code": 5}) if state is None else
                httpx.Response(200, json=response_data(state=state)))
    client = httpx.Client(base_url="https://lnd.invalid", transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.ReadTimeout):
        Lnd(client, "regtest").cancel(PAYMENT_HASH)
    assert calls == ["POST", "GET"]


def test_cancel_and_lookup_outage_does_not_retry_mutation():
    calls = []
    def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("LND unavailable")
    client = httpx.Client(base_url="https://lnd.invalid", transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.ReadTimeout):
        Lnd(client, "regtest").cancel(PAYMENT_HASH)
    assert calls == ["POST", "GET"]


@pytest.mark.parametrize("payment_hash", ["", "a", "FF" * 32, "0 " * 32])
def test_cancel_validates_identity_before_rpc(payment_hash):
    calls = []
    client = httpx.Client(base_url="https://lnd.invalid", transport=httpx.MockTransport(calls.append))
    with pytest.raises(ValueError, match="Invalid payment hash"):
        Lnd(client, "regtest").cancel(payment_hash)
    assert calls == []


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
    assert rates.current().usd_per_btc == Decimal("88888.887")
    assert rates.current().spot_usd_per_btc == Decimal("98765.43")
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", "100000001"])
def test_rate_not_finite_or_unbounded(value):
    with pytest.raises(ValueError):
        Rate(Decimal(value), 0)
