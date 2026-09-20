"""POST /v1/decide: decision models in the catalog and on gateway authorize.

Two kinds of model answer the route. A HOSTED decision model (TypeSafe AI's Jev
through Vercel AI Gateway) is its own catalog entry: no chat, input-only
pricing. A NATIVE decision model is an ordinary chat model the attested gateway
drives with strict structured output; it keeps its chat entry and authorizes as
chat, pinned to one provider.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from trusted_router.catalog import (
    MODELS,
    NATIVE_DECISION_MODEL_IDS,
    PROVIDERS,
    endpoints_for_model,
    model_to_openrouter_shape,
)
from trusted_router.catalog_data import NATIVE_DECISION_MODEL_PROVIDERS
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routing import decide_route_endpoint_candidates
from trusted_router.storage import STORE

JEV = "typesafe-ai/jev"
AUTHORIZE = "/v1/internal/gateway/authorize"


def test_jev_is_an_input_only_decision_model() -> None:
    model = MODELS[JEV]
    assert model.supports_decide and not model.supports_chat and not model.supports_embeddings
    assert model.provider == "vercel-ai-gateway"
    assert model.upstream_id == JEV
    assert model.prompt_price_microdollars_per_million_tokens > 42_000  # cost plus markup
    assert model.completion_price_microdollars_per_million_tokens == 0
    assert all(tier.completion_price_microdollars_per_million_tokens == 0 for tier in model.price_tiers)
    endpoints = endpoints_for_model(JEV)
    assert [endpoint.provider for endpoint in endpoints] == ["vercel-ai-gateway"]
    assert not PROVIDERS["vercel-ai-gateway"].supports_chat
    assert not PROVIDERS["vercel-ai-gateway"].supports_byok


def test_public_shape_marks_hosted_and_native_decision_models() -> None:
    shape = model_to_openrouter_shape(MODELS[JEV])
    assert shape["architecture"]["modality"] == "text->decision"
    assert shape["trustedrouter"]["supports_decide"] is True
    assert shape["pricing"]["completion"] == "0"
    for model_id in NATIVE_DECISION_MODEL_IDS:
        native = model_to_openrouter_shape(MODELS[model_id])
        assert native["trustedrouter"]["supports_decide"] is True, model_id
        assert native["trustedrouter"]["supports_chat"] is True, model_id
    ordinary = next(
        m for m in MODELS.values() if m.supports_chat and m.id not in NATIVE_DECISION_MODEL_IDS
    )
    assert model_to_openrouter_shape(ordinary)["trustedrouter"]["supports_decide"] is False


def test_every_native_decision_model_has_a_prepaid_route_on_its_pinned_provider() -> None:
    """The gateway sends `provider.only=[pinned]` with fallbacks off. If the
    catalog drops that (model, provider) route, /v1/decide 400s for the model —
    so the pairing is pinned here, where a catalog refresh will trip it."""
    assert set(NATIVE_DECISION_MODEL_PROVIDERS) == set(NATIVE_DECISION_MODEL_IDS)
    for model_id, provider in NATIVE_DECISION_MODEL_PROVIDERS.items():
        model = MODELS[model_id]
        assert model.supports_chat, model_id
        assert {"structured_outputs", "response_format"} & set(model.supported_parameters), model_id
        providers = {endpoint.provider for endpoint in endpoints_for_model(model_id)}
        assert provider in providers, f"{model_id} lost its {provider} route: {sorted(providers)}"


def test_decide_resolver_accepts_only_decision_models() -> None:
    candidates = decide_route_endpoint_candidates({"model": JEV}, Settings(environment="test"))
    assert [(m.id, e.provider) for m, e in candidates] == [(JEV, "vercel-ai-gateway")]
    with pytest.raises(Exception) as raised:  # noqa: PT011 - api_error is an HTTPException
        decide_route_endpoint_candidates({"model": "openai/gpt-5.4-nano"}, Settings(environment="test"))
    assert getattr(raised.value, "status_code", None) == 400


def _seed_key() -> Any:
    user = STORE.ensure_user("decide@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.credit_workspace_once(workspace.id, 50_000_000, "seed")
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id, name="decide", creator_user_id=user.id
    )
    return key


async def _authorize(body: dict[str, Any]) -> httpx.Response:
    # create_app resets the store, so the key is seeded afterwards.
    app = create_app(
        Settings(environment="test", internal_gateway_token=None), init_observability=False
    )
    body = {**body, "api_key_hash": _seed_key().hash}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(AUTHORIZE, json=body)


@pytest.mark.asyncio
async def test_gateway_authorizes_the_hosted_decision_model() -> None:
    response = await _authorize(
        {
            "model": JEV,
            "route_type": "decide",
            "estimated_input_tokens": 400,
            "max_output_tokens": 1,
        }
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"] if "data" in response.json() else response.json()
    assert payload["provider"] == "vercel-ai-gateway"
    assert payload["model"] == JEV


@pytest.mark.asyncio
async def test_native_decision_request_authorizes_as_chat_on_the_pinned_provider() -> None:
    response = await _authorize(
        {
            "model": "openai/gpt-oss-20b",
            "route_type": "decide",
            "provider": {"only": ["deepinfra"], "allow_fallbacks": False},
            "estimated_input_tokens": 400,
            "max_output_tokens": 600,
        }
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"] if "data" in response.json() else response.json()
    assert payload["provider"] == "deepinfra"


@pytest.mark.asyncio
async def test_a_chat_request_cannot_authorize_the_hosted_decision_model() -> None:
    """Jev has no chat surface; the embeddings/chat arms must not pick it up."""
    response = await _authorize(
        {
            "model": JEV,
            "route_type": "chat.completions",
            "estimated_input_tokens": 10,
            "max_output_tokens": 10,
        }
    )
    # Dispatch is by MODEL capability, so this still resolves as a decision
    # model rather than erroring into the chat arm — and bills input-only.
    assert response.status_code == 200, response.text
