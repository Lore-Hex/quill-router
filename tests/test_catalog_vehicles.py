"""The vehicle routes are in every test session's catalog, and only missing
ones are put back (tests/catalog_vehicles.py)."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import replace

from tests import catalog_vehicles
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, endpoints_for_model


def _frozen() -> dict:
    return json.loads(catalog_vehicles.FROZEN.read_text(encoding="utf-8"))


def test_every_vehicle_route_is_served_in_the_test_catalog() -> None:
    for endpoint_id in catalog_vehicles.VEHICLE_ENDPOINT_IDS:
        model_id = MODEL_ENDPOINTS[endpoint_id].model_id
        assert model_id in MODELS
        assert endpoint_id in {endpoint.id for endpoint in endpoints_for_model(model_id)}


def test_a_delisted_vehicle_is_put_back_as_frozen() -> None:
    frozen = _frozen()
    models: dict = {}
    endpoints: dict = {}

    added = catalog_vehicles.install_missing(models, endpoints, frozen)

    assert sorted(added) == sorted(
        [raw["id"] for raw in frozen["models"]] + [raw["id"] for raw in frozen["endpoints"]]
    )
    for raw in frozen["endpoints"]:
        endpoint = endpoints[raw["id"]]
        assert endpoint.prompt_price_microdollars_per_million_tokens == raw[
            "prompt_price_microdollars_per_million_tokens"
        ]
        assert endpoint.catalog_is_current()
        assert endpoint.model_id in models


def test_a_route_the_catalog_still_serves_is_left_alone() -> None:
    frozen = _frozen()
    endpoint_id = catalog_vehicles.VEHICLE_ENDPOINT_IDS[0]
    live = replace(MODEL_ENDPOINTS[endpoint_id], prompt_price_microdollars_per_million_tokens=7)
    model_id = live.model_id
    models = {model_id: MODELS[model_id]}
    endpoints = {endpoint_id: live}

    added = catalog_vehicles.install_missing(models, endpoints, frozen)

    assert endpoint_id not in added and model_id not in added
    assert endpoints[endpoint_id].prompt_price_microdollars_per_million_tokens == 7


def _frozen_vehicle_model() -> dict:
    """A vehicle model frozen as catalog_vehicles._freeze writes one."""
    model_id = catalog_vehicles.VEHICLE_ENDPOINT_IDS[0].split("@", 1)[0]
    return {"vehicle_models": [{
        "model": dataclasses.asdict(MODELS[model_id]),
        "endpoints": [
            {**dataclasses.asdict(endpoint), "catalog_valid_until": None}
            for endpoint in MODEL_ENDPOINTS.values()
            if endpoint.model_id == model_id
        ],
    }]}


def test_a_vanished_vehicle_model_is_put_back_with_every_frozen_route() -> None:
    frozen = _frozen_vehicle_model()
    vehicle = frozen["vehicle_models"][0]
    model_id = vehicle["model"]["id"]
    models: dict = {}
    endpoints: dict = {}

    added = catalog_vehicles.install_vanished(models, endpoints, frozen)

    assert added == [model_id, *(raw["id"] for raw in vehicle["endpoints"])]
    assert len(vehicle["endpoints"]) >= 2
    # The model round-trips exactly, documentation included.
    assert models[model_id] == MODELS[model_id]
    for raw in vehicle["endpoints"]:
        assert endpoints[raw["id"]].catalog_is_current()


def test_a_vehicle_model_any_host_still_serves_is_left_alone() -> None:
    frozen = _frozen_vehicle_model()
    model_id = frozen["vehicle_models"][0]["model"]["id"]
    # Every frozen route is gone but the model is still cataloged: a host still
    # serves it, and no provider's delisting is masked.
    models = {model_id: MODELS[model_id]}
    endpoints: dict = {}

    assert catalog_vehicles.install_vanished(models, endpoints, frozen) == []
    assert endpoints == {}
