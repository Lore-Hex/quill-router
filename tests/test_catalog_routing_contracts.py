from __future__ import annotations

import pytest

from trusted_router.catalog import (
    ADVISOR_MODEL_ID,
    ARISTOTLE_1_0_MODEL_ID,
    ARISTOTLE_MODEL_ID,
    AUTO_MODEL_ID,
    E2E_MODEL_ID,
    EU_FOCUSED_PROVIDER_ORDER,
    EU_MODEL_ID,
    FUSION_MODEL_ID,
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MAPREDUCE_MODEL_ID,
    MODEL_ENDPOINTS,
    MODELS,
    PLATO_1_0_MODEL_ID,
    PLATO_MODEL_ID,
    PLATO_PRO_1_0_MODEL_ID,
    PLATO_PRO_MODEL_ID,
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_ZERO_RETENTION,
    PROVIDERS,
    SELECTOR_MODEL_ID,
    SOCRATES_1_0_MODEL_ID,
    SOCRATES_1_1_MODEL_ID,
    SOCRATES_MODEL_ID,
    SOCRATES_PRO_1_0_MODEL_ID,
    SOCRATES_PRO_MODEL_ID,
    SOCRATES_PRO_PLUS_1_0_MODEL_ID,
    SOCRATES_PRO_PLUS_MODEL_ID,
    SYNTH_MODEL_ID,
    ZDR_MODEL_ID,
    auto_candidate_models,
    endpoints_for_model,
    meta_candidate_models,
    model_to_openrouter_shape,
    provider_privacy_tier,
)
from trusted_router.config import Settings
from trusted_router.routing import chat_route_candidates, chat_route_endpoint_candidates


def test_every_catalog_model_has_integer_prices_and_valid_provider() -> None:
    assert len(PROVIDERS) >= 8
    assert "kimi" in PROVIDERS
    assert "moonshotai/kimi-k2.6" in MODELS
    assert "moonshotai/kimi-k2.7-code" in MODELS
    assert "moonshotai/kimi-k2.6@kimi/prepaid" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.6@kimi/byok" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.7-code@kimi/prepaid" not in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.7-code@kimi/byok" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.7-code@novita/prepaid" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.7-code@novita/byok" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.6" in [model.id for model in auto_candidate_models()]
    for model_id, provider in [
        ("anthropic/claude-sonnet-4.6", "anthropic"),
        ("openai/gpt-4.1-mini", "openai"),
        ("google/gemini-2.5-flash", "gemini"),
        ("google/gemini-3.5-flash", "gemini"),
        ("deepseek/deepseek-v4-flash", "deepseek"),
        ("mistralai/mistral-small-2603", "mistral"),
        ("meta-llama/llama-3.1-8b-instruct", "novita"),
        ("moonshotai/kimi-k2.6", "kimi"),
        ("moonshotai/kimi-k2.7-code", "novita"),
        ("z-ai/glm-5.2", "zai"),
        ("z-ai/glm-5.2", "gmi"),
        ("z-ai/glm-5.2", "deepinfra"),
        ("z-ai/glm-5.2", "fireworks"),
        ("z-ai/glm-5.2", "novita"),
        ("z-ai/glm-5.2", "phala"),
        ("z-ai/glm-5.2", "siliconflow"),
        ("z-ai/glm-5.2", "tinfoil"),
        ("z-ai/glm-5.2", "together"),
        ("z-ai/glm-5.2", "venice"),
        ("z-ai/glm-5.2", "parasail"),
        ("z-ai/glm-5.2", "friendli"),
        ("z-ai/glm-5.2", "crusoe"),
        ("deepseek/deepseek-v4-flash", "crusoe"),
        ("moonshotai/kimi-k2.6", "crusoe"),
        ("cerebras/gpt-oss-120b", "cerebras"),
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


def test_model_storage_flag_is_gateway_scoped_endpoint_flag_is_provider_scoped() -> None:
    shape = model_to_openrouter_shape(MODELS["openai/gpt-4.1-mini"])
    meta = shape["trustedrouter"]

    # Top-level trustedrouter.stores_content is the router's own retention
    # contract: the attested gateway does not persist prompts or outputs.
    assert meta["stores_content"] is False

    # Endpoint rows still expose upstream-provider posture separately, so
    # dashboards can distinguish TR no-retention from provider ZDR/unknown.
    openai_endpoint = next(
        endpoint for endpoint in meta["endpoints"] if endpoint["provider"] == "openai"
    )
    assert openai_endpoint["stores_content"] is True
    assert openai_endpoint["provider_zero_data_retention"] is True


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
            20,
            [
                # Nebius retired Meta-Llama-3.1-8B + gemma-2-2b-it earlier, then
                # announced 11 more Token Factory model retirements for
                # 2026-06-22. These are intentionally absent; this contract keeps
                # representative non-deprecated Nebius routes alive.
                "meta-llama/Llama-3.3-70B-Instruct",
                "Qwen/Qwen3.5-397B-A17B",
                "deepseek-ai/DeepSeek-V4-Pro",
                "MiniMaxAI/MiniMax-M2.5",
            ],
        ),
        (
            "minimax",
            6,
            [
                "minimax/minimax-m3",
                "minimax/minimax-m2.7",
                "minimax/minimax-m2.7-highspeed",
                "minimax/minimax-m2.5-highspeed",
            ],
        ),
        (
            "cerebras",
            4,
            [
                "openai/gpt-oss-120b",
                "cerebras/gpt-oss-120b",
                "z-ai/glm-4.7",
                "cerebras/zai-glm-4.7",
            ],
        ),
        (
            "gemini",
            6,
            [
                "google/gemini-3.5-flash",
                "google/gemini-3.1-flash-lite-preview",
            ],
        ),
        (
            "zai",
            6,
            [
                "z-ai/glm-5.2",
                "z-ai/glm-5.1",
                "z-ai/glm-5",
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


def test_minimax_empty_operator_routes_are_not_prepaid() -> None:
    for model_id in ("minimax/minimax-m2.1", "minimax/minimax-m2.5"):
        assert f"{model_id}@minimax/prepaid" not in MODEL_ENDPOINTS
        assert f"{model_id}@minimax/byok" in MODEL_ENDPOINTS


@pytest.mark.parametrize(
    ("provider", "model_ids"),
    [
        (
            "gmi",
            ("google/gemma-4-26b-a4b-it", "google/gemma-4-31b-it"),
        ),
        ("kimi", ("moonshotai/kimi-k2.7-code",)),
        (
            "parasail",
            (
                "deepseek/deepseek-v3.2",
                "moonshotai/kimi-k2.5",
                "qwen/qwen3-235b-a22b-2507",
                "stepfun/step-3.5-flash",
                "z-ai/glm-4.7",
                "z-ai/glm-5",
            ),
        ),
        (
            "novita",
            (
                "deepseek/deepseek-prover-v2-671b",
                "meta-llama/llama-3-8b-instruct",
                "qwen/qwen2.5-vl-72b-instruct",
                "qwen/qwen3-4b-fp8",
            ),
        ),
    ],
)
def test_operator_unavailable_provider_routes_are_not_prepaid(
    provider: str, model_ids: tuple[str, ...]
) -> None:
    for model_id in model_ids:
        assert f"{model_id}@{provider}/prepaid" not in MODEL_ENDPOINTS
        assert f"{model_id}@{provider}/byok" in MODEL_ENDPOINTS


def test_minimax_m3_uses_provider_native_context_tiers() -> None:
    prepaid = MODEL_ENDPOINTS["minimax/minimax-m3@minimax/prepaid"]

    # The model row can come from the OpenRouter snapshot when that snapshot
    # catches up, but the provider-native MiniMax endpoint must still carry
    # MiniMax's exact context-tier billing data.
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


def test_privacy_meta_models_expand_to_expected_provider_pools() -> None:
    assert ZDR_MODEL_ID in MODELS
    assert E2E_MODEL_ID in MODELS
    assert EU_MODEL_ID in MODELS
    assert SYNTH_MODEL_ID in MODELS
    assert FUSION_MODEL_ID in MODELS

    zdr = meta_candidate_models(ZDR_MODEL_ID)
    e2e = meta_candidate_models(E2E_MODEL_ID)
    eu = meta_candidate_models(EU_MODEL_ID)

    assert zdr
    assert e2e
    assert eu
    assert eu[0].provider == "mistral"
    assert zdr[0].provider == "anthropic"
    assert any(model.provider == "openai" for model in zdr)
    assert any(model.provider == "gemini" for model in zdr)
    assert all(model.supports_chat for model in zdr + e2e)

    zdr_shape = model_to_openrouter_shape(MODELS[ZDR_MODEL_ID])
    e2e_shape = model_to_openrouter_shape(MODELS[E2E_MODEL_ID])
    eu_shape = model_to_openrouter_shape(MODELS[EU_MODEL_ID])
    assert zdr_shape["trustedrouter"]["route_kind"] == "zdr_pool"
    assert e2e_shape["trustedrouter"]["route_kind"] == "e2e_pool"
    assert eu_shape["trustedrouter"]["route_kind"] == "eu_pool"
    assert zdr_shape["trustedrouter"]["auto_candidates"]
    assert e2e_shape["trustedrouter"]["auto_candidates"]
    assert eu_shape["trustedrouter"]["auto_candidates"]


def test_synth_alias_is_cataloged_but_not_silent_auto_route() -> None:
    model = MODELS[SYNTH_MODEL_ID]
    shape = model_to_openrouter_shape(model)

    assert model.name == "TrustedRouter Synth"
    assert shape["trustedrouter"]["route_kind"] == "fusion_panel"
    assert meta_candidate_models(SYNTH_MODEL_ID) == []
    assert meta_candidate_models(FUSION_MODEL_ID) == []
    for model_id in (SYNTH_MODEL_ID, FUSION_MODEL_ID):
        with pytest.raises(Exception) as exc:
            chat_route_candidates({"model": model_id}, Settings(environment="test"))
        assert getattr(exc.value, "status_code", None) == 501
        assert "attested gateway" in str(exc.value)


def test_selector_and_mapreduce_primitives_are_cataloged_but_gateway_only() -> None:
    expected: dict[str, tuple[str, list[str]]] = {
        SELECTOR_MODEL_ID: (
            "selector_orchestration",
            [
                "minimax/minimax-m3",
                "moonshotai/kimi-k2.6",
                "z-ai/glm-5.2",
                "google/gemma-4-31b-it",
                "deepseek/deepseek-v4-pro",
                "moonshotai/kimi-k2.7-code",
            ],
        ),
        MAPREDUCE_MODEL_ID: (
            "mapreduce_orchestration",
            [
                "deepseek/deepseek-v4-flash",
                "minimax/minimax-m3",
                "cerebras/gpt-oss-120b",
                "moonshotai/kimi-k2.6",
                "z-ai/glm-5.2",
                "google/gemma-4-31b-it",
                "deepseek/deepseek-v4-pro",
            ],
        ),
    }
    for model_id, (route_kind, candidates) in expected.items():
        model = MODELS[model_id]
        shape = model_to_openrouter_shape(model)

        assert model.provider == "trustedrouter"
        assert shape["trustedrouter"]["route_kind"] == route_kind
        assert shape["trustedrouter"]["stores_content"] is False
        assert shape["trustedrouter"]["auto_candidates"] == candidates
        assert [model.id for model in meta_candidate_models(model_id)] == candidates

        with pytest.raises(Exception) as exc:
            chat_route_candidates({"model": model_id}, Settings(environment="test"))
        assert getattr(exc.value, "status_code", None) == 501
        assert "attested gateway" in str(exc.value)


def test_socrates_aliases_are_cataloged_with_advisor_candidates() -> None:
    expected_candidates = [
        "cerebras/gpt-oss-120b",
        "cerebras/zai-glm-4.7",
        "xiaomi/mimo-v2.5-pro-ultraspeed",
        "anthropic/claude-opus-4.8",
    ]

    for model_id in (SOCRATES_1_0_MODEL_ID, SOCRATES_MODEL_ID, ADVISOR_MODEL_ID):
        model = MODELS[model_id]
        shape = model_to_openrouter_shape(model)

        assert model.provider == "trustedrouter"
        assert shape["trustedrouter"]["route_kind"] == "advisor_orchestration"
        assert shape["trustedrouter"]["stores_content"] is False
        assert shape["trustedrouter"]["auto_candidates"] == expected_candidates
        assert [model.id for model in meta_candidate_models(model_id)] == expected_candidates


def test_advisor_combo_models_are_cataloged_with_concrete_candidates() -> None:
    expected: dict[str, list[str]] = {
        ARISTOTLE_1_0_MODEL_ID: [
            "deepseek/deepseek-v4-flash",
            "anthropic/claude-opus-4.8",
            "openai/gpt-5.5",
            "anthropic/claude-sonnet-4.6",
            "google/gemini-3.1-pro-preview",
            "moonshotai/kimi-k2.6",
        ],
        ARISTOTLE_MODEL_ID: [
            "deepseek/deepseek-v4-flash",
            "anthropic/claude-opus-4.8",
            "openai/gpt-5.5",
            "anthropic/claude-sonnet-4.6",
            "google/gemini-3.1-pro-preview",
            "moonshotai/kimi-k2.6",
        ],
        PLATO_1_0_MODEL_ID: [
            "deepseek/deepseek-v4-flash",
            "z-ai/glm-5.2",
            "minimax/minimax-m3",
            "moonshotai/kimi-k2.6",
            "google/gemma-4-31b-it",
            "deepseek/deepseek-v4-pro",
        ],
        PLATO_MODEL_ID: [
            "deepseek/deepseek-v4-flash",
            "z-ai/glm-5.2",
            "minimax/minimax-m3",
            "moonshotai/kimi-k2.6",
            "google/gemma-4-31b-it",
            "deepseek/deepseek-v4-pro",
        ],
        PLATO_PRO_1_0_MODEL_ID: [
            "z-ai/glm-5.2",
            "minimax/minimax-m3",
            "moonshotai/kimi-k2.6",
            "google/gemma-4-31b-it",
            "deepseek/deepseek-v4-pro",
        ],
        PLATO_PRO_MODEL_ID: [
            "z-ai/glm-5.2",
            "minimax/minimax-m3",
            "moonshotai/kimi-k2.6",
            "google/gemma-4-31b-it",
            "deepseek/deepseek-v4-pro",
        ],
        SOCRATES_PRO_1_0_MODEL_ID: [
            "cerebras/zai-glm-4.7",
            "xiaomi/mimo-v2.5-pro-ultraspeed",
            "anthropic/claude-opus-4.8",
        ],
        SOCRATES_PRO_MODEL_ID: [
            "cerebras/zai-glm-4.7",
            "xiaomi/mimo-v2.5-pro-ultraspeed",
            "anthropic/claude-opus-4.8",
        ],
        SOCRATES_PRO_PLUS_1_0_MODEL_ID: [
            "xiaomi/mimo-v2.5-pro-ultraspeed",
            "trustedrouter/zeus-1.0",
        ],
        SOCRATES_1_1_MODEL_ID: [
            "xiaomi/mimo-v2.5-pro-ultraspeed",
            "trustedrouter/zeus-1.0",
        ],
        SOCRATES_PRO_PLUS_MODEL_ID: [
            "xiaomi/mimo-v2.5-pro-ultraspeed",
            "trustedrouter/zeus-1.0",
        ],
    }
    for model_id, candidates in expected.items():
        shape = model_to_openrouter_shape(MODELS[model_id])

        assert shape["trustedrouter"]["route_kind"] == "advisor_orchestration"
        assert shape["trustedrouter"]["stores_content"] is False
        assert shape["trustedrouter"]["auto_candidates"] == candidates
        assert [model.id for model in meta_candidate_models(model_id)] == candidates


def test_privacy_meta_models_force_endpoint_privacy_floor() -> None:
    zdr_endpoints = chat_route_endpoint_candidates(
        {"model": ZDR_MODEL_ID},
        Settings(environment="test"),
    )
    e2e_endpoints = chat_route_endpoint_candidates(
        {"model": E2E_MODEL_ID},
        Settings(environment="test"),
    )

    assert zdr_endpoints
    assert e2e_endpoints
    assert zdr_endpoints[0][1].provider == "anthropic"
    assert e2e_endpoints[0][1].provider == "tinfoil"
    assert all(
        provider_privacy_tier(PROVIDERS[endpoint.provider]) >= PRIVACY_TIER_ZERO_RETENTION
        for _model, endpoint in zdr_endpoints
    )
    assert all(
        provider_privacy_tier(PROVIDERS[endpoint.provider]) >= PRIVACY_TIER_CONFIDENTIAL
        for _model, endpoint in e2e_endpoints
    )


def test_eu_meta_model_restricts_endpoint_pool_to_eu_focused_providers() -> None:
    eu_endpoints = chat_route_endpoint_candidates(
        {"model": EU_MODEL_ID},
        Settings(environment="test"),
    )

    assert eu_endpoints
    assert eu_endpoints[0][1].provider == "mistral"
    assert all(endpoint.provider in EU_FOCUSED_PROVIDER_ORDER for _model, endpoint in eu_endpoints)
    assert {"deepseek", "kimi", "zai"}.isdisjoint(
        {endpoint.provider for _model, endpoint in eu_endpoints}
    )

    narrowed = chat_route_endpoint_candidates(
        {"model": EU_MODEL_ID, "provider": {"only": ["gemini"]}},
        Settings(environment="test"),
    )
    assert narrowed
    assert {endpoint.provider for _model, endpoint in narrowed} == {"gemini"}


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


def test_xiaomi_mimo_provider_models_present_and_routable() -> None:
    """Xiaomi MiMo onboarding: the 5 chat models load from the static manifest,
    map to the right upstream ids, and have a prepaid (Credits) xiaomi endpoint
    the attested gateway can dispatch."""
    from trusted_router.catalog import PROVIDERS, endpoints_for_model

    assert "xiaomi" in PROVIDERS
    assert "xiaomi" in GATEWAY_PREPAID_PROVIDER_SLUGS
    expected = {
        "xiaomi/mimo-v2-flash": "mimo-v2-flash",
        "xiaomi/mimo-v2-pro": "mimo-v2-pro",
        "xiaomi/mimo-v2.5": "mimo-v2.5",
        "xiaomi/mimo-v2.5-pro": "mimo-v2.5-pro",
        "xiaomi/mimo-v2.5-pro-ultraspeed": "mimo-v2.5-pro-ultraspeed",
    }
    for model_id, upstream in expected.items():
        model = MODELS.get(model_id)
        assert model is not None, f"{model_id} missing from catalog"
        assert model.supports_chat, f"{model_id} not chat"
        assert model.provider == "xiaomi"
        assert model.upstream_id == upstream
        assert model.prompt_price_microdollars_per_million_tokens > 0
        credits = [
            e
            for e in endpoints_for_model(model_id)
            if str(e.usage_type) == "Credits" and e.provider == "xiaomi"
        ]
        assert credits, f"{model_id} has no xiaomi prepaid endpoint"

    # UltraSpeed is the 1T-param speed-serving tier with its own ¥9/¥18
    # ($1.305/$2.61) cost, marked up by the manifest loader (cost x 1.10,
    # $0.01/M floor). Guard the exact prices so a regen can't silently
    # collapse them onto the regular v2.5-pro numbers ($1.10/$3.30 customer).
    ultraspeed = MODELS["xiaomi/mimo-v2.5-pro-ultraspeed"]
    assert ultraspeed.prompt_price_microdollars_per_million_tokens == 1_435_500
    assert ultraspeed.completion_price_microdollars_per_million_tokens == 2_871_000
    # ...and that it is genuinely a distinct row from regular v2.5-pro.
    pro = MODELS["xiaomi/mimo-v2.5-pro"]
    assert (
        ultraspeed.completion_price_microdollars_per_million_tokens
        != pro.completion_price_microdollars_per_million_tokens
    )


def test_crusoe_provider_models_present_and_routable() -> None:
    """Crusoe onboarding: native /v1/models rows load from the manifest,
    preserve case-sensitive upstream ids, and create prepaid + BYOK endpoints."""

    assert "crusoe" in PROVIDERS
    assert "crusoe" in GATEWAY_PREPAID_PROVIDER_SLUGS
    expected = {
        "z-ai/glm-5.2": "zai/GLM-5.2",
        "deepseek/deepseek-v4-flash": "deepseek-ai/Deepseek-V4-Flash",
        "moonshotai/kimi-k2.6": "moonshotai/Kimi-K2.6",
        "openai/gpt-oss-120b": "openai/gpt-oss-120b",
        "google/gemma-4-31b-it": "google/gemma-4-31b-it",
    }
    crusoe_model_ids = {
        endpoint.model_id
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.provider == "crusoe" and str(endpoint.usage_type) == "Credits"
    }
    assert len(crusoe_model_ids) >= 15
    for model_id, upstream in expected.items():
        model = MODELS.get(model_id)
        assert model is not None, f"{model_id} missing from catalog"
        credits = [
            e
            for e in endpoints_for_model(model_id)
            if str(e.usage_type) == "Credits" and e.provider == "crusoe"
        ]
        byok = [
            e
            for e in endpoints_for_model(model_id)
            if str(e.usage_type) == "BYOK" and e.provider == "crusoe"
        ]
        assert credits, f"{model_id} has no crusoe prepaid endpoint"
        assert byok, f"{model_id} has no crusoe BYOK endpoint"
        assert credits[0].upstream_id == upstream
        assert credits[0].prompt_price_microdollars_per_million_tokens > 0


def test_anthropic_claude_fable_5_is_blocked_and_not_zdr_routable() -> None:
    """Claude Fable 5 is blocked and is not a ZDR route. It must not appear
    in the public catalog, endpoint list, or privacy-filtered candidate pools."""
    assert "anthropic/claude-fable-5" not in MODELS
    assert endpoints_for_model("anthropic/claude-fable-5") == []
    assert "anthropic/claude-fable-5" not in {
        model.id for model in meta_candidate_models(ZDR_MODEL_ID)
    }


def test_glm_52_supplements_publish_current_model_across_providers() -> None:
    model = MODELS["z-ai/glm-5.2"]
    prepaid = MODEL_ENDPOINTS["z-ai/glm-5.2@zai/prepaid"]
    byok = MODEL_ENDPOINTS["z-ai/glm-5.2@zai/byok"]
    gmi = MODEL_ENDPOINTS["z-ai/glm-5.2@gmi/prepaid"]
    deepinfra = MODEL_ENDPOINTS["z-ai/glm-5.2@deepinfra/prepaid"]
    fireworks = MODEL_ENDPOINTS["z-ai/glm-5.2@fireworks/prepaid"]
    novita = MODEL_ENDPOINTS["z-ai/glm-5.2@novita/prepaid"]
    phala = MODEL_ENDPOINTS["z-ai/glm-5.2@phala/prepaid"]
    siliconflow = MODEL_ENDPOINTS["z-ai/glm-5.2@siliconflow/prepaid"]
    tinfoil = MODEL_ENDPOINTS["z-ai/glm-5.2@tinfoil/prepaid"]
    together = MODEL_ENDPOINTS["z-ai/glm-5.2@together/prepaid"]
    venice = MODEL_ENDPOINTS["z-ai/glm-5.2@venice/prepaid"]
    parasail = MODEL_ENDPOINTS["z-ai/glm-5.2@parasail/prepaid"]
    friendli = MODEL_ENDPOINTS["z-ai/glm-5.2@friendli/prepaid"]
    baseten = MODEL_ENDPOINTS["z-ai/glm-5.2@baseten/prepaid"]
    wafer = MODEL_ENDPOINTS["z-ai/glm-5.2@wafer/prepaid"]
    crusoe = MODEL_ENDPOINTS["z-ai/glm-5.2@crusoe/prepaid"]

    assert model.provider == "zai"
    assert model.context_length == 1_048_576
    assert model.supports_chat
    assert prepaid.upstream_id == "glm-5.2"
    assert byok.upstream_id == "glm-5.2"
    assert gmi.upstream_id == "zai-org/GLM-5.2-FP8"
    assert deepinfra.upstream_id == "zai-org/GLM-5.2"
    assert fireworks.upstream_id == "accounts/fireworks/models/glm-5p2"
    assert novita.upstream_id == "zai-org/glm-5.2"
    assert phala.upstream_id == "phala/glm-5.2"
    assert siliconflow.upstream_id == "zai-org/GLM-5.2"
    assert tinfoil.upstream_id == "glm-5-2"
    assert together.upstream_id == "zai-org/GLM-5.2"
    assert venice.upstream_id == "zai-org-glm-5-2"
    assert parasail.upstream_id == "parasail-glm-52"
    assert friendli.upstream_id == "zai-org/GLM-5.2"
    assert baseten.upstream_id == "zai-org/GLM-5.2"
    assert wafer.upstream_id == "GLM-5.2"
    assert crusoe.upstream_id == "zai/GLM-5.2"
    assert prepaid.prompt_price_microdollars_per_million_tokens == 1_540_000
    assert prepaid.completion_price_microdollars_per_million_tokens == 4_840_000
    assert gmi.prompt_price_microdollars_per_million_tokens == 1_078_000
    assert gmi.completion_price_microdollars_per_million_tokens == 3_388_000
    assert deepinfra.prompt_price_microdollars_per_million_tokens == 1_320_000
    assert deepinfra.completion_price_microdollars_per_million_tokens == 4_620_000
    assert fireworks.prompt_price_microdollars_per_million_tokens == 1_540_000
    assert fireworks.completion_price_microdollars_per_million_tokens == 4_840_000
    assert novita.prompt_price_microdollars_per_million_tokens == 1_540_000
    assert novita.completion_price_microdollars_per_million_tokens == 4_840_000
    assert friendli.prompt_price_microdollars_per_million_tokens == 1_540_000
    assert friendli.completion_price_microdollars_per_million_tokens == 4_840_000
    assert baseten.prompt_price_microdollars_per_million_tokens == 1_540_000
    assert baseten.completion_price_microdollars_per_million_tokens == 4_840_000
    assert wafer.prompt_price_microdollars_per_million_tokens == 1_320_000
    assert wafer.completion_price_microdollars_per_million_tokens == 4_510_000
    assert crusoe.prompt_price_microdollars_per_million_tokens == 1_540_000
    assert crusoe.completion_price_microdollars_per_million_tokens == 4_840_000
