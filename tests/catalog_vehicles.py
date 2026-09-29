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

Some tests ride a whole MODEL for rules that are not about it: a template for
fixture models, a comparison peer, the model a settlement test authorizes. A
model disappears when its last host delists it. A model in VEHICLE_MODEL_IDS is
put back, with every route it had when frozen, only when the catalog has lost it
entirely; while any host still serves it, nothing is touched, so no provider's
delisting is masked.

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
from trusted_router.catalog_data import Model, ModelDocumentation, ModelEndpoint, PriceTier

FROZEN = Path(__file__).parent / "fixtures" / "vehicle_routes.json"

VEHICLE_ENDPOINT_IDS = (
    "anthropic/claude-haiku-4.5@anthropic/prepaid",
    "anthropic/claude-haiku-4.5@anthropic/byok",
    "anthropic/claude-opus-4.7@anthropic/prepaid",
    "anthropic/claude-opus-4.7@anthropic/byok",
    "anthropic/claude-sonnet-4.6@anthropic/prepaid",
    "anthropic/claude-sonnet-4.6@anthropic/byok",
)

# Chosen from `scripts/tombstone_sweep.py models` (2026-09-29): the models whose
# vanishing broke the most release tests that use them for rules not about them
# (templates for fixture models, comparison peers, the model a settlement,
# routing or streaming test drives).
VEHICLE_MODEL_IDS: tuple[str, ...] = (
    "deepseek/deepseek-v4-flash",
    "google/gemini-2.5-flash",
    "google/gemma-4-31b-it",
    "meta-llama/llama-3.1-8b-instruct",
    "minimax/minimax-m3",
    "mistralai/mistral-small-2603",
    "moonshotai/kimi-k2.6",
    "openai/gpt-5.4-nano",
    "openai/gpt-5.5",
    "z-ai/glm-5.3",
    "z-ai/glm-5.3-flash",
)


def _tiers(raw: list[dict[str, Any]]) -> tuple[PriceTier, ...]:
    return tuple(PriceTier(**tier) for tier in raw)


def _model(raw: dict[str, Any]) -> Model:
    fields = dict(raw)
    for name in ("supported_parameters", "input_modalities", "output_modalities"):
        fields[name] = tuple(fields[name])
    fields["price_tiers"] = _tiers(fields["price_tiers"])
    fields["published_price_tiers"] = _tiers(fields["published_price_tiers"])
    documentation = fields.get("documentation")
    fields["documentation"] = ModelDocumentation(**documentation) if documentation else None
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


def install_vanished(
    models: dict[str, Model], endpoints: dict[str, ModelEndpoint], frozen: dict[str, Any]
) -> list[str]:
    """Put back each frozen vehicle model the catalog has lost entirely, with the
    routes it had when frozen; returns the ids added."""
    added = []
    for vehicle in frozen.get("vehicle_models", []):
        model = vehicle["model"]
        if model["id"] in models:
            continue
        models[model["id"]] = _model(model)
        added.append(model["id"])
        for raw in vehicle["endpoints"]:
            if raw["id"] not in endpoints:
                endpoints[raw["id"]] = _endpoint(raw)
                added.append(raw["id"])
    return added


def registry_endpoints() -> dict[str, ModelEndpoint]:
    """MODEL_ENDPOINTS without the vehicles this session put back: the routes
    the catalog built from the data. A test of the registry's own output
    against the manifests or the snapshot (a provider routes exactly its
    routable rows; a tombstoned row has no route) reads these."""
    return {
        endpoint_id: endpoint
        for endpoint_id, endpoint in catalog_registry.MODEL_ENDPOINTS.items()
        if endpoint_id not in VEHICLES_ADDED
    }


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
        "vehicle_models": [
            {
                "model": dataclasses.asdict(catalog_registry.MODELS[model_id]),
                "endpoints": [
                    {**dataclasses.asdict(endpoint), "catalog_valid_until": None}
                    for endpoint in catalog_registry.MODEL_ENDPOINTS.values()
                    if endpoint.model_id == model_id
                ],
            }
            for model_id in VEHICLE_MODEL_IDS
        ],
    }
    FROZEN.write_text(json.dumps(frozen, indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__" and sys.argv[1:] == ["--freeze"]:
    _freeze()
else:
    # What this session added, which the registry did not build: a test of the
    # registry's own output against the manifests leaves these out
    # (registry_endpoints).
    _FROZEN_VEHICLES = json.loads(FROZEN.read_text(encoding="utf-8"))
    VEHICLES_ADDED = frozenset(
        install_missing(catalog_registry.MODELS, catalog_registry.MODEL_ENDPOINTS, _FROZEN_VEHICLES)
        + install_vanished(catalog_registry.MODELS, catalog_registry.MODEL_ENDPOINTS, _FROZEN_VEHICLES)
    )
