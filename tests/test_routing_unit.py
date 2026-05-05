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
        {"model": "openai/gpt-4o-mini"},
        _settings(),
    )
    assert [c.id for c in candidates] == ["openai/gpt-4o-mini"]


def test_chat_route_candidates_models_array_dedupes_and_preserves_order() -> None:
    candidates = chat_route_candidates(
        {
            "model": "openai/gpt-4o-mini",
            "models": [
                "openai/gpt-4o-mini",  # dup with primary, must be dropped
                "mistralai/mistral-small-2603",
                "openai/gpt-4o-mini",  # second dup
            ],
        },
        _settings(),
    )
    assert [c.id for c in candidates] == [
        "openai/gpt-4o-mini",
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
                "openai/gpt-4o-mini",
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
                "openai/gpt-4o-mini",
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
                "openai/gpt-4o-mini",
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
                "models": ["openai/gpt-4o-mini"],
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
                "openai/gpt-4o-mini",
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
                "openai/gpt-4o-mini",
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
                "openai/gpt-4o-mini",
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
                "openai/gpt-4o-mini",
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
