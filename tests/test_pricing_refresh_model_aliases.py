"""OpenRouter spellings must not rename already published models."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from scripts import ingest_openrouter_catalog
from scripts.pricing import base, refresh
from scripts.pricing.providers import mistral
from trusted_router import catalog_ingest

PUBLIC_ID = "mistralai/mistral-large-4"
OPENROUTER_ID = "mistralai/mistral-large-4-0"
NATIVE_ID = "mistral-large-4"
PRICING_HTML = """
<table><thead><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached input</th></tr></thead>
<tbody>
<tr><td>Mistral Large 4</td><td>$0.68</td><td>$2.09</td><td>$0.07</td></tr>
<tr><td>Mistral Small 4</td><td>$0.15</td><td>$0.60</td><td>$0.015</td></tr>
</tbody></table>
"""


@pytest.fixture
def openrouter_snapshot() -> dict[str, Any]:
    return {"models": [{
        "id": OPENROUTER_ID,
        "name": "Mistral: Mistral Large 4",
        "created": 200,
        "architecture": {"output_modalities": ["text"]},
        "endpoints": [{
            "tr_provider_slug": "mistral",
            "provider_name": "Mistral",
            "model_id": NATIVE_ID,
            "pricing": {"prompt": "0.00000068", "completion": "0.00000209"},
        }],
    }]}


@pytest.mark.parametrize("published_in", ["manifest", "snapshot", "neither"])
def test_mistral_openrouter_alias_does_not_trigger_self_heal(
    openrouter_snapshot: dict[str, Any],
    published_in: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    committed = {"models": [{"id": "known/latest", "created": 100}]}
    if published_in == "manifest":
        (tmp_path / "mistral.json").write_text(
            json.dumps({"models": [{"id": PUBLIC_ID}]}), encoding="utf-8",
        )
    elif published_in == "snapshot":
        committed["models"].append({"id": PUBLIC_ID, "created": 100})
    monkeypatch.setattr(refresh, "PROVIDER_MANIFEST_DIR", tmp_path)
    required = refresh._new_parser_requirements(openrouter_snapshot, committed)
    # configure_runtime_required_models rebinds this global; restore it at teardown.
    monkeypatch.setattr(base, "_RUNTIME_REQUIRED_MODELS", {})
    base.configure_runtime_required_models(required)
    monkeypatch.setattr(base, "fetch_html", lambda *_args, **_kwargs: PRICING_HTML)

    def unexpected_self_heal(**kwargs: Any) -> str:
        pytest.fail(f"Mistral's existing parser must pass validation: {kwargs['errors']}")

    monkeypatch.setattr(base, "self_heal_parser", unexpected_self_heal)
    result = base.fetch_provider(
        slug=mistral.SLUG, url=mistral.URL, expected_models=mistral.EXPECTED_MODELS,
    )
    assert result.source == "deterministic"
    assert PUBLIC_ID in result.prices
    assert OPENROUTER_ID not in result.prices
    assert required == ({"mistral": {PUBLIC_ID}} if published_in == "neither" else {})


@pytest.mark.parametrize("include_public_row", [False, True])
def test_catalog_ingests_one_mistral_large_four_under_published_id(
    openrouter_snapshot: dict[str, Any],
    include_public_row: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if include_public_row:
        openrouter_snapshot["models"].append({
            **openrouter_snapshot["models"][0], "id": PUBLIC_ID,
        })
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(openrouter_snapshot), encoding="utf-8")
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", snapshot_path)

    models, endpoints = catalog_ingest._ingested_models_and_endpoints()
    assert set(models) == {PUBLIC_ID}
    assert models[PUBLIC_ID].id == PUBLIC_ID
    assert set(endpoints) == {f"{PUBLIC_ID}@mistral/prepaid", f"{PUBLIC_ID}@mistral/byok"}
    assert all(endpoint.model_id == PUBLIC_ID for endpoint in endpoints.values())
    assert all(endpoint.upstream_id == NATIVE_ID for endpoint in endpoints.values())


def test_openrouter_snapshot_alias_retains_provider_prices_at_merge(
    openrouter_snapshot: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_model = openrouter_snapshot["models"][0]
    raw_model["endpoints"][0]["model_id"] = OPENROUTER_ID
    monkeypatch.setattr(ingest_openrouter_catalog, "fetch_models", lambda _client: [raw_model])

    def fetch_endpoints(_client: Any, model_id: str) -> list[dict[str, Any]]:
        # The OpenRouter HTTP endpoint still needs OpenRouter's spelling.
        assert model_id == OPENROUTER_ID
        return raw_model["endpoints"]

    monkeypatch.setattr(ingest_openrouter_catalog, "fetch_endpoints", fetch_endpoints)
    snapshot = ingest_openrouter_catalog.build_snapshot()
    assert snapshot["models"][0]["id"] == PUBLIC_ID
    assert raw_model["id"] == OPENROUTER_ID
    monkeypatch.setattr(mistral, "UPSTREAM_ID_MAP", {PUBLIC_ID: NATIVE_ID})
    merged = refresh._merge_snapshot(
        snapshot, {PUBLIC_ID: {"mistral": base.ModelPrice(680_000, 2_090_000)}}, set(),
    )
    assert [model["id"] for model in merged["models"]] == [PUBLIC_ID]
    endpoint = merged["models"][0]["endpoints"][0]
    assert endpoint["model_id"] == NATIVE_ID
    assert endpoint["pricing_source"] == "provider_direct"


def _build_snapshot_from_rows(
    rows: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    monkeypatch.setattr(ingest_openrouter_catalog, "fetch_models", lambda _client: rows)
    endpoints = {row["id"]: row["endpoints"] for row in rows}
    monkeypatch.setattr(
        ingest_openrouter_catalog, "fetch_endpoints", lambda _client, model_id: endpoints[model_id],
    )
    # Exercise both completion orders without a thread-scheduling race.
    monkeypatch.setattr(ingest_openrouter_catalog, "as_completed", lambda futures: iter(futures))
    return ingest_openrouter_catalog.build_snapshot()


def test_alias_snapshot_preserves_missing_native_id_through_ingest_and_refresh(
    openrouter_snapshot: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    raw = openrouter_snapshot["models"][0]
    del raw["endpoints"][0]["model_id"]
    original = deepcopy(raw)
    snapshot = _build_snapshot_from_rows([raw], monkeypatch)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", snapshot_path)

    models, endpoints = catalog_ingest._ingested_models_and_endpoints()
    assert set(models) == {PUBLIC_ID}
    assert endpoints
    assert {endpoint.upstream_id for endpoint in endpoints.values()} == {OPENROUTER_ID}
    assert snapshot["models"][0]["endpoints"][0]["model_id"] == OPENROUTER_ID
    monkeypatch.setattr(refresh, "_upstream_id_map_for", lambda _slug: {})
    merged = refresh._merge_snapshot(
        snapshot, {PUBLIC_ID: {"mistral": base.ModelPrice(680_000, 2_090_000)}}, set(),
    )
    assert merged["models"][0]["endpoints"][0]["model_id"] == OPENROUTER_ID
    assert raw == original


@pytest.mark.parametrize("boundary", ["snapshot", "refresh", "ingest"])
def test_alias_collisions_preserve_provider_metadata_in_either_order(
    boundary: str, openrouter_snapshot: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    alias = deepcopy(openrouter_snapshot["models"][0])
    alias["endpoints"] = [{
        **alias["endpoints"][0], "tr_provider_slug": "novita", "provider_name": "Novita",
        "model_id": OPENROUTER_ID, "supported_parameters": ["tools"],
    }]
    canonical = deepcopy(openrouter_snapshot["models"][0])
    canonical.update(id=PUBLIC_ID, name="Published Large 4")
    canonical["endpoints"][0]["supported_parameters"] = ["temperature"]
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", manifest_dir)
    snapshot_path = tmp_path / "snapshot.json"
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", snapshot_path)
    monkeypatch.setattr(refresh, "_upstream_id_map_for", lambda _slug: {})
    prices = {PUBLIC_ID: {
        slug: base.ModelPrice(680_000, 2_090_000) for slug in ("mistral", "novita")
    }}

    for duplicate_provider in (False, True):
        canonical_row = deepcopy(canonical)
        novita_id = OPENROUTER_ID
        novita_parameter = "tools"
        if duplicate_provider:
            novita_id = "novita-canonical-large-4"
            novita_parameter = "response_format"
            canonical_row["endpoints"].append({
                **alias["endpoints"][0], "model_id": novita_id,
                "supported_parameters": [novita_parameter],
            })
        outputs = []
        for rows in ([alias, canonical_row], [canonical_row, alias]):
            original = deepcopy(rows)
            if boundary == "ingest":
                snapshot_path.write_text(json.dumps({"models": rows}), encoding="utf-8")
                models, endpoints = catalog_ingest._ingested_models_and_endpoints()
                assert set(models) == {PUBLIC_ID}
                model = models[PUBLIC_ID]
                assert model.name == "Published Large 4"
                assert {"temperature", novita_parameter} <= set(model.supported_parameters)
                assert {endpoint.provider for endpoint in endpoints.values()} == {"mistral", "novita"}
                for endpoint in endpoints.values():
                    assert endpoint.model_id == PUBLIC_ID
                    if endpoint.provider == "novita":
                        assert endpoint.upstream_id == novita_id
                        assert novita_parameter in endpoint.supported_parameters
                        assert ("tools" in endpoint.supported_parameters) is not duplicate_provider
                    else:
                        assert endpoint.upstream_id == NATIVE_ID
                        assert "temperature" in endpoint.supported_parameters
                output = {
                    "models": {key: asdict(value) for key, value in models.items()},
                    "endpoints": {key: asdict(value) for key, value in endpoints.items()},
                }
            else:
                output = (
                    _build_snapshot_from_rows(rows, monkeypatch) if boundary == "snapshot"
                    else refresh._merge_snapshot({"models": rows}, prices, set())
                )
                snapshots = [output]
                if boundary == "snapshot":
                    snapshots.append(refresh._merge_snapshot(output, prices, set()))
                for snapshot in snapshots:
                    assert snapshot["model_count"] == 1
                    assert [model["id"] for model in snapshot["models"]] == [PUBLIC_ID]
                    model_row = snapshot["models"][0]
                    assert model_row["name"] == "Published Large 4"
                    by_provider = {ep["tr_provider_slug"]: ep for ep in model_row["endpoints"]}
                    assert len(model_row["endpoints"]) == len(by_provider) == 2
                    assert by_provider["novita"]["model_id"] == novita_id
                    assert by_provider["novita"]["supported_parameters"] == [novita_parameter]
                    assert by_provider["mistral"]["model_id"] == NATIVE_ID
                    assert by_provider["mistral"]["supported_parameters"] == ["temperature"]
            assert rows == original
            outputs.append(json.dumps(output, sort_keys=True))
        assert outputs[0] == outputs[1]
