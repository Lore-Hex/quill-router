"""OpenRouter spellings must not rename already published models."""

from __future__ import annotations

import json
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
