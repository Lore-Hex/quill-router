"""Holds placed on November 28, 2026 from production route-health alerts.

Databricks refused the gateway's credential on every route ("API tokens are
disabled for user"), so its prepaid routes were held until the token was
replaced the same day. Phala answers HTTP 502 for two models after each timed
unserved-route hold lifts, so those two routes are held until a reviewed
recovery. See provider_contracts for the evidence and how to lift each.
"""

from __future__ import annotations

import pytest

from trusted_router import catalog_ingest
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.provider_contracts import (
    OPERATOR_HELD_PROVIDER_MODELS,
    PREPAID_PROVIDER_HOLD_REASONS,
    provider_route_reviewed_hold,
)

PHALA_HELD = ("z-ai/glm-ocr", "z-ai/glm-5.3-uncensored")


def _endpoints(provider: str, models: tuple[str, ...]) -> dict[str, ModelEndpoint]:
    return {
        f"{model}@{provider}/{usage}": ModelEndpoint(
            id=f"{model}@{provider}/{usage}", model_id=model, provider=provider,
            usage_type=usage, upstream_id=model,
        )
        for model in models for usage in ("Credits", "BYOK")
    }


@pytest.fixture
def no_other_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the holds under test decide: no manifest state, allowlist or
    authoritative manifest is read."""
    monkeypatch.setattr(catalog_ingest, "_provider_manifest_dark_model_ids", lambda: {})
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_SERVED_MODEL_ALLOWLIST", {})
    monkeypatch.setattr(catalog_ingest, "_AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS", frozenset())


def test_databricks_prepaid_routes_serve_again_after_the_new_token() -> None:
    # The provider-wide hold was lifted on November 28 once the gateway's
    # Databricks token was replaced. Its prepaid routes are built and probed
    # again; route health holds any route that still fails.
    from trusted_router.catalog import MODEL_ENDPOINTS
    from trusted_router.synthetic.probes import rotation_candidates

    assert "databricks" not in PREPAID_PROVIDER_HOLD_REASONS
    assert [
        row for row in MODEL_ENDPOINTS.values()
        if row.provider == "databricks" and row.usage_type == "Credits"
    ]
    assert "databricks" in rotation_candidates()


@pytest.mark.parametrize("model_id", PHALA_HELD)
def test_phala_route_is_held_whatever_discovery_says(no_other_filters: None, model_id: str) -> None:
    assert ("phala", model_id) in OPERATOR_HELD_PROVIDER_MODELS
    assert provider_route_reviewed_hold("phala", model_id, None)
    # Discovery lists it as routable and the timed hold has lifted: the
    # route still does not serve, for either usage type.
    endpoints = {**_endpoints("phala", (model_id, "vendor/still-served"))}

    filtered = catalog_ingest._filter_unserved_provider_endpoints(endpoints)

    assert {row.model_id for row in filtered.values()} == {"vendor/still-served"}


def test_phala_holds_are_only_these_routes() -> None:
    # Phala's other routes are untouched, and no other host's route of these
    # models is held by this.
    for model_id in PHALA_HELD:
        assert not provider_route_reviewed_hold("other-host", model_id, None)
    assert not provider_route_reviewed_hold("phala", "vendor/still-served", None)
