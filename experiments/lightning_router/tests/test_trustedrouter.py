import httpx
import pytest
from lightning_router.trustedrouter import TrustedRouterCredits


def test_adapter_uses_dedicated_authority_and_fixed_usd_amount() -> None:
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + "t" * 32
        if request.url.path.endswith("resolve"):
            return httpx.Response(200, json={"account_id": "workspace"})
        if request.url.path.endswith("balance"):
            return httpx.Response(200, json={"available_microdollars": 1234567})
        return httpx.Response(200, json={"committed": True})

    bridge = TrustedRouterCredits("https://billing.example", "t" * 32, httpx.Client(transport=httpx.MockTransport(handle)))
    assert bridge.resolve("raw-secret", new=True) == "workspace"
    assert bridge.balance("workspace") == 1234567
    bridge.credit("workspace", "a" * 64, 1234567)
    assert len(requests) == 3
    assert b'"amount_microdollars":1234567' in requests[2].content
    assert all("raw-secret" not in str(request.url) for request in requests)


@pytest.mark.parametrize("endpoint", ["http://billing.example", "https://user:pass@billing.example", "https://billing.example/path", "https://billing.example?token=a"])
def test_reject_insecure_authority(endpoint):
    with pytest.raises(ValueError):
        TrustedRouterCredits(endpoint, "t" * 32, httpx.Client())


@pytest.mark.parametrize("payload", [{}, {"committed": False}, {"committed": "true"}])
def test_credit_requires_explicit_commit_ack(payload):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    with pytest.raises(ValueError):
        TrustedRouterCredits("https://billing.example", "t" * 32, client).credit("ws", "a" * 64, 1)


def test_authority_redirect_is_not_followed():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://evil.example"})

    client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True)
    with pytest.raises(httpx.HTTPStatusError):
        TrustedRouterCredits("https://billing.example", "t" * 32, client).resolve("secret", new=False)
    assert len(calls) == 1


def test_usage_uses_only_callers_key_and_returns_only_numeric_fields():
    def handle(request):
        assert request.method == "GET"
        assert str(request.url) == "https://billing.example/v1/key"
        assert request.headers["authorization"] == "Bearer customer-key"
        return httpx.Response(200, json={"data": {
            "usage_microdollars": 1234567, "limit_microdollars": None,
            "hash": "private-key-hash", "creator_user_id": "private-user", "name": "private-name",
        }})
    bridge = TrustedRouterCredits("https://billing.example", "operator-secret" * 3,
        httpx.Client(transport=httpx.MockTransport(handle)))
    result = bridge.usage("customer-key")
    assert result == {"usage_usd": "1.234567", "byok_usage_usd": None, "reserved_usd": None,
                      "limit_usd": None, "limit_remaining_usd": None}
    assert "private" not in str(result)


@pytest.mark.parametrize("status", [401, 403, 404, 307, 503])
def test_usage_never_redirects_customer_credential(status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://evil.example"})
    bridge = TrustedRouterCredits("https://billing.example", "t" * 32,
        httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True))
    with pytest.raises((KeyError, httpx.HTTPStatusError)):
        bridge.usage("customer-key")
    assert len(calls) == 1


@pytest.mark.parametrize("value", [True, -1, "123", 1.1])
def test_usage_requires_integer_money(value):
    bridge = TrustedRouterCredits("https://billing.example", "t" * 32,
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": {"usage_microdollars": value}}))))
    with pytest.raises(ValueError):
        bridge.usage("customer-key")
