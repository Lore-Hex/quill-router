from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing.providers import openrouter
from trusted_router.catalog import MODELS, PROVIDERS, endpoints_for_model
from trusted_router.catalog_privacy import endpoint_zero_data_retention

_UNION = "stealth/union-alpha"
_SEED = "bytedance-seed/seed-2-1-turbo"


@pytest.fixture
def feeds(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    payloads: dict[str, Any] = {
        openrouter.URL: {"data": [{"id": model} for model in (_SEED, _UNION, "other/free")]},
    }
    for model, provider, prompt, completion in (
        (_SEED, "Seed", "0.0000005", "0.0000025"),
        (_UNION, "Stealth", "0", "0"),
    ):
        payloads[f"{openrouter.URL}/{model}/endpoints"] = {"data": {
            "id": model, "name": model,
            "architecture": {"input_modalities": ["text", "image"],
                             "output_modalities": ["text"]},
            "endpoints": [{
                "model_id": model, "provider_name": provider, "status": 0,
                "context_length": 262144, "max_completion_tokens": 131072,
                "pricing": {"prompt": prompt, "completion": completion},
                "supported_parameters": ["max_tokens", "tools", "tool_choice"],
            }],
        }}
    monkeypatch.setattr(openrouter, "fetch_json", lambda url: payloads[url])
    monkeypatch.setattr(openrouter, "_DISCOVERED_MANIFEST_ROWS", {})
    return payloads


def _endpoint(feeds: dict[str, Any], model: str = _UNION) -> dict[str, Any]:
    return feeds[f"{openrouter.URL}/{model}/endpoints"]["data"]["endpoints"][0]


def test_explicit_union_zero_and_existing_seed_prices(feeds: dict[str, Any]) -> None:
    result = openrouter.fetch()
    assert set(result.prices) == {_SEED, _UNION}
    assert result.prices[_SEED].prompt_micro_per_m == 500_000
    assert result.prices[_SEED].completion_micro_per_m == 2_500_000
    assert result.prices[_UNION].prompt_micro_per_m == 0
    assert result.prices[_UNION].completion_micro_per_m == 0
    assert result.prices[_UNION].tiers[0].prompt_cached_micro_per_m is None
    row = openrouter._DISCOVERED_MANIFEST_ROWS[_UNION]
    assert row["context_length"] == 262144
    assert row["max_completion_tokens"] == 131072
    assert row["input_modalities"] == ["text", "image"]
    assert "tools" in row["supported_parameters"]


@pytest.mark.parametrize("invalid", [None, "", "NaN", "Infinity", "-0.01", "oops"])
def test_invalid_price_never_becomes_free(feeds: dict[str, Any], invalid: object) -> None:
    _endpoint(feeds)["pricing"]["prompt"] = invalid
    with pytest.raises(RuntimeError, match="invalid per-token price"):
        openrouter.fetch()


def test_seed_cannot_accidentally_become_free(feeds: dict[str, Any]) -> None:
    _endpoint(feeds, _SEED)["pricing"] = {"prompt": "0", "completion": "0"}
    with pytest.raises(RuntimeError, match="all prices are zero"):
        openrouter.fetch()


@pytest.mark.parametrize("field,value", [
    ("provider_name", "Unexpected"), ("model_id", "different/model"), ("status", -1),
])
def test_endpoint_identity_and_status_fail_closed(
    feeds: dict[str, Any], field: str, value: object,
) -> None:
    _endpoint(feeds)[field] = value
    with pytest.raises(RuntimeError, match="one active approved endpoint"):
        openrouter.fetch()


def test_paid_launch_and_cached_pricing_are_refreshed(
    feeds: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = tmp_path / "openrouter.json"
    path.write_text(openrouter.MANIFEST_PATH.read_text())
    monkeypatch.setattr(openrouter, "MANIFEST_PATH", path)
    _endpoint(feeds)["pricing"] = {
        "prompt": "0.000001", "completion": "0.000004", "input_cache_read": "0.0000001",
    }
    result = openrouter.fetch()
    openrouter.write_provider_manifest(result)
    rows = {row["id"]: row for row in json.loads(path.read_text())["models"]}
    assert rows[_UNION]["input_token_price_per_m"] == 1_000_000
    assert rows[_UNION]["output_token_price_per_m"] == 4_000_000
    assert rows[_UNION]["cached_input_token_price_per_m"] == 100_000
    assert rows[_SEED]["input_token_price_per_m"] == 500_000
    assert rows[_UNION]["reliability"]["first_token_timeout_seconds"] == 45
    _endpoint(feeds)["pricing"].pop("input_cache_read")
    openrouter.write_provider_manifest(openrouter.fetch())
    rows = {row["id"]: row for row in json.loads(path.read_text())["models"]}
    assert "cached_input_token_price_per_m" not in rows[_UNION]


def test_delisted_preview_respects_existing_mass_prune_guard(
    feeds: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = tmp_path / "openrouter.json"
    path.write_text(openrouter.MANIFEST_PATH.read_text())
    monkeypatch.setattr(openrouter, "MANIFEST_PATH", path)
    feeds[openrouter.URL] = {"data": [{"id": _SEED}]}
    notices = []
    for _ in range(2):
        result = openrouter.fetch()
        assert set(result.prices) == {_SEED}
        notices = openrouter.write_provider_manifest(result)
    rows = {row["id"]: row for row in json.loads(path.read_text())["models"]}
    # One of two approved routes disappearing requires review, not a bypass of
    # the shared mass-prune guard. Fresh price output still omits that model.
    assert any("mass-prune guard" in notice for notice in notices)
    assert rows[_UNION].get("routable") is not False
    assert rows[_SEED].get("routable") is not False


def test_union_route_uses_existing_billing_and_standard_privacy() -> None:
    model = MODELS[_UNION]
    assert model.supports_chat
    assert model.context_length == 262144
    assert model.input_modalities == ("text", "image")
    endpoints = endpoints_for_model(_UNION)
    assert len(endpoints) == 1
    endpoint = endpoints[0]
    assert endpoint.provider == "openrouter"
    assert endpoint.upstream_id == _UNION
    assert endpoint.usage_type == "Credits"
    assert endpoint.prompt_price_microdollars_per_million_tokens == 10_000
    assert endpoint.completion_price_microdollars_per_million_tokens == 10_000
    assert {"tools", "tool_choice"} <= set(endpoint.supported_parameters)
    assert endpoint_zero_data_retention(endpoint) is False
    assert PROVIDERS["openrouter"].provider_confidential_compute is False
    assert PROVIDERS["openrouter"].provider_e2ee is False
    assert PROVIDERS["openrouter"].supports_byok is False
