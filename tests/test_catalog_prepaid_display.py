from __future__ import annotations

from trusted_router.catalog import (
    _PROVIDER_DEPRECATED_UPSTREAM_MODELS,
    _PROVIDER_SERVED_MODEL_ALLOWLIST,
    MODEL_ENDPOINTS,
    MODELS,
    ModelEndpoint,
    endpoints_for_model,
)
from trusted_router.dashboard import _model_detail_view


def _a_supplemental_priced_model() -> str:
    """A model with raw prepaid_available=False but a real Credits endpoint —
    i.e. a supplemental provider-native model that IS prepaid-routable."""
    for model in MODELS.values():
        if model.prepaid_available:
            continue
        if any(e.usage_type == "Credits" for e in endpoints_for_model(model.id)):
            return model.id
    raise AssertionError("expected at least one supplemental priced model")


def test_supplemental_model_surfaces_as_prepaid_on_detail() -> None:
    model_id = _a_supplemental_priced_model()
    model = MODELS[model_id]
    # Premise: the raw catalog flag is a dedup marker (False)...
    assert model.prepaid_available is False
    # ...but the rendered detail view derives prepaid from endpoints → True.
    view = _model_detail_view(model)
    assert view["prepaid"] is True


def test_byok_only_model_stays_not_prepaid() -> None:
    # A model with no Credits endpoint and raw flag False must NOT flip to
    # prepaid (the `or model.prepaid_available` fallback is still conservative).
    for model in MODELS.values():
        if model.prepaid_available:
            continue
        if not any(e.usage_type == "Credits" for e in endpoints_for_model(model.id)):
            assert _model_detail_view(model)["prepaid"] is False
            return
    # If every non-prepaid model has a Credits endpoint, there's nothing to
    # assert — not a failure.


def test_cerebras_only_credits_serves_allowlisted_models() -> None:
    # The Cerebras account serves only a small set of models on OUR key;
    # routing a Credits request for any other model 502s. Credits endpoints
    # must never include a non-allowlisted model. (BYOK uses the customer's
    # own key and is intentionally left untouched.)
    allow = _PROVIDER_SERVED_MODEL_ALLOWLIST["cerebras"]
    cerebras_credits = {
        e.model_id
        for e in MODEL_ENDPOINTS.values()
        if e.provider == "cerebras" and e.usage_type == "Credits"
    }
    assert cerebras_credits <= allow
    assert {
        "openai/gpt-oss-120b",
        "cerebras/gpt-oss-120b",
        "z-ai/glm-4.7",
        "cerebras/zai-glm-4.7",
    } <= cerebras_credits


def test_cerebras_native_routes_use_verified_upstream_ids() -> None:
    assert MODEL_ENDPOINTS["openai/gpt-oss-120b@cerebras/prepaid"].upstream_id == (
        "gpt-oss-120b"
    )
    assert MODEL_ENDPOINTS["openai/gpt-oss-120b@cerebras/byok"].upstream_id == (
        "gpt-oss-120b"
    )
    assert MODEL_ENDPOINTS["cerebras/gpt-oss-120b@cerebras/prepaid"].upstream_id == (
        "gpt-oss-120b"
    )
    assert MODEL_ENDPOINTS["z-ai/glm-4.7@cerebras/prepaid"].upstream_id == (
        "zai-glm-4.7"
    )
    assert MODEL_ENDPOINTS["cerebras/zai-glm-4.7@cerebras/prepaid"].upstream_id == (
        "zai-glm-4.7"
    )


def test_nebius_deprecated_june_2026_models_are_not_routable() -> None:
    deprecated = _PROVIDER_DEPRECATED_UPSTREAM_MODELS["nebius"]
    nebius_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "nebius"
    ]

    assert nebius_endpoints
    for endpoint in nebius_endpoints:
        assert endpoint.model_id not in deprecated
        assert endpoint.upstream_id not in deprecated


def test_nebius_deprecation_does_not_remove_other_provider_routes() -> None:
    assert "minimax/minimax-m2.5@minimax/byok" in MODEL_ENDPOINTS
    assert "moonshotai/kimi-k2.5@kimi/prepaid" in MODEL_ENDPOINTS
    assert "openai/gpt-oss-120b@cerebras/prepaid" in MODEL_ENDPOINTS
    assert "z-ai/glm-5@zai/prepaid" in MODEL_ENDPOINTS


def test_tinfoil_june_2026_deprecations_and_replacements_are_routable() -> None:
    deprecated = _PROVIDER_DEPRECATED_UPSTREAM_MODELS["tinfoil"]
    tinfoil_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "tinfoil"
    ]

    assert tinfoil_endpoints
    for endpoint in tinfoil_endpoints:
        assert endpoint.model_id not in deprecated
        assert endpoint.upstream_id not in deprecated

    glm_52 = MODEL_ENDPOINTS["z-ai/glm-5.2@tinfoil/prepaid"]
    gemma4 = MODEL_ENDPOINTS["google/gemma-4-31b-it@tinfoil/prepaid"]
    assert glm_52.upstream_id == "glm-5-2"
    assert gemma4.upstream_id == "gemma4-31b"

    assert "z-ai/glm-5.1@tinfoil/prepaid" not in MODEL_ENDPOINTS
    assert "z-ai/glm-5.1@tinfoil/byok" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-30b-a3b-instruct@tinfoil/prepaid" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-30b-a3b-instruct@tinfoil/byok" not in MODEL_ENDPOINTS
    # Provider-scoped deprecation: non-Tinfoil routes for these model families
    # remain available when their provider still serves them.
    assert "z-ai/glm-5.1@zai/prepaid" in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-30b-a3b-instruct@phala/prepaid" in MODEL_ENDPOINTS


def test_novita_july_2026_retirements_and_replacements_are_routable() -> None:
    deprecated = _PROVIDER_DEPRECATED_UPSTREAM_MODELS["novita"]
    novita_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "novita"
    ]

    assert novita_endpoints
    for endpoint in novita_endpoints:
        assert endpoint.model_id not in deprecated
        assert endpoint.upstream_id not in deprecated

    assert "deepseek/deepseek-r1-distill-qwen-14b@novita/prepaid" not in MODEL_ENDPOINTS
    assert "deepseek/deepseek-r1-distill-qwen-14b@novita/byok" not in MODEL_ENDPOINTS
    assert "deepseek/deepseek-r1-distill-qwen-32b@novita/prepaid" not in MODEL_ENDPOINTS
    assert "deepseek/deepseek-r1-distill-qwen-32b@novita/byok" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-next-80b-a3b-thinking@novita/prepaid" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-next-80b-a3b-thinking@novita/byok" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-30b-a3b-thinking@novita/prepaid" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-30b-a3b-thinking@novita/byok" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-8b-instruct@novita/prepaid" not in MODEL_ENDPOINTS
    assert "qwen/qwen3-vl-8b-instruct@novita/byok" not in MODEL_ENDPOINTS

    assert "deepseek/deepseek-v4-flash@novita/prepaid" in MODEL_ENDPOINTS
    assert "deepseek/deepseek-v4-flash@novita/byok" in MODEL_ENDPOINTS
    assert "qwen/qwen3.6-27b@novita/prepaid" in MODEL_ENDPOINTS
    assert "qwen/qwen3.6-27b@novita/byok" in MODEL_ENDPOINTS
    assert "qwen/qwen3.6-35b-a3b@novita/prepaid" in MODEL_ENDPOINTS
    assert "qwen/qwen3.6-35b-a3b@novita/byok" in MODEL_ENDPOINTS


def test_friendli_july_2026_glm_5_deprecation_does_not_remove_glm_52() -> None:
    deprecated = _PROVIDER_DEPRECATED_UPSTREAM_MODELS["friendli"]
    friendli_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "friendli"
    ]

    assert friendli_endpoints
    for endpoint in friendli_endpoints:
        assert endpoint.model_id not in deprecated
        assert endpoint.upstream_id not in deprecated

    assert "z-ai/glm-5@friendli/prepaid" not in MODEL_ENDPOINTS
    assert "z-ai/glm-5@friendli/byok" not in MODEL_ENDPOINTS
    assert "z-ai/glm-5.2@friendli/prepaid" in MODEL_ENDPOINTS
    assert "z-ai/glm-5.2@friendli/byok" in MODEL_ENDPOINTS
    # Provider-scoped deprecation: other GLM-5 routes remain available if their
    # providers still serve them.
    assert "z-ai/glm-5@zai/prepaid" in MODEL_ENDPOINTS


def test_gemini_native_supplement_publishes_missing_text_models() -> None:
    gemini_35 = MODEL_ENDPOINTS["google/gemini-3.5-flash@gemini/prepaid"]
    image_preview = MODEL_ENDPOINTS[
        "google/gemini-3.1-flash-image-preview@gemini/prepaid"
    ]

    assert MODELS["google/gemini-3.5-flash"].context_length == 1_048_576
    assert gemini_35.upstream_id == "gemini-3.5-flash"
    assert gemini_35.prompt_price_microdollars_per_million_tokens == 1_650_000
    assert gemini_35.completion_price_microdollars_per_million_tokens == 9_900_000
    image_model = MODELS["google/gemini-3.1-flash-image-preview"]
    assert image_model.context_length == 65_536
    assert image_model.supports_chat
    assert image_preview.upstream_id == "gemini-3.1-flash-image-preview"
    assert image_preview.prompt_price_microdollars_per_million_tokens == 550_000
    assert image_preview.completion_price_microdollars_per_million_tokens == 66_000_000


def test_llama_33_70b_no_longer_credits_routes_to_cerebras() -> None:
    # Regression for the cerebras 502s: this model's Credits route used to
    # include cerebras (which can't serve it) and fail. Its prepaid routing
    # must now use only providers that actually serve it.
    credits_providers = {
        e.provider
        for e in endpoints_for_model("meta-llama/llama-3.3-70b-instruct")
        if e.usage_type == "Credits"
    }
    assert "cerebras" not in credits_providers
    assert credits_providers & {"novita", "parasail", "tinfoil", "together"}


def _endpoint_for(model_id: str, provider: str, usage_type: str) -> ModelEndpoint:
    for endpoint in endpoints_for_model(model_id):
        if endpoint.provider == provider and endpoint.usage_type == usage_type:
            return endpoint
    raise AssertionError(f"missing {provider} {usage_type} endpoint for {model_id}")


def test_novita_supplemental_prices_apply_manifest_scale() -> None:
    endpoint = _endpoint_for(
        "qwen/qwen3-235b-a22b-instruct-2507",
        provider="novita",
        usage_type="Credits",
    )

    assert endpoint.prompt_price_microdollars_per_million_tokens == 99_000
    assert endpoint.completion_price_microdollars_per_million_tokens == 638_000
    assert endpoint.prompt_price_microdollars_per_million_tokens > 10_000
