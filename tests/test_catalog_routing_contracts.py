from __future__ import annotations

import json

import pytest

from trusted_router.catalog import (
    ADVISOR_CATALOG_MODEL_ORDERS,
    ADVISOR_MODEL_ID,
    ARISTOTLE_1_0_MODEL_ID,
    ARISTOTLE_1_1_MODEL_ID,
    ARISTOTLE_2_0_MODEL_ID,
    ARISTOTLE_MODEL_ID,
    ATHENA_1_0_MODEL_ID,
    ATHENA_2_0_MODEL_ID,
    ATHENA_MODEL_ID,
    AUTO_MODEL_ID,
    CONFIDENTIAL_MODEL_ID,
    DEEPSEEK_V4_PRO_0423_MODEL_ID,
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
    E2E_MODEL_ID,
    EU_FOCUSED_PROVIDER_ORDER,
    EU_MODEL_ID,
    FUSION_MODEL_ID,
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    IRIS_1_0_MODEL_ID,
    IRIS_2_0_MODEL_ID,
    IRIS_3_0_MODEL_ID,
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
    OPEN_PATCHER_G3_MODEL_ID,
    OPEN_PATCHER_S1_MODEL_ID,
    OPEN_PATCHER_S2_MODEL_ID,
    OPEN_PATCHER_S3_MODEL_ID,
    PARASAIL_LIBERTY_2_0_MODEL_ID,
    PLATO_1_0_MODEL_ID,
    PLATO_3_0_MODEL_ID,
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
    PROMETHEUS_3_0_MODEL_ID,
    PROMETHEUS_MODEL_ID,
    PROVIDER_JURISDICTION_US,
    PROVIDERS,
    SELECTOR_MODEL_ID,
    SOCRATES_1_0_MODEL_ID,
    SOCRATES_1_1_MODEL_ID,
    SOCRATES_2_0_MODEL_ID,
    SOCRATES_MODEL_ID,
    SOCRATES_PRO_1_0_MODEL_ID,
    SOCRATES_PRO_MODEL_ID,
    SOCRATES_PRO_PLUS_1_0_MODEL_ID,
    SOCRATES_PRO_PLUS_MODEL_ID,
    SYNTH_MODEL_ID,
    ZDR_MODEL_ID,
    ZEUS_1_0_MINI_MODEL_ID,
    ZEUS_1_0_MODEL_ID,
    ZEUS_2_0_MODEL_ID,
    ZEUS_MODEL_ID,
    InvalidAutoModelOrder,
    auto_candidate_models,
    canonical_orchestration_model_id,
    endpoint_privacy_tier,
    endpoint_stores_content,
    endpoint_zero_data_retention,
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
from trusted_router.catalog_ingest import _authoritative_provider_model_ids, _modalities
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.provider_lifecycle import provider_model_retired
from trusted_router.routes.internal.gateway import _gateway_provider_route_payload
from trusted_router.routing import chat_route_candidates, chat_route_endpoint_candidates


def _cataloged_model_ids(model_ids: list[str]) -> list[str]:
    return [model_id for model_id in model_ids if model_id in MODELS]


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
    assert "moonshotai/kimi-k3@gmi/prepaid" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k3@gmi/byok" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k3@phala/prepaid" in MODEL_ENDPOINTS
    kimi_k3_routes = {
        (endpoint.provider, endpoint.usage_type)
        for endpoint in endpoints_for_model("moonshotai/kimi-k3")
    }
    assert {
        ("kimi", "Credits"),
        ("siliconflow", "Credits"),
        ("baseten", "Credits"),
        ("atlas-cloud", "Credits"),
        ("novita", "Credits"),
        ("nebius", "Credits"),
        ("fireworks", "Credits"),
        ("gmi", "BYOK"),
    } <= kimi_k3_routes
    kimi_k3 = MODELS["moonshotai/kimi-k3"]
    from trusted_router.catalog_ingest import _PROVIDER_MODELS_DIR
    from trusted_router.pricing import _customer_price

    kimi_manifest = json.loads((_PROVIDER_MODELS_DIR / "kimi.json").read_text(encoding="utf-8"))
    kimi_k3_row = next(row for row in kimi_manifest["models"] if row["id"] == "moonshotai/kimi-k3")
    expected_prompt_price = _customer_price(int(kimi_k3_row["input_token_price_per_m"]))
    expected_completion_price = _customer_price(int(kimi_k3_row["output_token_price_per_m"]))
    expected_cached_prompt_price = _customer_price(
        int(kimi_k3_row["cached_input_token_price_per_m"])
    )
    kimi_k3_endpoints = endpoints_for_model("moonshotai/kimi-k3")
    assert kimi_k3.context_length == 1_048_576
    assert kimi_k3.prompt_price_microdollars_per_million_tokens in {
        endpoint.prompt_price_microdollars_per_million_tokens for endpoint in kimi_k3_endpoints
    }
    assert kimi_k3.completion_price_microdollars_per_million_tokens in {
        endpoint.completion_price_microdollars_per_million_tokens for endpoint in kimi_k3_endpoints
    }
    kimi_k3_direct = MODEL_ENDPOINTS["moonshotai/kimi-k3@kimi/prepaid"]
    assert kimi_k3_direct.upstream_id == "kimi-k3"
    assert kimi_k3_direct.prompt_price_microdollars_per_million_tokens == expected_prompt_price
    assert (
        kimi_k3_direct.completion_price_microdollars_per_million_tokens == expected_completion_price
    )
    assert (
        kimi_k3_direct.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens
        == expected_cached_prompt_price
    )
    assert MODEL_ENDPOINTS["moonshotai/kimi-k3@novita/prepaid"].upstream_id == "moonshotai/kimi-k3"
    assert (
        MODEL_ENDPOINTS["moonshotai/kimi-k3@siliconflow/prepaid"].upstream_id
        == "moonshotai/Kimi-K3"
    )
    assert MODEL_ENDPOINTS["moonshotai/kimi-k3@baseten/prepaid"].upstream_id == "moonshotai/Kimi-K3"
    assert MODEL_ENDPOINTS["moonshotai/kimi-k3@nebius/prepaid"].upstream_id == "moonshotai/Kimi-K3"
    assert (
        MODEL_ENDPOINTS["moonshotai/kimi-k3@fireworks/prepaid"].upstream_id
        == "accounts/fireworks/models/kimi-k3"
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
        ("cerebras/gpt-oss-120b", "cerebras"),
    ]:
        assert f"{model_id}@{provider}/prepaid" in MODEL_ENDPOINTS
        assert f"{model_id}@{provider}/byok" in MODEL_ENDPOINTS
    for model in MODELS.values():
        assert model.provider in PROVIDERS
        assert isinstance(model.prompt_price_microdollars_per_million_tokens, int)
        assert isinstance(model.completion_price_microdollars_per_million_tokens, int)
        assert isinstance(model.minimum_charge_microdollars, int)
        assert model.prompt_price_microdollars_per_million_tokens >= 0
        assert model.completion_price_microdollars_per_million_tokens >= 0
        assert model.minimum_charge_microdollars >= 0
        assert (
            model.prompt_price_microdollars_per_million_tokens
            <= model.published_prompt_price_microdollars_per_million_tokens
        )
        assert (
            model.completion_price_microdollars_per_million_tokens
            <= model.published_completion_price_microdollars_per_million_tokens
        )


def test_parasail_liberty_catalog_publishes_fixed_credits_only_price() -> None:
    model = MODELS[PARASAIL_LIBERTY_2_0_MODEL_ID]
    shape = model_to_openrouter_shape(model)

    assert shape["pricing"]["prompt"] == "0.000002"
    assert shape["pricing"]["completion"] == "0.000019"
    assert shape["pricing"]["minimum"] == "0.001"
    assert shape["trustedrouter"]["prompt_price_microdollars_per_million_tokens"] == 2_000_000
    assert shape["trustedrouter"]["completion_price_microdollars_per_million_tokens"] == 19_000_000
    assert shape["trustedrouter"]["minimum_charge_microdollars"] == 1_000
    assert shape["trustedrouter"]["prepaid_available"] is True
    assert shape["trustedrouter"]["byok_available"] is False
    assert shape["trustedrouter"]["route_kind"] == "advisor_orchestration"
    assert [candidate.id for candidate in meta_candidate_models(model.id)] == [
        "nvidia/nemotron-3-ultra-550b-a55b",
        LIBERTY_1_0_1M_MODEL_ID,
        LIBERTY_1_0_MODEL_ID,
    ]


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
    openai_endpoints = [
        endpoint for endpoint in meta["endpoints"] if endpoint["provider"] == "openai"
    ]
    by_usage = {endpoint["usage_type"]: endpoint for endpoint in openai_endpoints}
    assert by_usage["Credits"]["stores_content"] is False
    assert by_usage["Credits"]["provider_zero_data_retention"] is True
    assert by_usage["Credits"]["zero_data_retention_scope"] == "trustedrouter_prepaid"
    assert by_usage["BYOK"]["stores_content"] is True
    assert by_usage["BYOK"]["provider_zero_data_retention"] is False
    assert by_usage["BYOK"]["zero_data_retention_scope"] is None


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
            18,
            [
                # Nebius retired Meta-Llama-3.1-8B + gemma-2-2b-it earlier, then
                # announced 11 more Token Factory model retirements for
                # 2026-06-22 and 12 more for 2026-08-31. Those are intentionally
                # absent after their cutovers; this contract keeps representative
                # non-deprecated Nebius routes alive.
                "Qwen/Qwen3.5-397B-A17B",
                "deepseek-ai/DeepSeek-V4-Pro",
                "MiniMaxAI/MiniMax-M3",
                "moonshotai/kimi-k3",
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
            5,
            [
                "x-ai/grok-4.6",
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


def test_cerebras_native_catalog_preserves_every_live_model_id() -> None:
    expected = _authoritative_provider_model_ids("cerebras")
    provider_model_ids = {
        endpoint.model_id
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.provider == "cerebras"
    }

    assert expected
    assert provider_model_ids == expected
    for model_id in expected:
        assert f"{model_id}@cerebras/prepaid" in MODEL_ENDPOINTS
        assert f"{model_id}@cerebras/byok" in MODEL_ENDPOINTS


def test_non_chat_deepseek_ocr_is_not_routable_as_chat() -> None:
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
    assert prepaid.prompt_price_microdollars_per_million_tokens > 0
    assert prepaid.completion_price_microdollars_per_million_tokens > 0
    cached_prompt = prepaid.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens
    assert cached_prompt is not None
    assert 0 < cached_prompt < prepaid.prompt_price_microdollars_per_million_tokens


def test_grok_46_uses_xai_native_model_id_and_long_context_pricing() -> None:
    model = MODELS["x-ai/grok-4.6"]
    prepaid = MODEL_ENDPOINTS["x-ai/grok-4.6@grok/prepaid"]
    byok = MODEL_ENDPOINTS["x-ai/grok-4.6@grok/byok"]

    assert model.provider == "grok"
    assert model.context_length == 500_000
    assert prepaid.upstream_id == "grok-4.6"
    assert byok.upstream_id == "grok-4.6"
    assert [tier.max_prompt_tokens for tier in prepaid.price_tiers] == [200_000, None]
    for tier in prepaid.price_tiers:
        assert tier.prompt_price_microdollars_per_million_tokens > 0
        assert tier.completion_price_microdollars_per_million_tokens > 0
        assert tier.prompt_cached_price_microdollars_per_million_tokens is not None
        assert (
            0
            < tier.prompt_cached_price_microdollars_per_million_tokens
            < tier.prompt_price_microdollars_per_million_tokens
        )


def test_qwen_38_routes_only_through_hosts_with_verified_pricing() -> None:
    model_id = "qwen/qwen3.8-max"

    assert f"{model_id}@novita/prepaid" in MODEL_ENDPOINTS
    assert f"{model_id}@atlas-cloud/prepaid" in MODEL_ENDPOINTS
    assert f"{model_id}@fireworks/prepaid" in MODEL_ENDPOINTS
    assert f"{model_id}@alibaba/prepaid" not in MODEL_ENDPOINTS
    assert f"{model_id}@alibaba/byok" not in MODEL_ENDPOINTS


def test_novita_hy3_uses_live_provider_id_and_price_floor() -> None:
    model = MODELS["tencent/hy3"]
    prepaid = MODEL_ENDPOINTS["tencent/hy3@novita/prepaid"]
    byok = MODEL_ENDPOINTS["tencent/hy3@novita/byok"]

    # Tencent has several independent hosts. Validate the Novita endpoint
    # itself rather than whichever host happens to sort first on the model.
    assert model.context_length == 262_144
    assert prepaid.upstream_id == "tencent/hy3"
    assert byok.upstream_id == "tencent/hy3"
    assert prepaid.prompt_price_microdollars_per_million_tokens == 147_700
    assert prepaid.completion_price_microdollars_per_million_tokens == 611_900
    assert prepaid.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 36_925


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
    for model_id in model_ids:
        if f"{model_id}@{provider}/byok" not in MODEL_ENDPOINTS:
            # Provider feeds may retire the route entirely. The suppression
            # contract applies only while the provider still advertises it.
            continue
        assert f"{model_id}@{provider}/prepaid" not in MODEL_ENDPOINTS
        assert f"{model_id}@{provider}/byok" in MODEL_ENDPOINTS


def test_minimax_m3_uses_provider_native_context_tiers() -> None:
    prepaid = MODEL_ENDPOINTS["minimax/minimax-m3@minimax/prepaid"]

    # The model row can come from the OpenRouter snapshot when that snapshot
    # catches up, but the provider-native MiniMax endpoint must still carry
    # MiniMax's exact context-tier billing data.
    assert [tier.max_prompt_tokens for tier in prepaid.price_tiers] == [512_000, None]

    low, high = prepaid.price_tiers
    assert low.prompt_price_microdollars_per_million_tokens == 316_500
    assert low.completion_price_microdollars_per_million_tokens == 1_266_000
    assert low.prompt_cached_price_microdollars_per_million_tokens == 63_300
    assert high.prompt_price_microdollars_per_million_tokens == 633_000
    assert high.completion_price_microdollars_per_million_tokens == 2_532_000
    assert high.prompt_cached_price_microdollars_per_million_tokens == 126_600


def test_prompt_price_equals_published_under_uniform_markup() -> None:
    """Under the uniform pricing formula (cost+5.5%, $0.01/M floor), TR no
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


def test_closed_provider_zdr_claims_are_route_scoped() -> None:
    """Keep public ZDR claims fail-closed for major closed providers.

    Amazon/Bedrock, Anthropic, and Google AI Studio remain outside
    trustedrouter/zdr until reviewed again. The managed Vertex account is
    contractually ZDR, but only its prepaid credential path may qualify.
    """
    provider_slugs_requiring_reverification = {
        "amazon",
        "anthropic",
        "aws",
        "bedrock",
        "google-ai-studio",
    }
    configured = provider_slugs_requiring_reverification & set(PROVIDERS)

    assert {"anthropic", "google-ai-studio"} <= configured
    for provider in sorted(configured):
        assert PROVIDERS[provider].provider_zero_data_retention is not True
        assert provider_privacy_tier(PROVIDERS[provider]) < PRIVACY_TIER_ZERO_RETENTION

    vertex = PROVIDERS["google-vertex"]
    assert vertex.provider_zero_data_retention is False
    assert vertex.prepaid_zero_data_retention is True
    assert vertex.prepaid_zero_data_retention_effective_on == "2026-07-28"
    assert provider_privacy_tier(vertex) < PRIVACY_TIER_ZERO_RETENTION
    vertex_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "google-vertex"
    ]
    assert vertex_endpoints
    assert all(endpoint.usage_type == "Credits" for endpoint in vertex_endpoints)
    assert all(
        endpoint_privacy_tier(endpoint) == PRIVACY_TIER_ZERO_RETENTION
        for endpoint in vertex_endpoints
    )
    assert all(endpoint_zero_data_retention(endpoint) is True for endpoint in vertex_endpoints)

    # OpenAI's guarantee is deliberately narrower: it belongs to
    # TrustedRouter's managed prepaid account, starts on July 28, and was
    # activated only after a live retention smoke passed.
    assert PROVIDERS["openai"].provider_zero_data_retention is False
    assert PROVIDERS["openai"].prepaid_zero_data_retention is True
    assert PROVIDERS["openai"].prepaid_zero_data_retention_effective_on == "2026-07-28"
    openai_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "openai"
    ]
    assert any(endpoint.usage_type == "Credits" for endpoint in openai_endpoints)
    assert any(endpoint.usage_type == "BYOK" for endpoint in openai_endpoints)
    sora_models = {"openai/sora-2", "openai/sora-2-pro"}
    assert all(
        endpoint_zero_data_retention(endpoint)
        is (endpoint.usage_type == "Credits" and endpoint.model_id not in sora_models)
        for endpoint in openai_endpoints
    )


def test_provider_deprecated_models_have_no_catalog_endpoints() -> None:
    quarantined_routes = [
        ("xiaomi", "xiaomi/mimo-v2-flash"),
        ("xiaomi", "xiaomi/mimo-v2-pro"),
        ("friendli", "meta-llama/llama-3.3-70b-instruct"),
        ("google-ai-studio", "google/gemini-3.1-flash-lite-preview"),
        ("google-ai-studio", "google/gemini-2.5-flash-lite"),
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
            if endpoint.provider == "atlas-cloud" and endpoint.model_id == kept_model
        ], f"atlas-cloud/{kept_model} should remain routable"

    assert "anthropic/claude-fable-5@anthropic/prepaid" in MODEL_ENDPOINTS
    # Policy (2026-07-18): Anthropic-authored models route via Anthropic only
    # for Credits — the reseller prepaid route is gone, its BYOK route stays.
    assert "anthropic/claude-fable-5@lightning/prepaid" not in MODEL_ENDPOINTS
    assert "anthropic/claude-fable-5@lightning/byok" in MODEL_ENDPOINTS
    # Residue quarantine is provider-scoped: healthy siblings survive.
    assert "z-ai/glm-5@zai/prepaid" in MODEL_ENDPOINTS
    assert "deepseek/deepseek-v4-pro@deepseek/prepaid" in MODEL_ENDPOINTS
    assert [
        endpoint
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.model_id == "google/gemini-2.5-flash-lite"
        and endpoint.provider != "google-ai-studio"
    ], "provider-scoped AI Studio retirement must preserve healthy routes"


def test_anthropic_opus_41_is_never_prepaid_during_retirement_transition() -> None:
    endpoints = [
        endpoint
        for endpoint in endpoints_for_model("anthropic/claude-opus-4.1")
        if endpoint.provider == "anthropic"
    ]

    assert not [endpoint for endpoint in endpoints if endpoint.usage_type == "Credits"]
    # Anthropic retired Opus 4.1 on 2026-08-05. A committed snapshot from
    # before retirement may retain its BYOK route until the next successful
    # hourly refresh; a fresh provider catalog may remove the route entirely.
    # Either state is safe, but it must never regain a prepaid operator route.
    assert not endpoints or [
        endpoint for endpoint in endpoints if endpoint.usage_type == "BYOK"
    ]


def test_deepseek_v4_pro_release_routes_are_keyed_and_credits_only() -> None:
    old_routes = endpoints_for_model(DEEPSEEK_V4_PRO_0423_MODEL_ID)
    current_routes = endpoints_for_model(DEEPSEEK_V4_PRO_0813_MODEL_ID)

    assert old_routes
    assert all(endpoint.usage_type == "Credits" for endpoint in old_routes)
    assert all(endpoint.provider != "deepseek" for endpoint in old_routes)
    assert {endpoint.provider for endpoint in old_routes} <= (
        GATEWAY_PREPAID_PROVIDER_SLUGS - {"deepseek"}
    )
    assert current_routes
    assert all(endpoint.usage_type == "Credits" for endpoint in current_routes)
    assert {endpoint.provider for endpoint in current_routes} == {
        "deepseek",
        "baseten",
        "fireworks",
    }
    assert {endpoint.provider for endpoint in current_routes} <= GATEWAY_PREPAID_PROVIDER_SLUGS
    assert [
        (endpoint.provider, endpoint.upstream_id)
        for endpoint in current_routes
        if endpoint.provider == "deepseek"
    ] == [("deepseek", "deepseek-v4-pro")]
    assert [
        (endpoint.provider, endpoint.upstream_id)
        for endpoint in current_routes
        if endpoint.provider == "baseten"
    ] == [("baseten", "deepseek-ai/DeepSeek-V4-Pro-0813")]
    assert [
        (endpoint.provider, endpoint.upstream_id)
        for endpoint in current_routes
        if endpoint.provider == "fireworks"
    ] == [("fireworks", "accounts/fireworks/models/deepseek-v4-pro-0813")]
    baseten = next(
        endpoint for endpoint in current_routes if endpoint.provider == "baseten"
    )
    # Public prepaid rates include the normal 5.5% TrustedRouter markup over the
    # exact provider-native $1.32 / $3.96 prices asserted in the parser test.
    assert baseten.prompt_price_microdollars_per_million_tokens == 1_392_600
    assert baseten.completion_price_microdollars_per_million_tokens == 4_177_800
    assert endpoint_zero_data_retention(baseten) is True
    assert endpoint_privacy_tier(baseten) >= PRIVACY_TIER_ZERO_RETENTION
    assert MODELS[DEEPSEEK_V4_PRO_0423_MODEL_ID].byok_available is False
    assert MODELS[DEEPSEEK_V4_PRO_0813_MODEL_ID].byok_available is False


@pytest.mark.parametrize(
    "model_id",
    [
        "moonshotai/kimi-k3",
        "z-ai/glm-5.2",
        "minimax/minimax-m3",
    ],
)
def test_current_orchestration_backups_have_zdr_routes(model_id: str) -> None:
    candidates = chat_route_endpoint_candidates(
        {
            "model": model_id,
            "messages": [{"role": "user", "content": "PONG"}],
            "provider": {"min_privacy": "zdr"},
        },
        Settings(environment="test"),
    )

    assert candidates
    assert all(
        endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_ZERO_RETENTION
        for _model, endpoint in candidates
    )


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
                DEEPSEEK_V4_PRO_0813_MODEL_ID,
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
                DEEPSEEK_V4_PRO_0813_MODEL_ID,
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
    socrates_1_1_candidates = [
        "xiaomi/mimo-v2.5-pro-ultraspeed",
        "minimax/minimax-m3",
        "z-ai/glm-5.2-fast",
        "deepseek/deepseek-v4-flash",
        "trustedrouter/zeus-1.0",
    ]
    socrates_2_0_candidates = [
        "xiaomi/mimo-v2.5-pro-ultraspeed",
        "minimax/minimax-m3",
        "z-ai/glm-5.2-fast",
        DEEPSEEK_V4_PRO_0813_MODEL_ID,
        ZEUS_2_0_MODEL_ID,
    ]

    for model_id, candidates in (
        (SOCRATES_1_0_MODEL_ID, socrates_1_0_candidates),
        (SOCRATES_1_1_MODEL_ID, socrates_1_1_candidates),
        (SOCRATES_2_0_MODEL_ID, socrates_2_0_candidates),
        (SOCRATES_MODEL_ID, socrates_2_0_candidates),
        (ADVISOR_MODEL_ID, socrates_1_0_candidates),
    ):
        model = MODELS[model_id]
        shape = model_to_openrouter_shape(model)
        available_candidates = _cataloged_model_ids(candidates)

        assert ADVISOR_CATALOG_MODEL_ORDERS[model_id] == tuple(candidates)
        assert model.provider == "trustedrouter"
        assert shape["trustedrouter"]["route_kind"] == "advisor_orchestration"
        assert shape["trustedrouter"]["orchestration_primitive"] == "advisor"
        assert shape["trustedrouter"]["stores_content"] is False
        assert shape["trustedrouter"]["auto_candidates"] == available_candidates
        assert [model.id for model in meta_candidate_models(model_id)] == available_candidates

    assert orchestration_role(ADVISOR_MODEL_ID) == "primitive"
    assert canonical_orchestration_model_id(ADVISOR_MODEL_ID) == ADVISOR_MODEL_ID
    assert orchestration_role(SOCRATES_MODEL_ID) == "rolling_alias"
    assert canonical_orchestration_model_id(SOCRATES_MODEL_ID) == SOCRATES_2_0_MODEL_ID
    assert orchestration_role(SOCRATES_1_1_MODEL_ID) == "named_preset"
    assert canonical_orchestration_model_id(SOCRATES_1_1_MODEL_ID) == SOCRATES_1_1_MODEL_ID


def test_orchestration_taxonomy_distinguishes_primitives_presets_and_legacy_aliases() -> None:
    expected = {
        ADVISOR_MODEL_ID: ("advisor", "primitive", ADVISOR_MODEL_ID),
        SYNTH_MODEL_ID: ("synth", "primitive", SYNTH_MODEL_ID),
        FUSION_MODEL_ID: ("synth", "legacy_alias", SYNTH_MODEL_ID),
        SELECTOR_MODEL_ID: ("selector", "primitive", SELECTOR_MODEL_ID),
        MAPREDUCE_MODEL_ID: ("mapreduce", "primitive", MAPREDUCE_MODEL_ID),
        SOCRATES_MODEL_ID: ("advisor", "rolling_alias", SOCRATES_2_0_MODEL_ID),
        SOCRATES_1_1_MODEL_ID: ("advisor", "named_preset", SOCRATES_1_1_MODEL_ID),
        SOCRATES_2_0_MODEL_ID: ("advisor", "named_preset", SOCRATES_2_0_MODEL_ID),
        ARISTOTLE_MODEL_ID: ("advisor", "rolling_alias", ARISTOTLE_2_0_MODEL_ID),
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
        ARISTOTLE_2_0_MODEL_ID: (
            "advisor",
            "named_preset",
            ARISTOTLE_2_0_MODEL_ID,
        ),
        PLATO_MODEL_ID: ("advisor", "rolling_alias", PLATO_3_0_MODEL_ID),
        PLATO_1_0_MODEL_ID: ("advisor", "named_preset", PLATO_1_0_MODEL_ID),
        PLATO_3_0_MODEL_ID: ("advisor", "named_preset", PLATO_3_0_MODEL_ID),
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
        IRIS_MODEL_ID: ("synth", "rolling_alias", IRIS_3_0_MODEL_ID),
        IRIS_2_0_MODEL_ID: ("synth", "named_preset", IRIS_2_0_MODEL_ID),
        IRIS_3_0_MODEL_ID: ("synth", "named_preset", IRIS_3_0_MODEL_ID),
        PROMETHEUS_MODEL_ID: ("synth", "rolling_alias", PROMETHEUS_3_0_MODEL_ID),
        PROMETHEUS_2_0_MODEL_ID: (
            "synth",
            "named_preset",
            PROMETHEUS_2_0_MODEL_ID,
        ),
        PROMETHEUS_3_0_MODEL_ID: (
            "synth",
            "named_preset",
            PROMETHEUS_3_0_MODEL_ID,
        ),
        ZEUS_MODEL_ID: ("synth", "rolling_alias", ZEUS_2_0_MODEL_ID),
        ZEUS_2_0_MODEL_ID: ("synth", "named_preset", ZEUS_2_0_MODEL_ID),
        OPEN_PATCHER_S1_MODEL_ID: ("synth", "named_preset", OPEN_PATCHER_S1_MODEL_ID),
        OPEN_PATCHER_S2_MODEL_ID: ("synth", "named_preset", OPEN_PATCHER_S2_MODEL_ID),
        OPEN_PATCHER_G2_MODEL_ID: (
            "advisor",
            "named_preset",
            OPEN_PATCHER_G2_MODEL_ID,
        ),
        OPEN_PATCHER_G3_MODEL_ID: (
            "advisor",
            "named_preset",
            OPEN_PATCHER_G3_MODEL_ID,
        ),
        ATHENA_MODEL_ID: ("advisor", "rolling_alias", ATHENA_2_0_MODEL_ID),
        ATHENA_1_0_MODEL_ID: ("advisor", "named_preset", ATHENA_1_0_MODEL_ID),
        ATHENA_2_0_MODEL_ID: ("advisor", "named_preset", ATHENA_2_0_MODEL_ID),
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
        DEEPSEEK_V4_PRO_0423_MODEL_ID,
        DEEPSEEK_V4_PRO_0813_MODEL_ID,
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.6",
        "moonshotai/kimi-k3",
        "google/gemma-4-31b-it",
        PROMETHEUS_MODEL_ID,
        PROMETHEUS_1_0_MODEL_ID,
        PROMETHEUS_2_0_MODEL_ID,
        PROMETHEUS_3_0_MODEL_ID,
        IRIS_2_0_MODEL_ID,
        IRIS_3_0_MODEL_ID,
        PLATO_MODEL_ID,
        PLATO_3_0_MODEL_ID,
        PLATO_1_0_MODEL_ID,
        PLATO_PRO_MODEL_ID,
        PLATO_PRO_1_0_MODEL_ID,
        PLATO_PRO_2_0_MODEL_ID,
        OPEN_PATCHER_S1_MODEL_ID,
        OPEN_PATCHER_A1_MODEL_ID,
        OPEN_PATCHER_FAST1_MODEL_ID,
        OPEN_PATCHER_G2_MODEL_ID,
        OPEN_PATCHER_G3_MODEL_ID,
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
        ZEUS_2_0_MODEL_ID,
        ARISTOTLE_MODEL_ID,
        ARISTOTLE_1_1_MODEL_ID,
        ARISTOTLE_1_0_MODEL_ID,
        ARISTOTLE_2_0_MODEL_ID,
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
            DEEPSEEK_V4_PRO_0423_MODEL_ID,
        ],
        ARISTOTLE_1_1_MODEL_ID: [
            "z-ai/glm-5.2-fast",
            "z-ai/glm-5.2",
            "trustedrouter/zeus-1.0",
        ],
        ARISTOTLE_MODEL_ID: [
            "z-ai/glm-5.2-fast",
            "z-ai/glm-5.2",
            ZEUS_2_0_MODEL_ID,
        ],
        ARISTOTLE_2_0_MODEL_ID: [
            "z-ai/glm-5.2-fast",
            "z-ai/glm-5.2",
            ZEUS_2_0_MODEL_ID,
        ],
        PLATO_1_0_MODEL_ID: [
            "deepseek/deepseek-v4-flash",
            "z-ai/glm-5.2",
            "minimax/minimax-m3",
            "moonshotai/kimi-k2.6",
            "google/gemma-4-31b-it",
            DEEPSEEK_V4_PRO_0423_MODEL_ID,
        ],
        PLATO_MODEL_ID: [
            DEEPSEEK_V4_PRO_0813_MODEL_ID,
            PROMETHEUS_3_0_MODEL_ID,
        ],
        PLATO_3_0_MODEL_ID: [
            DEEPSEEK_V4_PRO_0813_MODEL_ID,
            PROMETHEUS_3_0_MODEL_ID,
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
        SOCRATES_2_0_MODEL_ID: [
            "xiaomi/mimo-v2.5-pro-ultraspeed",
            "minimax/minimax-m3",
            "z-ai/glm-5.2-fast",
            DEEPSEEK_V4_PRO_0813_MODEL_ID,
            ZEUS_2_0_MODEL_ID,
        ],
        SOCRATES_MODEL_ID: [
            "xiaomi/mimo-v2.5-pro-ultraspeed",
            "minimax/minimax-m3",
            "z-ai/glm-5.2-fast",
            DEEPSEEK_V4_PRO_0813_MODEL_ID,
            ZEUS_2_0_MODEL_ID,
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
        OPEN_PATCHER_G3_MODEL_ID: [
            "moonshotai/kimi-k3",
            "google/gemma-4-31b-it",
            PROMETHEUS_3_0_MODEL_ID,
        ],
    }
    for model_id, candidates in expected.items():
        shape = model_to_openrouter_shape(MODELS[model_id])
        available_candidates = _cataloged_model_ids(candidates)

        assert shape["trustedrouter"]["route_kind"] == "advisor_orchestration"
        assert shape["trustedrouter"]["stores_content"] is False
        assert shape["trustedrouter"]["auto_candidates"] == available_candidates
        assert [model.id for model in meta_candidate_models(model_id)] == available_candidates
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
    # Inkling's serverless hosts currently advertise different verified
    # windows (256K, 512K, and 1M). The canonical row follows the largest live
    # routed endpoint, so an hourly availability refresh may legitimately move
    # it within this range. It must still satisfy Liberty 1.0's 256K contract
    # and must never overstate the largest verified 1M route.
    assert 262_144 <= inkling.context_length <= 1_048_576
    endpoints = endpoints_for_model(inkling.id)
    provider_ids = {endpoint.provider for endpoint in endpoints}
    assert inkling.provider in provider_ids
    assert "thinkingmachines" in provider_ids
    assert len(provider_ids) >= 2
    assert all(endpoint.upstream_id for endpoint in endpoints)
    assert any(endpoint.usage_type == "Credits" for endpoint in endpoints)
    assert any(endpoint.usage_type == "BYOK" for endpoint in endpoints)

    inkling_1m = MODELS["thinkingmachines/inkling-1m"]
    assert inkling_1m.context_length == 1_048_576
    assert inkling_1m.provider == "baseten"
    assert {
        (endpoint.provider, endpoint.upstream_id) for endpoint in endpoints_for_model(inkling_1m.id)
    } == {("baseten", "thinkingmachines/inkling")}

    inkling_small = MODELS["thinkingmachines/inkling-small"]
    inkling_small_shape = model_to_openrouter_shape(inkling_small)
    # The first-party route is 256K, while independent hosts can expose a
    # larger verified window for the same weights. The canonical model follows
    # the largest live routed endpoint and must stay within the audited range.
    assert 262_144 <= inkling_small.context_length <= 1_048_576
    assert inkling_small.input_modalities == ("text", "image")
    assert inkling_small.output_modalities == ("text",)
    assert inkling_small_shape["architecture"]["modality"] == "text+image->text"
    assert inkling_small_shape["architecture"]["input_modalities"] == [
        "text",
        "image",
    ]
    assert inkling_small_shape["trustedrouter"]["open_weights"] is True
    assert inkling_small.prompt_price_microdollars_per_million_tokens == 316_500
    assert inkling_small.completion_price_microdollars_per_million_tokens == 1_266_000
    inkling_small_routes = {
        (endpoint.provider, endpoint.upstream_id)
        for endpoint in endpoints_for_model(inkling_small.id)
    }
    assert (
        "thinkingmachines",
        "thinkingmachines/Inkling-Small:peft:262144:sampling-nvfp4",
    ) in inkling_small_routes
    assert all(provider and upstream_id for provider, upstream_id in inkling_small_routes)


def test_catalog_modalities_publish_only_gateway_supported_capabilities() -> None:
    assert _modalities(
        ["Text", "image", "audio", "image", "", 7],
        default=("text",),
    ) == ("text", "image")
    assert _modalities(["audio"], default=("text",)) == ("text",)


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
    if provider_model_retired(
        "nebius",
        model_id,
        "nvidia/Nemotron-3-Ultra-550b-a55b",
    ):
        assert "nebius" not in prepaid
    else:
        assert prepaid["nebius"] == "nvidia/Nemotron-3-Ultra-550b-a55b"
    assert "gmi" not in prepaid


def test_athena_catalog_hides_orchestration_configuration() -> None:
    expected = {
        ATHENA_1_0_MODEL_ID: [
            "z-ai/glm-5.2-fast",
            "z-ai/glm-5.2",
            ZEUS_1_0_MINI_MODEL_ID,
            "moonshotai/kimi-k2.7-code",
            "moonshotai/kimi-k2.6",
        ],
        ATHENA_2_0_MODEL_ID: [
            "z-ai/glm-5.2-fast",
            "z-ai/glm-5.2",
            ZEUS_2_0_MODEL_ID,
            "moonshotai/kimi-k2.7-code",
            "moonshotai/kimi-k2.6",
        ],
        ATHENA_MODEL_ID: [
            "z-ai/glm-5.2-fast",
            "z-ai/glm-5.2",
            ZEUS_2_0_MODEL_ID,
            "moonshotai/kimi-k2.7-code",
            "moonshotai/kimi-k2.6",
        ],
    }

    for model_id, candidates in expected.items():
        model = MODELS[model_id]
        shape = model_to_openrouter_shape(model)

        assert model.hidden_public_metadata is True
        assert model.context_length == 1_048_576
        assert shape["trustedrouter"]["route_kind"] == "private_orchestration"
        assert shape["trustedrouter"]["configuration_hidden"] is True
        assert shape["trustedrouter"]["auto_candidates"] is None
        assert [candidate.id for candidate in meta_candidate_models(model_id)] == candidates
        assert shape["trustedrouter"]["open_weights"] is False

    assert canonical_orchestration_model_id(ATHENA_MODEL_ID) == ATHENA_2_0_MODEL_ID


def test_zeus_versions_are_frozen_and_rolling_alias_uses_2_0() -> None:
    assert MODELS[ZEUS_MODEL_ID].context_length == 1_048_576
    assert MODELS[ZEUS_1_0_MODEL_ID].context_length == 1_048_576
    assert MODELS[ZEUS_1_0_MINI_MODEL_ID].context_length == 1_048_576
    zeus_1_0 = [
        "anthropic/claude-opus-4.8",
        "openai/gpt-5.5",
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.5-flash",
        "minimax/minimax-m3",
        "z-ai/glm-5.2",
        "xiaomi/mimo-v2.5-pro",
        DEEPSEEK_V4_PRO_0423_MODEL_ID,
    ]
    zeus_2_0 = [*zeus_1_0[:-1], DEEPSEEK_V4_PRO_0813_MODEL_ID]
    assert [model.id for model in meta_candidate_models(ZEUS_1_0_MODEL_ID)] == zeus_1_0
    assert [model.id for model in meta_candidate_models(ZEUS_2_0_MODEL_ID)] == zeus_2_0
    assert [model.id for model in meta_candidate_models(ZEUS_MODEL_ID)] == zeus_2_0
    zeus_shape = model_to_openrouter_shape(MODELS[ZEUS_1_0_MODEL_ID])
    assert zeus_shape["trustedrouter"]["us_provider_available"] is True
    assert zeus_shape["trustedrouter"]["eu_focused_provider_available"] is True
    assert [model.id for model in meta_candidate_models(ZEUS_1_0_MINI_MODEL_ID)] == [
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.5-flash",
        "minimax/minimax-m3",
        "z-ai/glm-5.2",
        "xiaomi/mimo-v2.5-pro",
        DEEPSEEK_V4_PRO_0423_MODEL_ID,
    ]
    assert model_us_provider_available(MODELS[ZEUS_1_0_MINI_MODEL_ID]) is True
    assert model_eu_focused_provider_available(MODELS[ZEUS_1_0_MINI_MODEL_ID]) is True
    assert canonical_orchestration_model_id(ZEUS_MODEL_ID) == ZEUS_2_0_MODEL_ID


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


def test_openpatcher_s3_uses_glm_and_deepseek_0813() -> None:
    model = MODELS[OPEN_PATCHER_S3_MODEL_ID]
    candidates = [candidate.id for candidate in meta_candidate_models(model.id)]

    assert model.name == "TrustedRouter OpenPatcher-S3"
    assert model.context_length == 1_048_576
    assert candidates == ["z-ai/glm-5.2", DEEPSEEK_V4_PRO_0813_MODEL_ID]
    assert model_to_openrouter_shape(model)["trustedrouter"]["auto_candidates"] == candidates


def test_iris_versions_are_frozen_and_rolling_alias_uses_3_0() -> None:
    iris_2_0 = [
        "minimax/minimax-m3",
        "moonshotai/kimi-k3",
        DEEPSEEK_V4_PRO_0423_MODEL_ID,
    ]
    iris_3_0 = [*iris_2_0[:-1], DEEPSEEK_V4_PRO_0813_MODEL_ID]

    assert [model.id for model in meta_candidate_models(IRIS_1_0_MODEL_ID)] == [
        "minimax/minimax-m3",
        "moonshotai/kimi-k2.6",
        DEEPSEEK_V4_PRO_0423_MODEL_ID,
    ]
    assert [model.id for model in meta_candidate_models(IRIS_2_0_MODEL_ID)] == iris_2_0
    for model_id in (IRIS_MODEL_ID, IRIS_3_0_MODEL_ID):
        assert MODELS[model_id].context_length == 1_048_576
        assert [model.id for model in meta_candidate_models(model_id)] == iris_3_0
        assert (
            model_to_openrouter_shape(MODELS[model_id])["trustedrouter"]["auto_candidates"]
            == iris_3_0
        )

    assert canonical_orchestration_model_id(IRIS_MODEL_ID) == IRIS_3_0_MODEL_ID


def test_prometheus_1m_uses_only_long_context_open_weight_components() -> None:
    model = MODELS[PROMETHEUS_1_0_1M_MODEL_ID]
    candidates = meta_candidate_models(PROMETHEUS_1_0_1M_MODEL_ID)
    candidate_ids = [candidate.id for candidate in candidates]

    assert model.name == "TrustedRouter Prometheus 1.0 1M"
    assert model.context_length == 1_048_576
    assert candidate_ids == [
        "xiaomi/mimo-v2.5-pro",
        "z-ai/glm-5.2",
        DEEPSEEK_V4_PRO_0423_MODEL_ID,
    ]
    assert all(candidate.context_length >= 1_000_000 for candidate in candidates)
    assert all(model_open_weights(candidate) for candidate in candidates)

    shape = model_to_openrouter_shape(model)
    assert shape["context_length"] == 1_048_576
    assert shape["trustedrouter"]["route_kind"] == "fusion_panel"
    assert shape["trustedrouter"]["auto_candidates"] == candidate_ids
    assert shape["trustedrouter"]["open_weights"] is True


def test_prometheus_versions_are_frozen_and_rolling_alias_uses_3_0() -> None:
    prometheus_2_0 = [
        "minimax/minimax-m3",
        "moonshotai/kimi-k3",
        "z-ai/glm-5.2",
        DEEPSEEK_V4_PRO_0423_MODEL_ID,
        "xiaomi/mimo-v2.5-pro",
    ]
    prometheus_3_0 = [
        *prometheus_2_0[:3],
        DEEPSEEK_V4_PRO_0813_MODEL_ID,
        *prometheus_2_0[4:],
    ]
    assert [
        candidate.id for candidate in meta_candidate_models(PROMETHEUS_2_0_MODEL_ID)
    ] == prometheus_2_0
    for model_id in (PROMETHEUS_MODEL_ID, PROMETHEUS_3_0_MODEL_ID):
        model = MODELS[model_id]
        shape = model_to_openrouter_shape(model)

        assert model.context_length == 1_048_576
        assert [candidate.id for candidate in meta_candidate_models(model_id)] == prometheus_3_0
        assert shape["trustedrouter"]["auto_candidates"] == prometheus_3_0
        assert shape["trustedrouter"]["open_weights"] is True

    assert canonical_orchestration_model_id(PROMETHEUS_MODEL_ID) == PROMETHEUS_3_0_MODEL_ID


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
        SOCRATES_2_0_MODEL_ID,
        SOCRATES_PRO_PLUS_1_0_MODEL_ID,
        OPEN_PATCHER_G1_MODEL_ID,
        OPEN_PATCHER_G2_MODEL_ID,
        OPEN_PATCHER_G3_MODEL_ID,
        OPEN_PATCHER_S2_MODEL_ID,
        OPEN_PATCHER_S3_MODEL_ID,
        IRIS_2_0_MODEL_ID,
        IRIS_3_0_MODEL_ID,
        PLATO_PRO_2_0_MODEL_ID,
        ZEUS_1_0_MODEL_ID,
        ZEUS_2_0_MODEL_ID,
        PROMETHEUS_1_0_1M_MODEL_ID,
        PROMETHEUS_2_0_MODEL_ID,
        PROMETHEUS_3_0_MODEL_ID,
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
        ATHENA_1_0_MODEL_ID,
        ATHENA_2_0_MODEL_ID,
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


def test_openpatcher_g1_and_g2_stay_frozen_while_g3_uses_prometheus_3() -> None:
    expected = {
        OPEN_PATCHER_G1_MODEL_ID: [
            "z-ai/glm-5.2-fast",
            "z-ai/glm-5.2",
            "moonshotai/kimi-k2.7-code",
            PROMETHEUS_1_0_1M_MODEL_ID,
        ],
        OPEN_PATCHER_G2_MODEL_ID: [
            "moonshotai/kimi-k3",
            "google/gemma-4-31b-it",
            PROMETHEUS_2_0_MODEL_ID,
        ],
        OPEN_PATCHER_G3_MODEL_ID: [
            "moonshotai/kimi-k3",
            "google/gemma-4-31b-it",
            PROMETHEUS_3_0_MODEL_ID,
        ],
    }

    for model_id, candidates in expected.items():
        assert [candidate.id for candidate in meta_candidate_models(model_id)] == candidates

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
    assert "chutes" in {endpoint.provider for _model, endpoint in e2e_endpoints}
    assert "anthropic" not in {endpoint.provider for _model, endpoint in zdr_endpoints}
    assert "google-ai-studio" not in {endpoint.provider for _model, endpoint in zdr_endpoints}
    assert "google-vertex" in {endpoint.provider for _model, endpoint in zdr_endpoints}
    assert "openai" in {endpoint.provider for _model, endpoint in zdr_endpoints}
    assert all(
        endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_ZERO_RETENTION
        for _model, endpoint in zdr_endpoints
    )
    assert all(
        provider_privacy_tier(PROVIDERS[endpoint.provider]) >= PRIVACY_TIER_CONFIDENTIAL
        for _model, endpoint in e2e_endpoints
    )
    assert "venice" not in {endpoint.provider for _model, endpoint in e2e_endpoints}
    assert "phala" not in {endpoint.provider for _model, endpoint in e2e_endpoints}


def test_phala_is_not_classified_as_verified_e2ee() -> None:
    provider = PROVIDERS["phala"]

    assert provider.provider_confidential_compute is True
    assert provider.provider_e2ee is False
    assert provider_privacy_tier(provider) == PRIVACY_TIER_ZERO_RETENTION
    assert "does not yet verify" in provider.provider_policy
    assert "excluded from trustedrouter/e2e" in provider.provider_policy


def test_venice_privacy_is_model_specific_and_never_claims_tee() -> None:
    provider = PROVIDERS["venice"]
    assert provider.stores_content is True
    assert provider.provider_zero_data_retention is False
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False

    private_models = {
        "qwen/qwen3-235b-a22b-thinking-2507",
        "qwen/qwen3.5-9b",
        "qwen/qwen3.6-27b",
        "z-ai/glm-4.6",
        "z-ai/glm-4.7",
        "z-ai/glm-4.7-flash",
        "z-ai/glm-5",
        "z-ai/glm-5.1",
        "z-ai/glm-5.2",
    }
    anonymized_models = {
        "qwen/qwen3.5-397b-a17b",
        "z-ai/glm-5-turbo",
        "z-ai/glm-5v-turbo",
    }
    video_models = {
        "bytedance/seedance-2.0",
        "bytedance/seedance-2.0-fast",
        "google/veo-3.1",
        "google/veo-3.1-fast",
        "google/gemini-omni-flash",
        "openai/sora-2",
        "openai/sora-2-pro",
        "runway/gen-4.5",
        "kling/v3-pro",
        "kling/o3-pro",
        "alibaba/wan-2.7",
        "shengshu/vidu-q3",
        "pixverse/c1",
        "lightricks/ltx-2.3",
        "lightricks/ltx-2.3-fast",
        "minimax/hailuo-3",
    }
    endpoints = [endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "venice"]
    assert private_models | anonymized_models | video_models <= {
        endpoint.model_id for endpoint in endpoints
    }
    assert all(
        PROVIDERS[endpoint.provider].provider_confidential_compute is False
        for endpoint in endpoints
    )
    assert all(PROVIDERS[endpoint.provider].provider_e2ee is False for endpoint in endpoints)

    for endpoint in endpoints:
        if endpoint.model_id in private_models:
            assert endpoint_privacy_tier(endpoint) == PRIVACY_TIER_ZERO_RETENTION
            assert endpoint_zero_data_retention(endpoint) is True
            assert endpoint_stores_content(endpoint) is False
        else:
            assert endpoint_privacy_tier(endpoint) == PRIVACY_TIER_STANDARD
            assert endpoint_zero_data_retention(endpoint) is False
            assert endpoint_stores_content(endpoint) is True

    for model_id in video_models:
        model = MODELS[model_id]
        assert model.supports_video is True
        assert model.supports_chat is False
        video_endpoints = [
            endpoint for endpoint in endpoints_for_model(model_id) if endpoint.provider == "venice"
        ]
        assert video_endpoints
        assert all(endpoint_zero_data_retention(endpoint) is False for endpoint in video_endpoints)
        assert all(endpoint_stores_content(endpoint) is True for endpoint in video_endpoints)

    private_shape = model_to_openrouter_shape(MODELS["z-ai/glm-5.2"])
    private_venice = [
        endpoint
        for endpoint in private_shape["trustedrouter"]["endpoints"]
        if endpoint["provider"] == "venice"
    ]
    assert private_venice
    assert all(endpoint["stores_content"] is False for endpoint in private_venice)
    assert all(endpoint["provider_zero_data_retention"] is True for endpoint in private_venice)
    assert all(endpoint["provider_confidential_compute"] is False for endpoint in private_venice)
    assert all(endpoint["provider_e2ee"] is False for endpoint in private_venice)

    anonymized_shape = model_to_openrouter_shape(MODELS["z-ai/glm-5-turbo"])
    anonymized_venice = [
        endpoint
        for endpoint in anonymized_shape["trustedrouter"]["endpoints"]
        if endpoint["provider"] == "venice"
    ]
    assert anonymized_venice
    assert all(endpoint["stores_content"] is True for endpoint in anonymized_venice)
    assert all(endpoint["provider_zero_data_retention"] is False for endpoint in anonymized_venice)


def test_every_tinfoil_endpoint_is_confidential_and_e2ee() -> None:
    provider = PROVIDERS["tinfoil"]
    endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "tinfoil"
    ]

    assert endpoints
    assert provider.provider_confidential_compute is True
    assert provider.provider_e2ee is True
    assert all(
        endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_CONFIDENTIAL for endpoint in endpoints
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
    xiaomi_credits = {}
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
        xiaomi_credits[model_id] = credits[0]

    pro = MODELS["xiaomi/mimo-v2.5-pro"]
    # Xiaomi documents this as a 1M context window. Live catalogs use both the
    # binary 1,048,576 value and a rounded 1,050,000 value, so guard the public
    # capability rather than freezing one representation.
    assert 1_000_000 <= pro.context_length <= 1_050_000
    pro_xiaomi = xiaomi_credits["xiaomi/mimo-v2.5-pro"]
    assert pro_xiaomi.prompt_price_microdollars_per_million_tokens == 458_925
    assert pro_xiaomi.completion_price_microdollars_per_million_tokens == 917_850
    # The model headline is the cheapest healthy route across every provider,
    # so a reseller may legitimately undercut Xiaomi's first-party endpoint.
    assert pro.prompt_price_microdollars_per_million_tokens <= 458_925
    assert pro.completion_price_microdollars_per_million_tokens <= 917_850

    # UltraSpeed is the 1T-param speed-serving tier with its own ¥9/¥18
    # ($1.305/$2.61) cost, marked up by the manifest loader (cost x 1.055,
    # $0.01/M floor). Guard the exact prices so a regen can't silently
    # collapse them onto the regular v2.5-pro numbers.
    ultraspeed_xiaomi = xiaomi_credits["xiaomi/mimo-v2.5-pro-ultraspeed"]
    assert ultraspeed_xiaomi.prompt_price_microdollars_per_million_tokens == 1_376_775
    assert ultraspeed_xiaomi.completion_price_microdollars_per_million_tokens == 2_753_550
    # ...and that it is genuinely a distinct row from regular v2.5-pro.
    assert (
        ultraspeed_xiaomi.completion_price_microdollars_per_million_tokens
        != pro_xiaomi.completion_price_microdollars_per_million_tokens
    )


def test_crusoe_provider_models_follow_authoritative_manifest() -> None:
    """Crusoe availability follows its generated, credential-aware manifest."""
    from trusted_router.catalog_ingest import _authoritative_provider_model_ids

    assert "crusoe" in PROVIDERS
    assert "crusoe" in GATEWAY_PREPAID_PROVIDER_SLUGS
    expected = _authoritative_provider_model_ids("crusoe")
    credits = {
        endpoint.model_id
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.provider == "crusoe" and endpoint.usage_type == "Credits"
    }
    byok = {
        endpoint.model_id
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.provider == "crusoe" and endpoint.usage_type == "BYOK"
    }
    assert credits == expected
    assert byok == expected


def test_makora_provider_models_follow_live_manifest() -> None:
    """Makora routes track its generated catalog without freezing retirements."""
    from trusted_router.catalog_ingest import (
        _PROVIDER_MODELS_DIR,
        _is_provider_deprecated_model,
    )

    assert "makora" in PROVIDERS
    assert "makora" in GATEWAY_PREPAID_PROVIDER_SLUGS
    raw = json.loads((_PROVIDER_MODELS_DIR / "makora.json").read_text(encoding="utf-8"))
    raw_models = raw.get("models")
    assert isinstance(raw_models, list)
    expected: dict[str, str] = {}
    for row in raw_models:
        assert isinstance(row, dict)
        if row.get("routable") is False:
            continue
        if row.get("model_type") not in (None, "chat"):
            continue
        if "chat/completions" not in {str(item) for item in (row.get("endpoints") or [])}:
            continue
        model_id = row.get("id")
        assert isinstance(model_id, str) and model_id
        upstream_id = str(row.get("upstream_id") or model_id)
        if _is_provider_deprecated_model("makora", model_id, upstream_id):
            continue
        expected[model_id] = upstream_id

    credits_model_ids = {
        endpoint.model_id
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.provider == "makora" and str(endpoint.usage_type) == "Credits"
    }
    byok_model_ids = {
        endpoint.model_id
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.provider == "makora" and str(endpoint.usage_type) == "BYOK"
    }

    assert expected
    assert credits_model_ids == set(expected)
    assert byok_model_ids == set(expected)
    assert "qwen/qwen3.6-27b" not in credits_model_ids
    assert "openai/gpt-oss-120b" not in credits_model_ids
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
    """Runtime prices follow Makora's generated authenticated catalog.

    Provider prices are mutable, so this contract compares the loaded routes
    to the checked-in provider-native manifest rather than freezing a stale
    homepage price table into source code.
    """
    from trusted_router.catalog_ingest import (
        _PROVIDER_MODELS_DIR,
        _is_provider_deprecated_model,
    )
    from trusted_router.pricing import _customer_price

    raw = json.loads((_PROVIDER_MODELS_DIR / "makora.json").read_text(encoding="utf-8"))
    expected_prices = {
        row["id"]: (
            _customer_price(int(row["input_token_price_per_m"])),
            _customer_price(int(row["output_token_price_per_m"])),
            _customer_price(int(row["cached_input_token_price_per_m"])),
        )
        for row in raw["models"]
        if row.get("routable") is not False
        and int(row.get("cached_input_token_price_per_m") or 0) > 0
        and not _is_provider_deprecated_model(
            "makora", str(row["id"]), str(row.get("upstream_id") or row["id"])
        )
    }

    assert expected_prices
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


@pytest.mark.parametrize(
    "endpoint_id",
    [
        "z-ai/glm-5.2@wafer/prepaid",
        "moonshotai/kimi-k3@wafer/prepaid",
        # moonshotai/kimi-k3-fast@wafer/prepaid retired 2026-08-17 00:00 UTC
        # (provider_lifecycle WAFER_AUGUST_2026_RETIREMENT_AT); the catalog is
        # built against the real clock, so the endpoint no longer exists.
        "deepseek/deepseek-v4-flash-0731-fast@wafer/prepaid",
    ],
)
def test_wafer_manifest_drives_zdr_routing_and_gateway_enforcement(
    endpoint_id: str,
) -> None:
    zdr_endpoint = MODEL_ENDPOINTS[endpoint_id]

    assert endpoint_privacy_tier(zdr_endpoint) == PRIVACY_TIER_ZERO_RETENTION
    assert endpoint_zero_data_retention(zdr_endpoint) is True
    assert _gateway_provider_route_payload(zdr_endpoint) == {
        "wafer_zdr_required": True
    }
    standard_endpoint = MODEL_ENDPOINTS.get("moonshotai/kimi-k2.6@wafer/prepaid")
    if standard_endpoint is not None:
        assert endpoint_zero_data_retention(standard_endpoint) is False
        assert _gateway_provider_route_payload(standard_endpoint) == {}


def test_glm_52_supplements_publish_current_model_across_providers() -> None:
    model = MODELS["z-ai/glm-5.2"]
    prepaid = MODEL_ENDPOINTS["z-ai/glm-5.2@zai/prepaid"]
    byok = MODEL_ENDPOINTS["z-ai/glm-5.2@zai/byok"]
    gmi = MODEL_ENDPOINTS.get("z-ai/glm-5.2@gmi/prepaid")
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
    if gmi is not None:
        assert gmi.upstream_id == "zai-org/GLM-5.2-FP8"
    assert deepinfra.upstream_id == "zai-org/GLM-5.2"
    assert fireworks.upstream_id == "accounts/fireworks/models/glm-5p2"
    assert novita.upstream_id == "zai-org/glm-5.2"
    assert phala.upstream_id == "z-ai/glm-5.2"
    assert siliconflow.upstream_id == "zai-org/GLM-5.2"
    assert tinfoil.upstream_id == "glm-5-2"
    assert together.upstream_id == "zai-org/GLM-5.2"
    assert venice.upstream_id == "zai-org-glm-5-2"
    assert parasail.upstream_id == "parasail-glm-52"
    assert friendli.upstream_id == "zai-org/GLM-5.2"
    assert baseten.upstream_id == "zai-org/GLM-5.2"
    assert wafer.upstream_id == "GLM-5.2"
    for endpoint in (
        deepinfra,
        fireworks,
        novita,
        friendli,
        baseten,
        wafer,
    ):
        assert endpoint.prompt_price_microdollars_per_million_tokens > 0
        assert endpoint.completion_price_microdollars_per_million_tokens > 0
    if gmi is not None:
        assert gmi.prompt_price_microdollars_per_million_tokens > 0
        assert gmi.completion_price_microdollars_per_million_tokens > 0


def test_parasail_qwen_397b_uses_working_native_upstream_id() -> None:
    prepaid = MODEL_ENDPOINTS["qwen/qwen3.5-397b-a17b@parasail/prepaid"]
    byok = MODEL_ENDPOINTS["qwen/qwen3.5-397b-a17b@parasail/byok"]

    assert MODELS["qwen/qwen3.5-397b-a17b"].context_length == 262_144
    assert prepaid.upstream_id == "parasail-qwen35-397b-a17b"
    assert byok.upstream_id == "parasail-qwen35-397b-a17b"
    assert prepaid.prompt_price_microdollars_per_million_tokens == 527_500
    assert prepaid.completion_price_microdollars_per_million_tokens == 3_798_000


def test_parasail_glm_53_routes_publish_verified_prices() -> None:
    flash_prepaid = MODEL_ENDPOINTS["z-ai/glm-5.3-flash@parasail/prepaid"]
    flash_byok = MODEL_ENDPOINTS["z-ai/glm-5.3-flash@parasail/byok"]
    full_prepaid = MODEL_ENDPOINTS["z-ai/glm-5.3@parasail/prepaid"]
    full_byok = MODEL_ENDPOINTS["z-ai/glm-5.3@parasail/byok"]

    assert flash_prepaid.upstream_id == "zai-org/GLM-5.3-Flash"
    assert flash_byok.upstream_id == "zai-org/GLM-5.3-Flash"
    assert flash_prepaid.prompt_price_microdollars_per_million_tokens == 158_250
    assert flash_prepaid.completion_price_microdollars_per_million_tokens == 527_500

    assert full_prepaid.upstream_id == "parasail-glm-53"
    assert full_byok.upstream_id == "parasail-glm-53"
    assert full_prepaid.prompt_price_microdollars_per_million_tokens == 1_477_000
    assert full_prepaid.completion_price_microdollars_per_million_tokens == 4_642_000
