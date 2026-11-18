"""Per-key lenient request parsing.

A lenient key tells the gateway to drop unknown and unsupported request
parameters instead of refusing the request. The gateway learns the setting
from key validation, so it must survive every store and reach that response.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage_key_patch import apply_key_patch

USER = {"x-trustedrouter-user": "alice@example.com"}


def _client() -> TestClient:
    return TestClient(create_app(Settings(environment="test"), init_observability=False))


def _validate(client: TestClient, key_hash: str) -> dict:
    response = client.post(
        "/v1/internal/gateway/validate",
        json={"api_key_hash": key_hash, "route_type": "chat.completions"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_keys_are_strict_by_default() -> None:
    client = _client()
    created = client.post("/v1/keys", headers=USER, json={"name": "strict"})
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["lenient"] is False
    assert _validate(client, data["hash"])["lenient"] is False


def test_lenient_key_reaches_gateway_validation() -> None:
    client = _client()
    created = client.post("/v1/keys", headers=USER, json={"name": "fallback", "lenient": True})
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["lenient"] is True
    assert _validate(client, data["hash"])["lenient"] is True

    fetched = client.get(f"/v1/keys/{data['hash']}", headers=USER)
    assert fetched.json()["data"]["lenient"] is True
    own = client.get("/v1/key", headers={"authorization": f"Bearer {created.json()['key']}"})
    assert own.status_code == 200, own.text
    assert own.json()["data"]["lenient"] is True


def test_patch_turns_lenient_on_and_off() -> None:
    client = _client()
    created = client.post("/v1/keys", headers=USER, json={"name": "toggle"})
    key_hash = created.json()["data"]["hash"]

    on = client.patch(f"/v1/keys/{key_hash}", headers=USER, json={"lenient": True})
    assert on.status_code == 200, on.text
    assert on.json()["data"]["lenient"] is True
    assert _validate(client, key_hash)["lenient"] is True

    unrelated = client.patch(f"/v1/keys/{key_hash}", headers=USER, json={"name": "renamed"})
    assert unrelated.json()["data"]["lenient"] is True

    off = client.patch(f"/v1/keys/{key_hash}", headers=USER, json={"lenient": False})
    assert off.json()["data"]["lenient"] is False
    assert _validate(client, key_hash)["lenient"] is False


def test_lenient_survives_the_spanner_entity_codec() -> None:
    store, _db = make_fake_store()
    workspace = store.create_workspace("owner", "lenient-codec")
    _raw, key = store.create_api_key(workspace_id=workspace.id, name="k", creator_user_id="owner")
    assert key.lenient is False

    updated = store.update_key(key.hash, {"lenient": True})
    assert updated is not None and updated.lenient is True
    reread = store.get_key_by_hash(key.hash)
    assert reread is not None and reread.lenient is True


def test_patch_without_lenient_leaves_it_alone() -> None:
    store, _db = make_fake_store()
    workspace = store.create_workspace("owner", "lenient-patch")
    _raw, key = store.create_api_key(workspace_id=workspace.id, name="k", creator_user_id="owner")
    key.lenient = True
    apply_key_patch(key, {"lenient": None, "name": "x"})
    assert key.lenient is True


def test_lenient_key_lookup_is_not_counted_as_a_refusal(monkeypatch) -> None:
    from trusted_router.services import gateway_contract_warnings

    reported: list[str] = []
    monkeypatch.setattr(
        gateway_contract_warnings,
        "report_gateway_contract_rejection",
        lambda rejection, **kwargs: reported.append(kwargs["credential_id"]),
    )
    client = _client()
    strict = client.post("/v1/keys", headers=USER, json={"name": "strict"}).json()["data"]
    lenient = client.post("/v1/keys", headers=USER, json={"name": "lenient", "lenient": True}).json()["data"]
    for key in (strict, lenient):
        response = client.post(
            "/v1/internal/gateway/validate",
            json={"api_key_hash": key["hash"], "route_type": "chat.completions", "lenience_probe": True},
        )
        assert response.status_code == 200, response.text
    # A lenient key's request that still failed is reported like any other.
    _validate(client, lenient["hash"])
    assert reported == [strict["hash"], lenient["hash"]]


def test_lenient_survives_federation_to_a_peer_plane() -> None:
    from trusted_router.storage_models import federated_api_key_from_record

    imported = federated_api_key_from_record({"key_hash": "h", "lookup_hash": "l", "workspace_id": "w", "lenient": True})
    assert imported.lenient is True
    assert federated_api_key_from_record({"key_hash": "h", "lookup_hash": "l", "workspace_id": "w"}).lenient is False
