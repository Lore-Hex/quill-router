from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.parsers import morph as parser
from scripts.pricing.providers import morph
from trusted_router.catalog import endpoints_for_model
from trusted_router.provider_contracts import (
    MORPH_KIMI_ABLITERATED_HOLD,
    MORPH_KIMI_ABLITERATED_ID,
    MORPH_KIMI_ABLITERATED_NATIVE,
    provider_model_operator_held,
)


def test_abliterated_price_requires_exact_sku() -> None:
    def row(native: str, prices: tuple[str, str, str]) -> str:
        return f"<tr><td>{native}</td>" + "".join(
            f"<td>$ <span>{p}</span> /1M</td>" for p in prices
        ) + "</tr>"

    standard = row("morph-kimik31", ("2.50", "0.29", "14"))
    variant = row(MORPH_KIMI_ABLITERATED_NATIVE, ("5", "0.58", "28"))
    assert parser.parse(standard).items() <= parser.parse(f"<table>{standard}{variant}</table>").items()
    assert parser.parse(f"<table>{standard}{variant}</table>")[MORPH_KIMI_ABLITERATED_ID] == {
        "prompt_micro_per_m": 5_000_000, "prompt_cached_micro_per_m": 580_000,
        "completion_micro_per_m": 28_000_000,
    }
    for text in (standard, variant.replace("abliterated", "abliterated-preview"), "Vercel Security Checkpoint"):
        assert MORPH_KIMI_ABLITERATED_ID not in parser.parse(text)


def test_held_morph_variant_not_served_under_either_key_path() -> None:
    rows = json.loads(Path(morph.MANIFEST_PATH).read_text())["models"]
    (entry,) = [r for r in rows if r["id"] == MORPH_KIMI_ABLITERATED_ID]
    assert entry["upstream_id"] == MORPH_KIMI_ABLITERATED_NATIVE
    assert entry["routable"] is False
    assert entry["routable_reason"] == MORPH_KIMI_ABLITERATED_HOLD
    assert provider_model_operator_held("morph", MORPH_KIMI_ABLITERATED_ID)
    assert provider_model_operator_held("morph", "other", MORPH_KIMI_ABLITERATED_NATIVE)
    assert not provider_model_operator_held("morph", "moonshotai/kimi-k3.1-pro", "morph-kimik31")
    assert not endpoints_for_model(MORPH_KIMI_ABLITERATED_ID)


def test_authenticated_refresh_preserves_hold_and_existing_price_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    prices = {model: ModelPrice(1_000_000, 2_000_000) for model in morph.EXPECTED_MODELS}
    prices[MORPH_KIMI_ABLITERATED_ID] = ModelPrice(5_000_000, 28_000_000, prompt_cached_micro_per_m=580_000)
    parsed = ProviderPricingResult(slug="morph", prices=prices, source="deterministic")
    monkeypatch.setattr(morph, "fetch_provider", lambda **kw: parsed)
    monkeypatch.setenv("MORPH_API_KEY", "test-key")
    monkeypatch.setattr(morph, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(morph, "UPSTREAM_ID_MAP", dict(morph.UPSTREAM_ID_MAP))
    def serve(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == morph.MODELS_URL
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"data": [{"id": n} for n in morph.UPSTREAM_ID_MAP.values()]})

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kw: httpx.MockTransport(serve))
    monkeypatch.setattr(morph, "MANIFEST_PATH", tmp_path / "morph.json")
    result = morph.fetch()
    assert result.price_index_model_ids == frozenset(morph.EXPECTED_MODELS)
    morph.write_provider_manifest(result)
    rows = json.loads(morph.MANIFEST_PATH.read_text())["models"]
    (held,) = [row for row in rows if row["id"] == MORPH_KIMI_ABLITERATED_ID]
    assert held["routable"] is False
    assert held["routable_reason"] == MORPH_KIMI_ABLITERATED_HOLD
    assert held["cached_input_token_price_per_m"] == 580_000
