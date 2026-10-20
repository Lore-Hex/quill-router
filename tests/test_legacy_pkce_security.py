from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_oauth_key_delegation import _create_code, _pkce_challenge
from trusted_router.storage import STORE


@pytest.mark.parametrize("method", ["plain", "none", "s256", ""])
def test_legacy_creation_rejects_pkce_downgrades(
    client: TestClient, user_headers: dict[str, str], method: str
) -> None:
    body = {"callback_url": "https://app.example.com/callback"}
    if method:
        body.update(code_challenge="v" * 48, code_challenge_method=method)
    response = client.post("/v1/auth/keys/code", headers=user_headers, json=body)
    assert response.status_code == 400, response.text
    assert not STORE.oauth_code_store.codes


@pytest.mark.parametrize("method,challenge", [("plain", "v" * 48), (None, None), ("unknown", "v" * 48)])
def test_legacy_exchange_rejects_previously_issued_insecure_codes(
    client: TestClient, user_headers: dict[str, str], method: str | None, challenge: str | None
) -> None:
    code, _ = _create_code(client, user_headers, verifier="v" * 48)
    stored = next(iter(STORE.oauth_code_store.codes.values()))
    stored.code_challenge_method = method
    stored.code_challenge = challenge
    response = client.post("/v1/auth/keys", json={"code": code, "code_verifier": "v" * 48})
    assert response.status_code == 400, response.text
    assert "S256" in response.json()["error"]["message"]
    assert not STORE.list_keys(stored.workspace_id)


def test_legacy_consent_cannot_create_plain_pkce_code(client: TestClient) -> None:
    response = client.get("/auth", params={
        "callback_url": "https://app.example.com/callback",
        "code_challenge": "v" * 48,
        "code_challenge_method": "plain",
    })
    assert response.status_code == 400, response.text


def test_legacy_s256_exchange_still_returns_house_envelope(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    verifier = "v" * 48
    code, _ = _create_code(client, user_headers, code_challenge=_pkce_challenge(verifier))
    response = client.post("/v1/auth/keys", json={"code": code, "code_verifier": verifier})
    assert response.status_code == 200, response.text
    assert {"key", "user_id", "identity", "data"} <= response.json().keys()
    assert "access_token" not in response.json()
