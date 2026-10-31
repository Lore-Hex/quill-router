"""Malformed request URLs follow the same route errors as other invalid URLs."""
from __future__ import annotations

import base64
import socket

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient

from tests.test_bedrock_group_buy import _payload
from tests.test_oauth_apps_increment_b import _active_session, _app_body, _identity_user
from tests.test_oauth_key_delegation import _pkce_challenge
from tests.test_paypal_processing_fees import _paypal_signature_headers, _verified_paypal_settings
from tests.test_provider_trust_slice1b import paypal_event
from tests.test_request_tagging import _create_tagged_key
from tests.test_ses_notifications import _envelope, _self_signed_cert_and_key
from tests.test_user_models import _body
from trusted_router.main import create_app
from trusted_router.storage import STORE

MALFORMED = [
    "http://[", "http://[]/", "http://example.com:abc/", "http://example.com:99999/",
    "https://[", "https://[]/", "https://example.com:abc/", "https://example.com:99999/",
]
VALID = "https://example.com:443/path;params?query=1#fragment"


def _error(message: str, *, status: int = 400, kind: str = "bad_request") -> dict:
    return {"error": {"code": status, "message": message, "type": kind, "source": "router"}}


@pytest.mark.parametrize("url", [*MALFORMED, VALID])
@pytest.mark.parametrize("kind", ["webhook", "posthog"])
@pytest.mark.parametrize("operation", ["create", "patch"])
def test_broadcast_url_boundary(client, user_headers, url, kind, operation):
    body = {"name": "URL test", "type": kind, "endpoint": VALID, "api_key": "test-key"}
    path = "/v1/broadcast/destinations"
    if operation == "patch":
        created = client.post(path, headers=user_headers, json=body)
        assert created.status_code == 201, created.text
        response = client.patch(f"{path}/{created.json()['data']['id']}", headers=user_headers,
                                json={"endpoint": url})
    else:
        response = client.post(path, headers=user_headers, json={**body, "endpoint": url})
    if url == VALID:
        assert response.status_code == (201 if operation == "create" else 200), response.text
        assert response.json()["data"]["endpoint"] == url
    else:
        assert response.status_code == 400, response.text
        assert response.json() == _error("endpoint must be an HTTP URL")


@pytest.mark.parametrize("url", [*MALFORMED, VALID])
@pytest.mark.parametrize("field", ["logo_url", "redirect_uris"])
def test_oauth_app_url_boundary(client, user_headers, url, field):
    _identity_user(user_headers["x-trustedrouter-user"])
    _active_session(client, user_headers["x-trustedrouter-user"])
    body = _app_body(**{field: [url] if field == "redirect_uris" else url})
    response = client.post("/v1/oauth/apps", headers=user_headers, json=body)
    if url == VALID:
        assert response.status_code == 201, response.text
        assert response.json()["data"][field] == body[field]
    else:
        message = "logo_url must be an https URL" if field == "logo_url" else _callback_error(url)
        assert response.status_code == 400, response.text
        assert response.json() == _error(message)


def _callback_error(url):
    if url.startswith("https://example.com:"):
        return "callback_url has an invalid port"
    return "callback_url must be an https URL"


@pytest.mark.parametrize("url", [*MALFORMED, VALID, "http://localhost:3210/callback", "myapp://callback/path"])
def test_oauth_callback_url_boundary(client, user_headers, url):
    response = client.post("/v1/auth/keys/code", headers=user_headers, json={
        "callback_url": url, "code_challenge": _pkce_challenge("a" * 48),
        "code_challenge_method": "S256",
    })
    if url not in MALFORMED:
        assert response.status_code == 200, response.text
        assert response.json()["data"]["id"]
    else:
        assert response.status_code == 400, response.text
        assert response.json() == _error(_callback_error(url))


@pytest.mark.parametrize("url", [*MALFORMED, "http://testserver:abc/", "http://testserver:99999/",
                                 "http://testserver/path", "http://testserver:3000/path"])
@pytest.mark.parametrize("header", ["origin", "referer"])
def test_group_buy_origin_url_boundary(client, user_headers, url, header):
    response = client.post("/v1/bedrock-group-buy/pledge", json=_payload(),
                           headers={**user_headers, header: url})
    if url in {"http://testserver/path", "http://testserver:3000/path"}:
        assert response.status_code == 200, response.text
        assert response.json()["pledge"]["company_url"] == _payload()["company_url"]
    else:
        assert response.status_code == 403, response.text
        assert response.json() == _error("Same-origin request required", status=403, kind="forbidden")
        assert STORE.list_bedrock_group_buy_private_pledges() == []


@pytest.mark.parametrize("url", [*MALFORMED, VALID])
def test_group_buy_company_url_boundary(client, user_headers, url):
    response = client.post("/v1/bedrock-group-buy/pledge", headers=user_headers,
                           json=_payload(company_url=url))
    if url == VALID:
        assert response.status_code == 200, response.text
        assert response.json()["pledge"]["company_url"] == url
    else:
        assert response.status_code == 422, response.text
        assert response.json() == {"error": {
            "message": "Enter a complete https:// company URL", "type": "invalid_request_error",
        }}


@pytest.mark.parametrize("url", [*MALFORMED, VALID])
def test_attribution_url_boundary(client, user_headers, monkeypatch, url):
    monkeypatch.setattr("trusted_router.gateway_timing.perf_counter", lambda: 0.0)
    created = _create_tagged_key(client, user_headers)
    response = client.post("/v1/internal/gateway/authorize", json={
        "api_key_hash": created["data"]["hash"], "model": "anthropic/claude-opus-4.7",
        "estimated_input_tokens": 12, "max_output_tokens": 4, "http_referer": url,
    })
    if url == VALID:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 400, response.text
        assert response.json() == {
            **_error("HTTP-Referer must be an http or https URL", kind="invalid_request_metadata"),
            "data": {"timing": {"total_ms": 0, "spanner_rpcs": 0, "key_lookup_ms": 0,
                                "routing_ms": 0, "store_ms": 0, "post_commit_ms": 0}},
        }
        assert STORE.credit_money[created["data"]["workspace_id"]].reserved_microdollars == 0


@pytest.mark.parametrize("url", [*MALFORMED, VALID])
@pytest.mark.parametrize("route", ["model", "image"])
def test_egress_url_boundary(client, user_headers, monkeypatch, httpx_mock, url, route):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0)),
    ])
    if route == "model":
        response = client.post("/v1/user-models", headers=user_headers,
                               json={**_body(), "endpoint_url": url})
        success = 201
    else:
        # Catch any accidental fetch, including ports httpx accepts but urllib rejects.
        httpx_mock.add_response(url=url if url == VALID else None, is_optional=url != VALID,
                                content=b"image", headers={"content-type": "image/png"})
        response = client.post("/v1/internal/gateway/fetch-image", json={"url": url})
        success = 200
    if url == VALID:
        assert response.status_code == success, response.text
        if route == "model":
            assert response.json()["data"]["endpoint_url"] == url
        else:
            assert response.json() == {"data": {"media_type": "image/png", "data_base64": "aW1hZ2U="}}
    else:
        assert response.status_code == 400, response.text
        assert response.json() == _error("invalid URL")
        assert httpx_mock.get_requests() == []


@pytest.mark.parametrize("url", [*MALFORMED, "https://api-m.paypal.com/cert.pem"])
def test_paypal_cert_url_boundary(monkeypatch, url):
    calls = []
    def verified(*args, **kwargs):
        calls.append(kwargs["json_body"]["cert_url"])
        return {"verification_status": "SUCCESS"}
    monkeypatch.setattr("trusted_router.services.paypal_billing._paypal_post", verified)
    with TestClient(create_app(_verified_paypal_settings(), init_observability=False)) as client:
        response = client.post("/v1/internal/paypal/webhook", json={"id": "WH-URL"},
                               headers={**_paypal_signature_headers(), "paypal-cert-url": url})
    if url not in MALFORMED:
        assert response.status_code == 200, response.text
        assert response.json() == {"data": {"ignored": True, "event_id": "WH-URL"}}
        assert calls == [url]
    else:
        assert response.status_code == 400, response.text
        assert response.json() == _error("Invalid PayPal webhook headers")
        assert calls == []


@pytest.mark.parametrize("url", [*MALFORMED, "https://sns.us-east-1.amazonaws.com/cert.pem"])
@pytest.mark.parametrize("early_rejection", [True, False])
def test_sns_cert_url_boundary(client, capsys, monkeypatch, url, early_rejection):
    # An earlier envelope error must not crash while logging its malformed cert URL.
    def no_fetch(*args, **kwargs):
        raise AssertionError("invalid envelopes must not fetch a certificate")
    monkeypatch.setattr("trusted_router.sns_verify._httpx_cert_fetcher", no_fetch)
    message = _envelope(SigningCertURL=url)
    if early_rejection:
        message["Type"] = "invalid"
    response = client.post("/internal/ses/notifications", json=message)
    assert response.status_code == 403, response.text
    assert response.json() == _error("SNS signature verification failed", status=403, kind="forbidden")
    expected = "<unparseable>" if url in MALFORMED else "sns.us-east-1.amazonaws.com"
    assert f"cert_host={expected!r}" in capsys.readouterr().err


def test_sns_valid_cert_url_still_verifies_through_route(client, monkeypatch):
    cert, key = _self_signed_cert_and_key()
    message = _envelope(SignatureVersion="2", Signature="")
    canonical = "".join(f"{field}\n{message[field]}\n" for field in
                        ("Message", "MessageId", "Timestamp", "TopicArn", "Type"))
    signature = key.sign(canonical.encode(), padding.PKCS1v15(), hashes.SHA256())
    message["Signature"] = base64.b64encode(signature).decode()
    monkeypatch.setattr("trusted_router.sns_verify._httpx_cert_fetcher",
                        lambda url: cert.public_bytes(serialization.Encoding.PEM))
    response = client.post("/internal/ses/notifications", json=message)
    assert response.status_code == 200, response.text
    assert response.json() == {"data": {"kind": "Bounce", "blocked_count": 0, "replayed": False}}


@pytest.mark.parametrize("url", [*MALFORMED, "https://api-m.paypal.com/v2/payments/captures/capture1"])
def test_paypal_refund_link_url_boundary(monkeypatch, url):
    monkeypatch.setattr("trusted_router.services.paypal_billing._paypal_post",
                        lambda *a, **kw: {"verification_status": "SUCCESS"})
    event = paypal_event()
    event["resource"].pop("supplementary_data")
    event["resource"]["links"] = [{"rel": "up", "href": url}]
    with TestClient(create_app(_verified_paypal_settings(), init_observability=False)) as client:
        response = client.post("/v1/internal/paypal/webhook", json=event,
                               headers=_paypal_signature_headers())
    if url not in MALFORMED:
        assert response.status_code == 200, response.text
        assert response.json() == {"data": {"event_id": "WH-test", "adverse": ["inbox"]}}
    else:
        assert response.status_code == 400, response.text
        assert response.json() == _error("Provider adverse and original payment references are required")
