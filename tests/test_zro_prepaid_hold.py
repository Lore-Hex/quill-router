from __future__ import annotations

import pytest

from trusted_router import catalog_ingest
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.provider_contracts import PREPAID_PROVIDER_HOLD_REASONS


def test_zro_operator_hold_survives_fresh_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    # The hourly price refresh rewrites zro's manifest as available. The hold
    # lives in code, so fresh discovery must not restore any zro route.
    assert PREPAID_PROVIDER_HOLD_REASONS.get("zro") == "operator-credit-exhausted"
    monkeypatch.setattr(catalog_ingest, "_provider_manifest_dark_model_ids", lambda: {})
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_SERVED_MODEL_ALLOWLIST", {})
    monkeypatch.setattr(catalog_ingest, "_AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS", frozenset())
    model = "z-ai/glm-5.3"
    endpoints = {
        slug: ModelEndpoint(
            id=slug, model_id=model, provider=slug, usage_type="Credits", upstream_id=model,
        )
        for slug in ("zro", "other-provider")
    }
    assert catalog_ingest._filter_unserved_provider_endpoints(
        endpoints, explicit_model_ids=frozenset({model}),
    ) == {"other-provider": endpoints["other-provider"]}


def test_zro_routes_leave_routing_and_probes_but_settle() -> None:
    from trusted_router.catalog import (
        MODEL_ENDPOINTS,
        PROVIDERS,
        endpoint_for_id,
        endpoints_for_model,
    )
    from trusted_router.catalog_registry import HELD_PREPAID_SETTLEMENT_ENDPOINTS
    from trusted_router.routes.internal.gateway import _endpoint_for_id_compat
    from trusted_router.synthetic.probes import rotation_candidates

    assert "zro" in PROVIDERS
    assert not [row for row in MODEL_ENDPOINTS.values() if row.provider == "zro"]
    assert "zro" not in rotation_candidates()
    held = [row for row in HELD_PREPAID_SETTLEMENT_ENDPOINTS.values() if row.provider == "zro"]
    assert held
    for row in held:
        assert endpoint_for_id(row.id) is None
        assert row not in endpoints_for_model(row.model_id)
        # Calls authorized before the hold can still settle at their tariff.
        assert _endpoint_for_id_compat(row.id) == row
    # Shared models keep serving through other providers.
    for model_id in ("z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash"):
        assert any(row.usage_type == "Credits" for row in endpoints_for_model(model_id))
