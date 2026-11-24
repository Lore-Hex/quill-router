from __future__ import annotations

import pytest

from trusted_router import catalog_ingest
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.provider_contracts import PREPAID_PROVIDER_HOLD_REASONS

# Closed-weight ByteDance/Seed models whose only authorized route is BytePlus.
_BYTEPLUS_ONLY_MODELS = frozenset({
    "bytedance/dola-seed-2-1-turbo-260628",
    "bytedance/seed-2-0-code-preview-260328",
    "bytedance/seed-2-0-lite-260428",
    "bytedance/seed-2-0-mini-260428",
    "bytedance/seed-2-0-pro-260328",
    "bytedance/seedance-2.0",
    "bytedance/seedance-2.0-fast",
    "bytedance/seedance-2.5",
})


def test_byteplus_operator_hold_survives_fresh_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    # The hourly price refresh rewrites BytePlus's manifest as available. The
    # hold lives in code, so fresh discovery must not restore a prepaid route.
    # A customer's own key (BYOK) is never affected by a prepaid hold.
    assert PREPAID_PROVIDER_HOLD_REASONS.get("byteplus") == "operator-credit-exhausted"
    monkeypatch.setattr(catalog_ingest, "_provider_manifest_dark_model_ids", lambda: {})
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_SERVED_MODEL_ALLOWLIST", {})
    monkeypatch.setattr(catalog_ingest, "_AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS", frozenset())
    model = "z-ai/glm-5.2"
    endpoints = {
        f"{slug}/{usage}": ModelEndpoint(
            id=f"{slug}/{usage}", model_id=model, provider=slug,
            usage_type=usage, upstream_id=model,
        )
        for slug in ("byteplus", "other-provider") for usage in ("Credits", "BYOK")
    }
    filtered = catalog_ingest._filter_unserved_provider_endpoints(
        endpoints, explicit_model_ids=frozenset({model}),
    )
    assert filtered == {key: row for key, row in endpoints.items() if key != "byteplus/Credits"}


def test_byteplus_routes_leave_routing_and_probes_but_settle() -> None:
    from trusted_router.catalog import (
        MODEL_ENDPOINTS,
        PROVIDERS,
        endpoint_for_id,
        endpoints_for_model,
    )
    from trusted_router.catalog_registry import HELD_PREPAID_SETTLEMENT_ENDPOINTS
    from trusted_router.routes.internal.gateway import _endpoint_for_id_compat
    from trusted_router.synthetic.probes import rotation_candidates

    assert "byteplus" in PROVIDERS
    assert not [row for row in MODEL_ENDPOINTS.values() if row.provider == "byteplus"]
    assert "byteplus" not in rotation_candidates()
    held = [row for row in HELD_PREPAID_SETTLEMENT_ENDPOINTS.values() if row.provider == "byteplus"]
    assert _BYTEPLUS_ONLY_MODELS <= {row.model_id for row in held}
    for row in held:
        assert endpoint_for_id(row.id) is None
        assert row not in endpoints_for_model(row.model_id)
        # Calls authorized before the hold can still settle at their tariff.
        assert _endpoint_for_id_compat(row.id) == row
    # BytePlus is the only authorized source for closed ByteDance/Seed models,
    # so they have no route at all until the hold is lifted.
    for model_id in _BYTEPLUS_ONLY_MODELS:
        assert not endpoints_for_model(model_id)
    # Open-weight models BytePlus also carried keep serving elsewhere.
    for model_id in ("z-ai/glm-5.2", "z-ai/glm-5.3-flash", "deepseek/deepseek-v4-flash-0731"):
        assert any(row.usage_type == "Credits" for row in endpoints_for_model(model_id))
