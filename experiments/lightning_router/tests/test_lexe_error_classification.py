import httpx
import pytest
from lightning_router.lexe import Lexe, LexeHTTPError


@pytest.mark.parametrize("message,reason", [
    ("Authentication error: Auth token or auth request is expired: private-token", "authentication/auth_expired"),
    ("Authentication error: Database error: private-query", "authentication/database"),
    ("Error connecting to a remote HTTP service: private-url", "connect"),
    ("Request timed out: private-url", "timeout"),
    ("Could not proxy request to node: private-node", "proxy"),
    ("Request measurement doesn't match current enclave measurement: private-measurement", "measurement_mismatch"),
    ("General server error: private-error", "server"),
    ("Server is at capacity: private-node", "capacity"),
    ("private-message: Authentication error: private-token", "unknown"),
    ("Authentication error: private-message: Database error: private-query", "authentication"),
    ("Authentication error: Authentication error: Authentication error: Authentication error: private-token",
     "authentication/authentication/authentication"),
    (None, "unknown"),
    ({"secret": "private-token"}, "unknown"),
])
def test_wrapped_client_info_failures_log_only_allowlisted_categories(message, reason, monkeypatch, caplog):
    monkeypatch.setattr("lightning_router.lexe.time.sleep", lambda _: None)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(500, json={"code": 100, "msg": message, "sensitive": True,
                                        "data": {"preimage": "private-preimage"}})

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        lexe = Lexe(client, "a" * 64)
        with pytest.raises(LexeHTTPError) as error:
            lexe.ready()
        assert lexe._checked == 0
    assert len(calls) == 5  # Classification must not silently change retry policy.
    assert f"lexe_reason={reason} " in caplog.text
    assert error.value.reason == reason
    assert str(error.value) == "Lexe request unavailable"
    assert "private-" not in caplog.text
    assert "private-" not in repr(error.value.__dict__)


@pytest.mark.parametrize("method,path,code", [
    ("POST", "/v2/node/create_invoice", 100),
    ("POST", "/v2/node/cancel_payment", 100),
    ("GET", "/v2/node/payment", 100),
    ("GET", "/v2/node/updated_payments", 100),
    ("GET", "/v2/node/node_info", 100),
    ("GET", "/v2/node/client_info", 101),
    ("GET", "/v2/node/client_info", True),
    ("GET", "/unrecognized", 100),
])
def test_message_classification_is_scoped_to_wrapped_client_info_errors(method, path, code, monkeypatch, caplog):
    monkeypatch.setattr("lightning_router.lexe.time.sleep", lambda _: None)
    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(
            lambda _: httpx.Response(500, json={"code": code, "msg": "Authentication error: private-token"}))) as client:
        with pytest.raises(LexeHTTPError):
            Lexe(client, "a" * 64).request(method, path)
    assert "lexe_reason=unknown " in caplog.text
    assert "private-token" not in caplog.text


@pytest.mark.parametrize("body", [b"not-json", b"[]", b'{"code":100,"msg":"' + b"x" * 4096 + b'"}'])
def test_unknown_or_oversized_body_has_no_classification(body, monkeypatch, caplog):
    monkeypatch.setattr("lightning_router.lexe.time.sleep", lambda _: None)
    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(
            lambda _: httpx.Response(500, content=body, headers={"content-type": "application/json"}))) as client:
        with pytest.raises(LexeHTTPError):
            Lexe(client, "a" * 64).ready()
    assert "lexe_reason=unknown " in caplog.text


def test_diagnostic_classification_does_not_mark_a_recovered_request_failed(monkeypatch, caplog):
    monkeypatch.setattr("lightning_router.lexe.time.sleep", lambda _: None)
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, json={"code": 100, "msg": "Could not proxy request to node: private-node"})
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(handle)) as client:
        assert Lexe(client, "a" * 64).request("GET", "/v2/node/client_info") == {"ok": True}
    assert "lexe_reason=proxy " in caplog.text
    assert "lightning.lexe_read_recovered" in caplog.text
    assert "lightning.lexe_http_failed" not in caplog.text
    assert "private-node" not in caplog.text
