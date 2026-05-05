from __future__ import annotations

import pytest

from trusted_router.catalog import (
    AUTO_MODEL_ID,
    MODEL_ENDPOINTS,
    MODELS,
    PROVIDERS,
    auto_candidate_models,
    endpoints_for_model,
)
from trusted_router.config import Settings
from trusted_router.routing import chat_route_candidates, chat_route_endpoint_candidates


def test_every_catalog_model_has_integer_prices_and_valid_provider() -> None:
    assert len(PROVIDERS) >= 8
    assert "kimi" in PROVIDERS
    assert "kimi/kimi-k2.6" in MODELS
    assert "kimi/kimi-k2.6@kimi/prepaid" in MODEL_ENDPOINTS
    assert "kimi/kimi-k2.6@kimi/byok" in MODEL_ENDPOINTS
    assert "kimi/kimi-k2.6" in [model.id for model in auto_candidate_models()]
    for model_id, provider in [
        ("anthropic/claude-3-5-sonnet", "anthropic"),
        ("openai/gpt-4o-mini", "openai"),
        ("google/gemini-1.5-flash", "gemini"),
        ("deepseek/deepseek-v4-flash", "deepseek"),
        ("mistral/mistral-small-2603", "mistral"),
        ("cerebras/llama3.1-8b", "cerebras"),
        ("kimi/kimi-k2.6", "kimi"),
    ]:
        assert f"{model_id}@{provider}/prepaid" in MODEL_ENDPOINTS
        assert f"{model_id}@{provider}/byok" in MODEL_ENDPOINTS
    for model in MODELS.values():
        assert model.provider in PROVIDERS
        assert isinstance(model.prompt_price_microdollars_per_million_tokens, int)
        assert isinstance(model.completion_price_microdollars_per_million_tokens, int)
        assert model.prompt_price_microdollars_per_million_tokens >= 0
        assert model.completion_price_microdollars_per_million_tokens >= 0
        assert (
            model.prompt_price_microdollars_per_million_tokens
            <= model.published_prompt_price_microdollars_per_million_tokens
        )
        assert (
            model.completion_price_microdollars_per_million_tokens
            <= model.published_completion_price_microdollars_per_million_tokens
        )


def test_prompt_price_equals_published_under_uniform_markup() -> None:
    """Under the uniform pricing formula (cost+10%, $0.10/M floor), TR no
    longer carries a separate 1¢/M discount. `prompt_price_*` and
    `published_*` are the same number — the customer pays the headline
    price. Any model where they differ is either pre-formula leftover
    code or a bug."""
    for model in MODELS.values():
        if model.id == AUTO_MODEL_ID:
            # Auto's pricing is 0 — billing happens at the chosen
            # candidate's price. /v1/models surfaces a min/max range
            # derived from the candidate set.
            continue
        assert (
            model.prompt_price_microdollars_per_million_tokens
            == model.published_prompt_price_microdollars_per_million_tokens
        ), f"{model.id}: prompt_price != published_prompt"
        assert (
            model.completion_price_microdollars_per_million_tokens
            == model.published_completion_price_microdollars_per_million_tokens
        ), f"{model.id}: completion_price != published_completion"


def test_auto_candidate_order_dedupes_unknowns_and_self_references() -> None:
    candidates = auto_candidate_models(
        ",".join(
            [
                AUTO_MODEL_ID,
                "missing/provider",
                "mistral/mistral-small-2603",
                "mistral/mistral-small-2603",
                "deepseek/deepseek-v4-flash",
            ]
        )
    )

    assert [model.id for model in candidates] == [
        "mistral/mistral-small-2603",
        "deepseek/deepseek-v4-flash",
    ]


def test_route_candidates_honor_models_provider_order_sort_and_dedupe() -> None:
    candidates = chat_route_candidates(
        {
            "model": "openai/gpt-4o-mini",
            "models": [
                "mistral/mistral-small-2603",
                "openai/gpt-4o-mini",
                "deepseek/deepseek-v4-flash",
            ],
            "provider": {
                "order": ["deepseek"],
                "only": ["openai", "mistral", "deepseek"],
                "sort": "price",
            },
        },
        Settings(environment="test"),
    )

    assert [model.id for model in candidates] == [
        "deepseek/deepseek-v4-flash",
        "mistral/mistral-small-2603",
        "openai/gpt-4o-mini",
    ]


@pytest.mark.parametrize(
    ("model_id", "provider"),
    [
        ("kimi/kimi-k2.6", "kimi"),
        ("openai/gpt-4o-mini", "openai"),
        ("mistral/mistral-small-2603", "mistral"),
        ("deepseek/deepseek-v4-flash", "deepseek"),
        ("cerebras/llama3.1-8b", "cerebras"),
        ("google/gemini-1.5-flash", "gemini"),
        ("anthropic/claude-3-5-sonnet", "anthropic"),
    ],
)
def test_endpoint_candidates_make_dual_mode_models_explicit(model_id: str, provider: str) -> None:
    endpoints = chat_route_endpoint_candidates(
        {"model": model_id},
        Settings(environment="test"),
    )
    assert [endpoint.id for _model, endpoint in endpoints] == [
        f"{model_id}@{provider}/prepaid",
        f"{model_id}@{provider}/byok",
    ]

    byok_only = chat_route_endpoint_candidates(
        {"model": model_id, "provider": {"usage": "byok"}},
        Settings(environment="test"),
    )
    assert [endpoint.usage_type for _model, endpoint in byok_only] == ["BYOK"]
    assert [endpoint.id for endpoint in endpoints_for_model(model_id)] == [
        f"{model_id}@{provider}/prepaid",
        f"{model_id}@{provider}/byok",
    ]


@pytest.mark.parametrize(
    "body,message",
    [
        ({"model": "openai/gpt-4o-mini", "models": "not-a-list"}, "models must be an array"),
        (
            {"model": "openai/gpt-4o-mini", "provider": {"allow_fallbacks": "yes"}},
            "allow_fallbacks",
        ),
        (
            {"model": "openai/gpt-4o-mini", "provider": {"sort": "random"}},
            "provider.sort",
        ),
    ],
)
def test_route_candidate_validation_errors_are_specific(body: dict, message: str) -> None:
    with pytest.raises(Exception) as exc_info:
        chat_route_candidates(body, Settings(environment="test"))
    assert message in str(exc_info.value)
