from datetime import date
from decimal import Decimal

import pytest

from scripts.pricing.openai_catalog import openai_model_price
from scripts.pricing.providers import regolo


def row(**changes):
    return {
        "model_group": "qwen3.5-9b", "mode": "chat",
        "input_cost_per_token": "0.00000007", "output_cost_per_token": "0.00000035",
        "max_input_tokens": 80000.0, "max_output_tokens": 120000.0, "max_tokens": 200000.0,
        "supports_function_calling": True, "supports_vision": False,
        "supported_openai_params": ["web_search_options", "tools", "store"],
        **changes,
    }


def test_eur_price_conversion_and_hosted_limits():
    normalized = regolo.normalize_catalog({"data": [row()]}, Decimal("1.1551"))[0]
    price = openai_model_price(normalized)
    assert price is not None
    assert price.prompt_micro_per_m == 80857
    assert price.completion_micro_per_m == 404285
    assert normalized["context_length"] == 80000
    assert normalized["max_output_tokens"] == 80000
    assert normalized["input_modalities"] == ["text"]
    assert "web_search_options" not in normalized["supported_sampling_parameters"]
    assert "function-calling" in normalized["supported_features"]


@pytest.mark.parametrize("changes", [
    {"input_cost_per_token": "NaN"}, {"output_cost_per_token": "Infinity"},
    {"input_cost_per_token": -1}, {"output_cost_per_token": 0},
    {"max_input_tokens": None}, {"mode": "image_generation"},
    {"model_group": "brick-complexity-pro"},
])
def test_unpriced_unknown_limit_and_non_direct_chat_rows_are_excluded(changes):
    assert regolo.normalize_catalog({"data": [row(**changes)]}, Decimal("1.1")) == []


def test_discovery_preserves_exact_names_and_allows_future_models():
    assert regolo.CATALOG.model_id("gemma4-31b") == "google/gemma-4-31b-it"
    assert regolo.CATALOG.model_id("new-chat-2027") == "regolo/new-chat-2027"
    with pytest.raises(RuntimeError, match="duplicate"):
        regolo.normalize_catalog({"data": [row(), row()]}, Decimal("1.1"))


def test_exchange_rate_is_dated_and_must_be_fresh():
    xml = '<Envelope><Cube><Cube time="2026-09-11"><Cube currency="USD" rate="1.1551"/></Cube></Cube></Envelope>'
    assert regolo.usd_per_eur(xml, today=date(2026, 9, 14)) == Decimal("1.1551")
    for today in (date(2026, 9, 10), date(2026, 9, 20)):
        with pytest.raises(RuntimeError):
            regolo.usd_per_eur(xml, today=today)


def test_refresh_canaries_new_models_and_preserves_failed_state(monkeypatch, tmp_path):
    import json
    from dataclasses import replace

    from scripts.pricing.providers import _direct_openai

    normalized = regolo.normalize_catalog({"data": [row(), row(model_group="new-model")]}, Decimal("1.1551"))
    catalog = _direct_openai.DirectOpenAIProvider(
        replace(regolo.CATALOG.spec, catalog_loader=lambda _: normalized),
        manifest_path=tmp_path / "regolo.json",
    )
    monkeypatch.setenv("REGOLO_API_KEY", "test-only-token")
    probes = []

    def probe(**kwargs):
        probes.append(kwargs)
        return kwargs["model"] == "qwen3.5-9b"

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    result = catalog.fetch()
    catalog.write_provider_manifest(result)
    models = {model["id"]: model for model in json.loads(catalog.manifest_path.read_text())["models"]}
    assert models["qwen/qwen3.5-9b"]["routable"] is True
    assert models["regolo/new-model"]["routable"] is False
    assert models["regolo/new-model"]["routable_reason"] == "provider-canary-failed"
    assert len(probes) == 2
    assert all(p["expected_content"] == "PONG" and p["max_tokens"] == 512 for p in probes)
    assert all(p["prompt"] == "Reply with exactly PONG and nothing else." for p in probes)
