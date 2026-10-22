"""Native Tinfoil decisions are input-only, not chat model aliases."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.pricing.providers import tinfoil
from tests.pinned_manifests import build_manifest_rows
from tests.test_decide_models import _assert_no_financial_side_effects, _authorize
from trusted_router.catalog import endpoint_e2ee
from trusted_router.catalog_data import model_origin_for_model_id
from trusted_router.catalog_privacy import served_by_model_vendor
from trusted_router.provider_manifest_policy import decision_manifest_price_is_valid


def decision_row(size: str = "small", price: str = "0.02") -> dict:
    return {
        "id": f"decision-{size}", "name": f"Decision {size.title()}",
        "type": "decision", "context_window": 32_768,
        "endpoints": ["/v1/decisions"], "multimodal": False,
        "pricing": {"inputTokenPricePer1M": price, "outputTokenPricePer1M": 0, "requestPrice": 0},
    }


def test_decisions_discovery_roundtrip_prices_capabilities_and_refresh(monkeypatch, tmp_path: Path) -> None:
    rows = [decision_row(size, price) for size, price in [("small", "0.02"), ("medium", "0.06"), ("large", "0.40")]]
    path = tmp_path / "tinfoil.json"
    path.write_text(json.dumps({"models": []}))
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", path)
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _: {"data": rows})
    result = tinfoil.fetch()
    tinfoil.write_provider_manifest(result)
    manifest = json.loads(path.read_text())["models"]
    models, endpoints = build_manifest_rows(monkeypatch, tmp_path / "catalog", "tinfoil", manifest)
    for size, price in [("small", 21_100), ("medium", 63_300), ("large", 422_000)]:
        model_id = f"tinfoil/decision-{size}"
        model = models[model_id]
        endpoint = endpoints[f"{model_id}@tinfoil/prepaid"]
        assert model.supports_decide and not model.supports_chat and not model.supports_embeddings
        assert model.context_length == 32_768
        assert model.output_modalities == ("decision",)
        assert endpoint.upstream_id == f"decision-{size}"
        assert endpoint.prompt_price_microdollars_per_million_tokens == price
        assert endpoint.completion_price_microdollars_per_million_tokens == 0
        assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens is None
        assert endpoint_e2ee(endpoint)
    rows[0]["pricing"]["inputTokenPricePer1M"] = "0.03"
    tinfoil.write_provider_manifest(tinfoil.fetch())
    refreshed = {row["id"]: row for row in json.loads(path.read_text())["models"]}
    assert refreshed["tinfoil/decision-small"]["input_token_price_per_m"] == 30_000


@pytest.mark.parametrize("change", [
    {"type": "chat"}, {"endpoints": ["/v1/chat/completions"]}, {"endpoints": []},
    {"multimodal": True}, {"context_window": 0}, {"context_window": True},
    {"pricing": "invalid"}, {"pricing": None},
    {"pricing": {"inputTokenPricePer1M": "NaN", "outputTokenPricePer1M": 0, "requestPrice": 0}},
    {"pricing": {"inputTokenPricePer1M": 0, "outputTokenPricePer1M": 0, "requestPrice": 0}},
    {"pricing": {"inputTokenPricePer1M": "0.02", "outputTokenPricePer1M": 1, "requestPrice": 0}},
    {"pricing": {"inputTokenPricePer1M": "0.02", "outputTokenPricePer1M": 0, "requestPrice": 1}},
    {"pricing": {"inputTokenPricePer1M": "0.02", "outputTokenPricePer1M": "0.00000001", "requestPrice": 0}},
    {"pricing": {"inputTokenPricePer1M": "0.02", "outputTokenPricePer1M": 0, "requestPrice": "0.00000001"}},
    {"pricing": {"inputTokenPricePer1M": "0.02", "outputTokenPricePer1M": 0}},
    {"pricing": {"inputTokenPricePer1M": "0.02", "outputTokenPricePer1M": 0, "requestPrice": 0, "cachedInputTokenPricePer1M": "0.01"}},
])
def test_decisions_contract_drift_blocks_refresh(monkeypatch, change: dict) -> None:
    row = decision_row()
    row.update(change)
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _: {"data": [row]})
    with pytest.raises(ValueError, match="decision contract"):
        tinfoil.fetch()


def test_unknown_decision_models_require_protocol_review(monkeypatch) -> None:
    row = decision_row("future")
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _: {"data": [row]})
    assert not any(model.startswith("tinfoil/decision") for model in tinfoil.fetch().prices)


def test_tinfoil_hosted_aliases_are_not_mistaken_for_the_model_author() -> None:
    for size in ("small", "medium", "large"):
        model_id = f"tinfoil/decision-{size}"
        assert not served_by_model_vendor(model_id, "tinfoil")
        origin = model_origin_for_model_id(model_id)
        assert origin is not None and origin.country is None
        assert origin.source_url == "https://docs.tinfoil.sh/models/decisions"
    assert served_by_model_vendor("tinfoil/decision-future", "tinfoil")


@pytest.mark.parametrize("change", [
    {"id": "tinfoil/decision-future"}, {"upstream_id": "decision-large"},
    {"input_modalities": ["image"]}, {"endpoints": ["chat/completions"]},
    {"output_token_price_per_m": 1}, {"cached_input_token_price_per_m": 1},
    {"price_tiers": []}, {"input_token_price_per_m": True},
])
def test_catalog_rejects_invalid_decision_tariff_or_identity(change: dict) -> None:
    row = {
        "id": "tinfoil/decision-small", "upstream_id": "decision-small",
        "model_type": "decision", "endpoints": ["decide"],
        "input_modalities": ["text"], "output_modalities": ["decision"],
        "input_token_price_per_m": 20_000, "output_token_price_per_m": 0,
    }
    assert decision_manifest_price_is_valid(row)
    invalid = deepcopy(row)
    invalid.update(change)
    assert not decision_manifest_price_is_valid(invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ["small", "medium", "large"])
@pytest.mark.parametrize("route_type", ["decide", "chat.completions", "responses", "embeddings"])
async def test_tinfoil_decisions_authorization_is_confidential_and_protocol_specific(
    size: str, route_type: str,
) -> None:
    model_id = f"tinfoil/decision-{size}"
    response = await _authorize({
        "model": model_id, "route_type": route_type,
        "estimated_input_tokens": 400, "max_output_tokens": 1,
        "provider": {"only": ["tinfoil"], "min_privacy": "confidential", "allow_fallbacks": False},
    })
    if route_type != "decide":
        assert response.status_code == 400, response.text
        _assert_no_financial_side_effects()
        return
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["provider"] == "tinfoil"
    assert payload["model"] == model_id
    assert payload["upstream_model"] == f"decision-{size}"
    assert payload["usage_type"] == "Credits"
