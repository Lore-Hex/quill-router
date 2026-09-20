"""POST /v1/decide: decision models in the catalog and on gateway authorize.

Two kinds of model answer the route. A HOSTED decision model (TypeSafe AI's Jev,
at TypeSafe's own API with Vercel AI Gateway as the failover) is its own catalog
entry: no chat, input-only pricing. A NATIVE decision model is an ordinary chat model the attested gateway
drives with strict structured output; it keeps its chat entry and authorizes as
chat, pinned to one provider.
"""

from __future__ import annotations

from pathlib import Path
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
    NAMED_DECISION_MODELS,
    NATIVE_DECISION_MODEL_PROVIDERS,
    PRIVATE_PROXY_MODEL_TARGETS,
    TREV_1_0_MODEL_ID,
)
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routing import decide_route_endpoint_candidates
from trusted_router.storage import STORE

JEV = "typesafe-ai/jev"
NAMED_IDS = [named.id for named in NAMED_DECISION_MODELS]
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
        # A NAME answers /v1/decide only, so that is what it advertises; the
        # chat model behind it is still a chat model under its own id.
        assert native["trustedrouter"]["supports_chat"] is (model_id not in NAMED_IDS), model_id
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


def test_the_named_models_are_the_five_people_were_promised() -> None:
    # Public model ids are forever once someone hardcodes one.
    assert NAMED_IDS == [
        "trustedrouter/trev-1.0",
        "trustedrouter/gev-1.0",
        "trustedrouter/dev-1.0",
        "trustedrouter/oev-1.0",
        "trustedrouter/mev-1.0",
    ]
    trev_chain = NAMED_DECISION_MODEL_PROVIDERS[TREV_1_0_MODEL_ID]
    assert trev_chain[0] == "cerebras"
    assert len(trev_chain) >= 3, "Cerebras is heavily rate limited; trev needs real fallbacks"


@pytest.mark.parametrize("model_id", NAMED_IDS)
def test_a_named_decision_model_is_priced_from_its_host_chain(model_id: str) -> None:
    named = MODELS[model_id]
    backing = MODELS[PRIVATE_PROXY_MODEL_TARGETS[model_id]]
    chain = NAMED_DECISION_MODEL_PROVIDERS[model_id]
    assert chain[0] == NATIVE_DECISION_MODEL_PROVIDERS[model_id]
    assert named.supports_decide and named.provider == "trustedrouter" and not named.byok_available

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
    assert named.prompt_price_microdollars_per_million_tokens == dearest_prompt
    assert named.completion_price_microdollars_per_million_tokens == dearest_completion
    assert dearest_prompt >= backing.prompt_price_microdollars_per_million_tokens
    assert len(named.price_tiers) == 1, (
        "a name has one price: its chain's, not the backing model's tiers"
    )

    shape = model_to_openrouter_shape(named)
    assert shape["architecture"]["modality"] == "text->decision"
    assert shape["trustedrouter"]["supports_decide"] is True
    public = str(shape).lower()
    backing_slug = backing.id.split("/", 1)[1].lower()
    for hidden in (backing.id.lower(), backing_slug, backing_slug.rsplit("-", 1)[0], *chain):
        assert hidden not in public, f"{hidden!r} leaked into the public entry for {model_id}"


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


async def _named_candidates(model_id: str, provider: dict[str, Any] | None) -> list[str]:
    body: dict[str, Any] = {
        "model": model_id,
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
    assert payload["model"] == PRIVATE_PROXY_MODEL_TARGETS[model_id]
    assert payload["response_model"] == model_id
    assert payload["hide_public_metadata"] is True
    ordered = [payload["provider"]]
    ordered += [
        c["provider"] for c in payload.get("route_candidates", []) if c["provider"] not in ordered
    ]
    return ordered


def _a_host_outside(chain: tuple[str, ...]) -> str:
    return next(host for host in ("deepinfra", "together", "cerebras") if host not in chain)


def _a_served_outsider(model_id: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """A host OUTSIDE the name's chain that really can serve its backing model on
    Credits. Without one, "pinned outside the chain is refused" proves nothing:
    a host that does not serve the model is refused with or without the chain
    guard (that was the case for oev and mev, and the tests passed with the
    guard removed). Where no such host exists, one is added to the catalog for
    the test."""
    from dataclasses import replace

    from trusted_router import catalog

    backing = PRIVATE_PROXY_MODEL_TARGETS[model_id]
    chain = NAMED_DECISION_MODEL_PROVIDERS[model_id]
    serving = [e for e in endpoints_for_model(backing) if not e.is_byok]
    outsiders = sorted({e.provider for e in serving if e.provider not in chain})
    if outsiders:
        return outsiders[0]
    host = _a_host_outside(chain)
    template = next(e for e in serving if e.provider in chain)
    added = replace(template, id=f"{backing}@{host}/prepaid", provider=host)
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", {**catalog.MODEL_ENDPOINTS, added.id: added})
    return host


async def _assert_the_outsider_is_routable(model_id: str, outsider: str) -> None:
    # The control: under its OWN id the backing model routes to the outsider, so
    # a refusal under the NAME can only be the chain.
    response = await _authorize(
        {
            "model": PRIVATE_PROXY_MODEL_TARGETS[model_id],
            "route_type": "chat.completions",
            "estimated_input_tokens": 480,
            "max_output_tokens": 700,
            "provider": {"only": [outsider]},
        }
    )
    assert response.status_code == 200, response.text
    assert response.json().get("data", response.json())["provider"] == outsider


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", NAMED_IDS)
async def test_a_named_model_authorizes_on_its_chain_in_order(model_id: str) -> None:
    chain = list(NAMED_DECISION_MODEL_PROVIDERS[model_id])
    assert await _named_candidates(model_id, None) == chain
    # What the attested gateway sends.
    assert (
        await _named_candidates(
            model_id, {"only": chain, "order": chain, "allow_fallbacks": len(chain) > 1}
        )
        == chain
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", NAMED_IDS)
@pytest.mark.parametrize("preference", ["none", "price", "outsider first"])
async def test_a_named_chain_cannot_be_widened_or_reordered_by_the_request(
    model_id: str, preference: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain is enforced at authorize, not merely requested by the gateway:
    no preference may add a host or promote one over the head of the chain."""
    chain = list(NAMED_DECISION_MODEL_PROVIDERS[model_id])
    outsider = _a_served_outsider(model_id, monkeypatch)
    await _assert_the_outsider_is_routable(model_id, outsider)
    provider: dict[str, Any] | None = {
        "none": None,
        "price": {"sort": "price"},
        "outsider first": {"order": [outsider, chain[-1]], "allow_fallbacks": True},
    }[preference]
    ordered = await _named_candidates(model_id, provider)
    assert ordered == chain[: len(ordered)], ordered
    assert ordered[0] == chain[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", NAMED_IDS)
async def test_a_named_model_refuses_a_request_pinned_outside_its_chain(
    model_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`only` a host outside the chain leaves nothing to route to. Refusing is
    right; serving from outside the chain is not. Asserted on the response
    itself: this used to be an `except AssertionError` around the helper, which
    would also have swallowed a 200 that leaked the backing model."""
    outsider = _a_served_outsider(model_id, monkeypatch)
    await _assert_the_outsider_is_routable(model_id, outsider)
    response = await _authorize(
        {
            "model": model_id,
            "route_type": "decide",
            "estimated_input_tokens": 480,
            "max_output_tokens": 700,
            "provider": {"only": [outsider]},
        }
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "bad_request"
    assert "retry-after" not in response.headers
    assert not STORE.api_keys.reservations
    assert not STORE.api_keys.gateway_authorizations
    assert outsider not in response.text
    assert PRIVATE_PROXY_MODEL_TARGETS[model_id] not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", NAMED_IDS)
@pytest.mark.parametrize("filter_kind", ["ignore_all", "only_then_ignore"])
async def test_excluding_every_named_host_is_not_a_retryable_outage(
    model_id: str, filter_kind: str
) -> None:
    chain = list(NAMED_DECISION_MODEL_PROVIDERS[model_id])
    preferences: dict[str, Any] = {"ignore": chain}
    if filter_kind == "only_then_ignore":
        preferences = {"only": [chain[0]], "ignore": [chain[0]]}
    response = await _authorize(
        {
            "model": model_id,
            "route_type": "decide",
            "provider": preferences,
            "estimated_input_tokens": 480,
            "max_output_tokens": 700,
        }
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "bad_request"
    assert "retry-after" not in response.headers
    assert not STORE.api_keys.reservations
    assert not STORE.api_keys.gateway_authorizations
    assert PRIVATE_PROXY_MODEL_TARGETS[model_id] not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", NAMED_IDS)
async def test_a_genuine_named_host_outage_stays_retryable_and_attributable(
    model_id: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from trusted_router.routes.internal import gateway

    monkeypatch.setattr(gateway, "provider_model_available_from_gateway_region", lambda *_: False)
    response = await _authorize(
        {
            "model": model_id,
            "route_type": "decide",
            "provider": {"only": list(NAMED_DECISION_MODEL_PROVIDERS[model_id])},
            "estimated_input_tokens": 480,
            "max_output_tokens": 700,
        }
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["type"] == "service_unavailable"
    assert response.headers["retry-after"] == "2"
    assert not STORE.api_keys.reservations
    assert not STORE.api_keys.gateway_authorizations
    assert PRIVATE_PROXY_MODEL_TARGETS[model_id] not in response.text
    assert "billing.authorize_named_chain_unavailable" in caplog.text
    assert "workspace_id=" in caplog.text
    assert f"model={model_id}" in caplog.text


# Every way routing rewrites a model string before resolving it. The first
# version of the guard handled the variant suffixes and missed the dated one;
# the combinations are here because the rewrites compose.
NON_CANONICAL_SPELLINGS = [":nitro", ":floor", "-2026-09-19", "-2026-09-19:nitro"]


def test_the_spellings_above_are_all_ones_routing_actually_rewrites() -> None:
    # If routing stops rewriting one of these the tests below would pass for the
    # wrong reason (unknown model -> 400), so pin that each still resolves.
    from trusted_router.routing import canonical_model_id

    for spelling in NON_CANONICAL_SPELLINGS:
        for model_id in (*NAMED_IDS, JEV):
            assert canonical_model_id(model_id + spelling) == model_id, (model_id, spelling)
    for model_id in NAMED_IDS:
        assert canonical_model_id(model_id) == model_id


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", NAMED_IDS)
@pytest.mark.parametrize("suffix", NON_CANONICAL_SPELLINGS)
@pytest.mark.parametrize("route_type", ["chat.completions", "decide"])
async def test_a_routing_variant_cannot_unlock_a_named_decision_model(
    model_id: str, suffix: str, route_type: str
) -> None:
    """`trev-1.0:nitro` is not `trev-1.0` to a string comparison, so the guards
    that key on the model id never fired: the request authorized as plain chat
    on any host, and the response named the backing model."""
    response = await _authorize(
        {
            "model": model_id + suffix,
            "route_type": route_type,
            "estimated_input_tokens": 480,
            "max_output_tokens": 700,
            "provider": {"only": [_a_host_outside(NAMED_DECISION_MODEL_PROVIDERS[model_id])]},
        }
    )
    assert response.status_code == 400, response.text
    # THIS refusal, not any 400: the host named above does not serve every
    # name's chat model, and "no route matches the provider filter" is also a
    # 400 -- one the request earns with the exact-id guard removed.
    assert "exact id" in response.json()["error"]["message"], response.text
    backing = PRIVATE_PROXY_MODEL_TARGETS[model_id]
    assert backing not in response.text
    assert backing.split("/", 1)[1] not in response.text


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


@pytest.mark.parametrize("model_id", NAMED_IDS)
def test_the_preferred_host_still_serves_the_backing_model(model_id: str) -> None:
    # The name sells this host's measured speed. If this fails, a provider
    # delisted the model: re-measure before changing the chain. Production is
    # already serving from the rest of the chain (or answering 503 for this one
    # name), which is why this is a test and not a RuntimeError at import.
    backing = PRIVATE_PROXY_MODEL_TARGETS[model_id]
    preferred = NAMED_DECISION_MODEL_PROVIDERS[model_id][0]
    assert preferred in {
        endpoint.provider for endpoint in endpoints_for_model(backing) if not endpoint.is_byok
    }


def test_a_decision_model_is_not_a_base_for_a_custom_chat_model() -> None:
    from trusted_router.custom_model_rules import (
        is_allowed_custom_model_base,
        require_custom_model_base_model,
    )

    for model_id in (JEV, *NAMED_IDS):
        assert not is_allowed_custom_model_base(MODELS[model_id]), model_id
        with pytest.raises(Exception) as raised:  # noqa: PT011 - api_error is an HTTPException
            require_custom_model_base_model(model_id)
        assert getattr(raised.value, "status_code", None) == 400
    # The chat models the gateway happens to drive as decision functions are
    # ordinary chat models and stay valid bases.
    ordinary = [model_id for model_id in NATIVE_DECISION_MODEL_IDS if model_id not in NAMED_IDS]
    assert ordinary, "fixture: the chat models behind the names should still be listed"
    for model_id in ordinary:
        assert is_allowed_custom_model_base(MODELS[model_id]), model_id


@pytest.mark.parametrize("model_id", NAMED_IDS)
def test_a_named_model_is_advertised_as_available_exactly_when_authorize_can_serve_it(
    model_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named decision model has no endpoints of its own, so GET /v1/models read
    an empty list and said `prepaid_available: false` while authorize served it
    on Credits. A client filtering the catalog on that flag never saw trev."""
    from trusted_router import catalog, catalog_registry

    shape = model_to_openrouter_shape(MODELS[model_id])["trustedrouter"]
    assert shape["prepaid_available"] is True  # type: ignore[index]
    assert shape["byok_available"] is False  # type: ignore[index]

    # ...and it is not a constant: with no chain host left, authorize answers
    # 503 and the catalog must stop advertising it.
    backing = PRIVATE_PROXY_MODEL_TARGETS[model_id]
    chain = NAMED_DECISION_MODEL_PROVIDERS[model_id]
    without_chain = {
        key: endpoint
        for key, endpoint in catalog_registry.MODEL_ENDPOINTS.items()
        if not (endpoint.model_id == backing and endpoint.provider in chain)
    }
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", without_chain)
    dark = model_to_openrouter_shape(MODELS[model_id])["trustedrouter"]
    assert dark["prepaid_available"] is False  # type: ignore[index]


@pytest.mark.parametrize("model_id", NAMED_IDS)
def test_the_control_plane_starts_without_a_named_models_backing_model(model_id: str) -> None:
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

    backing = PRIVATE_PROXY_MODEL_TARGETS[model_id]
    program = textwrap.dedent(
        f"""
        from trusted_router import catalog_ingest

        BACKING = {backing!r}

        removed_from = []

        def without_backing(source, build):
            def filtered():
                models, endpoints = build()
                if BACKING in models:
                    removed_from.append(source)
                return (
                    {{k: v for k, v in models.items() if k != BACKING}},
                    {{k: v for k, v in endpoints.items() if v.model_id != BACKING}},
                )
            return filtered

        # A backing model can come from the shared snapshot or from a provider's
        # own manifest; it has to be gone from both.
        catalog_ingest._ingested_models_and_endpoints = without_backing(
            "snapshot", catalog_ingest._ingested_models_and_endpoints
        )
        catalog_ingest._supplemental_provider_models_and_endpoints = without_backing(
            "supplemental", catalog_ingest._supplemental_provider_models_and_endpoints
        )

        from trusted_router.catalog import MODELS, model_to_openrouter_shape  # the import under test
        assert removed_from, "fixture: the backing model was in neither source"
        assert BACKING not in MODELS, "fixture: the backing model is still in the catalog"
        assert {model_id!r} not in MODELS, "a name is offered without a model behind it"
        assert "typesafe-ai/jev" in MODELS and len(MODELS) > 500
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


def test_a_named_model_is_offered_only_where_it_can_be_used(client: Any) -> None:
    """The chat picker and the custom-model base list read the public shape. A
    name kept `supports_chat` there (it needs it internally, so authorize can
    route its chat model) and was offered in both, which then refused it."""
    picker = {row["id"]: row for row in client.get("/v1/models/picker").json()["data"]}
    for model_id in NAMED_IDS:
        assert picker[model_id]["trustedrouter"]["supports_chat"] is False, model_id
        shape = model_to_openrouter_shape(MODELS[model_id])
        # ...and it lists what /v1/decide takes, not the chat model's parameters.
        assert shape["supported_parameters"] == ["max_tokens", "reasoning"], model_id
    assert picker["openai/gpt-oss-20b"]["trustedrouter"]["supports_chat"] is True


def test_a_named_model_is_never_drawn_as_a_chat_candidate() -> None:
    """The meta-routers (auto, cheap, monitor...) draw from pools of regular chat
    models. A name is a chat model INTERNALLY, and the cheapest one (mev) took its
    provider's slot in the cheap pool -- out of sight only while the pool was cut
    at eight. Drawn, it would have been authorized on a chat route and refused."""
    from trusted_router import routing_candidates

    everything = len(MODELS)
    pool = {model.id for model in routing_candidates.cheap_candidate_models(limit=everything)}
    assert pool, "control: the cheap pool is not empty"
    assert "openai/gpt-oss-20b" in {
        model.id for model in MODELS.values() if routing_candidates._is_regular_chat_model(model)
    }, "control: a plain chat model still counts"
    for model_id in NAMED_IDS:
        assert not routing_candidates._is_regular_chat_model(MODELS[model_id]), model_id
        assert model_id not in pool, model_id


@pytest.mark.parametrize("model_id", NAMED_IDS)
def test_a_named_model_reads_as_a_decision_model_everywhere_it_is_shown(
    model_id: str, client: Any
) -> None:
    shape = model_to_openrouter_shape(MODELS[model_id])
    assert shape["architecture"]["modality"] == "text->decision"
    page = client.get(f"/models/{model_id}")
    assert page.status_code == 200, page.text[:200]
    assert '<span class="pill">chat</span>' not in page.text
    assert '<span class="pill">decide</span>' in page.text


@pytest.mark.parametrize("model_id", [*NAMED_IDS, "typesafe-ai/jev"])
def test_a_decision_models_api_page_shows_the_decide_call(model_id: str, client: Any) -> None:
    """/models/<id>/api ended in `client.chat.completions.create(model=<id>)` for
    every model in the catalog, decision models included."""
    page = client.get(f"/models/{model_id}/api")
    assert page.status_code == 200, page.text[:200]
    assert "chat.completions.create" not in page.text
    assert "/decide" in page.text
    assert f'"model": "{model_id}"' in page.text
    # Control: a chat model's page is unchanged, tuned for /v1/decide or not.
    chat = client.get("/models/openai/gpt-oss-20b/api")
    assert chat.status_code == 200, chat.text[:200]
    assert "chat.completions.create" in chat.text


def test_a_comparison_page_shows_a_call_the_model_accepts(client: Any) -> None:
    """The comparison page ended with `client.chat.completions.create(model=<left>)`
    whatever the left model was: for a decision model, a call that is refused."""
    page = client.get("/compare/models/trustedrouter/mev-1.0/vs/trustedrouter/trev-1.0")
    assert page.status_code == 200, page.text[:200]
    assert "chat.completions.create" not in page.text
    assert "/decide" in page.text
    assert '"model": "trustedrouter/mev-1.0"' in page.text
    # Control: a chat model still gets the chat example, and a tuned chat model
    # is labelled for both routes.
    chat = client.get("/compare/models/openai/gpt-oss-20b/vs/openai/gpt-oss-120b")
    assert chat.status_code == 200, chat.text[:200]
    assert "chat.completions.create" in chat.text
    assert '<span class="pill">chat</span>' in chat.text
    assert '<span class="pill">decide</span>' in chat.text


def test_a_comparison_faq_names_a_call_both_models_accept(client: Any) -> None:
    """Its last answer was "change only the model id" for EVERY pair, which is a
    400 when one is a decision model and the call is chat. Two fixes then denied a
    call some pair DOES share. It now claims only what the flags prove (both chat;
    both take decisions -- authorize drives any chat model on /v1/decide) and
    otherwise promises nothing either way."""
    mixed = client.get("/compare/models/openai/gpt-oss-20b/vs/trustedrouter/oev-1.0").text
    assert "Both take the same request on POST /v1/decide" in mixed
    assert "TrustedRouter Oev 1.0 does not take chat requests." in mixed
    assert "OpenAI-compatible TrustedRouter base URL and API key" not in mixed  # the chat answer
    both_decide = client.get("/compare/models/trustedrouter/mev-1.0/vs/trustedrouter/trev-1.0").text
    assert "Both take the same request on POST /v1/decide" in both_decide
    assert "does not take chat requests" not in both_decide
    both_chat = client.get("/compare/models/openai/gpt-oss-20b/vs/openai/gpt-oss-120b").text
    assert "OpenAI-compatible TrustedRouter base URL and API key" in both_chat
    # Anything else gets no promise in either direction: not "swap the id" (an
    # embedding model beside a decision model), and not "they share no call" (a
    # chat model that also generates images, beside an image model, shares one).
    embedding = next(m.id for m in MODELS.values() if m.supports_embeddings and not m.supports_chat)
    for path in (
        f"/compare/models/{embedding}/vs/trustedrouter/trev-1.0",
        "/compare/models/google/gemini-3.1-flash-image-preview/vs/recraft/recraftv3",
    ):
        page = client.get(path)
        assert page.status_code == 200, (path, page.text[:200])
        assert "At least one of them is not a chat model" in page.text, path
        assert "change only the model id" not in page.text, path
        assert "not with the same call" not in page.text, path


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["openai/gpt-oss-20b", "trustedrouter/archimedes-1.0"])
async def test_any_chat_model_is_accepted_on_the_decide_route(model_id: str) -> None:
    # What the comparison FAQ's "both take /v1/decide" rests on.
    response = await _authorize(
        {
            "model": model_id,
            "route_type": "decide",
            "estimated_input_tokens": 300,
            "max_output_tokens": 300,
        }
    )
    assert response.status_code == 200, response.text


def test_a_deep_link_cannot_put_a_non_chat_model_in_a_chat() -> None:
    """/chat?model=<id> selects the model before the catalog is loaded and never
    looked again, so the picker's filter did not apply to it. (Behavior checked
    in a browser; this pins the pieces, in this repo's idiom for static scripts.)"""
    js = Path("src/trusted_router/static/chat.js").read_text()
    load = js[js.index("async function loadModels()") :]
    load = load[: load.index("function dropModelsThatCannotChat()")]
    assert "dropModelsThatCannotChat();" in load, "revalidate once the catalog is known"
    drop = js[js.index("function dropModelsThatCannotChat()") :]
    drop = drop[: drop.index("function normalizeModel(raw)")]
    assert "row.supports_chat !== false" in drop
    # NOT DEFAULT_MODEL_ID: with a deep link, that is the model being refused.
    assert "slot.model_id = FALLBACK_CHAT_MODEL_ID;" in drop
    assert "DEFAULT_MODEL_ID" not in drop
    assert 'const FALLBACK_CHAT_MODEL_ID = "trustedrouter/plato";' in js
    assert "URL_MODEL_CANNOT_CHAT ? FALLBACK_CHAT_MODEL_ID : DEFAULT_MODEL_ID" in js


def test_the_playgrounds_offer_only_chat_models() -> None:
    """/chat and /synth call chat completions and nothing else, and they read
    the picker projection. Both dropped `internal_only` rows and kept the rest,
    so every embedding, image, video and decision model was selectable and then
    refused. (Source assertions are this repo's idiom for its static scripts.)"""
    static = Path("src/trusted_router/static")
    for script in ("chat.js", "fusion.js"):
        source = (static / script).read_text()
        picker = source[source.index("function renderModelPicker()") :]
        picker = picker[: picker.index("PICKER_FILTERS.vision")]
        assert "supports_chat === false) return false" in picker, script
    # chat.js normalizes rows itself when the shared catalog script is absent. A
    # row without the flag reads `undefined`, which the filter lets through.
    chat = (static / "chat.js").read_text()
    fallback = chat[chat.index("function normalizeModel(raw)") :]
    fallback = fallback[: fallback.index("Heuristic capability detection")]
    assert "supports_chat: ext.supports_chat !== false" in fallback
    shared = (static / "model_catalog.js").read_text()
    assert "supports_chat: ext.supports_chat !== false" in shared
