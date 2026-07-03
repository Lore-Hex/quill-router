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
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _catalog_loaded_when_importing(module: str) -> bool:
    """Import `module` in a BRAND-NEW interpreter and report whether
    trusted_router.catalog got pulled in as a side effect.

    Deliberately a subprocess: the obvious in-process version would clear
    trusted_router.* out of sys.modules to force a fresh import, but doing that
    mid-suite poisons the singleton STORE and every active monkeypatch for the
    tests that run after this one (a real cascade — do not reintroduce it).
    """
    code = (
        f"import sys; import {module}; "
        "print('LOADED' if 'trusted_router.catalog' in sys.modules else 'CLEAN')"
    )
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")}
    out = subprocess.run(  # noqa: S603 - `module` is a hardcoded literal, not user input
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip() == "LOADED"


def test_privacy_and_routing_import_without_loading_catalog() -> None:
    # Importing either split module standalone must NOT drag in catalog.py —
    # that is the invariant that keeps the dependency graph acyclic.
    assert not _catalog_loaded_when_importing("trusted_router.catalog_privacy")
    assert not _catalog_loaded_when_importing("trusted_router.routing_candidates")


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
