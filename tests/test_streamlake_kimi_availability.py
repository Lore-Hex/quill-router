import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import streamlake
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider
from trusted_router import catalog
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.catalog_ingest import _filter_unserved_provider_endpoints
from trusted_router.provider_contracts import provider_model_operator_held
from trusted_router.synthetic.probes import rotation_candidates

MODEL = "moonshotai/kimi-k2-instruct-0905"
NATIVE = "Kimi-K2-Instruct-0905"
REASON = "provider-model-not-found"


def test_streamlake_missing_kimi_is_held_without_disabling_other_routes() -> None:
    assert provider_model_operator_held("streamlake", MODEL, NATIVE)
    assert not provider_model_operator_held("novita", MODEL, NATIVE)
    assert not provider_model_operator_held("streamlake", "moonshotai/kimi-k2.6", "Kimi-K2.6")
    assert not provider_model_operator_held("streamlake", "moonshotai/kimi-k2.7-code")


def test_stale_positive_catalog_cannot_restore_missing_kimi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoints = [
        ModelEndpoint(
            id=f"{MODEL}@{provider}/prepaid", model_id=MODEL, provider=provider,
            upstream_id=NATIVE, usage_type="Credits",
        )
        for provider in ("streamlake", "novita")
    ]
    monkeypatch.setattr(
        "trusted_router.catalog_ingest._provider_manifest_dark_model_ids", lambda: {},
    )
    filtered = _filter_unserved_provider_endpoints(
        {e.id: e for e in endpoints}, explicit_model_ids=frozenset({MODEL}),
    )
    assert [e.provider for e in filtered.values()] == ["novita"]


def test_missing_kimi_is_not_a_public_route_or_synthetic_candidate() -> None:
    rows = json.loads(streamlake.MANIFEST_PATH.read_text())["models"]
    row = next(row for row in rows if row["id"] == MODEL)
    assert row["routable"] is False
    assert row["routable_reason"] == REASON
    assert not any(e.provider == "streamlake" for e in catalog.endpoints_for_model(MODEL))
    assert MODEL not in rotation_candidates().get("streamlake", [])


def test_refresh_preserves_hold_even_with_stale_catalog_and_passing_canary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    checked: list[str] = []

    def probe(**kwargs: object) -> bool:
        checked.append(str(kwargs["model"]))
        return True

    monkeypatch.setenv("STREAMLAKE_API_KEY", "test-key")
    monkeypatch.setattr("scripts.pricing.providers._direct_openai.probe_openai_chat", probe)
    spec = replace(
        streamlake.CATALOG.spec,
        catalog_loader=lambda _key: [{"id": NATIVE}, {"id": "Kimi-K2.6"}],
        price_loader=lambda: {
            MODEL: ModelPrice(571_000, 2_286_000),
            "moonshotai/kimi-k2.6": ModelPrice(950_000, 4_000_000),
        },
        expected_models=(MODEL, "moonshotai/kimi-k2.6"),
    )
    manifest_path = tmp_path / "streamlake.json"
    adapter = DirectOpenAIProvider(spec, manifest_path=manifest_path)
    result = adapter.fetch()
    assert checked == ["Kimi-K2.6"]
    assert adapter.discovered_rows[MODEL]["routable"] is False
    assert adapter.discovered_rows[MODEL]["routable_reason"] == REASON
    # The final publication boundary must also reject a stale healthy flag.
    adapter.discovered_rows[MODEL]["routable"] = True
    adapter.discovered_rows[MODEL].pop("routable_reason")
    adapter.write_provider_manifest(result)
    rows = {row["id"]: row for row in json.loads(manifest_path.read_text())["models"]}
    assert rows[MODEL]["routable"] is False
    assert rows[MODEL]["routable_reason"] == REASON
    assert rows["moonshotai/kimi-k2.6"]["routable"] is True
