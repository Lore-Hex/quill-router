"""Mixed-host video routes the video mechanics tests ride, put back per test.

The video control-plane tests exercise rules that need one model with two
hosts: a token-billed host beside a fixed-quote host (Seedance on BytePlus and
Venice), or two fixed-quote hosts (Hailuo 3 on Venice and Atlas Cloud). The
closed-weight rule (trusted_router.closed_weight_routes) removed the reseller
side of each pair, and no catalog model has such a pair any more. The rules
under test (provider headers, frozen tariffs, idempotent replay, settlement)
are not about which hosts serve these models, so a test that needs the pair
borrows the archived routes (CLOSED_WEIGHT_SETTLEMENT_ENDPOINTS) for its own
duration. Tests about the catalog itself never use this; they assert the
routes are gone (tests/test_closed_weight_routes.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

from tests.fixture_routes import clear_catalog_caches
from trusted_router import catalog_registry

VIDEO_VEHICLE_MODEL_IDS = frozenset({
    "bytedance/seedance-2.5",
    "bytedance/seedance-2.0",
    "bytedance/seedance-2.0-fast",
    "minimax/hailuo-3",
    "minimax/h3-max",
})


@pytest.fixture
def archived_video_routes() -> Iterator[None]:
    """Borrow the archived mixed-host video routes for one test."""
    with pytest.MonkeyPatch.context() as patch:
        for endpoint_id, endpoint in catalog_registry.CLOSED_WEIGHT_SETTLEMENT_ENDPOINTS.items():
            if endpoint.model_id in VIDEO_VEHICLE_MODEL_IDS:
                patch.setitem(catalog_registry.MODEL_ENDPOINTS, endpoint_id, endpoint)
        for model_id in VIDEO_VEHICLE_MODEL_IDS:
            model = catalog_registry.MODELS[model_id]
            if not model.prepaid_available:
                patch.setitem(catalog_registry.MODELS, model_id, replace(model, prepaid_available=True))
        clear_catalog_caches()
        yield
    clear_catalog_caches()
