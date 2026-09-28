"""Routes the release suite borrows as vehicles, put back if the catalog lost them.

Money-path and routing tests exercise their rules through a few real catalog
routes, above all claude-haiku-4.5 on Anthropic, in some forty files. The
catalog is rebuilt hourly from provider feeds without a human in the loop, and
the price refresh publishes only if this suite passes. So Anthropic retiring
Haiku 4.5 would fail hundreds of tests that are not about Haiku, and freeze
every provider's prices. When a vehicle route is missing, importing this
module installs the copy frozen in tests/fixtures/vehicle_routes.json under the
same ids, before the app is built. A route the live catalog still serves is
left exactly as it is. Whether it still serves one is a provider_health
question, not a release one.

Refresh the frozen copy from today's catalog:

    PYTHONPATH="$PWD/src" python -m tests.catalog_vehicles --freeze
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from trusted_router import catalog_registry
from trusted_router.catalog_data import Model, ModelEndpoint, PriceTier

FROZEN = Path(__file__).parent / "fixtures" / "vehicle_routes.json"

VEHICLE_ENDPOINT_IDS = (
    "anthropic/claude-haiku-4.5@anthropic/prepaid",
    "anthropic/claude-haiku-4.5@anthropic/byok",
    "anthropic/claude-opus-4.7@anthropic/prepaid",
    "anthropic/claude-opus-4.7@anthropic/byok",
    "anthropic/claude-sonnet-4.6@anthropic/prepaid",
    "anthropic/claude-sonnet-4.6@anthropic/byok",
)


def _tiers(raw: list[dict[str, Any]]) -> tuple[PriceTier, ...]:
    return tuple(PriceTier(**tier) for tier in raw)


def _model(raw: dict[str, Any]) -> Model:
    fields = dict(raw)
    for name in ("supported_parameters", "input_modalities", "output_modalities"):
        fields[name] = tuple(fields[name])
    fields["price_tiers"] = _tiers(fields["price_tiers"])
    fields["published_price_tiers"] = _tiers(fields["published_price_tiers"])
    fields["documentation"] = None
    return Model(**fields)


def _endpoint(raw: dict[str, Any]) -> ModelEndpoint:
    fields = dict(raw)
    fields["supported_parameters"] = tuple(fields["supported_parameters"])
    fields["price_tiers"] = _tiers(fields["price_tiers"])
    fields["published_price_tiers"] = _tiers(fields["published_price_tiers"])
    fields["catalog_valid_until"] = None
    return ModelEndpoint(**fields)


def install_missing(
    models: dict[str, Model], endpoints: dict[str, ModelEndpoint], frozen: dict[str, Any]
) -> list[str]:
    """Add each frozen vehicle the catalog lacks; returns the ids added."""
    added = []
    for raw in frozen["models"]:
        if raw["id"] not in models:
            models[raw["id"]] = _model(raw)
            added.append(raw["id"])
    for raw in frozen["endpoints"]:
        if raw["id"] not in endpoints:
            endpoints[raw["id"]] = _endpoint(raw)
            added.append(raw["id"])
    return added


def _freeze() -> None:
    endpoints = [catalog_registry.MODEL_ENDPOINTS[endpoint_id] for endpoint_id in VEHICLE_ENDPOINT_IDS]
    model_ids = sorted({endpoint.model_id for endpoint in endpoints})
    frozen = {
        "models": [
            {**dataclasses.asdict(catalog_registry.MODELS[model_id]), "documentation": None}
            for model_id in model_ids
        ],
        "endpoints": [
            {**dataclasses.asdict(endpoint), "catalog_valid_until": None} for endpoint in endpoints
        ],
    }
    FROZEN.write_text(json.dumps(frozen, indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__" and sys.argv[1:] == ["--freeze"]:
    _freeze()
else:
    # What this session added, which the registry did not build: a test of the
    # registry's own output against the manifests leaves these out.
    VEHICLES_ADDED = frozenset(
        install_missing(
            catalog_registry.MODELS,
            catalog_registry.MODEL_ENDPOINTS,
            json.loads(FROZEN.read_text(encoding="utf-8")),
        )
    )
