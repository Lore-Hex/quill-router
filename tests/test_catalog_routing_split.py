"""Contract for the catalog.py -> catalog_privacy.py / routing_candidates.py
split (#38).

Privacy-posture tiers and meta-model candidate selection were lifted out of the
catalog.py god-module into two dedicated modules. catalog.py re-exports both so
every existing `from trusted_router.catalog import ...` caller keeps working.
These tests pin (a) no import cycle, (b) re-export identity, and (c) the
money-path routing/privacy layering (routing depends on privacy, both depend
only on the registry/data leaves).
"""

from __future__ import annotations

import importlib
import sys


def _fresh_import(name: str) -> object:
    for mod in list(sys.modules):
        if mod.startswith("trusted_router"):
            del sys.modules[mod]
    return importlib.import_module(name)


def test_privacy_and_routing_import_without_loading_catalog() -> None:
    # Importing either split module standalone must NOT drag in catalog.py —
    # that is the invariant that keeps the dependency graph acyclic.
    _fresh_import("trusted_router.catalog_privacy")
    assert "trusted_router.catalog" not in sys.modules
    _fresh_import("trusted_router.routing_candidates")
    assert "trusted_router.catalog" not in sys.modules


def test_catalog_reexports_the_same_function_objects() -> None:
    from trusted_router import catalog, catalog_privacy, routing_candidates

    # Re-export identity (not copies): callers importing from catalog get the
    # exact functions that live in the split modules.
    assert catalog.endpoint_privacy_tier is catalog_privacy.endpoint_privacy_tier
    assert catalog.provider_privacy_tier is catalog_privacy.provider_privacy_tier
    assert catalog.auto_candidate_models is routing_candidates.auto_candidate_models
    assert catalog.meta_candidate_models is routing_candidates.meta_candidate_models
    assert catalog.InvalidAutoModelOrder is routing_candidates.InvalidAutoModelOrder


def test_routing_uses_privacy_not_the_other_way() -> None:
    # Routing selection reads privacy tiers; privacy must stay a pure leaf that
    # never reaches back into routing (or catalog). This encodes the layering.
    privacy = importlib.import_module("trusted_router.catalog_privacy")
    routing = importlib.import_module("trusted_router.routing_candidates")
    assert routing.endpoint_privacy_tier is privacy.endpoint_privacy_tier
    for leaked in ("auto_candidate_models", "meta_candidate_models", "MODELS"):
        assert not hasattr(privacy, leaked), (
            f"{leaked} leaked into catalog_privacy; it must remain a pure leaf"
        )
