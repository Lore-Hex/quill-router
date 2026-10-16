from datetime import UTC, datetime, timedelta

import pytest

from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint


@pytest.mark.parametrize("provider,model,native,cutoff", [
    ("nvidia-nim", "moonshotai/kimi-k3", "moonshotai/kimi-k3",
     datetime(2026, 10, 15, 8, tzinfo=UTC)),
    ("streamlake", "qwen/qwen3-vl-235b-a22b-instruct", "Qwen3-VL-235B-A22B-Instruct",
     datetime(2026, 10, 16, 22, 1, tzinfo=UTC)),
    ("streamlake", "qwen/qwen3-235b-a22b-thinking-2507", "Qwen3-235B-A22B-Thinking-2507",
     datetime(2026, 10, 16, 22, 1, tzinfo=UTC)),
    ("streamlake", "qwen/qwen3-30b-a3b-instruct-2507", "Qwen3-30B-A3B-Instruct-2507",
     datetime(2026, 10, 16, 22, 1, tzinfo=UTC)),
])
def test_retirements_are_exact_provider_scoped_and_filter_stale_catalog(
    monkeypatch, provider, model, native, cutoff,
):
    assert not provider_lifecycle.provider_model_retired(
        provider, model, native, at=cutoff - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(provider, model, at=cutoff)
    assert provider_lifecycle.provider_model_retired(provider, "unknown", native, at=cutoff)
    assert not provider_lifecycle.provider_model_retired("other", model, native, at=cutoff)
    endpoints = [ModelEndpoint(
        id=f"{model}@{slug}/prepaid", model_id=model, provider=slug,
        upstream_id=native, usage_type="Credits",
    ) for slug in (provider, "other")]
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", {e.id: e for e in endpoints})
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: cutoff)
    assert [e.provider for e in catalog.endpoints_for_model(model)] == ["other"]


def test_lightning_security_incident_blocks_all_credentials_and_new_models():
    from trusted_router.provider_contracts import provider_model_operator_held

    for model in ("lightning-ai/glm-5.3", "lightning-ai/future-model"):
        assert provider_model_operator_held("lightning", model, model)
    assert not provider_model_operator_held("other", "lightning-ai/glm-5.3")
    assert not [e for e in catalog.MODEL_ENDPOINTS.values() if e.provider == "lightning"]
