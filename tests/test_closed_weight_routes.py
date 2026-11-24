"""Closed-weight models route only through their maker and authorized clouds.

Reseller catalogs list a maker's public ids (GMI: Gemini 4 Argon, GPT-6 Sol,
Claude; atlas-cloud: Gemini 3.8 Flash, Grok 4.6; novita: GPT-6.2). Listing an
id is not an admission, for Credits or BYOK, and the hourly refresh must not
be able to restore such a route. Open weights in the same namespace (Gemma,
gpt-oss, Whisper large, Seed-OSS, UI-TARS, MiniMax M-series) remain hostable by
anyone, as do open video weights from other makers (Wan, LTX).
"""

from dataclasses import replace

import pytest

from trusted_router import catalog_ingest
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, model_open_weights
from trusted_router.catalog_ingest import _filter_unserved_provider_endpoints

AUTHORIZED = {
    "google": ("google-ai-studio", "google-vertex"),
    "openai": ("openai", "azure"),
    "anthropic": ("anthropic", "google-vertex", "amazon-bedrock"),
    "x-ai": ("grok", "azure"),
    "bytedance": ("byteplus",),
    "bytedance-seed": ("byteplus",),
    "minimax": ("minimax",),
    "runway": ("runway",),
}
CLOSED_MODELS = (
    "google/gemini-4-argon",
    "google/gemini-3.8-flash",
    "google/gemini-4-hydrogen",
    "google/veo-3.1",
    "google/gemini-future",
    "openai/gpt-6-sol",
    "openai/o3",
    "openai/sora-2",
    "openai/gpt-future",
    "anthropic/claude-opus-5",
    "anthropic/claude-future",
    "x-ai/grok-4.6",
    "x-ai/grok-future",
    # GMI's malformed spellings (no hyphen), published via
    # canonicalize_native_model_id; the family matches the whole namespace.
    "x-ai/grok4.6",
    "x-ai/grok4.7",
    "x-ai/grok-2-1212",
    # ByteDance Seed and Doubao text, and Seedance video (venice, atlas-cloud,
    # deepinfra and openrouter list them).
    "bytedance/seed-1.8",
    "bytedance/seed-2.0-pro",
    "bytedance/doubao-seed-2.1-pro-260628",
    "bytedance/seedance-2.0",
    "bytedance/seedance-2.5",
    "bytedance-seed/seed-2-1-turbo",
    # MiniMax Hailuo video, including its H3 spellings.
    "minimax/hailuo-3",
    "minimax/h3-max",
    "minimax/video-01",
    "runway/gen-4.5",
)
# Not listed by any manifest, so no other filter can drop the direct routes.
UNLISTED_CLOSED_MODELS = (
    "google/gemini-future", "google/veo-future", "openai/gpt-future", "openai/o9",
    "openai/sora-future", "anthropic/claude-future", "x-ai/grok-future",
    "bytedance/seed-future", "bytedance/seedance-future", "bytedance-seed/seed-future",
    "minimax/hailuo-future", "runway/gen-future",
)
RESELLERS = (
    "gmi", "atlas-cloud", "deepinfra", "novita", "venice", "boundless", "fal", "openrouter",
    "future-reseller",
)
OPEN_WEIGHT_LOOKALIKES = (
    "google/gemma-4-31b-it",
    "google/embeddinggemma-300m",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-safeguard-20b",
    "openai/whisper-large-v3",
    "bytedance/ui-tars-1.5-7b",
    "bytedance-seed/seed-oss-36b-instruct",
    "ByteDance-Seed/Seed-Coder-8B-Instruct",
    "minimax/minimax-m3",
    "minimax/minimax-m2.5",
    "minimaxai/minimax-m1-80k",
    "alibaba/wan-2.7",
    "lightricks/ltx-2.3",
    "lightricks/ltx-2.3-fast",
)


def _route(model_id: str, provider: str, usage_type: str):
    template = next(iter(MODEL_ENDPOINTS.values()))
    suffix = "prepaid" if usage_type == "Credits" else "byok"
    return replace(
        template, id=f"{model_id}@{provider}/{suffix}", model_id=model_id,
        provider=provider, upstream_id=model_id.split("/", 1)[1], usage_type=usage_type,
    )


def _admitted(endpoint) -> bool:
    # Explicit admission bypasses the chat manifest allowlists, leaving the
    # routing-policy filters that apply to every route.
    kept = _filter_unserved_provider_endpoints(
        {endpoint.id: endpoint}, explicit_model_ids=frozenset({endpoint.model_id}),
    )
    return endpoint.id in kept


@pytest.mark.parametrize("usage_type", ["Credits", "BYOK"])
@pytest.mark.parametrize("provider", RESELLERS)
@pytest.mark.parametrize("model_id", CLOSED_MODELS)
def test_reseller_route_for_closed_model_is_refused(
    model_id: str, provider: str, usage_type: str,
) -> None:
    assert not _admitted(_route(model_id, provider, usage_type))


@pytest.mark.parametrize("usage_type", ["Credits", "BYOK"])
@pytest.mark.parametrize("model_id", UNLISTED_CLOSED_MODELS)
def test_maker_and_authorized_clouds_are_kept(
    model_id: str, usage_type: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The closed-weight rule alone: an operator's prepaid billing hold on an
    # authorized provider (tests/test_*_prepaid_hold.py) is a separate filter.
    monkeypatch.setattr(catalog_ingest, "PREPAID_PROVIDER_HOLD_REASONS", {})
    family = model_id.split("/", 1)[0]
    for provider in AUTHORIZED[family]:
        assert _admitted(_route(model_id, provider, usage_type)), provider


@pytest.mark.parametrize("usage_type", ["Credits", "BYOK"])
# Hosts without a manifest: a real host's discovery may hold a route for its
# own reasons, which says nothing about the closed-weight rule.
@pytest.mark.parametrize("provider", ["future-reseller", "independent-host"])
@pytest.mark.parametrize("model_id", OPEN_WEIGHT_LOOKALIKES)
def test_open_weights_in_a_closed_namespace_are_unrestricted(
    model_id: str, provider: str, usage_type: str,
) -> None:
    assert _admitted(_route(model_id, provider, usage_type))


def test_catalog_has_no_unauthorized_closed_weight_route() -> None:
    from tests.catalog_vehicles import registry_endpoints
    from trusted_router.closed_weight_routes import authorized_closed_weight_providers

    # The registry's own output: test vehicles frozen from an older catalog
    # (tests/catalog_vehicles.py) are not routes the catalog built.
    offenders = sorted(
        endpoint.id for endpoint in registry_endpoints().values()
        if (allowed := authorized_closed_weight_providers(endpoint.model_id)) is not None
        and endpoint.provider not in allowed
    )
    assert offenders == []


@pytest.mark.parametrize("endpoint_id", [
    "google/gemini-4-argon@gmi/byok",
    "google/gemini-3.8-flash@atlas-cloud/prepaid",
    "google/gemini-2.5-pro@deepinfra/prepaid",
    "google/veo-3.1@venice/prepaid",
    "openai/gpt-5.5@gmi/byok",
    "openai/gpt-6-luna@boundless/prepaid",
    "openai/gpt-6.2-sol@novita/prepaid",
    "openai/sora-2@venice/prepaid",
    "anthropic/claude-opus-5@deepinfra/byok",
    "x-ai/grok-4.6@atlas-cloud/prepaid",
    "x-ai/grok4.6@gmi/byok",
    "x-ai/grok4.7@gmi/byok",
    "bytedance/seed-1.8@deepinfra/byok",
    "bytedance/seed-2.0-pro@deepinfra/prepaid",
    "bytedance/doubao-seed-2.1-pro-260628@atlas-cloud/prepaid",
    "bytedance-seed/seed-2-1-turbo@openrouter/prepaid",
    "bytedance/seedance-2.0@venice/prepaid",
    "bytedance/seedance-2.5@venice/prepaid",
    "minimax/hailuo-3@atlas-cloud/prepaid",
    "minimax/hailuo-3@venice/prepaid",
    "minimax/h3-max@fal/prepaid",
    "runway/gen-4.5@venice/prepaid",
])
def test_known_reseller_routes_are_absent(endpoint_id: str) -> None:
    assert endpoint_id not in MODEL_ENDPOINTS


@pytest.mark.parametrize("endpoint_id", [
    "google/gemini-4-argon@google-ai-studio/byok",
    "google/gemini-3.8-flash@google-vertex/prepaid",
    "openai/gpt-5.5@openai/prepaid",
    "anthropic/claude-opus-5@anthropic/prepaid",
    "x-ai/grok-4.6@grok/prepaid",
    "openai/gpt-oss-120b@deepinfra/prepaid",
    "google/gemma-4-31b-it@parasail/prepaid",
    "bytedance/seed-2-0-pro-260328@byteplus/prepaid",
    "bytedance/seedance-2.0@byteplus/prepaid",
    "bytedance/seedance-2.5@byteplus/prepaid",
    "runway/gen-4.5@runway/prepaid",
    "bytedance/ui-tars-1.5-7b@parasail/byok",
    "minimax/minimax-m3@novita/prepaid",
    "minimax/minimax-m2.5@gmi/byok",
    "alibaba/wan-2.7@venice/prepaid",
    "lightricks/ltx-2.3@venice/prepaid",
])
def test_direct_and_open_weight_routes_remain(endpoint_id: str) -> None:
    from trusted_router.catalog_registry import HELD_PREPAID_SETTLEMENT_ENDPOINTS
    from trusted_router.provider_contracts import PREPAID_PROVIDER_HOLD_REASONS

    provider = endpoint_id.rsplit("@", 1)[1].split("/", 1)[0]
    if endpoint_id.endswith("/prepaid") and provider in PREPAID_PROVIDER_HOLD_REASONS:
        # An operator hold keeps the authorized route dark, settlement-only.
        assert endpoint_id in HELD_PREPAID_SETTLEMENT_ENDPOINTS
        assert endpoint_id not in MODEL_ENDPOINTS
    else:
        assert endpoint_id in MODEL_ENDPOINTS


def test_open_weight_badge_and_route_policy_agree() -> None:
    from trusted_router.closed_weight_routes import closed_weight_family

    contradictions = sorted(
        model.id for model in MODELS.values()
        if model_open_weights(model) and closed_weight_family(model.id) is not None
    )
    assert contradictions == []


def test_removed_routes_still_settle_previously_authorized_requests() -> None:
    """A control plane that still listed a reseller route may have authorized
    a request on it; settlement and refunds must find its quoted tariff."""
    from trusted_router.catalog import endpoint_for_id
    from trusted_router.catalog_registry import CLOSED_WEIGHT_SETTLEMENT_ENDPOINTS
    from trusted_router.closed_weight_routes import closed_weight_route_disallowed
    from trusted_router.routes.internal.gateway import _endpoint_for_id_compat

    assert CLOSED_WEIGHT_SETTLEMENT_ENDPOINTS
    for endpoint_id, archived in CLOSED_WEIGHT_SETTLEMENT_ENDPOINTS.items():
        assert closed_weight_route_disallowed(archived.provider, archived.model_id)
        assert endpoint_for_id(endpoint_id) is None
        assert _endpoint_for_id_compat(endpoint_id) is archived
