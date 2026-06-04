from __future__ import annotations

from trusted_router.catalog import (
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
