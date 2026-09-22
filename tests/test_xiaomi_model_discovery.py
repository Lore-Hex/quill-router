from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice, PriceTier, ProviderPricingResult
from scripts.pricing.parsers.xiaomi import parse
from scripts.pricing.providers import xiaomi
from trusted_router.catalog import endpoints_for_model
from trusted_router.pricing import _customer_price

FIXTURES = Path(__file__).parent / "fixtures" / "pricing"
CARD = (FIXTURES / "xiaomi_model_card.html").read_text()


@pytest.fixture
def manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "xiaomi.json"
    raw = json.loads(xiaomi.MANIFEST_PATH.read_text())
    raw["models"] = [row for row in raw["models"] if "v2.6" not in row["id"]]
    raw["model_count"] = len(raw["models"])
    target.write_text(json.dumps(raw))
    monkeypatch.setattr(xiaomi, "MANIFEST_PATH", target)
    return target


def prices() -> ProviderPricingResult:
    parsed = parse((FIXTURES / "xiaomi_realtime_batch.html").read_text())
    return ProviderPricingResult(
        slug="xiaomi", source="deterministic", fetched_url=xiaomi.URL,
        prices={model: ModelPrice(**value) for model, value in parsed.items()},
    )


def model_card(url: str) -> str:
    assert url.startswith("https://mimo.mi.com/models/en-US/mimo-")
    native_id = url.rsplit("/", 1)[-1]
    title = {
        "mimo-v2.6-pro": "MiMo-V2.6-Pro",
        "mimo-v2.6-flash": "MiMo-V2.6-Flash",
        "mimo-v2.6-pro-ultraspeed": "MiMo-V2.6-Pro-UltraSpeed",
    }.get(native_id, native_id)
    return CARD.replace("MiMo-V2.6-Pro", title)


def test_refresh_adds_all_new_priced_models(
    manifest: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xiaomi, "fetch_html", model_card)
    xiaomi.write_provider_manifest(prices())
    raw = json.loads(manifest.read_text())
    rows = {row["id"]: row for row in raw["models"]}
    assert raw["model_count"] == len(rows)
    expected = {
        "flash": (140_000, 2_800, 280_000),
        "pro": (435_000, 3_600, 870_000),
        "pro-ultraspeed": (4_350_000, 36_000, 8_700_000),
    }
    for suffix, (prompt, cached, completion) in expected.items():
        row = rows[f"xiaomi/mimo-v2.6-{suffix}"]
        assert row["upstream_id"] == f"mimo-v2.6-{suffix}"
        assert row["context_length"] == 1_048_576
        assert row["max_output_tokens"] == 131_072
        assert row["input_token_price_per_m"] == prompt
        assert row["cached_input_token_price_per_m"] == cached
        assert row["output_token_price_per_m"] == completion
        assert set(row["features"]) == {
            "serverless", "function-calling", "reasoning", "structured-output",
        }
        assert row["input_modalities"] == ["text"]
        assert "retirement_at" not in row
    assert rows["xiaomi/mimo-v2.5-pro-ultraspeed"]["retirement_at"] == "2026-09-07T16:00:00Z"
    assert rows["xiaomi/mimo-v2.6-pro"]["title"] == "MiMo-V2.6-Pro"
    assert rows["xiaomi/mimo-v2.6-pro"]["display_name"] == "Xiaomi MiMo V2.6 Pro"
    # Once discovered, refresh pricing without refetching the model card.
    monkeypatch.setattr(xiaomi, "fetch_html", lambda _url: pytest.fail("known card refetched"))
    xiaomi.write_provider_manifest(prices())
    assert json.loads(manifest.read_text())["models"] == raw["models"]


@pytest.mark.parametrize("model_id", ["xiaomi/mimo-v9-pro", "xiaomi/mimo-v2.6.1-pro"])
def test_future_priced_release_is_not_a_hardcoded_allowlist(
    manifest: Path, monkeypatch: pytest.MonkeyPatch, model_id: str,
) -> None:
    monkeypatch.setattr(xiaomi, "fetch_html", model_card)
    result = prices()
    result.prices[model_id] = ModelPrice(1_000_000, 2_000_000)
    xiaomi.write_provider_manifest(result)
    rows = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
    assert rows[model_id]["upstream_id"] == model_id.removeprefix("xiaomi/")
    original = manifest.read_bytes()
    with pytest.raises(RuntimeError, match="missing fresh prices"):
        xiaomi.write_provider_manifest(prices())
    assert manifest.read_bytes() == original


@pytest.mark.parametrize("suffix", ["pro", "flash", "pro-ultraspeed"])
def test_price_miss_does_not_renew_previously_discovered_rows(
    manifest: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    monkeypatch.setattr(xiaomi, "fetch_html", model_card)
    result = prices()
    xiaomi.write_provider_manifest(result)
    original = manifest.read_bytes()
    del result.prices[f"xiaomi/mimo-v2.6-{suffix}"]
    with pytest.raises(RuntimeError, match="missing fresh prices"):
        xiaomi.write_provider_manifest(result)
    assert manifest.read_bytes() == original


@pytest.mark.parametrize("old,new", [
    ("MiMo-V2.6-Pro", "MiMo-V2.5-Pro"),
    ("Model Specs", "Preview Specs"),
    ("1M tokens", "unlimited"),
    ("128K tokens", "2M tokens"),
    ("<span>Output Modality</span><span>Text", "<span>Output Modality</span><span>Audio"),
    ("Capabilities", "Example capabilities"),
    ("<span>1M tokens</span>", "<span>1M tokens</span><span>2M tokens</span>"),
])
def test_unproven_specs_never_partially_write_manifest(
    manifest: Path, monkeypatch: pytest.MonkeyPatch, old: str, new: str,
) -> None:
    monkeypatch.setattr(xiaomi, "fetch_html", lambda _url: CARD.replace(old, new))
    original = manifest.read_bytes()
    result = prices()
    result.prices = {
        key: value for key, value in result.prices.items()
        if key == "xiaomi/mimo-v2.6-pro" or "v2.6" not in key
    }
    with pytest.raises(ValueError, match="xiaomi:"):
        xiaomi.write_provider_manifest(result)
    assert manifest.read_bytes() == original


def test_card_failure_preserves_manifest(manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_url: str) -> str:
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(xiaomi, "fetch_html", unavailable)
    original = manifest.read_bytes()
    with pytest.raises(RuntimeError, match="upstream unavailable"):
        xiaomi.write_provider_manifest(prices())
    assert manifest.read_bytes() == original


@pytest.mark.parametrize("model_id", ["xiaomi/../../evil", "other/mimo-v2.6-pro"])
def test_invalid_identity_never_fetches(model_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xiaomi, "fetch_html", lambda _url: pytest.fail("unsafe fetch"))
    with pytest.raises(ValueError, match="invalid priced model id"):
        xiaomi._new_chat_model(model_id, created=1)


def test_tiered_prices_fail_closed(manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xiaomi, "fetch_html", model_card)
    original = manifest.read_bytes()
    result = prices()
    result.prices["xiaomi/mimo-v2.6-pro"] = ModelPrice(tiers=[
        PriceTier(256_000, 435_000, 870_000),
        PriceTier(None, 870_000, 1_740_000),
    ])
    with pytest.raises(ValueError, match="unreviewed tiered price"):
        xiaomi.write_provider_manifest(result)
    assert manifest.read_bytes() == original


@pytest.mark.parametrize("suffix", ["pro", "flash", "pro-ultraspeed"])
def test_mimo_26_has_native_credit_route(suffix: str) -> None:
    routes = [
        route for route in endpoints_for_model(f"xiaomi/mimo-v2.6-{suffix}")
        if route.provider == "xiaomi" and route.usage_type == "Credits"
    ]
    assert len(routes) == 1
    assert routes[0].upstream_id == f"mimo-v2.6-{suffix}"
    assert "tools" in routes[0].supported_parameters
    prompt, cached, output = {
        "flash": (140_000, 2_800, 280_000),
        "pro": (435_000, 3_600, 870_000),
        "pro-ultraspeed": (4_350_000, 36_000, 8_700_000),
    }[suffix]
    assert routes[0].prompt_price_microdollars_per_million_tokens == _customer_price(prompt)
    assert routes[0].completion_price_microdollars_per_million_tokens == _customer_price(output)
    assert (
        routes[0].price_tiers[0].prompt_cached_price_microdollars_per_million_tokens
        == _customer_price(cached)
    )
