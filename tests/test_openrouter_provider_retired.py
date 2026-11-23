"""OpenRouter is gone as a routing provider and cannot return as one.

#2793 kept an internal-only `openrouter` Provider and one frozen Seed 2.1 Turbo
row so requests authorized before the closed-weight rule (#2786) could settle.
That window (2 h reservations, 26 h native batch) has passed, and the shell is
removed. OpenRouter remains a price-reference and comparison source
(openrouter_snapshot.json, competitor pages); that is not routing.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from scripts.check_price_coverage import _run_audit
from scripts.pricing import refresh
from trusted_router import catalog_ingest
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS, endpoint_for_id
from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS
from trusted_router.catalog_ingest import (
    _AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS,
    _PROVIDER_MODELS_DIR,
)
from trusted_router.catalog_registry import (
    CLOSED_WEIGHT_SETTLEMENT_ENDPOINTS,
    HELD_PREPAID_SETTLEMENT_ENDPOINTS,
)
from trusted_router.provider_branding import PROVIDER_BRANDS
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS
from trusted_router.routes.internal.gateway import _endpoint_for_id_compat
from trusted_router.routing import provider_route_preferences

_SLUG = "openrouter"
_SEED = "bytedance-seed/seed-2-1-turbo"
_SEED_ROUTE = f"{_SEED}@openrouter/prepaid"


def test_provider_record_and_plumbing_are_gone() -> None:
    assert _SLUG not in PROVIDERS
    assert _SLUG not in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert _SLUG not in _AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS
    assert not (_PROVIDER_MODELS_DIR / f"{_SLUG}.json").exists()
    assert _SLUG not in refresh.PROVIDER_SLUGS
    assert _SLUG not in refresh.RETIRED_PROVIDER_SLUGS
    assert _SLUG not in OPENAI_COMPATIBLE_PROVIDERS
    assert _SLUG not in PROVIDER_BRANDS


def test_no_route_or_archive_names_openrouter() -> None:
    for registry in (MODEL_ENDPOINTS, CLOSED_WEIGHT_SETTLEMENT_ENDPOINTS, HELD_PREPAID_SETTLEMENT_ENDPOINTS):
        assert not [endpoint.id for endpoint in registry.values() if endpoint.provider == _SLUG]
    assert endpoint_for_id(_SEED_ROUTE) is None
    assert _endpoint_for_id_compat(_SEED_ROUTE) is None
    # Seed 2.1 Turbo existed only through this provider.
    assert _SEED not in MODELS


def test_a_reintroduced_openrouter_manifest_builds_no_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # The same rows under a live provider's slug and under `openrouter`:
    # ingestion builds routes for the control and none for OpenRouter.
    control = "meta"
    committed = json.loads((_PROVIDER_MODELS_DIR / f"{control}.json").read_text(encoding="utf-8"))
    manifests = tmp_path / "provider_models"
    manifests.mkdir()
    (manifests / f"{control}.json").write_text(json.dumps(committed), encoding="utf-8")
    (manifests / f"{_SLUG}.json").write_text(
        json.dumps({**committed, "provider": _SLUG}), encoding="utf-8",
    )
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", manifests)
    _models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    assert [endpoint for endpoint in endpoints.values() if endpoint.provider == control]
    assert not [endpoint for endpoint in endpoints.values() if endpoint.provider == _SLUG]


def test_openrouter_is_still_a_router_name_not_a_provider_filter() -> None:
    with pytest.raises(HTTPException) as ctx:
        provider_route_preferences({"provider": {"only": [_SLUG]}})
    assert ctx.value.status_code == 400
    assert "router name 'openrouter'" in ctx.value.detail["error"]["message"]


def test_price_audit_has_no_openrouter_entry_or_exemption() -> None:
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=365)
    warnings, info, hard_failures = _run_audit(30, later, check_model_discovery=False)
    assert not [line for line in warnings + hard_failures + info if line.startswith(f"{_SLUG}:")]
    assert not [line for line in info if "settlement-only archive" in line]
