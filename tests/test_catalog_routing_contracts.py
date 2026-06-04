from __future__ import annotations

import pytest

from trusted_router.catalog import (
    AUTO_MODEL_ID,
    GATEWAY_PREPAID_PROVIDER_SLUGS,
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
    assert "moonshotai/kimi-k2.6" in MODELS
    assert "moonshotai/kimi-k2.6@kimi/prepaid" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.6@kimi/byok" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.6" in [model.id for model in auto_candidate_models()]
    for model_id, provider in [
        ("anthropic/claude-sonnet-4.6", "anthropic"),
        ("openai/gpt-4.1-mini", "openai"),
        ("google/gemini-2.5-flash", "gemini"),
        ("deepseek/deepseek-v4-flash", "deepseek"),
        ("mistralai/mistral-small-2603", "mistral"),
        ("meta-llama/llama-3.1-8b-instruct", "novita"),
        ("moonshotai/kimi-k2.6", "kimi"),
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


def test_every_prepaid_endpoint_is_backed_by_attested_gateway_dispatch() -> None:
    credits_providers = {
        endpoint.provider
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.usage_type == "Credits"
    }
    assert credits_providers <= GATEWAY_PREPAID_PROVIDER_SLUGS
    assert {
        "anthropic",
        "openai",
        "gemini",
        "deepseek",
        "mistral",
        "kimi",
        "zai",
    } <= credits_providers


@pytest.mark.parametrize(
    ("provider", "min_model_count", "sample_ids"),
    [
        (
            "novita",
            100,
            [
                "moonshotai/kimi-k2.6",
                "deepseek/deepseek-ocr-2",
                "xiaomimimo/mimo-v2.5-pro",
                "zai-org/glm-5.1",
                "Sao10K/L3-8B-Stheno-v3.2",
            ],
        ),
        (
            "nebius",
            30,
            [
                # Nebius retired Meta-Llama-3.1-8B + gemma-2-2b-it (dropped from
                # Credits via _PROVIDER_UNSERVED_CREDITS_MODELS); Llama-3.3-70B
                # is its current live Llama route.
                "meta-llama/Llama-3.3-70B-Instruct",
                "Qwen/Qwen3.5-397B-A17B",
                "deepseek-ai/DeepSeek-V4-Pro",
                "MiniMaxAI/MiniMax-M2.5",
            ],
        ),
        (
            "minimax",
            8,
            [
                "minimax/minimax-m3",
                "minimax/minimax-m2.7",
                "minimax/minimax-m2.7-highspeed",
                "minimax/minimax-m2.5",
            ],
        ),
    ],
)
def test_native_provider_catalog_preserves_live_model_ids(
    provider: str,
    min_model_count: int,
    sample_ids: list[str],
) -> None:
    """Provider-native `/models` feeds can be ahead of OpenRouter's
    endpoint feed. TR should publish those routes with exact upstream
    IDs so the enclave can dispatch them without strip-author bugs."""
    provider_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == provider
    ]
    provider_model_ids = {endpoint.model_id for endpoint in provider_endpoints}

    assert len(provider_model_ids) >= min_model_count
    for model_id in sample_ids:
        assert f"{model_id}@{provider}/prepaid" in MODEL_ENDPOINTS
        assert f"{model_id}@{provider}/byok" in MODEL_ENDPOINTS
        assert MODEL_ENDPOINTS[f"{model_id}@{provider}/prepaid"].upstream_id
        assert MODEL_ENDPOINTS[f"{model_id}@{provider}/byok"].upstream_id
    assert "deepseek/deepseek-ocr-2@deepseek/prepaid" not in MODEL_ENDPOINTS
    assert "deepseek/deepseek-ocr-2@deepseek/byok" not in MODEL_ENDPOINTS


def test_minimax_public_ids_map_to_exact_upstream_ids() -> None:
    assert MODEL_ENDPOINTS["minimax/minimax-m3@minimax/prepaid"].upstream_id == "MiniMax-M3"
    assert MODEL_ENDPOINTS["minimax/minimax-m3@minimax/byok"].upstream_id == "MiniMax-M3"
    assert MODEL_ENDPOINTS["minimax/minimax-m2.7@minimax/prepaid"].upstream_id == "MiniMax-M2.7"
    assert (
        MODEL_ENDPOINTS["minimax/minimax-m2.7-highspeed@minimax/byok"].upstream_id
        == "MiniMax-M2.7-highspeed"
    )


def test_minimax_m3_uses_provider_native_context_tiers() -> None:
    m3 = MODELS["minimax/minimax-m3"]
    prepaid = MODEL_ENDPOINTS["minimax/minimax-m3@minimax/prepaid"]

    assert m3.context_length == 1_000_000
    assert [tier.max_prompt_tokens for tier in m3.price_tiers] == [512_000, None]
    assert [tier.max_prompt_tokens for tier in prepaid.price_tiers] == [512_000, None]

    low, high = prepaid.price_tiers
    assert low.prompt_price_microdollars_per_million_tokens == 660_000
    assert low.completion_price_microdollars_per_million_tokens == 2_640_000
    assert low.prompt_cached_price_microdollars_per_million_tokens == 132_000
    assert high.prompt_price_microdollars_per_million_tokens == 1_320_000
    assert high.completion_price_microdollars_per_million_tokens == 5_280_000
    assert high.prompt_cached_price_microdollars_per_million_tokens == 264_000


def test_prompt_price_equals_published_under_uniform_markup() -> None:
    """Under the uniform pricing formula (cost+10%, $0.01/M floor), TR no
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
                "mistralai/mistral-small-2603",
                "mistralai/mistral-small-2603",
                "deepseek/deepseek-v4-flash",
            ]
        )
    )

    assert [model.id for model in candidates] == [
        "mistralai/mistral-small-2603",
        "deepseek/deepseek-v4-flash",
    ]


def test_route_candidates_honor_models_provider_order_sort_and_dedupe() -> None:
    candidates = chat_route_candidates(
        {
            "model": "openai/gpt-4.1-mini",
            "models": [
                "mistralai/mistral-small-2603",
                "openai/gpt-4.1-mini",
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

    # provider.order=["deepseek"] pins deepseek first. The remaining two
    # are price-sorted: mistral-small-2603 is cheaper than the current
    # OpenAI low-end probe.
    assert [model.id for model in candidates] == [
        "deepseek/deepseek-v4-flash",
        "mistralai/mistral-small-2603",
        "openai/gpt-4.1-mini",
    ]


@pytest.mark.parametrize(
    ("model_id", "provider"),
    [
        ("moonshotai/kimi-k2.6", "kimi"),
        ("openai/gpt-4.1-mini", "openai"),
        ("mistralai/mistral-small-2603", "mistral"),
        ("deepseek/deepseek-v4-flash", "deepseek"),
        ("meta-llama/llama-3.1-8b-instruct", "novita"),
        ("google/gemini-2.5-flash", "gemini"),
        ("anthropic/claude-sonnet-4.6", "anthropic"),
    ],
)
def test_endpoint_candidates_make_dual_mode_models_explicit(model_id: str, provider: str) -> None:
    endpoints = chat_route_endpoint_candidates(
        {"model": model_id},
        Settings(environment="test"),
    )
    prepaid = f"{model_id}@{provider}/prepaid"
    byok = f"{model_id}@{provider}/byok"
    # Dual-mode is explicit: a provider's prepaid endpoint is immediately
    # followed by its BYOK twin. We check adjacency rather than absolute
    # position, since the full candidate list is ordered by provider rank and
    # a BYOK-only provider (e.g. Cerebras for Llama) can sort ahead.
    route_ids = [endpoint.id for _model, endpoint in endpoints]
    assert prepaid in route_ids
    assert route_ids[route_ids.index(prepaid) + 1] == byok

    byok_only = chat_route_endpoint_candidates(
        {"model": model_id, "provider": {"usage": "byok"}},
        Settings(environment="test"),
    )
    assert [endpoint.usage_type for _model, endpoint in byok_only] == ["BYOK"] * len(byok_only)
    catalog_ids = [endpoint.id for endpoint in endpoints_for_model(model_id)]
    assert catalog_ids[catalog_ids.index(prepaid) + 1] == byok


@pytest.mark.parametrize(
    "body,message",
    [
        ({"model": "openai/gpt-5.4-nano", "models": "not-a-list"}, "models must be an array"),
        (
            {"model": "openai/gpt-5.4-nano", "provider": {"allow_fallbacks": "yes"}},
            "allow_fallbacks",
        ),
        (
            {"model": "openai/gpt-5.4-nano", "provider": {"sort": "random"}},
            "provider.sort",
        ),
    ],
)
def test_route_candidate_validation_errors_are_specific(body: dict, message: str) -> None:
    with pytest.raises(Exception) as exc_info:
        chat_route_candidates(body, Settings(environment="test"))
    assert message in str(exc_info.value)
