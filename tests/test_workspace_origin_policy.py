from dataclasses import replace

import pytest
from fastapi import HTTPException

from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.services.federation import FederationUnavailable
from trusted_router.storage import STORE
from trusted_router.workspace_origin_policy import COUNTRIES, eligible_candidates


def pair(model_id, operator):
    model = MODELS[model_id]
    return model, replace(endpoints_for_model("openai/gpt-oss-120b")[0], model_id=model_id, provider=operator)


@pytest.mark.parametrize(("model", "operator", "us", "western"), [
    ("openai/gpt-oss-120b", "cerebras", True, True),
    ("qwen/qwen3.8-27b", "cerebras", False, False),
    ("mistralai/mistral-large-4", "deepinfra", False, True),
    ("openai/gpt-oss-120b", "privatemode", False, True),
    ("openai/gpt-oss-120b", "openrouter", False, False),
    ("openai/gpt-oss-120b", "not-a-provider", False, False),
    ("trustedrouter/auto", "anthropic", False, False),
])
def test_model_developer_and_operator(model, operator, us, western):
    candidates = [pair(model, operator)]
    assert eligible_candidates("unrestricted", candidates) is candidates
    assert bool(eligible_candidates("us", candidates)) is us
    assert bool(eligible_candidates("western", candidates)) is western
    assert not eligible_candidates("unknown-policy", candidates)
    assert not eligible_candidates("us", [(MODELS["openai/gpt-oss-120b"], pair("qwen/qwen3.8-27b", operator)[1])])


def test_approved_country_sets():
    assert COUNTRIES["us"] == {"US"}
    assert COUNTRIES["western"] == frozenset(
        "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE "
        "IS LI NO GB CH US CA AU NZ".split()
    )


def test_authorize_and_replay_after_policy_change(client):
    user = STORE.ensure_user("origin-policy@example.com")
    ws = STORE.list_workspaces_for_user(user.id)[0]
    _, key = STORE.create_api_key(workspace_id=ws.id, name="test", creator_user_id=user.id)
    STORE.credit_workspace_once(ws.id, 5_000_000, "origin-test")
    path = "/v1/internal/gateway/authorize"
    body = {"api_key_hash": key.hash, "model": "qwen/qwen3.8-27b",
            "provider": {"only": ["cerebras"]}, "idempotency_key": "origin-before-tightening"}
    original = client.post(path, json=body)
    assert original.status_code == 200, original.text
    STORE.update_workspace(ws.id, origin_policy="us")
    for attempted in [body, {**body, "idempotency_key": "new-attempt"}]:
        denied = client.post(path, json=attempted)
        assert denied.status_code == 403, denied.text
        assert "Workspace origin policy" in denied.text
    allowed = client.post(path, json={"api_key_hash": key.hash, "model": "openai/gpt-oss-120b",
                                      "provider": {"only": ["cerebras"]}})
    assert allowed.status_code == 200, allowed.text
    assert {c["provider"] for c in allowed.json()["data"]["route_candidates"]} == {"cerebras"}

    _, new_key = STORE.create_api_key(workspace_id=ws.id, name="after-change", creator_user_id=user.id)
    denied = client.post(path, json={**body, "api_key_hash": new_key.hash, "idempotency_key": "new-key"})
    assert denied.status_code == 403, denied.text
    mixed = client.post(path, json={
        "api_key_hash": new_key.hash, "model": "qwen/qwen3.8-27b",
        "models": ["openai/gpt-oss-120b"], "provider": {"only": ["cerebras"]},
    })
    assert mixed.status_code == 200, mixed.text
    routes = mixed.json()["data"]["route_candidates"]
    assert {(c["model"], c["provider"]) for c in routes} == {("openai/gpt-oss-120b", "cerebras")}


def test_federation_capability_and_stale_unrestricted_shadow(client, monkeypatch):
    user = STORE.ensure_user("peer-origin@example.com")
    ws = STORE.list_workspaces_for_user(user.id)[0]
    _, key = STORE.create_api_key(workspace_id=ws.id, name="test", creator_user_id=user.id)
    STORE.update_workspace(ws.id, origin_policy="us")
    peer = {"x-trustedrouter-federation-token": "test-secret"}
    monkeypatch.setattr(client.app.state.settings, "federation_peer_token", "test-secret")
    def resolve(headers):
        return client.post("/v1/internal/federation/resolve-key", headers=headers,
                           json={"api_key_lookup_hash": key.lookup_hash})
    assert resolve(peer).status_code == 404
    record = resolve({**peer, "x-trustedrouter-federation-features": "workspace-origin-v1"}).json()["data"]
    assert record["workspace_origin_policy"] == "us"
    shadow = replace(ws, federated_home="peer", origin_policy="unrestricted")
    settings = Settings(environment="test", workspace_origin_peer_checks_enabled=True,
                        federation_home_base_url="https://home.example", federation_home_token=peer["x-trustedrouter-federation-token"])
    class Peer:
        def resolve(self, lookup_hash):
            assert lookup_hash == key.lookup_hash
            return record
    monkeypatch.setattr(gateway, "_federation_client", lambda *_: Peer())
    mixed = [pair("openai/gpt-oss-120b", "cerebras"), pair("qwen/qwen3.8-27b", "cerebras")]
    assert gateway._workspace_origin_candidates(shadow, key, settings, mixed) == mixed[:1]
    with pytest.raises(HTTPException, match="Workspace origin"):
        gateway._workspace_origin_candidates(shadow, key, settings, mixed, replay=True)
    def unavailable(*_):
        raise FederationUnavailable("test outage")
    monkeypatch.setattr(Peer, "resolve", unavailable)
    assert gateway._workspace_origin_candidates(shadow, key, settings, mixed) == mixed[:1]
