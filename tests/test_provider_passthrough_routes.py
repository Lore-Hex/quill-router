"""Native-hosting policy must survive refreshes and stale catalog snapshots."""

from dataclasses import replace

import pytest

from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.catalog_ingest import _filter_unserved_provider_endpoints
from trusted_router.provider_contracts import provider_model_operator_held


@pytest.mark.parametrize("model_id", [
    "google/gemini-2.5-flash", "google/gemini-future-preview",
    "google/imagen-future", "google/veo-future",
    "openai/gpt-6-sol", "openai/gpt-future", "anthropic/claude-future",
    "x-ai/grok-future", "new-author/new-model",
])
@pytest.mark.parametrize("provider", ["lightning", "cloudflare-workers-ai"])
@pytest.mark.parametrize("usage_type", ["Credits", "BYOK"])
def test_stale_or_future_passthrough_cannot_bypass_policy(
    model_id: str, provider: str, usage_type: str,
) -> None:
    template = next(iter(MODEL_ENDPOINTS.values()))
    endpoint = replace(template, id="stale-passthrough", provider=provider, model_id=model_id,
                       upstream_id=model_id, usage_type=usage_type)
    assert provider_model_operator_held(provider, model_id)
    # Explicit media registrations and stale snapshots cannot bypass the hold.
    assert _filter_unserved_provider_endpoints(
        {endpoint.id: endpoint}, explicit_model_ids=frozenset({model_id}),
    ) == {}


@pytest.mark.parametrize(("provider", "prefix"), [
    ("lightning", "lightning-ai/"), ("cloudflare-workers-ai", "@cf/"),
])
def test_catalog_exposes_native_hosting_only(provider: str, prefix: str) -> None:
    endpoints = [ep for ep in MODEL_ENDPOINTS.values() if ep.provider == provider]
    assert endpoints
    assert all(ep.upstream_id.startswith(prefix) or
               (provider, ep.upstream_id) == ("cloudflare-workers-ai", "moonshotai/kimi-k3")
               for ep in endpoints)


@pytest.mark.parametrize(("provider", "model_prefix"), [
    ("openai", "openai/gpt-"), ("anthropic", "anthropic/claude-"),
    ("google-ai-studio", "google/gemini-"), ("google-vertex", "google/gemini-"),
])
def test_direct_providers_are_untouched(provider: str, model_prefix: str) -> None:
    assert not provider_model_operator_held(provider, model_prefix + "future")
    assert any(ep.provider == provider and ep.model_id.startswith(model_prefix)
                   and ep.usage_type == "Credits" for ep in MODEL_ENDPOINTS.values())


@pytest.mark.parametrize(("provider", "model_id", "upstream_id"), [
    ("lightning", "google/gemma-4-31b-it", "lightning-ai/gemma-4-31B-it"),
    ("lightning", "meta-llama/llama-3.3-70b-instruct", "lightning-ai/llama-3.3-70b"),
    ("cloudflare-workers-ai", "openai/gpt-oss-120b", "@cf/openai/gpt-oss-120b"),
    ("cloudflare-workers-ai", "google/gemma-4-26b-a4b-it", "@cf/google/gemma-4-26b-a4b-it"),
    ("cloudflare-workers-ai", "moonshotai/kimi-k3", "moonshotai/kimi-k3"),
])
def test_hosting_not_model_author_controls_eligibility(
    provider: str, model_id: str, upstream_id: str,
) -> None:
    assert not provider_model_operator_held(provider, model_id, upstream_id)
    template = next(iter(MODEL_ENDPOINTS.values()))
    endpoint = replace(template, id="hosted", provider=provider, model_id=model_id,
                       upstream_id=upstream_id, usage_type="Credits")
    assert _filter_unserved_provider_endpoints(
        {endpoint.id: endpoint}, explicit_model_ids=frozenset({model_id}),
    ) == {endpoint.id: endpoint}
