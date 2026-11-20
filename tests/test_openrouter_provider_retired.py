"""OpenRouter is retired as a routing provider and Union Alpha is gone.

The provider stays only as a settlement-only archive: requests authorized for
ByteDance Seed 2.1 Turbo on OpenRouter before the closed-weight rule (#2786)
must still settle or refund at the tariff they were quoted.
"""

from __future__ import annotations

import datetime as dt
import json

from scripts.check_price_coverage import _run_audit
from scripts.pricing.refresh import PROVIDER_SLUGS
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS, endpoint_for_id
from trusted_router.catalog_ingest import _PROVIDER_MODELS_DIR
from trusted_router.catalog_registry import CLOSED_WEIGHT_SETTLEMENT_ENDPOINTS
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS
from trusted_router.routes.internal.gateway import _endpoint_for_id_compat

_UNION = "stealth/union-alpha"
_SEED_ROUTE = "bytedance-seed/seed-2-1-turbo@openrouter/prepaid"


def test_union_alpha_is_removed_everywhere() -> None:
    assert _UNION not in MODELS
    assert not [endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.model_id == _UNION]
    rows = json.loads((_PROVIDER_MODELS_DIR / "openrouter.json").read_text())["models"]
    assert _UNION not in {row["id"] for row in rows}


def test_no_openrouter_route_is_routable() -> None:
    assert not [endpoint.id for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "openrouter"]
    assert endpoint_for_id(_SEED_ROUTE) is None
    assert "openrouter" not in PROVIDER_SLUGS
    assert "openrouter" not in OPENAI_COMPATIBLE_PROVIDERS
    # No public provider page, /v1/providers row or social card.
    assert PROVIDERS["openrouter"].internal_only


def test_archived_seed_route_still_settles_at_its_quoted_tariff() -> None:
    archived = CLOSED_WEIGHT_SETTLEMENT_ENDPOINTS[_SEED_ROUTE]
    assert _endpoint_for_id_compat(_SEED_ROUTE) is archived
    assert archived.provider == "openrouter"
    assert archived.usage_type == "Credits"
    assert archived.prompt_price_microdollars_per_million_tokens > 0
    assert archived.completion_price_microdollars_per_million_tokens > 0
    # Settlement reads the model and provider records of the archived route.
    assert archived.model_id in MODELS
    assert not MODELS[archived.model_id].prepaid_available
    assert archived.provider in PROVIDERS


def test_frozen_settlement_manifest_never_fails_price_publication() -> None:
    # The manifest has no refresher, so it ages past every staleness window.
    # A retired provider with no routable route must not block publication.
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=365)
    warnings, info, hard_failures = _run_audit(30, later, check_model_discovery=False)
    assert not [warning for warning in warnings + hard_failures if warning.startswith("openrouter:")]
    assert "openrouter: retired; settlement-only archive with no routable routes" in info
