from datetime import UTC, datetime

from scripts import prod_embeddings_probe as probe
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
from trusted_router.provider_lifecycle import provider_model_retired


def test_daily_probe_has_no_retired_or_unroutable_targets() -> None:
    for spec in probe.PROBES:
        provider, model = str(spec["provider"]), str(spec["model"])
        assert not provider_model_retired(provider, model, at=datetime(2026, 9, 20, tzinfo=UTC))
        assert MODELS[model].supports_embeddings
        assert any(
            endpoint.provider == provider and endpoint.model_id == model
            for endpoint in MODEL_ENDPOINTS.values()
        )


def test_together_retirement_does_not_replace_with_dedicated_only_route() -> None:
    assert "together" not in {spec["provider"] for spec in probe.PROBES}
    assert not any(
        endpoint.provider == "together" and MODELS[endpoint.model_id].supports_embeddings
        for endpoint in MODEL_ENDPOINTS.values()
    )
    assert next(spec for spec in probe.PROBES if spec["provider"] == "openai")["required"]
