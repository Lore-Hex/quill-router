"""A model's context window: the first window any of its routes states, and
null when none states one.

Built from fixture manifests and fixture routes, not today's catalog: the
hourly price refresh rewrites provider listings, and a test that pinned a live
model's window held the refresh back the day its provider changed it
(Neurometric halved google/gemma-4-12b-it's window to 16384 on 2026-11-17).
"""

import json

import pytest

from tests.fixture_routes import serve_on_fixture_route
from trusted_router import catalog_ingest
from trusted_router.routes.catalog import _public_catalog_payload

MODEL = "fixture/context-window"


def _manifest(directory, provider: str, **window: int) -> None:
    row = {
        "id": MODEL,
        "upstream_id": "fixture-context-window",
        "model_type": "chat",
        "endpoints": ["chat/completions"],
        "input_token_price_per_m": 300_000,
        "output_token_price_per_m": 2_000_000,
        **window,
    }
    (directory / f"{provider}.json").write_text(
        json.dumps({"provider": provider, "price_scale": "microdollars_per_million", "models": [row]})
    )


@pytest.mark.parametrize("stating", ["novita", "nebius"])
def test_a_window_stated_on_any_route_reaches_the_model(monkeypatch, tmp_path, stating) -> None:
    # A model created from a provider row with no window ignored a later
    # row's window and published 0 (google/gemma-4-12b-it, 2026-11-01).
    # Ingest reads novita's manifest before nebius's, so this covers the
    # window on the row that creates the model and on a later one.
    for provider in ("novita", "nebius"):
        _manifest(tmp_path, provider, **({"context_length": 32768} if provider == stating else {}))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)

    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()

    # Both rows made routes, so the model was built from both.
    assert {endpoint.provider for endpoint in endpoints.values() if endpoint.model_id == MODEL} == {
        "novita", "nebius",
    }
    assert models[MODEL].context_length == 32768


def test_published_catalog_never_states_a_zero_window(monkeypatch) -> None:
    # An unknown window is published as null rather than dropped, since 0
    # reads as a model that accepts no input.
    serve_on_fixture_route(monkeypatch, MODEL, "deepinfra", author="deepinfra", context_length=0)
    rows = _public_catalog_payload((0, "context-window-regression")).shapes
    for row in rows:
        assert row["context_length"] != 0, row["id"]
        assert row["top_provider"]["context_length"] != 0, row["id"]
    assert {row["id"]: row["context_length"] for row in rows}[MODEL] is None
