"""Google's Markdoc Standard tables must retain context and cached rates."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.check_price_spike import _load_provider_manifests
from scripts.pricing import refresh
from scripts.pricing.base import (
    ModelPrice,
    PriceTier,
    ProviderPricingResult,
    _coerce_to_model_prices,
    normalize_parser_input,
)
from scripts.pricing.parsers import gemini as parser
from scripts.pricing.providers import gemini as provider

MODEL = "google/gemini-2.5-pro"


def _table(prompt: str, completion: str, cached: str) -> str:
    return f"""
  {{% table %}}
  * Feature
  * Free Tier
  * Paid Tier, per 1M tokens in USD

  ---

  * Input price
  * Free of charge
  * {prompt}

  ---

  * Output price (including thinking tokens)
  * Free of charge
  * {completion}

  ---

  * Context caching price
  * Not available
  * {cached}
  {{% /table %}}
"""


def _tab(title: str, table: str) -> str:
    return f'  {{% tab title="{title}" %}}\n{table}\n  {{% /tab %}}\n'


def _page(*, standard: bool = True) -> str:
    # Minimal shapes from the official aistudio.google.com/docs/pricing page.
    tiered = _table(
        "$1.25, prompts <= 200k tokens\n  $2.50, prompts > 200k tokens",
        "$10.00, prompts <= 200k tokens\n  $15.00, prompts > 200k",
        "$0.125, prompts <= 200k tokens\n  $0.25, prompts > 200k\n"
        "  $4.50 / 1,000,000 tokens per hour (storage price)",
    )
    return (
        '## Gemini 2.5 Pro  {% id="gemini-2.5-pro" %}\n{% tabs %}\n'
        + _tab("Batch", _table("$0.625", "$5.00", "Not available"))
        + (_tab("Standard", tiered) if standard else "")
        + _tab("Priority", _table("$9.00", "$90.00", "$0.90"))
        + "{% /tabs %}\n"
    )


def _prices(source: str) -> dict[str, ModelPrice]:
    prices, errors = _coerce_to_model_prices(parser.parse(normalize_parser_input(source)))
    assert errors == []
    assert prices is not None
    return prices


def test_markdoc_selects_standard_and_preserves_both_cached_context_tiers() -> None:
    prices = _prices(_page())
    assert prices[MODEL].tiers == [
        PriceTier(200_000, 1_250_000, 10_000_000, 125_000),
        PriceTier(None, 2_500_000, 15_000_000, 250_000),
    ]


@pytest.mark.parametrize("less,greater", [("&lt;", "&gt;"), ("&#60;", "&#62;")])
@pytest.mark.parametrize("model,heading,rates", [
    (MODEL, "Gemini 2.5 Pro", ("1.25", "2.50", "10.00", "15.00", "0.125", "0.25")),
    ("google/gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview",
     ("2.00", "4.00", "12.00", "18.00", "0.20", "0.40")),
])
def test_encoded_context_comparisons_survive_parser_and_manifest(
    tmp_path: Path, less: str, greater: str, model: str, heading: str, rates: tuple[str, ...],
) -> None:
    def cell(low: str, high: str) -> str:
        return f"${low}, prompts {less}= 200k tokens\n  ${high}, prompts {greater} 200k"

    source = f'## {heading} {{% id="pro" %}}\n' + _tab("Standard", _table(
        cell(*rates[:2]), cell(*rates[2:4]), cell(*rates[4:])
        + "\n  $4.50 / 1,000,000 tokens per hour (storage price)",
    ))
    price = _prices(source)[model]
    expected = [
        PriceTier(200_000, int(float(rates[0]) * 1_000_000),
                  int(float(rates[2]) * 1_000_000), int(float(rates[4]) * 1_000_000)),
        PriceTier(None, int(float(rates[1]) * 1_000_000),
                  int(float(rates[3]) * 1_000_000), int(float(rates[5]) * 1_000_000)),
    ]
    assert price.tiers == expected
    row = {"id": model}
    assert provider._refresh_price(row, _result({model: price}), model)
    assert [tier["max_prompt_tokens"] for tier in row["price_tiers"]] == [200_000, None]
    assert len(_load_row(tmp_path, row)) == 5


@pytest.mark.parametrize("completion", ["$10.00", "$10.00, prompts <= 100k tokens "
                                      "$15.00, prompts > 100k tokens"])
def test_mismatched_context_tiers_are_not_flat_prices(completion: str) -> None:
    source = "## Gemini 2.5 Pro\n" + _tab("Standard", _table(
        "$1.25, prompts <= 200k tokens $2.50, prompts > 200k tokens",
        completion, "$0.125",
    ))
    assert MODEL not in parser.parse(source)


def test_markdoc_does_not_use_batch_or_another_models_standard_table() -> None:
    source = _page(standard=False) + (
        '## Gemini 3.7 Flash  {% id="gemini-3.7-flash" %}\n'
        + _tab("Standard", _table("$0.40", "$2.50", "$0.04"))
    )
    assert _prices(source) == {
        "google/gemini-3.7-flash": ModelPrice(400_000, 2_500_000,
                                           prompt_cached_micro_per_m=40_000),
    }


@pytest.mark.parametrize("heading", ["Gemini 3.8 Flash TTS", "Gemini 2.5 Flash Image"])
def test_markdoc_media_headings_are_not_text_prices(heading: str) -> None:
    source = f'## {heading} {{% id="media" %}}\n' + _tab(
        "Standard", _table("$1", "$2", "$0.10"),
    )
    assert parser.parse(source) == {}


def _result(prices: dict[str, ModelPrice]) -> ProviderPricingResult:
    return ProviderPricingResult(slug="gemini", prices=prices, source="deterministic")


def _load_row(tmp_path: Path, row: dict) -> dict[str, dict[str, str]]:
    (tmp_path / "google-ai-studio.json").write_text(json.dumps({
        "provider": "google-ai-studio",
        "models": [row],
    }))
    return _load_provider_manifests(tmp_path)


def test_markdoc_manifest_round_trip_keeps_cached_rates_visible_to_spike_gate(
    tmp_path: Path,
) -> None:
    row = {"id": MODEL}
    assert provider._refresh_price(row, _result(_prices(_page())), MODEL)
    rates = _load_row(tmp_path, row)
    assert len(rates) == 5
    cached = {key.rsplit(" tier[", 1)[-1]: value["prompt"]
              for key, value in rates.items() if key.endswith("cached-input")}
    assert cached == {
        "max=200000] cached-input": "1.25E-7",
        "uncapped] cached-input": "2.5E-7",
    }


@pytest.mark.parametrize("cached", [None, 0, 125_000])
def test_optional_cached_rate_never_serializes_null(tmp_path: Path, cached: int | None) -> None:
    price = ModelPrice(tiers=[
        PriceTier(200_000, 1_250_000, 10_000_000, cached),
        PriceTier(None, 2_500_000, 15_000_000, cached),
    ])
    row = {"id": MODEL}
    assert provider._refresh_price(row, _result({MODEL: price}), MODEL)
    tiers = row["price_tiers"]
    assert all("cached_input_token_price_per_m" in tier for tier in tiers) == (cached is not None)
    assert all(tier.get("cached_input_token_price_per_m", 0) is not None for tier in tiers)
    _load_row(tmp_path, row)


@pytest.mark.parametrize("old_tiered", [False, True])
@pytest.mark.parametrize("new_tiered", [False, True])
def test_missing_cache_evidence_cannot_silently_remove_existing_discount(
    old_tiered: bool, new_tiered: bool,
) -> None:
    row = {"id": MODEL, "input_token_price_per_m": 1_250_000,
           "output_token_price_per_m": 10_000_000}
    if old_tiered:
        row["price_tiers"] = [{"cached_input_token_price_per_m": 125_000}]
    else:
        row["cached_input_token_price_per_m"] = 125_000
    before = copy.deepcopy(row)
    tiers = [PriceTier(None, 1_250_000, 10_000_000)]
    if new_tiered:
        tiers.insert(0, PriceTier(200_000, 1_250_000, 10_000_000, 125_000))
    with pytest.raises(ValueError, match="cached.*missing"):
        provider._refresh_price(row, _result({MODEL: ModelPrice(tiers=tiers)}), MODEL)
    assert row == before


def test_price_spike_gate_still_rejects_explicit_null_cache(tmp_path: Path) -> None:
    row = {"id": MODEL, "input_token_price_per_m": 1_250_000,
           "output_token_price_per_m": 10_000_000,
           "cached_input_token_price_per_m": None}
    with pytest.raises(ValueError, match="price must be a non-negative number"):
        _load_row(tmp_path, row)


@pytest.mark.parametrize("threshold", [None, 100_000, 300_000])
def test_context_boundary_loss_is_rejected_before_row_mutation(threshold: int | None) -> None:
    row = {"id": MODEL}
    assert provider._refresh_price(row, _result(_prices(_page())), MODEL)
    before = copy.deepcopy(row)
    tiers = [PriceTier(None, 2_500_000, 15_000_000, 250_000)]
    if threshold is not None:
        tiers.insert(0, PriceTier(threshold, 1_250_000, 10_000_000, 125_000))
    with pytest.raises(ValueError, match="context tier boundaries"):
        provider._refresh_price(row, _result({MODEL: ModelPrice(tiers=tiers)}), MODEL)
    assert row == before


def test_unchanged_context_boundaries_allow_updated_rates() -> None:
    row = {"id": MODEL}
    assert provider._refresh_price(row, _result(_prices(_page())), MODEL)
    price = ModelPrice(tiers=[PriceTier(200_000, 1_300_000, 11_000_000, 130_000),
                              PriceTier(None, 2_600_000, 16_000_000, 260_000)])
    assert provider._refresh_price(row, _result({MODEL: price}), MODEL)
    assert row["price_tiers"][1]["input_token_price_per_m"] == 2_600_000


def test_manifest_refresh_retains_file_and_reports_lost_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {"id": MODEL}
    assert provider._refresh_price(row, _result(_prices(_page())), MODEL)
    path = tmp_path / "google-ai-studio.json"
    original = json.dumps({"models": [row], "generated_at": "2026-11-02T12:14:32Z"})
    path.write_text(original)
    monkeypatch.setattr(provider, "MANIFEST_PATH", path)
    monkeypatch.setattr(provider, "_DISCOVERED_MANIFEST_ROWS", {MODEL: {"id": MODEL}})
    monkeypatch.setattr(refresh, "_import_provider", lambda _slug: provider)
    notes, failures = refresh._write_provider_manifests({"gemini": _result({
        MODEL: ModelPrice(1_250_000, 10_000_000, prompt_cached_micro_per_m=125_000),
    })})
    assert notes == []
    assert len(failures) == 1
    assert failures[0][0] == "gemini"
    assert "stage=manifest" in failures[0][1]
    assert path.read_text() == original
