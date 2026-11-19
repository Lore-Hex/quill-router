from fastapi.testclient import TestClient

from trusted_router.openai_safety import owner_digest, valid_digest
from trusted_router.storage import STORE
from trusted_router.storage_models import federated_api_key_from_record


def test_owner_identifier_is_stable_and_provider_scoped() -> None:
    owner = owner_digest("same-user", "workspace-a")
    assert len(bytes.fromhex(owner)) == 32
    assert owner_digest("same-user", "workspace-b") == owner
    assert owner_digest("other-user", "workspace-a") != owner
    assert owner_digest(None, "same-user") != owner
    assert owner_digest(None, "workspace-a") != owner_digest(None, "workspace-b")
    assert valid_digest(owner) == owner
    assert valid_digest("not-a-digest@example.com") == ""


def test_authorization_replay_rotation_and_federation(client: TestClient) -> None:
    ws = STORE.create_workspace("workspace-owner", "safety", trial_credit_microdollars=2_000_000)
    for n, user in enumerate(("same-user", "same-user", "different-user")):
        _, key = STORE.create_api_key(workspace_id=ws.id, creator_user_id=user, name=f"key-{n}")
        body = {
            "api_key_hash": key.hash, "model": "openai/gpt-4.1", "estimated_input_tokens": 12,
            "max_tokens": 16, "idempotency_key": f"test-safety-{n}",
            "provider": {"only": ["openai"], "allow_fallbacks": False},
        }
        for _ in range(2):
            r = client.post("/v1/internal/gateway/authorize", json=body, headers={"X-TR-OpenAI-Safety": "v1"})
            assert r.status_code == 200, r.text
            assert r.json()["data"]["openai_safety_identifier"] == owner_digest(user, ws.id)
        old = client.post("/v1/internal/gateway/authorize", json=body)
        assert old.status_code == 200, old.text
        assert "openai_safety_identifier" not in old.json()["data"]
        client.app.state.settings.federation_peer_token = "test-peer"  # noqa: S105
        peer = client.post(
            "/v1/internal/federation/resolve-key",
            json={"api_key_lookup_hash": key.lookup_hash},
            headers={"x-trustedrouter-federation-token": "test-peer", "x-trustedrouter-federation-features": "openai-safety-v1"},
        )
        assert peer.status_code == 200, peer.text
        assert federated_api_key_from_record(peer.json()["data"]).federated_openai_safety_identifier == owner_digest(user, ws.id)
