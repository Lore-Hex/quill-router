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

from trusted_router.config import Settings
from trusted_router.routing import (
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
    """sort=throughput orders by the configured throughput-rank map.
    Cerebras (rank 0) > vertex (1) > gemini (2) > deepseek (3) > kimi (4) >
    mistral (5) > openai (6) > anthropic (7)."""
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
        {"provider": {"only": ["google-ai-studio", "mistralai", "moonshot"]}}
    )
    assert prefs.only == frozenset({"gemini", "mistral", "kimi"})


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

    p = provider_route_preferences(
        {"model": "x", "provider": {"min_privacy": "zdr"}}
    )
    assert p.min_privacy_rank == PRIVACY_TIER_ZERO_RETENTION
    p2 = provider_route_preferences(
        {"model": "x", "provider": {"min_privacy": "maximum"}}
    )
    assert p2.min_privacy_rank == PRIVACY_TIER_CONFIDENTIAL
    # Default: no filter.
    assert provider_route_preferences({"model": "x"}).min_privacy_rank == 0


def test_min_privacy_rejects_unknown_value() -> None:
    with pytest.raises(HTTPException) as exc:
        provider_route_preferences(
            {"model": "x", "provider": {"min_privacy": "kinda-private"}}
        )
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
            {"model": "openai/gpt-5.4-nano", "provider": {"data_collection": "deny"}},
            _settings(),
        )
    assert exc.value.status_code == 400


def test_unverified_provider_defaults_to_stores_content() -> None:
    # Conservative default: a provider with no explicit posture is assumed
    # to store content (tier STANDARD), never silently "no-store".
    from trusted_router.catalog import PRIVACY_TIER_STANDARD, PROVIDERS, provider_privacy_tier

    assert provider_privacy_tier(PROVIDERS["openai"]) == PRIVACY_TIER_STANDARD
    assert PROVIDERS["openai"].stores_content is True
