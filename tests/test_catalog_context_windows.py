import json
from pathlib import Path

import pytest

from trusted_router import catalog_ingest
from trusted_router.routes.catalog import _public_catalog_payload


@pytest.mark.parametrize("window", [16_384, 32_768])
@pytest.mark.parametrize("stated_by", ["novita", "deepinfra"])
def test_a_window_stated_on_any_route_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, window: int, stated_by: str,
) -> None:
    model_id = "fixture/context-window"
    for host in ("novita", "deepinfra"):
        row = {
            "id": model_id, "endpoints": ["chat/completions"],
            "input_token_price_per_m": 1, "output_token_price_per_m": 2,
            **({"context_length": window} if host == stated_by else {}),
        }
        (tmp_path / f"{host}.json").write_text(json.dumps({"models": [row]}), encoding="utf-8")
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)

    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    for host in ("novita", "deepinfra"):
        assert endpoints[f"{model_id}@{host}/prepaid"].context_length == (window if host == stated_by else 0)
    assert models[model_id].context_length == window


def test_published_catalog_never_states_a_zero_window() -> None:
    rows = _public_catalog_payload((0, "context-window-regression")).shapes
    unknown = []
    for row in rows:
        assert row["context_length"] != 0, row["id"]
        assert row["top_provider"]["context_length"] != 0, row["id"]
        if row["context_length"] is None:
            unknown.append(row["id"])
    # An unknown window is published as null rather than dropped.
    assert "cohere/command-a" in unknown
