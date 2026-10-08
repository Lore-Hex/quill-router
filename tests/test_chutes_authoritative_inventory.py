"""Chutes' TEE manifest governs both customer-key and Credits routes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.providers import chutes
from tests import catalog_vehicles
from trusted_router import catalog_ingest
from trusted_router.catalog_data import ModelEndpoint


@pytest.mark.parametrize("usage_type", ["Credits", "BYOK"])
def test_chutes_catalog_matches_its_authoritative_tee_manifest(usage_type: str) -> None:
    assert "chutes" in catalog_ingest._AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS
    expected = catalog_ingest._authoritative_provider_model_ids("chutes")
    actual = {
        endpoint.model_id for endpoint in catalog_vehicles.registry_endpoints().values()
        if endpoint.provider == "chutes" and endpoint.usage_type == usage_type
    }
    assert actual == expected
    assert not actual.intersection(chutes._OPERATOR_HOLD_REASONS)


@pytest.mark.parametrize("usage_type", ["Credits", "BYOK"])
@pytest.mark.parametrize("inventory", [
    "active", "held", "delisted", "missing", "malformed", "wrong-shape", "unlisted",
])
def test_stale_snapshot_cannot_bypass_chutes_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, usage_type: str, inventory: str,
) -> None:
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    model_id = "fixture/future-tee-model"
    native_id = "Fixture/Future-TEE-Model-TEE"
    row: dict[str, object] = {
        "id": model_id,
        "upstream_id": native_id,
        "model_type": "chat",
        "endpoints": ["chat/completions"],
        "confidential_compute": True,
        "input_token_price_per_m": 100_000,
        "output_token_price_per_m": 200_000,
    }
    if inventory in {"held", "delisted"}:
        row.update(
            routable=False,
            routable_reason=(
                "attestation-evidence-unavailable" if inventory == "held" else "delisted-upstream"
            ),
        )
    elif inventory == "unlisted":
        row["id"] = "fixture/another-tee-model"
    path = tmp_path / "chutes.json"
    if inventory == "malformed":
        path.write_text("not-json")
    elif inventory == "wrong-shape":
        path.write_text(json.dumps({"provider": "chutes", "models": {}}))
    elif inventory != "missing":
        path.write_text(json.dumps({"provider": "chutes", "models": [row]}))

    stale = ModelEndpoint(
        id=f"{model_id}@chutes/{usage_type}", model_id=model_id, provider="chutes",
        upstream_id=native_id, usage_type=usage_type,
    )
    other = ModelEndpoint(
        id=f"{model_id}@gmi/byok", model_id=model_id, provider="gmi",
        upstream_id="unrelated-native-id", usage_type="BYOK",
    )
    kept = catalog_ingest._filter_unserved_provider_endpoints({stale.id: stale, other.id: other})
    assert (stale.id in kept) is (inventory == "active")
    assert kept[other.id] == other


@pytest.mark.parametrize("usage_type", ["Credits", "BYOK"])
def test_real_qwen_evidence_hold_rejects_a_reintroduced_snapshot_route(usage_type: str) -> None:
    model_id = "qwen/qwen3-235b-a22b-thinking-2507"
    stale = ModelEndpoint(
        id=f"{model_id}@chutes/{usage_type}", model_id=model_id, provider="chutes",
        upstream_id="Qwen/Qwen3-235B-A22B-Thinking-2507-TEE", usage_type=usage_type,
    )
    assert catalog_ingest._filter_unserved_provider_endpoints({stale.id: stale}) == {}
