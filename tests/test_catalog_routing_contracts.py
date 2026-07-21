from __future__ import annotations

import pytest

from trusted_router.catalog import (
    ADVISOR_MODEL_ID,
    ARISTOTLE_1_0_MODEL_ID,
    ARISTOTLE_1_1_MODEL_ID,
    ARISTOTLE_MODEL_ID,
    ATHENA_MODEL_ID,
    AUTO_MODEL_ID,
    CONFIDENTIAL_MODEL_ID,
    E2E_MODEL_ID,
    EU_FOCUSED_PROVIDER_ORDER,
    EU_MODEL_ID,
    FUSION_MODEL_ID,
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    IRIS_1_0_MODEL_ID,
    IRIS_2_0_MODEL_ID,
    IRIS_MODEL_ID,
    LIBERTY_1_0_1M_MODEL_ID,
    LIBERTY_1_0_MODEL_ID,
    LIBERTY_2_0_MODEL_ID,
    LIBERTY_3_0_MODEL_ID,
    MAPREDUCE_MODEL_ID,
    META_MODEL_IDS,
    MODEL_ENDPOINTS,
    MODELS,
    OPEN_PATCHER_A1_MODEL_ID,
    OPEN_PATCHER_FAST1_MODEL_ID,
    OPEN_PATCHER_G1_MODEL_ID,
    OPEN_PATCHER_G2_MODEL_ID,
    OPEN_PATCHER_S1_MODEL_ID,
    OPEN_PATCHER_S2_MODEL_ID,
    PLATO_1_0_MODEL_ID,
    PLATO_MODEL_ID,
    PLATO_PRO_1_0_MODEL_ID,
    PLATO_PRO_2_0_MODEL_ID,
    PLATO_PRO_MODEL_ID,
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_ZERO_RETENTION,
    PROMETHEUS_1_0_1M_MODEL_ID,
    PROMETHEUS_1_0_MODEL_ID,
    PROMETHEUS_2_0_MODEL_ID,
    PROMETHEUS_MODEL_ID,
    PROVIDER_JURISDICTION_US,
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
    ZEUS_1_0_MINI_MODEL_ID,
    ZEUS_1_0_MODEL_ID,
    ZEUS_MODEL_ID,
    InvalidAutoModelOrder,
    auto_candidate_models,
    canonical_orchestration_model_id,
    endpoint_privacy_tier,
    endpoints_for_model,
    meta_candidate_models,
    model_eu_focused_provider_available,
    model_open_weights,
    model_to_openrouter_shape,
    model_us_provider_available,
    orchestration_primitive,
    orchestration_role,
    provider_privacy_tier,
)
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routing import chat_route_candidates, chat_route_endpoint_candidates


def test_every_catalog_model_has_integer_prices_and_valid_provider() -> None:
    assert len(PROVIDERS) >= 8
    assert "kimi" in PROVIDERS
    assert "moonshotai/kimi-k3" in MODELS
    assert "moonshotai/kimi-k2.6" in MODELS
    assert "moonshotai/kimi-k2.7-code" in MODELS
    assert "moonshotai/kimi-k2.7-code-highspeed" in MODELS
    assert "moonshotai/kimi-k2.6@kimi/prepaid" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.6@kimi/byok" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.7-code@kimi/prepaid" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.7-code@kimi/byok" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.7-code-highspeed@kimi/prepaid" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.7-code-highspeed@kimi/byok" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k3@kimi/prepaid" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k3@kimi/byok" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k3@novita/prepaid" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k3@novita/byok" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k3@gmi/prepaid" not in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k3@gmi/byok" in MODEL_ENDPOINTS
    kimi_k3 = MODELS["moonshotai/kimi-k3"]
    assert kimi_k3.context_length == 1_048_576
    assert kimi_k3.prompt_price_microdollars_per_million_tokens == 3_150_000
    assert kimi_k3.completion_price_microdollars_per_million_tokens == 15_750_000
    assert kimi_k3.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 315_000
    assert MODEL_ENDPOINTS["moonshotai/kimi-k3@kimi/prepaid"].upstream_id == "kimi-k3"
    assert (
        MODEL_ENDPOINTS["moonshotai/kimi-k3@novita/prepaid"].upstream_id
        == "moonshotai/kimi-k3"
    )
    assert (
        MODEL_ENDPOINTS["moonshotai/kimi-k2.7-code-highspeed@kimi/prepaid"].upstream_id
        == "kimi-k2.7-code-highspeed"
    )
    assert "moonshotai/kimi-k2.7-code@novita/prepaid" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.7-code@novita/byok" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.6" in [model.id for model in auto_candidate_models()]
    for model_id, provider in [
        ("anthropic/claude-sonnet-4.6", "anthropic"),
        ("openai/gpt-4.1-mini", "openai"),
        ("google/gemini-2.5-flash", "google-ai-studio"),
        ("google/gemini-3.5-flash", "google-ai-studio"),
        ("google/gemini-3.6-flash", "google-ai-studio"),
        ("deepseek/deepseek-v4-flash", "deepseek"),
        ("mistralai/mistral-small-2603", "mistral"),
        ("meta-llama/llama-3.1-8b-instruct", "novita"),
        ("moonshotai/kimi-k2.6", "kimi"),
        ("moonshotai/kimi-k2.7-code", "novita"),
        ("tencent/hy3", "novita"),
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
        ("z-ai/glm-5.2", "makora"),
        ("deepseek/deepseek-v4-flash", "makora"),
        ("moonshotai/kimi-k2.7-code", "makora"),
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
        "google-ai-studio",
        "google-vertex",
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
    assert openai_endpoint["provider_zero_data_retention"] is False


@pytest.mark.parametrize(
    ("provider", "min_model_count", "sample_ids"),
    [
        (
            "novita",
            100,
            [
                "moonshotai/kimi-k2.6",
                "deepseek/deepseek-ocr-2",
                "tencent/hy3",
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
            "google-ai-studio",
            5,
            [
                "google/gemini-3.5-flash",
                "google/gemini-3.6-flash",
                "google/gemini-3.1-flash-image-preview",
            ],
        ),
        (
            "grok",
            4,
            [
                "x-ai/grok-4.5",
                "x-ai/grok-4.3",
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


def test_grok_45_uses_xai_native_model_id_and_pricing() -> None:
    model = MODELS["x-ai/grok-4.5"]
    prepaid = MODEL_ENDPOINTS["x-ai/grok-4.5@grok/prepaid"]
    byok = MODEL_ENDPOINTS["x-ai/grok-4.5@grok/byok"]

    assert model.provider == "grok"
    assert model.context_length == 500_000
    assert prepaid.upstream_id == "grok-4.5"
    assert byok.upstream_id == "grok-4.5"
    assert prepaid.prompt_price_microdollars_per_million_tokens == 2_100_000
    assert prepaid.completion_price_microdollars_per_million_tokens == 6_300_000
    assert prepaid.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 525_000


def test_novita_hy3_uses_live_provider_id_and_price_floor() -> None:
    model = MODELS["tencent/hy3"]
    prepaid = MODEL_ENDPOINTS["tencent/hy3@novita/prepaid"]
    byok = MODEL_ENDPOINTS["tencent/hy3@novita/byok"]

    # Tencent has several independent hosts. Validate the Novita endpoint
    # itself rather than whichever host happens to sort first on the model.
    assert model.context_length == 262_144
    assert prepaid.upstream_id == "tencent/hy3"
    assert byok.upstream_id == "tencent/hy3"
    assert prepaid.prompt_price_microdollars_per_million_tokens == 147_000
    assert prepaid.completion_price_microdollars_per_million_tokens == 609_000
    assert prepaid.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 36_750


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
    checked = 0
    for model_id in model_ids:
        if f"{model_id}@{provider}/byok" not in MODEL_ENDPOINTS:
            # Provider feeds may retire the route entirely. The suppression
            # contract applies only while the provider still advertises it.
            continue
        assert f"{model_id}@{provider}/prepaid" not in MODEL_ENDPOINTS
        assert f"{model_id}@{provider}/byok" in MODEL_ENDPOINTS
        checked += 1
    assert checked > 0


def test_minimax_m3_uses_provider_native_context_tiers() -> None:
    prepaid = MODEL_ENDPOINTS["minimax/minimax-m3@minimax/prepaid"]

    # The model row can come from the OpenRouter snapshot when that snapshot
    # catches up, but the provider-native MiniMax endpoint must still carry
    # MiniMax's exact context-tier billing data.
    assert [tier.max_prompt_tokens for tier in prepaid.price_tiers] == [512_000, None]

    low, high = prepaid.price_tiers
    assert low.prompt_price_microdollars_per_million_tokens == 315_000
    assert low.completion_price_microdollars_per_million_tokens == 1_260_000
    assert low.prompt_cached_price_microdollars_per_million_tokens == 63_000
    assert high.prompt_price_microdollars_per_million_tokens == 630_000
    assert high.completion_price_microdollars_per_million_tokens == 2_520_000
    assert high.prompt_cached_price_microdollars_per_million_tokens == 126_000


def test_prompt_price_equals_published_under_uniform_markup() -> None:
    """Under the uniform pricing formula (cost+5%, $0.01/M floor), TR no
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


def test_auto_candidate_order_rejects_meta_and_orchestration_models() -> None:
    with pytest.raises(InvalidAutoModelOrder, match="TR_AUTO_MODEL_ORDER"):
        auto_candidate_models(
            ",".join(
                [
                    AUTO_MODEL_ID,
                    SYNTH_MODEL_ID,
                    SOCRATES_1_1_MODEL_ID,
                    ADVISOR_MODEL_ID,
                    SELECTOR_MODEL_ID,
                    "mistralai/mistral-small-2603",
                ]
            )
        )


def test_app_startup_rejects_orchestration_in_auto_model_order() -> None:
    with pytest.raises(InvalidAutoModelOrder, match=SYNTH_MODEL_ID):
        create_app(
            Settings(environment="test", auto_model_order=SYNTH_MODEL_ID),
            configure_store_arg=False,
            init_observability=False,
        )


def test_auto_candidate_order_dedupes_unknowns() -> None:
    candidates = auto_candidate_models(
        ",".join(
            [
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
    assert CONFIDENTIAL_MODEL_ID in MODELS
    assert EU_MODEL_ID in MODELS
    assert SYNTH_MODEL_ID in MODELS
    assert FUSION_MODEL_ID in MODELS

    zdr = meta_candidate_models(ZDR_MODEL_ID)
    e2e = meta_candidate_models(E2E_MODEL_ID)
    confidential = meta_candidate_models(CONFIDENTIAL_MODEL_ID)
    eu = meta_candidate_models(EU_MODEL_ID)

    assert zdr
    assert e2e
    assert [model.id for model in confidential] == [model.id for model in e2e]
    assert eu
    assert eu[0].provider == "mistral"
    assert all(model.supports_chat for model in zdr + e2e)

    zdr_shape = model_to_openrouter_shape(MODELS[ZDR_MODEL_ID])
    e2e_shape = model_to_openrouter_shape(MODELS[E2E_MODEL_ID])
    confidential_shape = model_to_openrouter_shape(MODELS[CONFIDENTIAL_MODEL_ID])
    eu_shape = model_to_openrouter_shape(MODELS[EU_MODEL_ID])
    assert zdr_shape["trustedrouter"]["route_kind"] == "zdr_pool"
    assert e2e_shape["trustedrouter"]["route_kind"] == "e2e_pool"
    assert confidential_shape["trustedrouter"]["route_kind"] == "e2e_pool"
    assert confidential_shape["trustedrouter"]["canonical_model_id"] == E2E_MODEL_ID
    assert eu_shape["trustedrouter"]["route_kind"] == "eu_pool"
    assert zdr_shape["trustedrouter"]["auto_candidates"]
    assert e2e_shape["trustedrouter"]["auto_candidates"]
    assert eu_shape["trustedrouter"]["auto_candidates"]


def test_uncontracted_closed_providers_are_not_marked_zdr() -> None:
    """Keep public ZDR claims fail-closed for major closed providers.

    If Amazon/Bedrock or Google/Vertex are added as explicit providers later,
    they should remain outside trustedrouter/zdr until reviewed again.
    """
    provider_slugs_requiring_reverification = {
        "amazon",
        "anthropic",
        "aws",
        "bedrock",
        "google-ai-studio",
        "google-vertex",
    }
    configured = provider_slugs_requiring_reverification & set(PROVIDERS)

    assert {"anthropic", "google-ai-studio", "google-vertex"} <= configured
    for provider in sorted(configured):
        assert PROVIDERS[provider].provider_zero_data_retention is not True
        assert provider_privacy_tier(PROVIDERS[provider]) < PRIVACY_TIER_ZERO_RETENTION

    # OpenAI's guarantee is deliberately narrower: it belongs to
    # TrustedRouter's managed prepaid account, starts on July 28, and is not
    # activated until a live retention smoke passes.
    assert PROVIDERS["openai"].provider_zero_data_retention is False
    assert PROVIDERS["openai"].prepaid_zero_data_retention is False
    assert PROVIDERS["openai"].prepaid_zero_data_retention_effective_on == "2026-07-28"


def test_provider_deprecated_models_have_no_catalog_endpoints() -> None:
    quarantined_routes = [
        ("xiaomi", "xiaomi/mimo-v2-flash"),
        ("xiaomi", "xiaomi/mimo-v2-pro"),
        ("friendli", "meta-llama/llama-3.3-70b-instruct"),
        ("google-ai-studio", "google/gemini-3.1-flash-lite-preview"),
        ("google-vertex", "google/gemini-3.1-flash-lite-preview"),
        ("novita", "baidu/ernie-4.5-vl-28b-a3b"),
        ("novita", "meta-llama/llama-3-70b-instruct"),
        ("makora", "amd/llama-3.3-70b-instruct-fp8-kv"),
        ("gmi", "anthropic/claude-fable-5"),
        ("gmi", "anthropic/claude-sonnet-5"),
        ("gmi", "anthropic/claude-opus-4.1"),
        ("deepinfra", "anthropic/claude-fable-5"),
        ("deepinfra", "anthropic/claude-sonnet-5"),
        ("phala", "anthropic/claude-sonnet-5"),
        ("phala", "anthropic/claude-opus-4.1"),
        ("together", "z-ai/glm-5"),
        ("deepseek", "deepseek/deepseek-r1-0528"),
        ("kimi", "moonshotai/kimi-k2-thinking"),
        ("mistral", "mistralai/mixtral-8x22b-instruct"),
        ("lightning", "openai/gpt-5-mini"),
        # atlas-cloud (#244) advertises these openai/* models but its router
        # returns 400 "router not found"; provider-scoped quarantine.
        ("atlas-cloud", "openai/gpt-4.1"),
        ("atlas-cloud", "openai/gpt-4o"),
        ("atlas-cloud", "openai/gpt-5.1-codex"),
        ("atlas-cloud", "openai/o3-pro"),
    ]

    for provider, model_id in quarantined_routes:
        assert not [
            endpoint
            for endpoint in MODEL_ENDPOINTS.values()
            if endpoint.provider == provider and endpoint.model_id == model_id
        ], f"{provider}/{model_id} should be quarantined"

    # atlas-cloud's healthy openai routes must stay live — only the
    # router-not-found phantoms are quarantined, not the whole openai namespace.
    for kept_model in ("openai/gpt-4.1-mini", "openai/gpt-5.5", "openai/gpt-5.6-sol"):
        assert [
            endpoint
            for endpoint in MODEL_ENDPOINTS.values()
            if endpoint.provider == "atlas-cloud"
            and endpoint.model_id == kept_model
        ], f"atlas-cloud/{kept_model} should remain routable"

    assert "anthropic/claude-fable-5@anthropic/prepaid" in MODEL_ENDPOINTS
    # Policy (2026-07-18): Anthropic-authored models route via Anthropic only
    # for Credits — the reseller prepaid route is gone, its BYOK route stays.
    assert "anthropic/claude-fable-5@lightning/prepaid" not in MODEL_ENDPOINTS
    assert "anthropic/claude-fable-5@lightning/byok" in MODEL_ENDPOINTS
    # Residue quarantine is provider-scoped: healthy siblings survive.
    assert "z-ai/glm-5@zai/prepaid" in MODEL_ENDPOINTS
    assert "deepseek/deepseek-v4-pro@deepseek/prepaid" in MODEL_ENDPOINTS


def test_anthropic_opus_41_drops_prepaid_but_keeps_byok_until_retirement() -> None:
    endpoints = [
        endpoint
        for endpoint in endpoints_for_model("anthropic/claude-opus-4.1")
        if endpoint.provider == "anthropic"
    ]

    assert not [endpoint for endpoint in endpoints if endpoint.usage_type == "Credits"]
    assert [endpoint for endpoint in endpoints if endpoint.usage_type == "BYOK"]


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
    socrates_1_0_candidates = [
        "cerebras/gpt-oss-120b",
        "deepseek/deepseek-v4-flash",
        "cerebras/zai-glm-4.7",
        "xiaomi/mimo-v2.5-pro-ultraspeed",
        "anthropic/claude-opus-4.8",
    ]
    rolling_candidates = [
        "xiaomi/mimo-v2.5-pro-ultraspeed",
        "minimax/minimax-m3",
        "z-ai/glm-5.2-fast",
        "deepseek/deepseek-v4-flash",
        "trustedrouter/zeus-1.0",
    ]

    for model_id, candidates in (
        (SOCRATES_1_0_MODEL_ID, socrates_1_0_candidates),
        (SOCRATES_1_1_MODEL_ID, rolling_candidates),
        (SOCRATES_MODEL_ID, rolling_candidates),
        (ADVISOR_MODEL_ID, socrates_1_0_candidates),
    ):
        model = MODELS[model_id]
        shape = model_to_openrouter_shape(model)

        assert model.provider == "trustedrouter"
        assert shape["trustedrouter"]["route_kind"] == "advisor_orchestration"
        assert shape["trustedrouter"]["orchestration_primitive"] == "advisor"
        assert shape["trustedrouter"]["stores_content"] is False
        assert shape["trustedrouter"]["auto_candidates"] == candidates
        assert [model.id for model in meta_candidate_models(model_id)] == candidates

    assert orchestration_role(ADVISOR_MODEL_ID) == "primitive"
    assert canonical_orchestration_model_id(ADVISOR_MODEL_ID) == ADVISOR_MODEL_ID
    assert orchestration_role(SOCRATES_MODEL_ID) == "rolling_alias"
    assert canonical_orchestration_model_id(SOCRATES_MODEL_ID) == SOCRATES_1_1_MODEL_ID
    assert orchestration_role(SOCRATES_1_1_MODEL_ID) == "named_preset"
    assert canonical_orchestration_model_id(SOCRATES_1_1_MODEL_ID) == SOCRATES_1_1_MODEL_ID


def test_orchestration_taxonomy_distinguishes_primitives_presets_and_legacy_aliases() -> None:
    expected = {
        ADVISOR_MODEL_ID: ("advisor", "primitive", ADVISOR_MODEL_ID),
        SYNTH_MODEL_ID: ("synth", "primitive", SYNTH_MODEL_ID),
        FUSION_MODEL_ID: ("synth", "legacy_alias", SYNTH_MODEL_ID),
        SELECTOR_MODEL_ID: ("selector", "primitive", SELECTOR_MODEL_ID),
        MAPREDUCE_MODEL_ID: ("mapreduce", "primitive", MAPREDUCE_MODEL_ID),
        SOCRATES_MODEL_ID: ("advisor", "rolling_alias", SOCRATES_1_1_MODEL_ID),
        SOCRATES_1_1_MODEL_ID: ("advisor", "named_preset", SOCRATES_1_1_MODEL_ID),
        ARISTOTLE_MODEL_ID: ("advisor", "rolling_alias", ARISTOTLE_1_1_MODEL_ID),
        ARISTOTLE_1_1_MODEL_ID: (
            "advisor",
            "named_preset",
            ARISTOTLE_1_1_MODEL_ID,
        ),
        ARISTOTLE_1_0_MODEL_ID: (
            "advisor",
            "named_preset",
            ARISTOTLE_1_0_MODEL_ID,
        ),
        PLATO_MODEL_ID: ("advisor", "rolling_alias", PLATO_PRO_1_0_MODEL_ID),
        PLATO_1_0_MODEL_ID: ("advisor", "named_preset", PLATO_1_0_MODEL_ID),
        PLATO_PRO_MODEL_ID: ("advisor", "rolling_alias", PLATO_PRO_2_0_MODEL_ID),
        PLATO_PRO_1_0_MODEL_ID: (
            "advisor",
            "named_preset",
            PLATO_PRO_1_0_MODEL_ID,
        ),
        PLATO_PRO_2_0_MODEL_ID: (
            "advisor",
            "named_preset",
            PLATO_PRO_2_0_MODEL_ID,
        ),
        IRIS_MODEL_ID: ("synth", "rolling_alias", IRIS_2_0_MODEL_ID),
        IRIS_2_0_MODEL_ID: ("synth", "named_preset", IRIS_2_0_MODEL_ID),
        PROMETHEUS_MODEL_ID: ("synth", "rolling_alias", PROMETHEUS_2_0_MODEL_ID),
        PROMETHEUS_2_0_MODEL_ID: (
            "synth",
            "named_preset",
            PROMETHEUS_2_0_MODEL_ID,
        ),
        OPEN_PATCHER_S1_MODEL_ID: ("synth", "named_preset", OPEN_PATCHER_S1_MODEL_ID),
        OPEN_PATCHER_S2_MODEL_ID: ("synth", "named_preset", OPEN_PATCHER_S2_MODEL_ID),
        OPEN_PATCHER_G2_MODEL_ID: (
            "advisor",
            "named_preset",
            OPEN_PATCHER_G2_MODEL_ID,
        ),
    }

    for model_id, (primitive, role, canonical) in expected.items():
        shape = model_to_openrouter_shape(MODELS[model_id])
        tr_meta = shape["trustedrouter"]

        assert orchestration_primitive(model_id) == primitive
        assert tr_meta["orchestration_primitive"] == primitive
        assert orchestration_role(model_id) == role
        assert tr_meta["orchestration_role"] == role
        assert canonical_orchestration_model_id(model_id) == canonical
        assert tr_meta["canonical_model_id"] == canonical


def test_open_weights_badge_is_recursive_for_combo_models() -> None:
    open_ids = [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.6",
        "moonshotai/kimi-k3",
        "google/gemma-4-31b-it",
        PROMETHEUS_MODEL_ID,
        PROMETHEUS_1_0_MODEL_ID,
        PROMETHEUS_2_0_MODEL_ID,
        IRIS_2_0_MODEL_ID,
        PLATO_MODEL_ID,
        PLATO_1_0_MODEL_ID,
        PLATO_PRO_MODEL_ID,
        PLATO_PRO_1_0_MODEL_ID,
        PLATO_PRO_2_0_MODEL_ID,
        OPEN_PATCHER_S1_MODEL_ID,
        OPEN_PATCHER_A1_MODEL_ID,
        OPEN_PATCHER_FAST1_MODEL_ID,
        OPEN_PATCHER_G2_MODEL_ID,
        OPEN_PATCHER_S2_MODEL_ID,
    ]
    for model_id in open_ids:
        assert model_open_weights(MODELS[model_id]), model_id
        assert model_to_openrouter_shape(MODELS[model_id])["trustedrouter"]["open_weights"] is True

    closed_ids = [
        "anthropic/claude-opus-4.8",
        SOCRATES_1_1_MODEL_ID,
        SOCRATES_PRO_PLUS_1_0_MODEL_ID,
        ZEUS_MODEL_ID,
        ZEUS_1_0_MODEL_ID,
        ARISTOTLE_MODEL_ID,
        ARISTOTLE_1_1_MODEL_ID,
        ARISTOTLE_1_0_MODEL_ID,
    ]
    for model_id in closed_ids:
        assert not model_open_weights(MODELS[model_id]), model_id
        assert model_to_openrouter_shape(MODELS[model_id])["trustedrouter"]["open_weights"] is False


def test_advisor_combo_models_are_cataloged_with_concrete_candidates() -> None:
    expected: dict[str, list[str]] = {
        ARISTOTLE_1_0_MODEL_ID: [
            "deepseek/deepseek-v4-flash",
            "anthropic/claude-opus-4.8",
            "openai/gpt-5.5",
            "google/gemini-3.1-pro-preview",
            "google/gemini-3.5-flash",
            "minimax/minimax-m3",
            "z-ai/glm-5.2",
            "xiaomi/mimo-v2.5-pro",
            "deepseek/deepseek-v4-pro",
        ],
        ARISTOTLE_1_1_MODEL_ID: [
            "z-ai/glm-5.2-fast",
            "z-ai/glm-5.2",
            "trustedrouter/zeus-1.0",
        ],
        ARISTOTLE_MODEL_ID: [
            "z-ai/glm-5.2-fast",
            "z-ai/glm-5.2",
            "trustedrouter/zeus-1.0",
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
            "z-ai/glm-5.2",
            "trustedrouter/prometheus-1.0-1m",
        ],
        PLATO_PRO_1_0_MODEL_ID: [
            "z-ai/glm-5.2",
            "trustedrouter/prometheus-1.0-1m",
        ],
        PLATO_PRO_2_0_MODEL_ID: [
            "z-ai/glm-5.2",
            "trustedrouter/prometheus-2.0",
        ],
        PLATO_PRO_MODEL_ID: [
            "z-ai/glm-5.2",
            "trustedrouter/prometheus-2.0",
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
            "minimax/minimax-m3",
            "z-ai/glm-5.2-fast",
            "deepseek/deepseek-v4-flash",
            "trustedrouter/zeus-1.0",
        ],
        SOCRATES_1_1_MODEL_ID: [
            "xiaomi/mimo-v2.5-pro-ultraspeed",
            "minimax/minimax-m3",
            "z-ai/glm-5.2-fast",
            "deepseek/deepseek-v4-flash",
            "trustedrouter/zeus-1.0",
        ],
        SOCRATES_PRO_PLUS_MODEL_ID: [
            "xiaomi/mimo-v2.5-pro-ultraspeed",
            "minimax/minimax-m3",
            "z-ai/glm-5.2-fast",
            "deepseek/deepseek-v4-flash",
            "trustedrouter/zeus-1.0",
        ],
        OPEN_PATCHER_A1_MODEL_ID: [
            "trustedrouter/openpatcher-s1",
            "trustedrouter/prometheus-1.0",
        ],
        OPEN_PATCHER_FAST1_MODEL_ID: [
            "z-ai/glm-5.2-fast",
            "trustedrouter/openpatcher-a1",
        ],
        OPEN_PATCHER_G1_MODEL_ID: [
            "z-ai/glm-5.2-fast",
            "z-ai/glm-5.2",
            "moonshotai/kimi-k2.7-code",
            "trustedrouter/prometheus-1.0-1m",
        ],
        OPEN_PATCHER_G2_MODEL_ID: [
            "moonshotai/kimi-k3",
            "google/gemma-4-31b-it",
            "trustedrouter/prometheus-2.0",
        ],
    }
    for model_id, candidates in expected.items():
        shape = model_to_openrouter_shape(MODELS[model_id])

        assert shape["trustedrouter"]["route_kind"] == "advisor_orchestration"
        assert shape["trustedrouter"]["stores_content"] is False
        assert shape["trustedrouter"]["auto_candidates"] == candidates
        assert [model.id for model in meta_candidate_models(model_id)] == candidates
    assert MODELS[PLATO_PRO_1_0_MODEL_ID].context_length == 1_048_576
    assert MODELS[PLATO_PRO_2_0_MODEL_ID].context_length == 1_048_576
    assert MODELS[PLATO_PRO_MODEL_ID].context_length == 1_048_576
    assert MODELS[OPEN_PATCHER_G1_MODEL_ID].context_length == 1_048_576
    assert MODELS[OPEN_PATCHER_G2_MODEL_ID].context_length == 1_048_576
    assert MODELS[ARISTOTLE_1_1_MODEL_ID].context_length == 1_048_576
    assert MODELS[ARISTOTLE_MODEL_ID].context_length == 1_048_576


def test_liberty_models_publish_verified_components_and_honest_context_limits() -> None:
    expected = {
        LIBERTY_1_0_MODEL_ID: (
            "fusion_panel",
            262_144,
            [
                "thinkingmachines/inkling",
                "nvidia/nemotron-3-ultra-550b-a55b",
                "google/gemma-4-31b-it",
            ],
        ),
        LIBERTY_1_0_1M_MODEL_ID: (
            "fusion_panel",
            1_048_576,
            [
                "thinkingmachines/inkling-1m",
                "nvidia/nemotron-3-ultra-550b-a55b",
            ],
        ),
        LIBERTY_2_0_MODEL_ID: (
            "advisor_orchestration",
            262_144,
            [
                "nvidia/nemotron-3-ultra-550b-a55b",
                LIBERTY_1_0_1M_MODEL_ID,
                LIBERTY_1_0_MODEL_ID,
            ],
        ),
        LIBERTY_3_0_MODEL_ID: (
            "advisor_orchestration",
            1_048_576,
            [
                "nvidia/nemotron-3-ultra-550b-a55b",
                "google/gemma-4-31b-it",
                "openai/gpt-oss-120b",
                LIBERTY_1_0_1M_MODEL_ID,
                "thinkingmachines/inkling",
            ],
        ),
    }

    for model_id, (route_kind, context_length, candidates) in expected.items():
        model = MODELS[model_id]
        metadata = model_to_openrouter_shape(model)["trustedrouter"]

        assert model.context_length == context_length
        assert metadata["route_kind"] == route_kind
        assert metadata["auto_candidates"] == candidates
        assert [candidate.id for candidate in meta_candidate_models(model_id)] == candidates
        assert metadata["open_weights"] is True

    inkling = MODELS["thinkingmachines/inkling"]
    assert inkling.context_length == 1_048_576
    assert inkling.provider == "together"
    endpoints = endpoints_for_model(inkling.id)
    assert {(endpoint.provider, endpoint.upstream_id) for endpoint in endpoints} == {
        ("gmi", "thinkingmachines/Inkling"),
        ("thinkingmachines", "thinkingmachines/Inkling:peft:262144"),
        ("together", "thinkingmachines/Inkling"),
    }
    assert any(endpoint.usage_type == "Credits" for endpoint in endpoints)
    assert any(endpoint.usage_type == "BYOK" for endpoint in endpoints)

    inkling_1m = MODELS["thinkingmachines/inkling-1m"]
    assert inkling_1m.context_length == 1_048_576
    assert inkling_1m.provider == "baseten"
    assert {
        (endpoint.provider, endpoint.upstream_id) for endpoint in endpoints_for_model(inkling_1m.id)
    } == {("baseten", "thinkingmachines/inkling")}


def test_liberty_nemotron_resolves_only_to_working_canonical_prepaid_routes() -> None:
    model_id = "nvidia/nemotron-3-ultra-550b-a55b"
    assert model_id in MODELS
    assert "nvidia/nvidia-nemotron-3-ultra-550b-a55b" not in MODELS
    assert "nvidia/Nemotron-3-Ultra-550b-a55b" not in MODELS

    prepaid = {
        endpoint.provider: endpoint.upstream_id
        for endpoint in endpoints_for_model(model_id)
        if endpoint.usage_type == "Credits"
    }
    assert prepaid["baseten"] == "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B"
    assert prepaid["nebius"] == "nvidia/Nemotron-3-Ultra-550b-a55b"
    assert prepaid["together"] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert "gmi" not in prepaid


def test_athena_catalog_hides_orchestration_configuration() -> None:
    from trusted_router.catalog import ATHENA_MODEL_ID

    model = MODELS[ATHENA_MODEL_ID]
    shape = model_to_openrouter_shape(model)

    assert model.name == "TrustedRouter Athena"
    assert model.hidden_public_metadata is True
    assert model.context_length == 1_048_576
    assert shape["trustedrouter"]["route_kind"] == "private_orchestration"
    assert shape["trustedrouter"]["configuration_hidden"] is True
    assert shape["trustedrouter"]["auto_candidates"] is None
    assert [model.id for model in meta_candidate_models(ATHENA_MODEL_ID)] == [
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        "trustedrouter/zeus-1.0-mini",
        "moonshotai/kimi-k2.7-code",
        "moonshotai/kimi-k2.6",
    ]
    assert shape["trustedrouter"]["open_weights"] is False


def test_zeus_1_0_and_mini_have_expected_panels() -> None:
    assert MODELS[ZEUS_MODEL_ID].context_length == 1_048_576
    assert MODELS[ZEUS_1_0_MODEL_ID].context_length == 1_048_576
    assert MODELS[ZEUS_1_0_MINI_MODEL_ID].context_length == 1_048_576
    assert [model.id for model in meta_candidate_models(ZEUS_1_0_MODEL_ID)] == [
        "anthropic/claude-opus-4.8",
        "openai/gpt-5.5",
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.5-flash",
        "minimax/minimax-m3",
        "z-ai/glm-5.2",
        "xiaomi/mimo-v2.5-pro",
        "deepseek/deepseek-v4-pro",
    ]
    zeus_shape = model_to_openrouter_shape(MODELS[ZEUS_1_0_MODEL_ID])
    assert zeus_shape["trustedrouter"]["us_provider_available"] is True
    assert zeus_shape["trustedrouter"]["eu_focused_provider_available"] is True
    assert [model.id for model in meta_candidate_models(ZEUS_1_0_MINI_MODEL_ID)] == [
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.5-flash",
        "minimax/minimax-m3",
        "z-ai/glm-5.2",
        "xiaomi/mimo-v2.5-pro",
        "deepseek/deepseek-v4-pro",
    ]
    assert model_us_provider_available(MODELS[ZEUS_1_0_MINI_MODEL_ID]) is True
    assert model_eu_focused_provider_available(MODELS[ZEUS_1_0_MINI_MODEL_ID]) is True


def test_openpatcher_s1_is_cataloged_as_custom_synth_preset() -> None:
    model = MODELS[OPEN_PATCHER_S1_MODEL_ID]
    shape = model_to_openrouter_shape(model)

    assert model.name == "TrustedRouter OpenPatcher-S1"
    assert shape["trustedrouter"]["route_kind"] == "fusion_panel"
    assert shape["trustedrouter"]["stores_content"] is False
    assert shape["trustedrouter"]["auto_candidates"] == [
        "moonshotai/kimi-k2.7-code",
        "z-ai/glm-5.2",
    ]
    assert [model.id for model in meta_candidate_models(OPEN_PATCHER_S1_MODEL_ID)] == [
        "moonshotai/kimi-k2.7-code",
        "z-ai/glm-5.2",
    ]


def test_openpatcher_s2_replaces_k2_with_k3_without_mutating_s1() -> None:
    s1_candidates = [model.id for model in meta_candidate_models(OPEN_PATCHER_S1_MODEL_ID)]
    s2 = MODELS[OPEN_PATCHER_S2_MODEL_ID]
    s2_candidates = [model.id for model in meta_candidate_models(OPEN_PATCHER_S2_MODEL_ID)]

    assert s1_candidates == ["moonshotai/kimi-k2.7-code", "z-ai/glm-5.2"]
    assert s2.name == "TrustedRouter OpenPatcher-S2"
    assert s2.context_length == 1_048_576
    assert s2_candidates == ["moonshotai/kimi-k3", "z-ai/glm-5.2"]
    assert model_to_openrouter_shape(s2)["trustedrouter"]["auto_candidates"] == s2_candidates


def test_iris_2_and_rolling_alias_use_k3_while_iris_1_stays_pinned() -> None:
    expected = [
        "minimax/minimax-m3",
        "moonshotai/kimi-k3",
        "deepseek/deepseek-v4-pro",
    ]

    assert [model.id for model in meta_candidate_models(IRIS_1_0_MODEL_ID)] == [
        "minimax/minimax-m3",
        "moonshotai/kimi-k2.6",
        "deepseek/deepseek-v4-pro",
    ]
    for model_id in (IRIS_MODEL_ID, IRIS_2_0_MODEL_ID):
        assert MODELS[model_id].context_length == 1_048_576
        assert [model.id for model in meta_candidate_models(model_id)] == expected
        assert (
            model_to_openrouter_shape(MODELS[model_id])["trustedrouter"]["auto_candidates"]
            == expected
        )

    assert canonical_orchestration_model_id(IRIS_MODEL_ID) == IRIS_2_0_MODEL_ID


def test_prometheus_1m_uses_only_long_context_open_weight_components() -> None:
    model = MODELS[PROMETHEUS_1_0_1M_MODEL_ID]
    candidates = meta_candidate_models(PROMETHEUS_1_0_1M_MODEL_ID)
    candidate_ids = [candidate.id for candidate in candidates]

    assert model.name == "TrustedRouter Prometheus 1.0 1M"
    assert model.context_length == 1_048_576
    assert candidate_ids == [
        "minimax/minimax-m3",
        "xiaomi/mimo-v2.5-pro",
        "z-ai/glm-5.2",
        "deepseek/deepseek-v4-pro",
    ]
    assert all(candidate.context_length >= 1_000_000 for candidate in candidates)
    assert all(model_open_weights(candidate) for candidate in candidates)

    shape = model_to_openrouter_shape(model)
    assert shape["context_length"] == 1_048_576
    assert shape["trustedrouter"]["route_kind"] == "fusion_panel"
    assert shape["trustedrouter"]["auto_candidates"] == candidate_ids
    assert shape["trustedrouter"]["open_weights"] is True


def test_prometheus_2_uses_requested_one_million_open_weight_graph() -> None:
    expected = [
        "minimax/minimax-m3",
        "moonshotai/kimi-k3",
        "z-ai/glm-5.2",
        "deepseek/deepseek-v4-pro",
        "xiaomi/mimo-v2.5-pro",
    ]
    for model_id in (PROMETHEUS_MODEL_ID, PROMETHEUS_2_0_MODEL_ID):
        model = MODELS[model_id]
        shape = model_to_openrouter_shape(model)

        assert model.context_length == 1_048_576
        assert [candidate.id for candidate in meta_candidate_models(model_id)] == expected
        assert shape["trustedrouter"]["auto_candidates"] == expected
        assert shape["trustedrouter"]["open_weights"] is True

    assert canonical_orchestration_model_id(PROMETHEUS_MODEL_ID) == PROMETHEUS_2_0_MODEL_ID


def test_trustedrouter_meta_models_are_credits_only_not_byok() -> None:
    for model_id in sorted(META_MODEL_IDS):
        shape = model_to_openrouter_shape(MODELS[model_id])
        tr_meta = shape["trustedrouter"]

        assert tr_meta["prepaid_available"] is True, model_id
        assert tr_meta["byok_available"] is False, model_id
        assert not [
            endpoint for endpoint in endpoints_for_model(model_id) if endpoint.usage_type == "BYOK"
        ], model_id


@pytest.mark.parametrize(
    "model_id",
    [
        ZDR_MODEL_ID,
        E2E_MODEL_ID,
        EU_MODEL_ID,
        SOCRATES_1_1_MODEL_ID,
        SOCRATES_PRO_PLUS_1_0_MODEL_ID,
        OPEN_PATCHER_G1_MODEL_ID,
        OPEN_PATCHER_G2_MODEL_ID,
        OPEN_PATCHER_S2_MODEL_ID,
        IRIS_2_0_MODEL_ID,
        PLATO_PRO_2_0_MODEL_ID,
        ZEUS_1_0_MODEL_ID,
        PROMETHEUS_1_0_1M_MODEL_ID,
        PROMETHEUS_2_0_MODEL_ID,
    ],
)
def test_trustedrouter_meta_route_expansion_is_credits_only(model_id: str) -> None:
    endpoints = chat_route_endpoint_candidates(
        {"model": model_id},
        Settings(environment="test"),
    )

    assert endpoints
    assert all(endpoint.usage_type == "Credits" for _model, endpoint in endpoints)

    with pytest.raises(Exception) as exc:
        chat_route_endpoint_candidates(
            {"model": model_id, "provider": {"usage": "byok"}},
            Settings(environment="test"),
        )
    assert getattr(exc.value, "status_code", None) == 400
    assert "do not support BYOK" in str(exc.value)


@pytest.mark.parametrize(
    "model_id",
    [
        OPEN_PATCHER_S1_MODEL_ID,
        OPEN_PATCHER_A1_MODEL_ID,
        OPEN_PATCHER_FAST1_MODEL_ID,
        OPEN_PATCHER_G1_MODEL_ID,
        ATHENA_MODEL_ID,
    ],
)
def test_openpatcher_and_athena_force_us_provider_routes(model_id: str) -> None:
    shape = model_to_openrouter_shape(MODELS[model_id])
    assert shape["trustedrouter"]["required_provider_jurisdiction"] == PROVIDER_JURISDICTION_US

    if model_id == OPEN_PATCHER_A1_MODEL_ID:
        # A1 is meta-on-meta: the enclave decomposes it into OpenPatcher-S1
        # and Prometheus sub-orchestrations. There is no direct control-plane
        # endpoint list to inspect here; Go tests pin the subrequest policy.
        return

    endpoints = chat_route_endpoint_candidates(
        {"model": model_id},
        Settings(environment="test"),
    )

    assert endpoints
    assert all(
        PROVIDERS[endpoint.provider].provider_headquarters_country == PROVIDER_JURISDICTION_US
        for _model, endpoint in endpoints
    )
    assert {"kimi", "zai", "xiaomi", "minimax", "siliconflow"}.isdisjoint(
        {endpoint.provider for _model, endpoint in endpoints}
    )


def test_openpatcher_g2_explicitly_uses_global_moonshot_k3_route() -> None:
    shape = model_to_openrouter_shape(MODELS[OPEN_PATCHER_G2_MODEL_ID])

    assert shape["trustedrouter"]["required_provider_jurisdiction"] is None
    assert shape["trustedrouter"]["auto_candidates"] == [
        "moonshotai/kimi-k3",
        "google/gemma-4-31b-it",
        "trustedrouter/prometheus-2.0",
    ]

    s2_shape = model_to_openrouter_shape(MODELS[OPEN_PATCHER_S2_MODEL_ID])
    assert s2_shape["trustedrouter"]["required_provider_jurisdiction"] is None
    assert s2_shape["trustedrouter"]["auto_candidates"] == [
        "moonshotai/kimi-k3",
        "z-ai/glm-5.2",
    ]


def test_provider_jurisdiction_filter_keeps_only_us_based_endpoints() -> None:
    endpoints = chat_route_endpoint_candidates(
        {"model": "z-ai/glm-5.2", "provider": {"jurisdiction": "us"}},
        Settings(environment="test"),
    )

    assert endpoints
    assert all(
        PROVIDERS[endpoint.provider].provider_headquarters_country == PROVIDER_JURISDICTION_US
        for _model, endpoint in endpoints
    )


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
    assert e2e_endpoints[0][1].provider == "tinfoil"
    assert "anthropic" not in {endpoint.provider for _model, endpoint in zdr_endpoints}
    assert "google-ai-studio" not in {
        endpoint.provider for _model, endpoint in zdr_endpoints
    }
    assert "google-vertex" not in {
        endpoint.provider for _model, endpoint in zdr_endpoints
    }
    assert "openai" not in {endpoint.provider for _model, endpoint in zdr_endpoints}
    assert all(
        endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_ZERO_RETENTION
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
    assert {endpoint.provider for _model, endpoint in narrowed} == {"google-vertex"}


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
        ("moonshotai/kimi-k3", "kimi"),
        ("moonshotai/kimi-k2.6", "kimi"),
        ("openai/gpt-4.1-mini", "openai"),
        ("mistralai/mistral-small-2603", "mistral"),
        ("deepseek/deepseek-v4-flash", "deepseek"),
        ("meta-llama/llama-3.1-8b-instruct", "novita"),
        ("google/gemini-2.5-flash", "google-ai-studio"),
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
    """Xiaomi MiMo onboarding: the live chat models load from the static manifest,
    map to the right upstream ids, and have a prepaid (Credits) xiaomi endpoint
    the attested gateway can dispatch."""
    from trusted_router.catalog import PROVIDERS, endpoints_for_model

    assert "xiaomi" in PROVIDERS
    assert "xiaomi" in GATEWAY_PREPAID_PROVIDER_SLUGS
    expected = {
        "xiaomi/mimo-v2.5": "mimo-v2.5",
        "xiaomi/mimo-v2.5-pro": "mimo-v2.5-pro",
        "xiaomi/mimo-v2.5-pro-ultraspeed": "mimo-v2.5-pro-ultraspeed",
    }
    for model_id, upstream in expected.items():
        model = MODELS.get(model_id)
        assert model is not None, f"{model_id} missing from catalog"
        assert model.supports_chat, f"{model_id} not chat"
        assert model.provider == "xiaomi"
        assert model.prompt_price_microdollars_per_million_tokens > 0
        credits = [
            e
            for e in endpoints_for_model(model_id)
            if str(e.usage_type) == "Credits" and e.provider == "xiaomi"
        ]
        assert credits, f"{model_id} has no xiaomi prepaid endpoint"
        assert {endpoint.upstream_id for endpoint in credits} == {upstream}

    pro = MODELS["xiaomi/mimo-v2.5-pro"]
    assert pro.context_length == 1_048_576
    assert pro.prompt_price_microdollars_per_million_tokens == 456_750
    assert pro.completion_price_microdollars_per_million_tokens == 913_500

    # UltraSpeed is the 1T-param speed-serving tier with its own ¥9/¥18
    # ($1.305/$2.61) cost, marked up by the manifest loader (cost x 1.05,
    # $0.01/M floor). Guard the exact prices so a regen can't silently
    # collapse them onto the regular v2.5-pro numbers.
    ultraspeed = MODELS["xiaomi/mimo-v2.5-pro-ultraspeed"]
    assert ultraspeed.prompt_price_microdollars_per_million_tokens == 1_370_250
    assert ultraspeed.completion_price_microdollars_per_million_tokens == 2_740_500
    # ...and that it is genuinely a distinct row from regular v2.5-pro.
    assert (
        ultraspeed.completion_price_microdollars_per_million_tokens
        != pro.completion_price_microdollars_per_million_tokens
    )


def test_crusoe_provider_models_fail_closed_after_account_auth_failure() -> None:
    """A rejected operator credential must darken stale Crusoe inventory."""

    assert "crusoe" in PROVIDERS
    assert "crusoe" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert not [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "crusoe"
    ]


def test_makora_provider_models_present_and_routable() -> None:
    """Makora onboarding: native ids from Makora's OpenAI-compatible catalog
    load from the manifest and create prepaid + BYOK endpoints."""

    assert "makora" in PROVIDERS
    assert "makora" in GATEWAY_PREPAID_PROVIDER_SLUGS
    expected = {
        "deepseek/deepseek-v4-flash": "deepseek-ai/DeepSeek-V4-Flash",
        "deepseek/deepseek-v4-pro": "deepseek-ai/DeepSeek-V4-Pro",
        "google/gemma-4-26b-a4b-it": "google/gemma-4-26B-A4B",
        "z-ai/glm-5.2": "zai-org/GLM-5.2-FP8",
        "z-ai/glm-5.2-nvfp4": "zai-org/GLM-5.2-NVFP4",
        "moonshotai/kimi-k2.7-code": "moonshotai/Kimi-K2.7-Code",
        "qwen/qwen3.6-35b-a3b": "unsloth/Qwen3.6-35B-A3B-NVFP4",
    }
    makora_model_ids = {
        endpoint.model_id
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.provider == "makora" and str(endpoint.usage_type) == "Credits"
    }
    assert len(makora_model_ids) >= 8
    assert "qwen/qwen3.6-27b" not in makora_model_ids
    assert "openai/gpt-oss-120b" not in makora_model_ids
    for model_id, upstream in expected.items():
        model = MODELS.get(model_id)
        assert model is not None, f"{model_id} missing from catalog"
        credits = [
            e
            for e in endpoints_for_model(model_id)
            if str(e.usage_type) == "Credits" and e.provider == "makora"
        ]
        byok = [
            e
            for e in endpoints_for_model(model_id)
            if str(e.usage_type) == "BYOK" and e.provider == "makora"
        ]
        assert credits, f"{model_id} has no makora prepaid endpoint"
        assert byok, f"{model_id} has no makora BYOK endpoint"
        assert credits[0].upstream_id == upstream
        assert credits[0].prompt_price_microdollars_per_million_tokens > 0


def test_makora_provider_prices_follow_published_lineup() -> None:
    """Makora publishes per-token model prices on its homepage lineup.

    The provider manifest stores raw upstream cost in microdollars/M; the
    catalog applies the standard 5% customer markup at load time.
    """

    expected_prices = {
        "deepseek/deepseek-v4-flash": (119_070, 293_055, 89_355),
        "deepseek/deepseek-v4-pro": (1_383_900, 2_767_905, 1_037_925),
        "z-ai/glm-5.2": (1_417_500, 4_189_500, 252_000),
        "moonshotai/kimi-k2.7-code": (798_000, 3_963_645, 604_485),
        "meta-llama/llama-3.3-70b-instruct": (189_000, 420_000, 157_500),
        "qwen/qwen3.6-35b-a3b": (180_600, 1_260_210, 135_450),
    }

    for model_id, (prompt, completion, cached_prompt) in expected_prices.items():
        credits = [
            e
            for e in endpoints_for_model(model_id)
            if str(e.usage_type) == "Credits" and e.provider == "makora"
        ]
        assert credits, f"{model_id} has no makora prepaid endpoint"
        endpoint = credits[0]
        assert endpoint.prompt_price_microdollars_per_million_tokens == prompt
        assert endpoint.completion_price_microdollars_per_million_tokens == completion
        assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == (
            cached_prompt
        )


def test_anthropic_claude_fable_5_is_available_but_not_zdr_routable() -> None:
    """Claude Fable 5 is available again, but it is not a ZDR route."""
    model = MODELS["anthropic/claude-fable-5"]
    endpoints = endpoints_for_model(model.id)
    assert endpoints
    assert "anthropic" in {endpoint.provider for endpoint in endpoints}
    assert all(endpoint_privacy_tier(endpoint) == PRIVACY_TIER_STANDARD for endpoint in endpoints)

    shape = model_to_openrouter_shape(model)
    meta = shape["trustedrouter"]
    assert meta["provider_zero_data_retention"] is False
    assert meta["privacy_tier"] == PRIVACY_TIER_STANDARD
    assert "not tracked as ZDR" in str(meta["provider_policy"])
    assert all(endpoint["provider_zero_data_retention"] is False for endpoint in meta["endpoints"])
    assert "anthropic/claude-fable-5" not in {
        model.id for model in meta_candidate_models(ZDR_MODEL_ID)
    }
    with pytest.raises(Exception) as exc:
        chat_route_endpoint_candidates(
            {
                "model": "anthropic/claude-fable-5",
                "messages": [{"role": "user", "content": "pong"}],
                "provider": {"min_privacy": "zdr"},
            },
            Settings(environment="test"),
        )
    assert getattr(exc.value, "status_code", None) == 400
    assert "No route candidates match" in str(exc.value)


def test_wafer_kimi_k26_is_available_but_standard_tier_only() -> None:
    model = MODELS["moonshotai/kimi-k2.6"]
    wafer_endpoints = [
        endpoint for endpoint in endpoints_for_model(model.id) if endpoint.provider == "wafer"
    ]
    if not wafer_endpoints:
        pytest.skip("wafer no longer lists kimi-k2.6 — delisted upstream")
    assert all(
        endpoint_privacy_tier(endpoint) == PRIVACY_TIER_STANDARD for endpoint in wafer_endpoints
    )

    shape = model_to_openrouter_shape(model)
    wafer_meta = [
        endpoint
        for endpoint in shape["trustedrouter"]["endpoints"]
        if endpoint["provider"] == "wafer"
    ]
    assert wafer_meta
    assert all(endpoint["provider_zero_data_retention"] is False for endpoint in wafer_meta)
    assert "withdrew ZDR support" in str(wafer_meta[0]["provider_policy"])

    zdr_endpoints = chat_route_endpoint_candidates(
        {"model": ZDR_MODEL_ID},
        Settings(environment="test"),
    )
    assert not [
        endpoint
        for _model, endpoint in zdr_endpoints
        if endpoint.provider == "wafer" and endpoint.model_id == "moonshotai/kimi-k2.6"
    ]
    with pytest.raises(Exception) as exc:
        chat_route_endpoint_candidates(
            {
                "model": "moonshotai/kimi-k2.6",
                "messages": [{"role": "user", "content": "pong"}],
                "provider": {"only": ["wafer"], "min_privacy": "zdr"},
            },
            Settings(environment="test"),
        )
    assert getattr(exc.value, "status_code", None) == 400
    assert "No route candidates match" in str(exc.value)


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
    assert gmi.prompt_price_microdollars_per_million_tokens == 1_029_000
    assert gmi.completion_price_microdollars_per_million_tokens == 3_234_000
    assert deepinfra.prompt_price_microdollars_per_million_tokens == 1_260_000
    assert deepinfra.completion_price_microdollars_per_million_tokens == 4_410_000
    assert fireworks.prompt_price_microdollars_per_million_tokens == 1_470_000
    assert fireworks.completion_price_microdollars_per_million_tokens == 4_620_000
    assert novita.prompt_price_microdollars_per_million_tokens == 1_470_000
    assert novita.completion_price_microdollars_per_million_tokens == 4_620_000
    assert friendli.prompt_price_microdollars_per_million_tokens == 1_470_000
    assert friendli.completion_price_microdollars_per_million_tokens == 4_620_000
    assert baseten.prompt_price_microdollars_per_million_tokens == 1_470_000
    assert baseten.completion_price_microdollars_per_million_tokens == 4_620_000
    assert wafer.prompt_price_microdollars_per_million_tokens == 1_260_000
    assert wafer.completion_price_microdollars_per_million_tokens == 4_305_000
    assert "z-ai/glm-5.2@crusoe/prepaid" not in MODEL_ENDPOINTS


def test_parasail_qwen_397b_uses_working_native_upstream_id() -> None:
    prepaid = MODEL_ENDPOINTS["qwen/qwen3.5-397b-a17b@parasail/prepaid"]
    byok = MODEL_ENDPOINTS["qwen/qwen3.5-397b-a17b@parasail/byok"]

    assert MODELS["qwen/qwen3.5-397b-a17b"].context_length == 262_144
    assert prepaid.upstream_id == "parasail-qwen35-397b-a17b"
    assert byok.upstream_id == "parasail-qwen35-397b-a17b"
    assert prepaid.prompt_price_microdollars_per_million_tokens == 525_000
    assert prepaid.completion_price_microdollars_per_million_tokens == 3_780_000
