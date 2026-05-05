"""Coverage for OpenRouter-style model-id variant suffixes (`:nitro`,
`:floor`). Per OpenRouter docs: `:nitro` is exactly equivalent to setting
`provider.sort = "throughput"`. This file pins:

- the suffix-stripping primitive,
- the override flow into RoutePreferences,
- composition with `provider.only` / `provider.ignore` / `provider.order`,
- single-endpoint no-op behavior,
- precedence over body-set `provider.sort`.
"""

from __future__ import annotations

import pytest

from trusted_router.config import Settings
from trusted_router.routing import (
    _routing_for_body,
    _strip_variant_suffix,
    chat_route_candidates,
    chat_route_endpoint_candidates,
)


def test_strip_variant_suffix_handles_known_suffixes() -> None:
    assert _strip_variant_suffix("z-ai/glm-4.6:nitro") == (
        "z-ai/glm-4.6",
        {"sort": "throughput"},
    )
    assert _strip_variant_suffix("moonshotai/kimi-k2.6:floor") == (
        "moonshotai/kimi-k2.6",
        {"sort": "price"},
    )


def test_strip_variant_suffix_unknown_returns_unchanged() -> None:
    assert _strip_variant_suffix("z-ai/glm-4.6") == ("z-ai/glm-4.6", {})
    assert _strip_variant_suffix("z-ai/glm-4.6:bogus") == ("z-ai/glm-4.6:bogus", {})
    assert _strip_variant_suffix("") == ("", {})


def test_routing_for_body_propagates_nitro_to_route_preferences() -> None:
    ids, prefs = _routing_for_body(
        {"model": "z-ai/glm-4.6:nitro"}, Settings(environment="test")
    )
    assert ids == ["z-ai/glm-4.6"]
    assert prefs.sort == "throughput"


def test_routing_for_body_propagates_floor_to_route_preferences() -> None:
    ids, prefs = _routing_for_body(
        {"model": "z-ai/glm-4.6:floor"}, Settings(environment="test")
    )
    assert ids == ["z-ai/glm-4.6"]
    assert prefs.sort == "price"


def test_nitro_suffix_wins_over_body_provider_sort() -> None:
    """Per OpenRouter convention, the model-id suffix is the explicit
    shorthand and beats any conflicting `provider.sort` set on the body."""
    _ids, prefs = _routing_for_body(
        {
            "model": "z-ai/glm-4.6:nitro",
            "provider": {"sort": "price"},
        },
        Settings(environment="test"),
    )
    assert prefs.sort == "throughput"


def test_nitro_composes_with_provider_only_filter() -> None:
    """Suffix sets sort, but doesn't disable other provider filters."""
    candidates = chat_route_endpoint_candidates(
        {
            "model": "z-ai/glm-4.6:nitro",
            "provider": {"only": ["zai"]},
        },
        Settings(environment="test"),
    )
    assert candidates, "expected at least one zai endpoint for z-ai/glm-4.6"
    assert all(endpoint.provider == "zai" for _model, endpoint in candidates)


def test_nitro_on_single_provider_model_does_not_error() -> None:
    """Models with a single inference provider can't gain anything from
    :nitro, but the suffix shouldn't break the request — it should
    no-op gracefully."""
    cands_plain = chat_route_endpoint_candidates(
        {"model": "z-ai/glm-4.6"}, Settings(environment="test")
    )
    cands_nitro = chat_route_endpoint_candidates(
        {"model": "z-ai/glm-4.6:nitro"}, Settings(environment="test")
    )
    # Same set of endpoints either way, just possibly reordered.
    assert {endpoint.id for _m, endpoint in cands_plain} == {
        endpoint.id for _m, endpoint in cands_nitro
    }


def test_nitro_suffix_resolves_to_known_model_or_404s() -> None:
    """`bogus-model:nitro` strips to `bogus-model`, then catalog lookup
    fails the same way as bare `bogus-model` — 400 model_not_supported."""
    with pytest.raises(Exception) as exc_info:
        chat_route_candidates(
            {"model": "bogus/nope:nitro"}, Settings(environment="test")
        )
    # Whichever 400 the catalog raises is fine — the point is we don't
    # silently route to a different model.
    assert "bogus/nope" in str(exc_info.value)


def test_nitro_suffix_in_models_array_is_also_applied() -> None:
    """Suffix on a fallback entry should still set sort=throughput."""
    _ids, prefs = _routing_for_body(
        {
            "model": "anthropic/claude-3-5-sonnet",
            "models": ["openai/gpt-4o-mini:nitro"],
        },
        Settings(environment="test"),
    )
    assert prefs.sort == "throughput"
