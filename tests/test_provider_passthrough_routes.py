"""Native-hosting policy must survive refreshes and stale catalog snapshots."""

from dataclasses import replace

import pytest

from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.catalog_ingest import _filter_unserved_provider_endpoints
from trusted_router.provider_contracts import (
    PROVIDER_INCIDENT_HOLD_REASONS,
    provider_model_is_openai_reseller,
    provider_model_operator_held,
)


@pytest.mark.parametrize("model_id", [
    "google/gemini-2.5-flash", "google/gemini-future-preview",
    "google/imagen-future", "google/veo-future",
    "openai/gpt-6-sol", "openai/gpt-future", "anthropic/claude-future",
    "x-ai/grok-future", "new-author/new-model",
])
@pytest.mark.parametrize("provider", ["lightning", "cloudflare-workers-ai"])
@pytest.mark.parametrize("usage_type", ["Credits", "BYOK"])
def test_stale_or_future_passthrough_cannot_bypass_policy(
    monkeypatch: pytest.MonkeyPatch, model_id: str, provider: str, usage_type: str,
) -> None:
    # Hosting restrictions must still apply after a temporary incident clears.
    monkeypatch.delitem(PROVIDER_INCIDENT_HOLD_REASONS, provider, raising=False)
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
    if provider in PROVIDER_INCIDENT_HOLD_REASONS:
        assert not endpoints
        return
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
    monkeypatch: pytest.MonkeyPatch, provider: str, model_id: str, upstream_id: str,
) -> None:
    monkeypatch.delitem(PROVIDER_INCIDENT_HOLD_REASONS, provider, raising=False)
    assert not provider_model_operator_held(provider, model_id, upstream_id)
    template = next(iter(MODEL_ENDPOINTS.values()))
    endpoint = replace(template, id="hosted", provider=provider, model_id=model_id,
                       upstream_id=upstream_id, usage_type="Credits")
    assert _filter_unserved_provider_endpoints(
        {endpoint.id: endpoint}, explicit_model_ids=frozenset({model_id}),
    ) == {endpoint.id: endpoint}


@pytest.mark.parametrize("provider", ["atlas-cloud", "boundless", "gmi", "venice", "future-reseller"])
@pytest.mark.parametrize("model_id", ["openai/gpt-6-sol", "openai/gpt-future", "openai/o3", "openai/sora-2", "openai/whisper-1"])
@pytest.mark.parametrize("usage_type", ["Credits", "BYOK"])
def test_openai_resellers_are_blocked_even_with_fresh_or_explicit_routes(
    provider: str, model_id: str, usage_type: str,
) -> None:
    template = next(iter(MODEL_ENDPOINTS.values()))
    endpoint = replace(template, id="reseller", provider=provider, model_id=model_id,
                       upstream_id=model_id.removeprefix("openai/"), usage_type=usage_type)
    assert provider_model_is_openai_reseller(provider, model_id)
    assert _filter_unserved_provider_endpoints(
        {endpoint.id: endpoint}, explicit_model_ids=frozenset({model_id}),
    ) == {}


@pytest.mark.parametrize("provider", ["openai", "azure"])
@pytest.mark.parametrize("usage_type", ["Credits", "BYOK"])
def test_openai_direct_deployments_are_not_resellers(provider: str, usage_type: str) -> None:
    model_id = "openai/gpt-future"
    template = next(iter(MODEL_ENDPOINTS.values()))
    endpoint = replace(template, id="direct", provider=provider, model_id=model_id,
                       upstream_id="deployment-name", usage_type=usage_type)
    assert not provider_model_operator_held(provider, model_id, endpoint.upstream_id)
    assert _filter_unserved_provider_endpoints(
        {endpoint.id: endpoint}, explicit_model_ids=frozenset({model_id}),
    ) == {endpoint.id: endpoint}


@pytest.mark.parametrize("provider,model_id", [
    ("atlas-cloud", "openai/gpt-oss-120b"),
    ("gmi", "openai/gpt-oss-120b"),
    ("tinfoil", "openai/gpt-oss-safeguard-120b"),
    ("privatemode", "openai/whisper-large-v3"),
    ("tinfoil", "openai/whisper-large-v3-turbo"),
])
def test_independently_hosted_openai_weights_stay_allowed(provider: str, model_id: str) -> None:
    assert not provider_model_operator_held(provider, model_id)
    template = next(iter(MODEL_ENDPOINTS.values()))
    endpoint = replace(template, id="hosted", provider=provider, model_id=model_id,
                       upstream_id=model_id, usage_type="Credits")
    assert _filter_unserved_provider_endpoints(
        {endpoint.id: endpoint}, explicit_model_ids=frozenset({model_id}),
    ) == {endpoint.id: endpoint}


def test_catalog_cannot_publish_proprietary_openai_resellers() -> None:
    from tests.catalog_vehicles import registry_endpoints

    endpoints = list(registry_endpoints().values())
    forbidden = [
        endpoint.id for endpoint in endpoints
        if endpoint.model_id.startswith("openai/")
        and not endpoint.model_id.startswith(("openai/gpt-oss-", "openai/whisper-large-"))
        and endpoint.provider not in {"openai", "azure"}
    ]
    assert forbidden == []
    assert any(endpoint.provider == "openai" and endpoint.usage_type == "Credits" for endpoint in endpoints)
    assert any(endpoint.provider == "openai" and endpoint.usage_type == "BYOK" for endpoint in endpoints)
    assert any(endpoint.provider == "azure" and endpoint.model_id.startswith("openai/") for endpoint in endpoints)
    assert any(endpoint.provider == "gmi" and endpoint.model_id == "openai/gpt-oss-120b" for endpoint in endpoints)


def test_reseller_tariffs_are_available_only_to_previously_authorized_requests() -> None:
    from types import SimpleNamespace

    from trusted_router.catalog import endpoint_for_id, endpoints_for_model
    from trusted_router.catalog_registry import HELD_POLICY_SETTLEMENT_ENDPOINTS
    from trusted_router.routes.internal.gateway import _select_authorized_endpoint
    from trusted_router.schemas import GatewaySettleRequest

    assert HELD_POLICY_SETTLEMENT_ENDPOINTS
    for identity, endpoint in HELD_POLICY_SETTLEMENT_ENDPOINTS.items():
        assert provider_model_is_openai_reseller(endpoint.provider, endpoint.model_id)
        assert endpoint_for_id(identity) is None
        assert endpoint not in endpoints_for_model(endpoint.model_id)
        auth = SimpleNamespace(
            user_provided_model_id=None, model_id=endpoint.model_id,
            endpoint_id=identity, candidate_endpoint_ids=[identity],
        )
        body = GatewaySettleRequest(authorization_id="previously-authorized", selected_endpoint=identity)
        assert _select_authorized_endpoint(auth, body) == endpoint
        auth.candidate_endpoint_ids = ["unrelated@openai/prepaid"]
        assert _select_authorized_endpoint(auth, body) is None
