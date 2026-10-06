"""Google passthrough retirement must not disable Lightning-hosted Gemma."""

from dataclasses import replace

import pytest

from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.catalog_ingest import _filter_unserved_provider_endpoints
from trusted_router.provider_contracts import provider_model_operator_held


@pytest.mark.parametrize("model_id", [
    "google/gemini-2.5-flash", "google/gemini-future-preview",
    "google/imagen-future", "google/veo-future",
])
@pytest.mark.parametrize("usage_type", ["Credits", "BYOK"])
def test_stale_or_future_google_route_cannot_bypass_policy(model_id: str, usage_type: str) -> None:
    template = next(iter(MODEL_ENDPOINTS.values()))
    endpoint = replace(template, id="stale-google", provider="lightning", model_id=model_id,
                       upstream_id=model_id, usage_type=usage_type)
    assert provider_model_operator_held("lightning", model_id)
    # Explicit media registrations and stale snapshots cannot bypass the hold.
    assert _filter_unserved_provider_endpoints(
        {endpoint.id: endpoint}, explicit_model_ids=frozenset({model_id}),
    ) == {}


def test_catalog_removes_google_passthrough_only() -> None:
    lightning = [ep for ep in MODEL_ENDPOINTS.values() if ep.provider == "lightning"]
    assert lightning
    assert not [ep for ep in lightning if ep.model_id.startswith("google/gemini-")]
    assert any(ep.model_id == "google/gemma-4-31b-it" for ep in lightning)
    for provider in ("google-ai-studio", "google-vertex"):
        assert not provider_model_operator_held(provider, "google/gemini-3.5-flash")
        assert any(ep.provider == provider and ep.model_id.startswith("google/gemini-")
                   and ep.usage_type == "Credits" for ep in MODEL_ENDPOINTS.values())
    assert not provider_model_operator_held("lightning", "openai/gpt-4.1")
    assert not provider_model_operator_held("lightning", "google/gemma-4-31b-it")
