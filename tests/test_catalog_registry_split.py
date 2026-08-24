"""Contract for the catalog.py -> catalog_registry.py split (#38).

The money-critical MODELS / MODEL_ENDPOINTS construction lives in
catalog_registry.py; catalog.py re-exports it and layers the privacy / routing
/ serialization query helpers on top. These tests pin the split so a future
refactor can't silently (a) reintroduce an import cycle, or (b) make catalog's
re-export drift from the registry's built objects.
"""

from __future__ import annotations

import importlib


def test_registry_imports_standalone_without_catalog() -> None:
    # The registry must build purely from the catalog_data / catalog_ingest /
    # pricing leaves — importing it must not require catalog.py (no cycle).
    registry = importlib.import_module("trusted_router.catalog_registry")
    assert registry.MODELS, "registry built an empty MODELS"
    assert registry.MODEL_ENDPOINTS, "registry built an empty MODEL_ENDPOINTS"


def test_catalog_reexports_the_same_objects() -> None:
    from trusted_router import catalog, catalog_registry

    # Re-export identity, not a copy: catalog's helpers and the registry read
    # the exact same dicts, so there is a single source of truth for routing.
    assert catalog.MODELS is catalog_registry.MODELS
    assert catalog.MODEL_ENDPOINTS is catalog_registry.MODEL_ENDPOINTS
    # Sanity: the hand-coded Auto meta-model and the ingested endpoints are present.
    assert catalog.AUTO_MODEL_ID in catalog.MODELS
    assert len(catalog.MODEL_ENDPOINTS) > 100


def test_registry_module_defines_no_catalog_query_helpers() -> None:
    # Guardrail for the split's intent: the registry is construction only; the
    # query surface (privacy tiers, routing candidates, serialization) stays in
    # catalog.py. If someone moves a helper into the registry, this flags it.
    registry = importlib.import_module("trusted_router.catalog_registry")
    for helper in ("endpoint_privacy_tier", "auto_candidate_models", "endpoints_for_model"):
        assert not hasattr(registry, helper), (
            f"{helper} leaked into catalog_registry; query helpers belong in catalog.py"
        )
