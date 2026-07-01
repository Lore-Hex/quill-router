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
    chat_route_candidates,
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
    assert prefs.only == frozenset({"gemini", "mistral", "kimi"})


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
    # Default: no filter.
    assert provider_route_preferences({"model": "x"}).min_privacy_rank == 0


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

    # no-store via deepseek, confidential via phala — must still route.
    candidates = chat_route_candidates(
        {"model": "deepseek/deepseek-v3.2", "provider": {"min_privacy": "confidential"}},
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

    # Anthropic is zero-retention → tier label present and >= ZDR.
    anth = next(m for m in MODELS.values() if m.provider == "anthropic")
    shape = model_to_openrouter_shape(anth)
    tr = shape["trustedrouter"]
    assert "privacy_tier" in tr
    assert "privacy_tier_label" in tr
    assert tr["privacy_tier"] >= 2  # zero retention or better


def test_data_collection_deny_keeps_zdr_drops_standard() -> None:
    # deny == "no data collection" → require >= no-store tier. ZDR
    # providers (anthropic) carry the conservative stores_content=True
    # default but must NOT be dropped; standard providers must be.
    from trusted_router.catalog import PRIVACY_TIER_NO_STORE, model_max_privacy_tier

    kept = chat_route_candidates(
        {"model": "anthropic/claude-sonnet-4.6", "provider": {"data_collection": "deny"}},
        _settings(),
    )
    assert kept and all(model_max_privacy_tier(m) >= PRIVACY_TIER_NO_STORE for m in kept)

    with pytest.raises(HTTPException) as exc:
        chat_route_candidates(
            {"model": "mistralai/mistral-small-2603", "provider": {"data_collection": "deny"}},
            _settings(),
        )
    assert exc.value.status_code == 400


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
