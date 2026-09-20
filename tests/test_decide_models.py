"""POST /v1/decide: decision models in the catalog and on gateway authorize.

Two kinds of model answer the route. A HOSTED decision model (TypeSafe AI's Jev,
at TypeSafe's own API with Vercel AI Gateway as the failover) is its own catalog
entry: no chat, input-only pricing. A NATIVE decision model is an ordinary chat model the attested gateway
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
from trusted_router.catalog_data import (
    NAMED_DECISION_MODEL_PROVIDERS,
    NATIVE_DECISION_MODEL_PROVIDERS,
    PRIVATE_PROXY_MODEL_TARGETS,
    TREV_1_0_MODEL_ID,
)
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routing import decide_route_endpoint_candidates
from trusted_router.storage import STORE

JEV = "typesafe-ai/jev"
AUTHORIZE = "/v1/internal/gateway/authorize"


JEV_HOSTS = [("typesafe", "jev-latest"), ("vercel-ai-gateway", JEV)]


def test_jev_is_an_input_only_decision_model() -> None:
    model = MODELS[JEV]
    assert model.supports_decide and not model.supports_chat and not model.supports_embeddings
    assert model.provider == "typesafe"
    assert model.upstream_id == "jev-latest"
    assert model.prompt_price_microdollars_per_million_tokens > 42_000  # cost plus markup
    assert model.completion_price_microdollars_per_million_tokens == 0
    assert all(
        tier.completion_price_microdollars_per_million_tokens == 0 for tier in model.price_tiers
    )


def test_jev_runs_at_its_vendor_with_the_relay_as_a_priced_fallback() -> None:
    endpoints = endpoints_for_model(JEV)
    # Each host is called by ITS name for the model: the vendor's alias is not
    # the relay's id, and sending one to the other is a 404 on every request.
    assert [(e.provider, e.upstream_id) for e in endpoints] == JEV_HOSTS
    for endpoint in endpoints:
        assert endpoint.usage_type == "Credits" and not endpoint.is_byok
        # The host that serves a request bills it, so EACH endpoint carries a
        # real input price and meters no output.
        assert endpoint.prompt_price_microdollars_per_million_tokens > 42_000, endpoint.id
        assert endpoint.completion_price_microdollars_per_million_tokens == 0, endpoint.id
        assert not PROVIDERS[endpoint.provider].supports_chat
        assert not PROVIDERS[endpoint.provider].supports_byok
    # No ZDR is configured on our TypeSafe account; the catalog must not claim it.
    assert not PROVIDERS["typesafe"].provider_zero_data_retention
    assert not PROVIDERS["typesafe"].prepaid_zero_data_retention


def test_every_decision_fallback_route_became_an_endpoint() -> None:
    # A fallback on a provider missing from PROVIDERS or the prepaid set is
    # skipped by the builder. Silently losing the failover is the failure this
    # guards: the vendor has an outage and there is nowhere to go.
    from trusted_router.catalog_data import _DECISION_SPECS

    for spec in _DECISION_SPECS:
        served = {(e.provider, e.upstream_id) for e in endpoints_for_model(spec["id"])}
        assert (spec["provider"], spec["upstream_id"]) in served
        assert spec["fallback_routes"], f"{spec['id']} has no failover host"
        for route in spec["fallback_routes"]:
            assert (route["provider"], route["upstream_id"]) in served, route


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
    """The gateway asks for the pinned host with fallbacks off. If the catalog
    drops that (model, provider) route, /v1/decide 400s for the model -- so the
    pairing is pinned here, where a catalog refresh will trip it."""
    assert set(NATIVE_DECISION_MODEL_PROVIDERS) == set(NATIVE_DECISION_MODEL_IDS)
    for model_id, provider in NATIVE_DECISION_MODEL_PROVIDERS.items():
        assert MODELS[model_id].supports_chat, model_id
        backing = PRIVATE_PROXY_MODEL_TARGETS.get(model_id, model_id)
        providers = {e.provider for e in endpoints_for_model(backing) if not e.is_byok}
        assert provider in providers, f"{model_id} lost its {provider} route: {sorted(providers)}"


def test_trev_is_a_named_decision_model_priced_from_its_host_chain() -> None:
    trev = MODELS[TREV_1_0_MODEL_ID]
    backing = MODELS[PRIVATE_PROXY_MODEL_TARGETS[TREV_1_0_MODEL_ID]]
    chain = NAMED_DECISION_MODEL_PROVIDERS[TREV_1_0_MODEL_ID]
    assert chain[0] == NATIVE_DECISION_MODEL_PROVIDERS[TREV_1_0_MODEL_ID] == "cerebras"
    assert len(chain) >= 3, "Cerebras is heavily rate limited; the name needs real fallbacks"
    assert trev.supports_decide and trev.provider == "trustedrouter" and not trev.byok_available

    # Each request bills at the serving host's rate, so the honest public price
    # is the DEAREST host in the chain -- not the backing model's cheapest host.
    chain_endpoints = [
        e for e in endpoints_for_model(backing.id) if e.provider in chain and not e.is_byok
    ]
    assert {e.provider for e in chain_endpoints} == set(chain), "a chained host lost the model"
    dearest_prompt = max(e.prompt_price_microdollars_per_million_tokens for e in chain_endpoints)
    dearest_completion = max(
        e.completion_price_microdollars_per_million_tokens for e in chain_endpoints
    )
    assert trev.prompt_price_microdollars_per_million_tokens == dearest_prompt
    assert trev.completion_price_microdollars_per_million_tokens == dearest_completion
    assert dearest_prompt > backing.prompt_price_microdollars_per_million_tokens

    shape = model_to_openrouter_shape(trev)
    assert shape["architecture"]["modality"] == "text->decision"
    assert shape["trustedrouter"]["supports_decide"] is True
    public = str(shape).lower()
    for hidden in ("gpt-oss", *chain):
        assert hidden not in public, f"{hidden!r} leaked into the public catalog entry"


def test_decide_resolver_accepts_only_decision_models() -> None:
    candidates = decide_route_endpoint_candidates({"model": JEV}, Settings(environment="test"))
    # Vendor first by default, relay second: the order IS the failover plan.
    assert [(m.id, e.provider) for m, e in candidates] == [
        (JEV, "typesafe"),
        (JEV, "vercel-ai-gateway"),
    ]
    with pytest.raises(Exception) as raised:  # noqa: PT011 - api_error is an HTTPException
        decide_route_endpoint_candidates(
            {"model": "openai/gpt-5.4-nano"}, Settings(environment="test")
        )
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
    assert payload["provider"] == "typesafe"
    assert payload["model"] == JEV
    # The gateway fails over by walking this list, so both hosts must be in
    # it, in order, each with the upstream id that host understands.
    assert [
        (candidate["provider"], candidate["upstream_model"])
        for candidate in payload["route_candidates"]
    ] == JEV_HOSTS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        # Default and an explicit vendor-first order agree.
        (None, ["typesafe", "vercel-ai-gateway"]),
        ({"order": ["typesafe", "vercel-ai-gateway"]}, ["typesafe", "vercel-ai-gateway"]),
        # A caller's explicit preference still beats the default ranking.
        ({"order": ["vercel-ai-gateway"]}, ["vercel-ai-gateway", "typesafe"]),
        ({"only": ["vercel-ai-gateway"]}, ["vercel-ai-gateway"]),
        ({"only": ["typesafe"]}, ["typesafe"]),
        ({"ignore": ["typesafe"]}, ["vercel-ai-gateway"]),
    ],
)
async def test_jev_host_order_follows_the_default_then_the_caller(
    provider: dict[str, Any] | None, expected: list[str]
) -> None:
    body: dict[str, Any] = {
        "model": JEV,
        "route_type": "decide",
        "estimated_input_tokens": 400,
        "max_output_tokens": 1,
    }
    if provider is not None:
        body["provider"] = provider
    response = await _authorize(body)
    assert response.status_code == 200, response.text
    payload = response.json().get("data", response.json())
    assert [c["provider"] for c in payload["route_candidates"]] == expected
    assert payload["provider"] == expected[0]


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
@pytest.mark.parametrize("model_id", [JEV, TREV_1_0_MODEL_ID])
@pytest.mark.parametrize("route_type", ["chat.completions", "responses", "embeddings", None])
async def test_a_decision_model_answers_only_the_decide_route(
    model_id: str, route_type: str | None
) -> None:
    """Jev has no chat surface, and trev-1.0 over chat would silently be a plain
    alias for its backing model on the cheapest host -- not what the name sells."""
    body: dict[str, Any] = {
        "model": model_id,
        "estimated_input_tokens": 10,
        "max_output_tokens": 10,
    }
    if route_type is not None:
        body["route_type"] = route_type
    response = await _authorize(body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "model_not_supported"
    assert "POST /v1/decide" in response.json()["error"]["message"]


async def _trev_candidates(provider: dict[str, Any] | None) -> list[str]:
    body: dict[str, Any] = {
        "model": TREV_1_0_MODEL_ID,
        "route_type": "decide",
        "estimated_input_tokens": 480,
        "max_output_tokens": 700,
    }
    if provider is not None:
        body["provider"] = provider
    response = await _authorize(body)
    assert response.status_code == 200, response.text
    payload = response.json().get("data", response.json())
    # Billing uses the concrete model; the caller is shown the name only.
    assert payload["model"] == PRIVATE_PROXY_MODEL_TARGETS[TREV_1_0_MODEL_ID]
    assert payload["response_model"] == TREV_1_0_MODEL_ID
    assert payload["hide_public_metadata"] is True
    ordered = [payload["provider"]]
    ordered += [
        c["provider"] for c in payload.get("route_candidates", []) if c["provider"] not in ordered
    ]
    return ordered


@pytest.mark.asyncio
async def test_trev_authorizes_on_its_chain_in_order() -> None:
    chain = list(NAMED_DECISION_MODEL_PROVIDERS[TREV_1_0_MODEL_ID])
    # What the attested gateway sends.
    assert await _trev_candidates({"only": chain, "order": chain, "allow_fallbacks": True}) == chain


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    [
        None,
        {"sort": "price"},
        {"order": ["deepinfra", "together"], "allow_fallbacks": True},
    ],
)
async def test_trev_chain_cannot_be_widened_or_reordered_by_the_request(
    provider: dict[str, Any] | None,
) -> None:
    """The chain is enforced at authorize, not merely requested by the gateway:
    no preference may add a slow host or promote one over Cerebras."""
    chain = list(NAMED_DECISION_MODEL_PROVIDERS[TREV_1_0_MODEL_ID])
    ordered = await _trev_candidates(provider)
    assert ordered == chain[: len(ordered)], ordered
    assert ordered[0] == "cerebras"


@pytest.mark.asyncio
async def test_trev_refuses_a_request_pinned_outside_its_chain() -> None:
    """`only` a host outside the chain leaves nothing to route to. Refusing is
    right; serving from outside the chain is not. Asserted on the response
    itself: this used to be an `except AssertionError` around the helper, which
    would also have swallowed a 200 that leaked the backing model."""
    response = await _authorize(
        {
            "model": TREV_1_0_MODEL_ID,
            "route_type": "decide",
            "estimated_input_tokens": 480,
            "max_output_tokens": 700,
            "provider": {"only": ["deepinfra"]},
        }
    )
    assert response.status_code in {400, 503}, response.text
    assert "deepinfra" not in response.text
    assert PRIVATE_PROXY_MODEL_TARGETS[TREV_1_0_MODEL_ID] not in response.text


# Every way routing rewrites a model string before resolving it. The first
# version of the guard handled the variant suffixes and missed the dated one;
# the combinations are here because the rewrites compose.
NON_CANONICAL_SPELLINGS = [":nitro", ":floor", "-2026-09-19", "-2026-09-19:nitro"]


def test_the_spellings_above_are_all_ones_routing_actually_rewrites() -> None:
    # If routing stops rewriting one of these the tests below would pass for the
    # wrong reason (unknown model -> 400), so pin that each still resolves.
    from trusted_router.routing import canonical_model_id

    for spelling in NON_CANONICAL_SPELLINGS:
        assert canonical_model_id(TREV_1_0_MODEL_ID + spelling) == TREV_1_0_MODEL_ID, spelling
        assert canonical_model_id(JEV + spelling) == JEV, spelling
    assert canonical_model_id(TREV_1_0_MODEL_ID) == TREV_1_0_MODEL_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", NON_CANONICAL_SPELLINGS)
@pytest.mark.parametrize("route_type", ["chat.completions", "decide"])
async def test_a_routing_variant_cannot_unlock_a_named_decision_model(
    suffix: str, route_type: str
) -> None:
    """`trev-1.0:nitro` is not `trev-1.0` to a string comparison, so the guards
    that key on the model id never fired: the request authorized as plain chat
    on any host, and the response named the backing model."""
    response = await _authorize(
        {
            "model": TREV_1_0_MODEL_ID + suffix,
            "route_type": route_type,
            "estimated_input_tokens": 480,
            "max_output_tokens": 700,
            "provider": {"only": ["deepinfra"]},
        }
    )
    assert response.status_code == 400, response.text
    assert PRIVATE_PROXY_MODEL_TARGETS[TREV_1_0_MODEL_ID] not in response.text
    assert "gpt-oss" not in response.text


@pytest.mark.asyncio
async def test_a_routing_variant_cannot_unmask_any_private_proxy_model() -> None:
    # The same hole, older than trev: every private proxy keyed on the raw id.
    for model_id, backing in PRIVATE_PROXY_MODEL_TARGETS.items():
        route_type = "decide" if MODELS[model_id].supports_decide else "chat.completions"
        for spelling in NON_CANONICAL_SPELLINGS:
            response = await _authorize(
                {
                    "model": model_id + spelling,
                    "route_type": route_type,
                    "estimated_input_tokens": 100,
                    "max_output_tokens": 100,
                }
            )
            assert response.status_code == 400, (model_id, spelling, response.text)
            assert "exact id" in response.text, (model_id, spelling, response.text)
            assert backing not in response.text, (model_id, spelling)


@pytest.mark.asyncio
async def test_a_routing_variant_in_a_fallback_array_is_still_a_private_proxy() -> None:
    response = await _authorize(
        {
            "model": "openai/gpt-oss-20b",
            "models": [TREV_1_0_MODEL_ID + ":floor"],
            "route_type": "chat.completions",
            "estimated_input_tokens": 100,
            "max_output_tokens": 100,
        }
    )
    assert response.status_code == 400, response.text
    dated = await _authorize(
        {
            "model": "openai/gpt-oss-20b",
            "models": [TREV_1_0_MODEL_ID + "-2026-09-19"],
            "route_type": "chat.completions",
            "estimated_input_tokens": 100,
            "max_output_tokens": 100,
        }
    )
    assert dated.status_code == 400, dated.text


@pytest.mark.asyncio
async def test_ordinary_models_keep_their_variants_and_dated_spellings() -> None:
    # The refusal is for pinned models only. Everyone else's `:nitro` still works.
    response = await _authorize(
        {
            "model": "openai/gpt-oss-20b:nitro",
            "route_type": "chat.completions",
            "estimated_input_tokens": 100,
            "max_output_tokens": 100,
        }
    )
    assert response.status_code == 200, response.text


def test_a_host_delisting_the_backing_model_never_stops_the_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chain pricing runs at import. It used to raise when the preferred host
    stopped serving the backing model, so one provider's hourly manifest refresh
    could keep the whole control plane from starting."""
    from trusted_router import catalog_registry

    backing = PRIVATE_PROXY_MODEL_TARGETS[TREV_1_0_MODEL_ID]
    chain = NAMED_DECISION_MODEL_PROVIDERS[TREV_1_0_MODEL_ID]
    live = dict(catalog_registry.MODEL_ENDPOINTS)

    def without(*providers: str) -> dict[str, Any]:
        return {
            key: endpoint
            for key, endpoint in live.items()
            if not (endpoint.model_id == backing and endpoint.provider in providers)
        }

    def chain_prices(endpoints: dict[str, Any]) -> list[int]:
        return [
            e.prompt_price_microdollars_per_million_tokens
            for e in endpoints.values()
            if e.model_id == backing and e.provider in chain and not e.is_byok
        ]

    # The preferred host is gone: still priced, from the hosts that remain.
    monkeypatch.setattr(catalog_registry, "MODEL_ENDPOINTS", without(chain[0]))
    degraded = catalog_registry._named_decision_model_with_chain_prices(TREV_1_0_MODEL_ID)
    remaining = chain_prices(without(chain[0]))
    assert remaining, "fixture: the chain needs more than one host for this to mean anything"
    assert degraded.prompt_price_microdollars_per_million_tokens == max(remaining)

    # Every chain host is gone: no price to read, and still no exception.
    monkeypatch.setattr(catalog_registry, "MODEL_ENDPOINTS", without(*chain))
    unroutable = catalog_registry._named_decision_model_with_chain_prices(TREV_1_0_MODEL_ID)
    assert unroutable == MODELS[TREV_1_0_MODEL_ID]


def test_the_preferred_host_still_serves_the_backing_model() -> None:
    # The name sells this host's speed. If this fails, a provider delisted the
    # model: re-measure the chain before reordering it. Production is already
    # serving from the rest of the chain, which is why this is a test and not a
    # RuntimeError at import.
    backing = PRIVATE_PROXY_MODEL_TARGETS[TREV_1_0_MODEL_ID]
    preferred = NAMED_DECISION_MODEL_PROVIDERS[TREV_1_0_MODEL_ID][0]
    assert preferred in {
        endpoint.provider for endpoint in endpoints_for_model(backing) if not endpoint.is_byok
    }


def test_a_decision_model_is_not_a_base_for_a_custom_chat_model() -> None:
    from trusted_router.custom_model_rules import (
        is_allowed_custom_model_base,
        require_custom_model_base_model,
    )

    for model_id in (JEV, TREV_1_0_MODEL_ID):
        assert not is_allowed_custom_model_base(MODELS[model_id]), model_id
        with pytest.raises(Exception) as raised:  # noqa: PT011 - api_error is an HTTPException
            require_custom_model_base_model(model_id)
        assert getattr(raised.value, "status_code", None) == 400
    # The chat models the gateway happens to drive as decision functions are
    # ordinary chat models and stay valid bases.
    for model_id in NATIVE_DECISION_MODEL_IDS:
        if model_id != TREV_1_0_MODEL_ID:
            assert is_allowed_custom_model_base(MODELS[model_id]), model_id


def test_trev_is_advertised_as_available_exactly_when_authorize_can_serve_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named decision model has no endpoints of its own, so GET /v1/models read
    an empty list and said `prepaid_available: false` while authorize served it
    on Credits. A client filtering the catalog on that flag never saw trev."""
    from trusted_router import catalog, catalog_registry

    shape = model_to_openrouter_shape(MODELS[TREV_1_0_MODEL_ID])["trustedrouter"]
    assert shape["prepaid_available"] is True  # type: ignore[index]
    assert shape["byok_available"] is False  # type: ignore[index]

    # ...and it is not a constant: with no chain host left, authorize answers
    # 503 and the catalog must stop advertising it.
    backing = PRIVATE_PROXY_MODEL_TARGETS[TREV_1_0_MODEL_ID]
    chain = NAMED_DECISION_MODEL_PROVIDERS[TREV_1_0_MODEL_ID]
    without_chain = {
        key: endpoint
        for key, endpoint in catalog_registry.MODEL_ENDPOINTS.items()
        if not (endpoint.model_id == backing and endpoint.provider in chain)
    }
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", without_chain)
    dark = model_to_openrouter_shape(MODELS[TREV_1_0_MODEL_ID])["trustedrouter"]
    assert dark["prepaid_available"] is False  # type: ignore[index]


def test_the_control_plane_starts_without_trevs_backing_model() -> None:
    """The catalog is rebuilt at import from data an hourly job refreshes. If the
    backing model left it ALTOGETHER, `MODELS[backing]` in the trev clone raised
    KeyError and nothing started. The earlier delisting test removed endpoints
    AFTER a successful import, so it could not see this. A fresh interpreter is
    the only honest way to test import, and the only one that does not poison
    the modules the rest of the suite shares."""
    import os
    import subprocess
    import sys
    import textwrap

    backing = PRIVATE_PROXY_MODEL_TARGETS[TREV_1_0_MODEL_ID]
    program = textwrap.dedent(
        f"""
        from trusted_router import catalog_ingest

        BACKING = {backing!r}

        def without_backing(build):
            def filtered():
                models, endpoints = build()
                assert BACKING in models, "fixture: nothing to remove"
                return (
                    {{k: v for k, v in models.items() if k != BACKING}},
                    {{k: v for k, v in endpoints.items() if v.model_id != BACKING}},
                )
            return filtered

        catalog_ingest._ingested_models_and_endpoints = without_backing(
            catalog_ingest._ingested_models_and_endpoints
        )
        supplemental = catalog_ingest._supplemental_provider_models_and_endpoints
        def supplemental_without_backing():
            models, endpoints = supplemental()
            return (
                {{k: v for k, v in models.items() if k != BACKING}},
                {{k: v for k, v in endpoints.items() if v.model_id != BACKING}},
            )
        catalog_ingest._supplemental_provider_models_and_endpoints = supplemental_without_backing

        from trusted_router.catalog import MODELS, model_to_openrouter_shape  # the import under test
        assert BACKING not in MODELS, "fixture: the backing model is still in the catalog"
        assert {TREV_1_0_MODEL_ID!r} not in MODELS, "trev is offered without a model behind it"
        assert "typesafe-ai/jev" in MODELS and "openai/gpt-oss-20b" in MODELS
        print("STARTED", len(MODELS))
        """
    )
    result = subprocess.run(  # noqa: S603 - fixed argv: this interpreter, a literal program
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONPATH": "src"},
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1500:]
    assert "STARTED" in result.stdout
