import logging

import httpx
import pytest
from lightning_router.lexe import PERMISSIONS, SCOPES, Lexe, LexeReadinessError


@pytest.fixture(autouse=True)
def no_real_backoff(monkeypatch):
    monkeypatch.setattr("lightning_router.lexe.time.sleep", lambda _: None)


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                                  httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError])
@pytest.mark.parametrize("path", [
    "/v2/node/client_info", "/v2/node/node_info", "/v2/node/payment",
    "/v2/node/updated_payments",
])
def test_transient_safe_read_recovers_with_one_short_retry(path, error, caplog):
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise error("sensitive upstream diagnostics")
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(base_url="http://127.0.0.1:5393", timeout=20,
                          transport=httpx.MockTransport(handle))
    lexe = Lexe(client, "a" * 64)
    with caplog.at_level(logging.WARNING):
        assert lexe.request("GET", path, params={"index": "private-payment-index"}) == {"ok": True}
    assert len(requests) == 2
    assert requests[0].url == requests[1].url
    assert requests[0].extensions["timeout"]["read"] == 20
    assert requests[1].extensions["timeout"]["read"] <= 5
    assert "lightning.lexe_read_retry" in caplog.text
    assert f"error_type={error.__name__}" in caplog.text
    assert "sensitive upstream" not in caplog.text
    assert "private-payment-index" not in caplog.text


def test_persistent_read_timeout_still_fails_closed(caplog):
    attempts = []

    def handle(request):
        attempts.append(request)
        raise httpx.ReadTimeout("private provider body")

    lexe = Lexe(httpx.Client(base_url="http://127.0.0.1:5393",
                             transport=httpx.MockTransport(handle)), "a" * 64)
    with pytest.raises(httpx.ReadTimeout):
        lexe.ready()
    assert len(attempts) == 5
    assert lexe._checked == 0
    assert "lightning.lexe_read_failed" in caplog.text
    assert "operation=client_info" in caplog.text
    assert "private provider body" not in caplog.text


@pytest.mark.parametrize("method,path", [
    ("POST", "/v2/node/create_invoice"),
    ("POST", "/v2/node/cancel_payment"),
    ("GET", "/unexpected-endpoint"),
])
def test_mutations_and_unrecognized_reads_are_never_retried(method, path):
    attempts = []

    def handle(request):
        attempts.append(request)
        raise httpx.ReadTimeout("ambiguous mutation")

    lexe = Lexe(httpx.Client(base_url="http://127.0.0.1:5393",
                             transport=httpx.MockTransport(handle)), "a" * 64)
    with pytest.raises(httpx.ReadTimeout):
        lexe.request(method, path)
    assert len(attempts) == 1


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 501])
def test_provider_or_authority_rejections_are_never_retried(status):
    attempts = []

    def handle(request):
        attempts.append(request)
        return httpx.Response(status, json={"error": "private rejection"})

    lexe = Lexe(httpx.Client(base_url="http://127.0.0.1:5393",
                             transport=httpx.MockTransport(handle)), "a" * 64)
    with pytest.raises(RuntimeError, match="Lexe request unavailable"):
        lexe.ready()
    assert len(attempts) == 1
    assert lexe._checked == 0


def test_exhausted_retry_preserves_receiving_alert_and_reconciliation(funding, caplog):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret diagnostics")

    funding.lexe = Lexe(httpx.Client(base_url="http://127.0.0.1:5393",
                                     transport=httpx.MockTransport(handle)), "a" * 64)
    funding.new_invoice_backend = "lexe"
    funding.check_capacity = True
    funding._last_health_log = -1000
    assert funding.reconcile() == {"checked": 0, "failed": 0}
    assert len(calls) == 5
    assert "lightning.liquidity_check_failed error_type=ReadTimeout" in caplog.text
    assert '"receive_authority_ready": true' not in caplog.text
    assert "secret diagnostics" not in caplog.text


def test_retry_does_not_accept_invalid_authority_or_cache_success():
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("transient")
        return httpx.Response(200, json={"kind": "root"})

    lexe = Lexe(httpx.Client(base_url="http://127.0.0.1:5393",
                             transport=httpx.MockTransport(handle)), "a" * 64)
    with pytest.raises(ValueError, match="Receive-only Lexe authority required"):
        lexe.ready()
    assert len(calls) == 2
    assert lexe._checked == 0


@pytest.mark.parametrize("patch,reason", [
    ({"kind": "root_seed"}, "authority_invalid"),
    ({"scopes": [*SCOPES, "spend"]}, "authority_invalid"),
    ({"permissions": ["pay_invoice"]}, "authority_invalid"),
    ({"effective_permissions": None}, "effective_permissions_invalid"),
    ({"effective_permissions": [None]}, "effective_permissions_invalid"),
    ({"effective_permissions": []}, "required_permissions_missing"),
    ({"effective_permissions": [*PERMISSIONS, "private-unknown-permission"]}, "unreviewed_permissions"),
    ({"expires_at": None}, "credential_expiry_invalid"),
    ({"expires_at": True}, "credential_expiry_invalid"),
    ({"expires_at": 1}, "credential_expiring"),
    ({"wallet": "b" * 64}, "wallet_mismatch"),
])
def test_readiness_reasons_are_static_and_fail_closed(patch, reason, caplog):
    info = {"kind": "client_credentials", "scopes": sorted(SCOPES),
            "effective_permissions": sorted(PERMISSIONS), "expires_at": 9_999_999_999_999}
    info.update(patch)

    def handle(request):
        return httpx.Response(200, json=info if request.url.path.endswith("client_info") else
                              {"user_pk": patch.get("wallet", "a" * 64)})

    lexe = Lexe(httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)), "a" * 64)
    with pytest.raises(LexeReadinessError) as error:
        lexe.ready()
    assert error.value.reason == reason
    assert lexe._checked == 0
    assert f"lightning.lexe_readiness_failed reason={reason}" in caplog.text
    assert "private-unknown-permission" not in caplog.text
    assert "b" * 64 not in caplog.text
