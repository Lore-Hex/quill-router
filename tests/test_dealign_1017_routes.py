import json
from datetime import UTC, datetime

import pytest

from scripts.pricing.providers import venice
from trusted_router.authorize_candidates import CandidateRequest, plan_route, resolve_candidates
from trusted_router.catalog import MODELS
from trusted_router.config import Settings
from trusted_router.provider_contracts import (
    DEALIGN_1017_SOURCE,
    MAPLE_MODEL_ID,
    MAPLE_NATIVE_ID,
    VENICE_1017_LEGACY_ID,
)
from trusted_router.routes.internal.gateway import _endpoint_for_id_compat
from trusted_router.routing import canonical_model_id


def test_both_1017_spellings_expose_same_route_contracts(client):
    assert venice._model_id(MAPLE_NATIVE_ID) == MAPLE_MODEL_ID
    assert VENICE_1017_LEGACY_ID not in MODELS
    assert canonical_model_id(VENICE_1017_LEGACY_ID + ":nitro") == MAPLE_MODEL_ID
    assert canonical_model_id("deepseek/deepseek-v4-1-flash-uncensored") != MAPLE_MODEL_ID
    responses = [client.get(f"/v1/models/{name}/endpoints") for name in (MAPLE_MODEL_ID, VENICE_1017_LEGACY_ID)]
    assert all(r.status_code == 200 for r in responses)
    assert responses[0].json() == responses[1].json()
    routes = responses[0].json()["data"]
    assert {(r["provider"], r["usage_type"]) for r in routes} == {
        ("maple", "Credits"), ("venice", "Credits"), ("venice", "BYOK"),
    }
    assert {r["upstream_id"] for r in routes} == {MAPLE_NATIVE_ID}


@pytest.mark.parametrize("model", [MAPLE_MODEL_ID, VENICE_1017_LEGACY_ID])
@pytest.mark.parametrize(("fields", "expected"), [
    ({"provider": {"only": ["maple"], "allow_fallbacks": False}}, {"maple"}),
    ({"provider": {"only": ["venice"], "allow_fallbacks": False}}, {"venice"}),
    ({"provider": {"min_privacy": "confidential"}}, {"maple"}),
    ({"input_modalities": ["text", "image"]}, {"venice"}),
    ({"requested_parameters": ["tools"]}, {"venice"}),
])
def test_route_contracts_for_both_spellings(model, fields, expected):
    settings = Settings(environment="test")
    request = CandidateRequest(route_type="chat.completions", route_model_id=model,
                               routing_body={"model": model, **fields}, body_region="us-central1")
    plan = plan_route(request, settings, region="us-central1")
    result = resolve_candidates(plan, request, region="us-central1", at=datetime.now(UTC))
    assert {e.provider for _, e in result.candidates} == expected


@pytest.mark.parametrize("tier", ["prepaid", "byok"])
def test_legacy_venice_settlement_keeps_the_authorized_identity_and_price(tier):
    old_id = f"{VENICE_1017_LEGACY_ID}@venice/{tier}"
    old = _endpoint_for_id_compat(old_id)
    new = _endpoint_for_id_compat(f"{MAPLE_MODEL_ID}@venice/{tier}")
    assert old and new
    assert (old.id, old.model_id, old.provider, old.upstream_id) == (
        old_id, VENICE_1017_LEGACY_ID, "venice", MAPLE_NATIVE_ID,
    )
    assert old.price_tiers == new.price_tiers
    assert old.usage_type == new.usage_type


@pytest.mark.parametrize("source", [DEALIGN_1017_SOURCE, None, "https://huggingface.co/deepseek-ai/stock"])
def test_venice_manifest_requires_matching_source_only_for_1017(monkeypatch, tmp_path, source):
    path = tmp_path / "venice.json"
    monkeypatch.setattr(venice, "MANIFEST_PATH", path)
    monkeypatch.setattr(venice, "UPSTREAM_ID_MAP", dict(venice.UPSTREAM_ID_MAP))
    monkeypatch.setattr(venice, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setenv("VENICE_API_KEY", "test-key")
    spec = {"pricing": {"input": {"usd": "1"}, "output": {"usd": "2"}}}
    payload = {"data": [
        {"id": "zai-org-glm-5-2", "model_spec": spec},
        {"id": MAPLE_NATIVE_ID, "model_spec": {**spec, "modelSource": DEALIGN_1017_SOURCE}},
    ]}
    monkeypatch.setattr(venice, "fetch_json", lambda *args, **kwargs: payload)
    venice.write_provider_manifest(venice.fetch())
    payload["data"][1]["model_spec"]["modelSource"] = source
    venice.write_provider_manifest(venice.fetch())
    rows = {row["id"]: row for row in json.loads(path.read_text())["models"]}
    assert rows["z-ai/glm-5.2"].get("routable", True)
    assert rows[MAPLE_MODEL_ID].get("routable", True) is (source == DEALIGN_1017_SOURCE)
    if source != DEALIGN_1017_SOURCE:
        assert rows[MAPLE_MODEL_ID]["routable_reason"] == "checkpoint-source-unverified"
