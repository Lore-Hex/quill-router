from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE

SHARED_SECRET = "veriff-webhook-test-secret"  # noqa: S105 - test fixture.


def _payload(
    *,
    session_id: str = "session-one",
    status: str = "approved",
    code: int = 9001,
    vendor_data: str,
) -> bytes:
    return json.dumps(
        {
            "verification": {
                "id": session_id,
                "status": status,
                "code": code,
                "vendorData": vendor_data,
                "person": {"firstName": "Ada", "lastName": "Lovelace"},
            }
        },
        separators=(",", ":"),
    ).encode()


def _signature(raw: bytes) -> str:
    return hmac.new(SHARED_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _client_and_user() -> tuple[TestClient, Any]:
    settings = Settings(
        environment="test",
        veriff_enabled=True,
        veriff_api_key="api-key",
        veriff_shared_secret_key=SHARED_SECRET,
    )
    client = TestClient(create_app(settings, init_observability=False))
    user = STORE.ensure_user("veriff-webhook@example.com")
    STORE.set_user_identity_status(
        user.id,
        status="pending",
        session_id="session-one",
        session_url="https://verify.example/session-one",
        increment_attempts=True,
    )
    return client, user


def _post(
    client: TestClient, raw: bytes, signature: str | None = None
) -> httpx.Response:
    headers = {"content-type": "application/json"}
    if signature is not None:
        headers["x-hmac-signature"] = signature
    return client.post("/v1/internal/veriff/webhook", content=raw, headers=headers)


def test_veriff_webhook_rejects_missing_and_near_miss_hmac() -> None:
    client, user = _client_and_user()
    raw = _payload(vendor_data=user.id)
    valid = _signature(raw)

    missing = _post(client, raw)
    near_miss = _post(client, raw, valid[:-1] + ("0" if valid[-1] != "0" else "1"))

    assert missing.status_code == 403
    assert near_miss.status_code == 403


def test_valid_approved_webhook_sets_name_and_one_time_verified_timestamp() -> None:
    client, user = _client_and_user()
    raw = _payload(vendor_data=user.id)

    response = _post(client, raw, _signature(raw))
    approved = STORE.get_user(user.id)
    assert approved is not None
    first_timestamp = approved.identity_verified_at
    captured_name = approved.identity_verified_name
    STORE.set_user_identity_status(user.id, status="approved", verified_name="Changed Name")
    approved_again = STORE.get_user(user.id)

    assert response.status_code == 200
    assert response.json() == {"data": {"status": "approved"}}
    assert approved.identity_verified
    assert first_timestamp is not None
    assert captured_name == "Ada Lovelace"
    assert approved.veriff_decision_code == 9001
    assert approved_again is not None
    assert approved_again.identity_verified_at == first_timestamp


def test_veriff_webhook_replay_is_reported_and_does_not_change_state() -> None:
    client, user = _client_and_user()
    raw = _payload(vendor_data=user.id)
    signature = _signature(raw)

    first = _post(client, raw, signature)
    after_first = STORE.get_user(user.id)
    assert after_first is not None
    snapshot = (
        after_first.identity_status,
        after_first.identity_verified_at,
        after_first.identity_verified_name,
        after_first.veriff_decision_code,
    )
    replay = _post(client, raw, signature)
    after_replay = STORE.get_user(user.id)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == {"data": {"replayed": True}}
    assert after_replay is not None
    assert (
        after_replay.identity_status,
        after_replay.identity_verified_at,
        after_replay.identity_verified_name,
        after_replay.veriff_decision_code,
    ) == snapshot


def test_veriff_webhook_unknown_type_is_ignored() -> None:
    client, user = _client_and_user()
    raw = _payload(status="mystery", code=9999, vendor_data=user.id)
    response = _post(client, raw, _signature(raw))
    assert response.status_code == 200
    assert response.json() == {"data": {"ignored": True}}


def test_veriff_webhook_stale_session_is_ignored() -> None:
    client, user = _client_and_user()
    raw = _payload(session_id="stale-session", vendor_data=user.id)
    response = _post(client, raw, _signature(raw))
    assert response.status_code == 200
    assert response.json() == {"data": {"ignored": True}}
    current = STORE.get_user(user.id)
    assert current is not None
    assert current.identity_status == "pending"


def test_veriff_webhook_unknown_vendor_data_is_ignored() -> None:
    client, _user = _client_and_user()
    raw = _payload(vendor_data="missing-user")
    response = _post(client, raw, _signature(raw))
    assert response.status_code == 200
    assert response.json() == {"data": {"ignored": True}}
