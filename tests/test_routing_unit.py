"""Unit-level coverage of routing.py — the chat-candidate selection
machinery sits between the request body and the inference runners and
its branches matter for billing correctness (wrong candidate = wrong
provider charged).

The integration tests in test_auth_and_routing exercise the happy
paths through the FastAPI app; these tests pin the edge cases:
provider order/only/ignore/sort/data_collection precedence, fallback
gating, model-id validation."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from trusted_router.catalog import PROVIDER_JURISDICTION_US
from trusted_router.config import Settings
from trusted_router.routing import (
    _sort_endpoint_candidates,
    catalog_endpoint_candidates,
    chat_route_candidates,
    chat_route_endpoint_candidates,
    embeddings_route_endpoint_candidates,
    provider_route_preferences,
)


def _settings() -> Settings:
    return Settings(environment="test")


# ── chat_route_candidates ───────────────────────────────────────────────


def test_chat_route_candidates_explicit_model_yields_one_candidate() -> None:
    candidates = chat_route_candidates(
        {"model": "openai/gpt-5.4-nano"},
        _settings(),
    )
    assert [c.id for c in candidates] == ["openai/gpt-5.4-nano"]


def test_chat_route_candidates_models_array_dedupes_and_preserves_order() -> None:
    candidates = chat_route_candidates(
        {
            "model": "openai/gpt-5.4-nano",
            "models": [
                "openai/gpt-5.4-nano",  # dup with primary, must be dropped
                "mistralai/mistral-small-2603",
                "openai/gpt-5.4-nano",  # second dup
            ],
        },
        _settings(),
    )
    assert [c.id for c in candidates] == [
        "openai/gpt-5.4-nano",
        "mistralai/mistral-small-2603",
    ]


def test_chat_route_candidates_unknown_model_raises_model_not_supported() -> None:
    with pytest.raises(HTTPException) as ctx:
        chat_route_candidates({"model": "skunk/skunkworks"}, _settings())
    assert ctx.value.status_code == 400
    assert ctx.value.detail["error"]["type"] == "model_not_supported"


def test_chat_route_candidates_provider_only_filter_keeps_matching_only() -> None:
    candidates = chat_route_candidates(
        {
            "models": [
                "openai/gpt-5.4-nano",
                "mistralai/mistral-small-2603",
                "anthropic/claude-sonnet-4.6",
            ],
            "provider": {"only": ["mistral", "anthropic"]},
        },
        _settings(),
    )
    assert {c.provider for c in candidates} == {"mistral", "anthropic"}
    assert all(c.provider != "openai" for c in candidates)


def test_chat_route_candidates_provider_ignore_filter_drops_matching() -> None:
    candidates = chat_route_candidates(
        {
            "models": [
                "openai/gpt-5.4-nano",
                "mistralai/mistral-small-2603",
                "anthropic/claude-sonnet-4.6",
            ],
            "provider": {"ignore": ["openai"]},
        },
        _settings(),
    )
    assert all(c.provider != "openai" for c in candidates)
    assert {c.provider for c in candidates} == {"mistral", "anthropic"}


def test_chat_route_candidates_only_and_ignore_combine() -> None:
    """only first, then ignore — ignore takes precedence on overlap."""
    candidates = chat_route_candidates(
        {
            "models": [
                "openai/gpt-5.4-nano",
                "mistralai/mistral-small-2603",
                "anthropic/claude-sonnet-4.6",
            ],
            "provider": {"only": ["openai", "mistral"], "ignore": ["openai"]},
        },
        _settings(),
    )
    assert [c.id for c in candidates] == ["mistralai/mistral-small-2603"]


def test_chat_route_candidates_filter_eliminates_all_raises() -> None:
    with pytest.raises(HTTPException) as ctx:
        chat_route_candidates(
            {
                "models": ["openai/gpt-5.4-nano"],
                "provider": {"only": ["mistral"]},
            },
            _settings(),
        )
    assert ctx.value.status_code == 400
    assert "filters" in ctx.value.detail["error"]["message"].lower()


def test_chat_route_candidates_provider_order_reorders() -> None:
    candidates = chat_route_candidates(
        {
            "models": [
                "openai/gpt-5.4-nano",
                "mistralai/mistral-small-2603",
                "anthropic/claude-sonnet-4.6",
            ],
            "provider": {"order": ["mistral", "anthropic", "openai"]},
        },
        _settings(),
    )
    assert [c.provider for c in candidates] == ["mistral", "anthropic", "openai"]


def test_chat_route_candidates_allow_fallbacks_false_returns_only_head() -> None:
    candidates = chat_route_candidates(
        {
            "models": [
                "openai/gpt-5.4-nano",
                "mistralai/mistral-small-2603",
                "anthropic/claude-sonnet-4.6",
            ],
            "provider": {"allow_fallbacks": False, "order": ["mistral"]},
        },
        _settings(),
    )
    assert len(candidates) == 1
    assert candidates[0].provider == "mistral"


def test_chat_route_candidates_sort_by_throughput_uses_rank_table() -> None:
    """sort=throughput orders by the configured throughput-rank map."""
    candidates = chat_route_candidates(
        {
            "models": [
                "anthropic/claude-sonnet-4.6",
                "openai/gpt-5.4-nano",
                "meta-llama/llama-3.1-8b-instruct",
                "deepseek/deepseek-v4-flash",
            ],
            "provider": {"sort": "throughput"},
        },
        _settings(),
    )
    providers_in_order = [c.provider for c in candidates]
    # cerebras + deepseek should land before openai + anthropic.
    assert providers_in_order.index("cerebras") < providers_in_order.index("openai")
    assert providers_in_order.index("deepseek") < providers_in_order.index("anthropic")


def test_chat_route_candidates_sort_by_price_orders_cheapest_first() -> None:
    candidates = chat_route_candidates(
        {
            "models": [
                "anthropic/claude-sonnet-4.6",
                "openai/gpt-5.4-nano",
                "deepseek/deepseek-v4-flash",
            ],
            "provider": {"sort": "price"},
        },
        _settings(),
    )
    prices = [
        c.prompt_price_microdollars_per_million_tokens
        + c.completion_price_microdollars_per_million_tokens
        for c in candidates
    ]
    assert prices == sorted(prices), prices


# ── provider_route_preferences ──────────────────────────────────────────


def test_provider_route_preferences_handles_missing_provider_key() -> None:
    prefs = provider_route_preferences({})
    assert prefs.order == ()
    assert prefs.only == frozenset()
    assert prefs.ignore == frozenset()
    assert prefs.allow_fallbacks is True
    assert prefs.sort is None
    assert prefs.data_collection is None


def test_provider_route_preferences_handles_provider_as_non_dict() -> None:
    """An SDK that sends `provider: "openai"` (string instead of dict)
    must not crash — we treat it as no preferences."""
    prefs = provider_route_preferences({"provider": "openai"})
    assert prefs.order == ()
    assert prefs.allow_fallbacks is True


def test_provider_route_preferences_normalizes_provider_aliases() -> None:
    """`google-ai-studio`, `mistralai`, `moonshot` all resolve to
    canonical provider slugs so client-side aliases land in the same
    filter buckets as the canonical forms."""
    prefs = provider_route_preferences(
        {
            "provider": {
                "only": [
                    "google-ai-studio",
                    "google-vertex",
                    "vertex-ai",
                    "mistralai",
                    "moonshot",
                ]
            }
        }
    )
    assert prefs.only == frozenset(
        {"google-ai-studio", "google-vertex", "mistral", "kimi"}
    )


@pytest.mark.parametrize("legacy", ["gemini", "google"])
def test_provider_route_preferences_expands_legacy_google_group(legacy: str) -> None:
    prefs = provider_route_preferences(
        {"provider": {"order": [legacy], "only": [legacy], "ignore": [legacy]}}
    )
    expected = ("google-vertex", "google-ai-studio")
    assert prefs.order == expected
    assert prefs.only == frozenset(expected)
    assert prefs.ignore == frozenset(expected)


def test_provider_route_preferences_accepts_comma_separated_order() -> None:
    prefs = provider_route_preferences({"provider": {"order": "anthropic, openai"}})
    assert prefs.order == ("anthropic", "openai")


@pytest.mark.parametrize("value", ["us", "US", "usa", "united-states", "united states"])
def test_provider_route_preferences_accepts_us_jurisdiction_aliases(value: str) -> None:
    prefs = provider_route_preferences({"provider": {"jurisdiction": value}})
    assert prefs.provider_jurisdiction == PROVIDER_JURISDICTION_US


def test_provider_route_preferences_rejects_non_us_jurisdiction() -> None:
    with pytest.raises(HTTPException) as ctx:
        provider_route_preferences({"provider": {"jurisdiction": "eu"}})
    assert ctx.value.status_code == 400
    assert "supports only 'us'" in ctx.value.detail["error"]["message"]


def test_provider_route_preferences_rejects_router_as_provider() -> None:
    with pytest.raises(HTTPException) as ctx:
        provider_route_preferences({"provider": {"only": ["openrouter"]}})
    assert ctx.value.status_code == 400
    assert ctx.value.detail["error"]["message"] == (
        "Routing filters cannot contain router name 'openrouter'. "
        "Use model='trustedrouter/zdr' or another TrustedRouter alias, "
        "and omit the router from provider filters."
    )


def test_provider_route_preferences_rejects_unknown_provider_filter() -> None:
    with pytest.raises(HTTPException) as ctx:
        provider_route_preferences({"provider": {"only": ["not-a-provider"]}})
    assert ctx.value.status_code == 400
    assert "Unknown provider" in ctx.value.detail["error"]["message"]


def test_provider_route_preferences_rejects_non_boolean_allow_fallbacks() -> None:
    with pytest.raises(HTTPException) as ctx:
        provider_route_preferences({"provider": {"allow_fallbacks": "yes"}})
    assert ctx.value.status_code == 400
    assert ctx.value.detail["error"]["type"] == "bad_request"


def test_provider_route_preferences_data_collection_validates_enum() -> None:
    allowed = provider_route_preferences({"provider": {"data_collection": "deny"}})
    assert allowed.data_collection == "deny"

    with pytest.raises(HTTPException):
        provider_route_preferences({"provider": {"data_collection": "maybe"}})


def test_provider_route_preferences_rejects_unknown_sort_mode() -> None:
    with pytest.raises(HTTPException):
        provider_route_preferences({"provider": {"sort": "popularity"}})


# ── provider.min_privacy (privacy-tier routing) ─────────────────────────


def test_min_privacy_parses_friendly_aliases() -> None:
    from trusted_router.catalog import (
        PRIVACY_TIER_CONFIDENTIAL,
        PRIVACY_TIER_ZERO_RETENTION,
    )

    p = provider_route_preferences({"model": "x", "provider": {"min_privacy": "zdr"}})
    assert p.min_privacy_rank == PRIVACY_TIER_ZERO_RETENTION
    p2 = provider_route_preferences({"model": "x", "provider": {"min_privacy": "maximum"}})
    assert p2.min_privacy_rank == PRIVACY_TIER_CONFIDENTIAL
    p3 = provider_route_preferences({"model": "x", "provider": {"min_privacy": "e2ee"}})
    assert p3.min_privacy_rank == PRIVACY_TIER_CONFIDENTIAL
    # Default: no filter.
    assert provider_route_preferences({"model": "x"}).min_privacy_rank == 0


def test_confidential_privacy_tier_requires_compute_and_e2ee() -> None:
    from trusted_router.catalog import (
        PRIVACY_TIER_CONFIDENTIAL,
        PRIVACY_TIER_STANDARD,
        Provider,
        provider_privacy_tier,
    )

    confidential_compute_only = Provider(
        slug="compute-only",
        name="Compute only",
        provider_confidential_compute=True,
        provider_e2ee=False,
    )
    e2ee_only = Provider(
        slug="e2ee-only",
        name="E2EE only",
        provider_confidential_compute=False,
        provider_e2ee=True,
    )
    both = Provider(
        slug="both",
        name="Both",
        provider_confidential_compute=True,
        provider_e2ee=True,
    )

    assert provider_privacy_tier(confidential_compute_only) == PRIVACY_TIER_STANDARD
    assert provider_privacy_tier(e2ee_only) == PRIVACY_TIER_STANDARD
    assert provider_privacy_tier(both) == PRIVACY_TIER_CONFIDENTIAL


def test_min_privacy_rejects_unknown_value() -> None:
    with pytest.raises(HTTPException) as exc:
        provider_route_preferences({"model": "x", "provider": {"min_privacy": "kinda-private"}})
    assert exc.value.status_code == 400


def test_min_privacy_zdr_on_auto_keeps_only_zdr_reachable() -> None:
    from trusted_router.catalog import (
        PRIVACY_TIER_ZERO_RETENTION,
        model_max_privacy_tier,
    )

    candidates = chat_route_candidates(
        {"model": "trustedrouter/auto", "provider": {"min_privacy": "zdr"}},
        _settings(),
    )
    assert candidates, "expected ZDR-reachable candidates in the Auto pool"
    for model in candidates:
        assert model_max_privacy_tier(model) >= PRIVACY_TIER_ZERO_RETENTION


def test_min_privacy_confidential_keeps_confidential_reachable_model() -> None:
    from trusted_router.catalog import (
        PRIVACY_TIER_CONFIDENTIAL,
        model_max_privacy_tier,
    )

    # GLM 5.2 has a current confidential Phala route and must still route.
    candidates = chat_route_candidates(
        {"model": "z-ai/glm-5.2", "provider": {"min_privacy": "confidential"}},
        _settings(),
    )
    assert candidates
    assert all(model_max_privacy_tier(m) >= PRIVACY_TIER_CONFIDENTIAL for m in candidates)


def test_min_privacy_too_high_for_model_raises() -> None:
    # A no-store-only model demanded at confidential tier has no route —
    # fail closed rather than silently downgrade.
    with pytest.raises(HTTPException) as exc:
        chat_route_candidates(
            {"model": "openai/gpt-5.4-nano", "provider": {"min_privacy": "confidential"}},
            _settings(),
        )
    assert exc.value.status_code == 400


def test_model_shape_exposes_privacy_tier() -> None:
    from trusted_router.catalog import MODELS, model_to_openrouter_shape

    # A model served by a zero-retention+ provider (deepseek via phala/tinfoil)
    # exposes the tier label and >= ZDR. (anthropic/openai/google were downgraded
    # from ZDR to standard in 4faa10d, so they no longer clear tier 2.)
    zdr_model = MODELS["deepseek/deepseek-v3.2"]
    shape = model_to_openrouter_shape(zdr_model)
    tr = shape["trustedrouter"]
    assert "privacy_tier" in tr
    assert "privacy_tier_label" in tr
    assert tr["privacy_tier"] >= 2  # zero retention or better


def test_data_collection_deny_soft_fallback_keeps_standard_only_model_and_endpoints() -> None:
    from trusted_router.catalog import (
        MODELS,
        PRIVACY_TIER_NO_STORE,
        endpoint_privacy_tier,
        endpoints_for_model,
        model_max_privacy_tier,
    )

    model_id = "mistralai/mistral-small-2603"
    catalog_endpoints = endpoints_for_model(model_id)
    assert model_max_privacy_tier(MODELS[model_id]) < PRIVACY_TIER_NO_STORE
    assert all(endpoint_privacy_tier(endpoint) < PRIVACY_TIER_NO_STORE for endpoint in catalog_endpoints)

    body = {"model": model_id, "provider": {"data_collection": "deny"}}
    candidates = chat_route_candidates(body, _settings())
    endpoint_candidates = chat_route_endpoint_candidates(body, _settings())

    assert [model.id for model in candidates] == [model_id]
    assert {endpoint.id for _model, endpoint in endpoint_candidates} == {
        endpoint.id for endpoint in catalog_endpoints
    }


def test_data_collection_deny_still_filters_when_satisfiable() -> None:
    from trusted_router.catalog import (
        PRIVACY_TIER_NO_STORE,
        endpoint_privacy_tier,
        endpoints_for_model,
        model_max_privacy_tier,
    )

    standard_model_id = "mistralai/mistral-small-2603"
    private_model_id = "deepseek/deepseek-v3.2"
    candidates = chat_route_candidates(
        {
            "models": [standard_model_id, private_model_id],
            "provider": {"data_collection": "deny"},
        },
        _settings(),
    )
    assert [model.id for model in candidates] == [private_model_id]
    assert all(model_max_privacy_tier(model) >= PRIVACY_TIER_NO_STORE for model in candidates)

    endpoint_candidates = chat_route_endpoint_candidates(
        {"model": private_model_id, "provider": {"data_collection": "deny"}},
        _settings(),
    )
    assert endpoint_candidates
    assert any(
        endpoint_privacy_tier(endpoint) < PRIVACY_TIER_NO_STORE
        for endpoint in endpoints_for_model(private_model_id)
    )
    assert all(
        endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_NO_STORE
        for _model, endpoint in endpoint_candidates
    )


def test_embeddings_data_collection_deny_soft_fallback_keeps_standard_model_endpoints() -> None:
    from trusted_router.catalog import (
        PRIVACY_TIER_NO_STORE,
        endpoint_privacy_tier,
        endpoints_for_model,
    )

    model_id = "openai/text-embedding-3-large"
    catalog_endpoints = endpoints_for_model(model_id)
    assert catalog_endpoints
    assert all(endpoint_privacy_tier(endpoint) < PRIVACY_TIER_NO_STORE for endpoint in catalog_endpoints)

    candidates_without_deny = embeddings_route_endpoint_candidates({"model": model_id}, _settings())
    candidates_with_deny = embeddings_route_endpoint_candidates(
        {"model": model_id, "provider": {"data_collection": "deny"}},
        _settings(),
    )

    assert [endpoint.id for _model, endpoint in candidates_with_deny] == [
        endpoint.id for _model, endpoint in candidates_without_deny
    ]


def test_embeddings_provider_only_stays_hard_when_data_collection_soft_falls_back() -> None:
    with pytest.raises(HTTPException) as exc:
        embeddings_route_endpoint_candidates(
            {
                "model": "openai/text-embedding-3-large",
                "provider": {"only": ["cohere"], "data_collection": "deny"},
            },
            _settings(),
        )
    assert exc.value.status_code == 400
    assert "filters" in exc.value.detail["error"]["message"].lower()


def test_catalog_data_collection_deny_soft_fallback_and_satisfiable_filtering() -> None:
    from trusted_router.catalog import (
        MODELS,
        PRIVACY_TIER_NO_STORE,
        endpoint_privacy_tier,
        endpoints_for_model,
    )

    standard_model_id = "openai/text-embedding-3-large"
    standard_endpoints = endpoints_for_model(standard_model_id)
    assert standard_endpoints
    assert all(
        endpoint_privacy_tier(endpoint) < PRIVACY_TIER_NO_STORE for endpoint in standard_endpoints
    )

    deny_prefs = provider_route_preferences({"provider": {"data_collection": "deny"}})
    standard_candidates = catalog_endpoint_candidates(MODELS[standard_model_id], deny_prefs)
    assert {endpoint.id for _model, endpoint in standard_candidates} == {
        endpoint.id for endpoint in standard_endpoints
    }

    mixed_model_id = None
    mixed_endpoints = []
    mixed_qualifying_endpoints = []
    for model_id in MODELS:
        endpoints = endpoints_for_model(model_id)
        qualifying_endpoints = [
            endpoint
            for endpoint in endpoints
            if endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_NO_STORE
        ]
        if qualifying_endpoints and len(qualifying_endpoints) < len(endpoints):
            mixed_model_id = model_id
            mixed_endpoints = endpoints
            mixed_qualifying_endpoints = qualifying_endpoints
            break

    assert mixed_model_id is not None
    mixed_candidates = catalog_endpoint_candidates(MODELS[mixed_model_id], deny_prefs)
    mixed_candidate_ids = {endpoint.id for _model, endpoint in mixed_candidates}
    mixed_endpoint_ids = {endpoint.id for endpoint in mixed_endpoints}
    mixed_qualifying_endpoint_ids = {endpoint.id for endpoint in mixed_qualifying_endpoints}

    assert mixed_candidate_ids == mixed_qualifying_endpoint_ids
    assert mixed_candidate_ids
    assert mixed_candidate_ids < mixed_endpoint_ids


def test_provider_only_stays_hard_when_data_collection_soft_falls_back() -> None:
    with pytest.raises(HTTPException) as exc:
        chat_route_candidates(
            {
                "model": "mistralai/mistral-small-2603",
                "provider": {"only": ["openai"], "data_collection": "deny"},
            },
            _settings(),
        )
    assert exc.value.status_code == 400
    assert "filters" in exc.value.detail["error"]["message"].lower()


def test_min_privacy_stays_hard_when_data_collection_soft_falls_back() -> None:
    with pytest.raises(HTTPException) as exc:
        chat_route_candidates(
            {
                "model": "mistralai/mistral-small-2603",
                "provider": {"data_collection": "deny", "min_privacy": "no_store"},
            },
            _settings(),
        )
    assert exc.value.status_code == 400
    assert "filters" in exc.value.detail["error"]["message"].lower()


def test_unverified_provider_defaults_to_stores_content() -> None:
    # Conservative default: a provider with no explicit posture is assumed
    # to store content (tier STANDARD), never silently "no-store".
    from trusted_router.catalog import PRIVACY_TIER_STANDARD, PROVIDERS, provider_privacy_tier

    assert provider_privacy_tier(PROVIDERS["mistral"]) == PRIVACY_TIER_STANDARD
    assert PROVIDERS["mistral"].stores_content is True


# ── reliability-informed endpoint preference (Phase 4) ──────────────────


def _credits_endpoint(provider: str):
    """First prepaid (Credits) endpoint for a provider, from the live catalog."""
    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS

    for ep in MODEL_ENDPOINTS.values():
        if ep.usage_type == "Credits" and ep.provider == provider:
            return MODELS[ep.model_id], ep
    raise AssertionError(f"no prepaid endpoint for {provider}")


def test_default_endpoint_routing_prefers_reliable_host_over_flaky() -> None:
    # parasail/novita/gmi are demoted below reliable hosts, so default routing
    # tries deepinfra before parasail even when parasail is listed first.
    flaky = _credits_endpoint("parasail")
    reliable = _credits_endpoint("deepinfra")
    ordered = _sort_endpoint_candidates([flaky, reliable], provider_route_preferences({}))
    assert [ep.provider for _, ep in ordered] == ["deepinfra", "parasail"]


def test_explicit_provider_order_overrides_reliability_preference() -> None:
    flaky = _credits_endpoint("parasail")
    reliable = _credits_endpoint("deepinfra")
    prefs = provider_route_preferences({"provider": {"order": ["parasail"]}})
    ordered = _sort_endpoint_candidates([flaky, reliable], prefs)
    assert ordered[0][1].provider == "parasail"  # caller's explicit order wins


def test_glm_52_defaults_to_parasail_with_fallbacks_intact() -> None:
    candidates = chat_route_endpoint_candidates(
        {"model": "z-ai/glm-5.2", "provider": {"usage": "credits"}},
        _settings(),
    )

    assert candidates[0][1].provider == "parasail"
    assert len(candidates) > 1


def test_glm_52_explicit_provider_preferences_override_parasail_default() -> None:
    ordered = chat_route_endpoint_candidates(
        {
            "model": "z-ai/glm-5.2",
            "provider": {"usage": "credits", "order": ["baseten"]},
        },
        _settings(),
    )
    price_sorted = chat_route_endpoint_candidates(
        {
            "model": "z-ai/glm-5.2",
            "provider": {"usage": "credits", "sort": "price"},
        },
        _settings(),
    )

    assert ordered[0][1].provider == "baseten"
    assert (
        price_sorted[0][1].prompt_price_microdollars_per_million_tokens
        + price_sorted[0][1].completion_price_microdollars_per_million_tokens
    ) == min(
        endpoint.prompt_price_microdollars_per_million_tokens
        + endpoint.completion_price_microdollars_per_million_tokens
        for _model, endpoint in price_sorted
    )


def test_glm_52_provider_preference_does_not_override_primary_model_order() -> None:
    candidates = chat_route_endpoint_candidates(
        {
            "model": "deepseek/deepseek-v4-flash",
            "models": ["z-ai/glm-5.2"],
            "provider": {"usage": "credits"},
        },
        _settings(),
    )

    assert candidates[0][0].id == "deepseek/deepseek-v4-flash"


def test_confidential_alias_uses_exact_e2e_endpoint_pool() -> None:
    confidential = chat_route_endpoint_candidates(
        {"model": "trustedrouter/confidential"},
        _settings(),
    )
    e2e = chat_route_endpoint_candidates(
        {"model": "trustedrouter/e2e"},
        _settings(),
    )

    assert [endpoint.id for _model, endpoint in confidential] == [
        endpoint.id for _model, endpoint in e2e
    ]


def test_same_preference_tier_keeps_catalog_order() -> None:
    # Two reliable hosts share the default tier -> original order preserved.
    a = _credits_endpoint("deepinfra")
    b = _credits_endpoint("cerebras")
    ordered = _sort_endpoint_candidates([a, b], provider_route_preferences({}))
    assert [ep.provider for _, ep in ordered] == ["deepinfra", "cerebras"]


# ── OpenAI bare/dated model-id aliasing ─────────────────────────────────
# LiteLLM / the OpenAI SDK send the bare name (`gpt-4.1`) or OpenAI's dated
# snapshot (`gpt-4.1-2025-04-14`); the catalog ids are vendor-prefixed. These
# must resolve to the canonical id so standard OpenAI tooling works shim-free.


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("gpt-4.1", "openai/gpt-4.1"),
        ("gpt-4.1-2025-04-14", "openai/gpt-4.1"),  # dated snapshot
        ("gpt-4.1-mini", "openai/gpt-4.1-mini"),
        ("gpt-4.1-nano-2025-04-14", "openai/gpt-4.1-nano"),  # bare + dated
        ("openai/gpt-4.1", "openai/gpt-4.1"),  # canonical id unchanged
    ],
)
def test_chat_route_candidates_resolves_openai_aliases(requested: str, expected: str) -> None:
    candidates = chat_route_candidates({"model": requested}, _settings())
    assert [c.id for c in candidates] == [expected]


def test_chat_route_candidates_unknown_model_still_rejected() -> None:
    # The aliaser is conservative: an id that resolves to nothing in the catalog
    # must still surface MODEL_NOT_SUPPORTED, not get silently rewritten.
    with pytest.raises(HTTPException):
        chat_route_candidates({"model": "gpt-9.9-imaginary-2099-01-01"}, _settings())


def test_resolve_model_alias_is_a_noop_for_canonical_and_meta_ids() -> None:
    from trusted_router.routing import resolve_model_alias

    assert resolve_model_alias("openai/gpt-4.1") == "openai/gpt-4.1"
    assert resolve_model_alias("trustedrouter/auto") == "trustedrouter/auto"
    assert resolve_model_alias("totally-unknown") == "totally-unknown"
