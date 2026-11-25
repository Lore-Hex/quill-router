"""Offline regressions for flat Anthropic cards and their published tier bounds."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, ProviderPricingResult, _coerce_to_model_prices
from scripts.pricing.parsers import anthropic as parser
from scripts.pricing.providers import anthropic as provider
from trusted_router.pricing import PriceTier, resolve_request_rates

HAIKU = "anthropic/claude-haiku-5.5"
HAIKU_LINES = [
    "Haiku 5.5",
    "Fastest, most cost-efficient model",
    "Prompt caching",
    "Prompts ≤ 100K tokens",
    "Read",
    "$0.01",
    "/ MTok",
    "Write",
    "$0.125",
    "/ MTok",
    "Prompts > 100K tokens",
    "Read",
    "$0.05",
    "/ MTok",
    "Write",
    "$0.625",
    "/ MTok",
    "Input",
    "Prompts ≤ 100K tokens",
    "$0.10",
    "/ MTok",
    "Prompts > 100K tokens",
    "$0.50",
    "/ MTok",
    "Output",
    "Prompts ≤ 100K tokens",
    "$0.50",
    "/ MTok",
    "Prompts > 100K tokens",
    "$2.50",
    "/ MTok",
]
BEFORE = [
    "Sonnet 5.5", "Prompt caching", "Read", "$0.20", "/ MTok",
    "Write", "$2.50", "/ MTok", "Input", "$2.00", "/ MTok",
    "Output", "$10.00", "/ MTok",
]
AFTER = ["Opus 5.5", "Input", "$5.00", "/ MTok", "Output", "$25.00", "/ MTok"]
# Frozen output of the pre-change parser on BEFORE + AFTER, including absence
# of a cached key for Opus. This pins the old behavior without a git dependency.
OLD_UNTIERED_OUTPUT = {
    "anthropic/claude-sonnet-5.5": {
        "prompt_micro_per_m": 2_000_000,
        "completion_micro_per_m": 10_000_000,
        "prompt_cached_micro_per_m": 200_000,
    },
    "anthropic/claude-opus-5.5": {
        "prompt_micro_per_m": 5_000_000,
        "completion_micro_per_m": 25_000_000,
    },
}
EXPECTED_TIERS = [
    {
        "max_prompt_tokens": 100_000,
        "prompt_micro_per_m": 100_000,
        "completion_micro_per_m": 500_000,
        "prompt_cached_micro_per_m": 10_000,
    },
    {
        "max_prompt_tokens": None,
        "prompt_micro_per_m": 500_000,
        "completion_micro_per_m": 2_500_000,
        "prompt_cached_micro_per_m": 50_000,
    },
]


def _html(lines: list[str]) -> str:
    return "".join(f"<div>{escape(line)}</div>" for line in lines)


def _parse_card(lines: list[str]) -> dict:
    all_lines = BEFORE + lines + AFTER
    html = _html(all_lines)
    soup = BeautifulSoup(html, "html.parser")
    assert parser._collect_lines(soup) == all_lines
    assert parser._parse_structured(soup) == {}
    rows = parser.parse(html)
    assert {key: rows[key] for key in OLD_UNTIERED_OUTPUT} == OLD_UNTIERED_OUTPUT
    return rows


def _coerced_prices() -> dict[str, ModelPrice]:
    prices, errors = _coerce_to_model_prices(_parse_card(HAIKU_LINES))
    assert errors == []
    assert prices is not None
    return prices


def test_literal_flat_card_preserves_both_tiers_and_neighbours() -> None:
    assert _parse_card(HAIKU_LINES) == {**OLD_UNTIERED_OUTPUT, HAIKU: {"tiers": EXPECTED_TIERS}}


def test_untiered_fixture_matches_pre_change_output_exactly() -> None:
    lines = BEFORE + AFTER
    assert parser._collect_lines(BeautifulSoup(_html(lines), "html.parser")) == lines
    assert parser.parse(_html(lines)) == OLD_UNTIERED_OUTPUT


@pytest.mark.parametrize("bound,threshold", [("100K", 100_000), ("1M", 1_000_000)])
@pytest.mark.parametrize("low_operator", ["≤", "<="])
def test_supported_heading_spellings(bound: str, threshold: int, low_operator: str) -> None:
    lines = [line.replace("100K", bound).replace("≤", low_operator) for line in HAIKU_LINES]
    expected = [dict(tier) for tier in EXPECTED_TIERS]
    expected[0]["max_prompt_tokens"] = threshold
    assert _parse_card(lines)[HAIKU] == {"tiers": expected}


@pytest.mark.parametrize("bad_heading", [
    "Prompts < 100K tokens", "Prompts >= 100K tokens", "Prompts ≤ 100k tokens",
    "Prompts ≤ 100000 tokens", "Prompts ≤ 0.1M tokens", "Prompts ≤ 0K tokens",
    "Prompts ≤ 100K tokens extra",
])
def test_does_not_guess_unsupported_headings(bad_heading: str) -> None:
    lines = list(HAIKU_LINES)
    lines[18] = bad_heading
    assert HAIKU not in _parse_card(lines)


@pytest.mark.parametrize("start,end", [(18, 21), (21, 24), (25, 28), (28, 31)])
def test_each_input_and_output_tier_is_required(start: int, end: int) -> None:
    lines = HAIKU_LINES[:start] + HAIKU_LINES[end:]
    assert HAIKU not in _parse_card(lines)


@pytest.mark.parametrize("index", [3, 10, 18, 21, 25, 28])
def test_mismatched_thresholds_reject_card(index: int) -> None:
    lines = list(HAIKU_LINES)
    lines[index] = lines[index].replace("100K", "200K")
    assert HAIKU not in _parse_card(lines)


@pytest.mark.parametrize("indices", [(3, 10), (18, 21), (25, 28)])
def test_reversed_headings_reject_card(indices: tuple[int, int]) -> None:
    lines = list(HAIKU_LINES)
    low, high = indices
    lines[low], lines[high] = lines[high], lines[low]
    assert HAIKU not in _parse_card(lines)


@pytest.mark.parametrize("position", [17, 24, 31])
def test_third_tier_heading_rejects_card(position: int) -> None:
    lines = HAIKU_LINES[:position] + ["Prompts > 100K tokens", "$3", "/ MTok"]
    lines += HAIKU_LINES[position:]
    assert HAIKU not in _parse_card(lines)


@pytest.mark.parametrize("price_index", [5, 8, 12, 15, 19, 22, 26, 29])
@pytest.mark.parametrize("bad_price", ["$0", "$-0.1", "$NaN", "$Infinity", "$1e-1", "$oops"])
def test_prices_must_be_positive_decimals(price_index: int, bad_price: str) -> None:
    lines = list(HAIKU_LINES)
    lines[price_index] = bad_price
    assert HAIKU not in _parse_card(lines)


@pytest.mark.parametrize("read_index", [4, 11])
def test_one_sided_read_omits_cached_price_from_both_tiers(read_index: int) -> None:
    lines = HAIKU_LINES[:read_index] + HAIKU_LINES[read_index + 3:]
    expected = [{key: value for key, value in tier.items() if key != "prompt_cached_micro_per_m"}
                for tier in EXPECTED_TIERS]
    rows = _parse_card(lines)
    assert rows[HAIKU] == {"tiers": expected}
    prices, errors = _coerce_to_model_prices(rows)
    assert not errors and prices is not None
    assert all(tier.prompt_cached_micro_per_m is None for tier in prices[HAIKU].tiers)


@pytest.mark.parametrize("start,end", [(3, 10), (10, 17)])
def test_one_sided_cache_section_omits_both_cached_prices(start: int, end: int) -> None:
    lines = HAIKU_LINES[:start] + HAIKU_LINES[end:]
    expected = [{key: value for key, value in tier.items() if key != "prompt_cached_micro_per_m"}
                for tier in EXPECTED_TIERS]
    assert _parse_card(lines)[HAIKU] == {"tiers": expected}


def test_caching_section_is_optional() -> None:
    lines = HAIKU_LINES[:2] + HAIKU_LINES[17:]
    expected = [{key: value for key, value in tier.items() if key != "prompt_cached_micro_per_m"}
                for tier in EXPECTED_TIERS]
    assert _parse_card(lines)[HAIKU] == {"tiers": expected}


def test_high_tier_prices_may_be_lower() -> None:
    lines = list(HAIKU_LINES)
    lines[22], lines[29] = "$0.05", "$0.25"
    expected = [dict(tier) for tier in EXPECTED_TIERS]
    expected[1].update(prompt_micro_per_m=50_000, completion_micro_per_m=250_000)
    assert _parse_card(lines)[HAIKU] == {"tiers": expected}


def test_fast_mode_flat_base() -> None:
    rows = parser.parse(_html(BEFORE + ["Fast mode for Sonnet 5.5 at 2.5x standard pricing"]))
    assert rows["anthropic/claude-sonnet-5.5-fast"] == {
        "prompt_micro_per_m": 5_000_000,
        "completion_micro_per_m": 25_000_000,
        "prompt_cached_micro_per_m": 500_000,
    }


def test_fast_mode_tiered_base() -> None:
    rows = _parse_card(HAIKU_LINES + ["Fast mode for Haiku 5.5 at 2.5x standard pricing"])
    assert rows[HAIKU] == {"tiers": EXPECTED_TIERS}
    assert rows[f"{HAIKU}-fast"] == {"tiers": [
        {"max_prompt_tokens": 100_000, "prompt_micro_per_m": 250_000,
         "completion_micro_per_m": 1_250_000, "prompt_cached_micro_per_m": 25_000},
        {"max_prompt_tokens": None, "prompt_micro_per_m": 1_250_000,
         "completion_micro_per_m": 6_250_000, "prompt_cached_micro_per_m": 125_000},
    ]}


def test_provider_fetch_and_real_writer_publish_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "anthropic.json"
    # Start with the defective flat row, using only synthetic fixture prices.
    manifest.write_text(json.dumps({"provider": "anthropic", "models": [{
        "id": HAIKU, "input_token_price_per_m": 100_000,
        "output_token_price_per_m": 500_000,
    }]}))
    monkeypatch.setattr(provider, "MANIFEST_PATH", manifest)
    monkeypatch.delenv(provider.API_KEY_ENV, raising=False)
    monkeypatch.setattr(provider, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(provider, "_DISCOVERY_SOURCE_URL", provider.URL)
    result = ProviderPricingResult(slug="anthropic", source="deterministic", prices=_coerced_prices())
    monkeypatch.setattr(provider, "fetch_provider", lambda **_kwargs: result)
    assert provider.fetch() is result
    assert set(provider._DISCOVERED_MANIFEST_ROWS) == set(result.prices)
    assert provider.write_provider_manifest(result)
    rows = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
    assert rows[HAIKU]["input_token_price_per_m"] == 100_000
    assert rows[HAIKU]["output_token_price_per_m"] == 500_000
    assert rows[HAIKU]["cached_input_token_price_per_m"] == 10_000
    assert rows[HAIKU]["price_tiers"] == [
        {"max_prompt_tokens": 100_000, "input_token_price_per_m": 100_000,
         "output_token_price_per_m": 500_000, "cached_input_token_price_per_m": 10_000},
        {"max_prompt_tokens": None, "input_token_price_per_m": 500_000,
         "output_token_price_per_m": 2_500_000, "cached_input_token_price_per_m": 50_000},
    ]


@pytest.mark.parametrize("prompt_tokens,expected", [
    (100_000, (100_000, 500_000, 10_000)),
    (100_001, (500_000, 2_500_000, 50_000)),
])
def test_parser_threshold_matches_runtime_inclusive_boundary(
    prompt_tokens: int, expected: tuple[int, int, int],
) -> None:
    price = _coerced_prices()[HAIKU]
    tiers = tuple(PriceTier(
        max_prompt_tokens=tier.max_prompt_tokens,
        prompt_price_microdollars_per_million_tokens=tier.prompt_micro_per_m,
        completion_price_microdollars_per_million_tokens=tier.completion_micro_per_m,
        prompt_cached_price_microdollars_per_million_tokens=tier.prompt_cached_micro_per_m,
    ) for tier in price.tiers)
    rates = resolve_request_rates(
        tiers, headline_prompt_micro_per_m=price.prompt_micro_per_m,
        headline_completion_micro_per_m=price.completion_micro_per_m,
        total_prompt_tokens=prompt_tokens,
    )
    assert (
        rates.prompt_price_microdollars_per_million_tokens,
        rates.completion_price_microdollars_per_million_tokens,
        rates.prompt_cached_price_microdollars_per_million_tokens,
    ) == expected


@pytest.mark.parametrize("low,high", [
    ("Up to 100K tokens", "Over 100K tokens"),
    ("Standard context", "Long context"),
    ("≤ 100K", "> 100K"),
])
def test_reworded_tier_headings_publish_no_price(low: str, high: str) -> None:
    # A card that still has two prices under each label, with headings this
    # parser does not know, must not come out as the low tier for every prompt.
    lines = [
        low if line == "Prompts ≤ 100K tokens" else high if line == "Prompts > 100K tokens" else line
        for line in HAIKU_LINES
    ]
    assert not any(line.startswith("Prompts") for line in lines)
    rows = _parse_card(lines)
    assert HAIKU not in rows


def test_reworded_headings_without_a_unit_line_publish_no_price() -> None:
    lines = [
        "Haiku 5.5", "Input", "Short prompts", "$0.10", "Long prompts", "$0.50",
        "Output", "Short prompts", "$0.50", "Long prompts", "$2.50",
    ]
    assert HAIKU not in _parse_card(lines)


def test_untiered_card_with_cache_prices_is_still_read() -> None:
    lines = [
        "Haiku 5.5", "Prompt caching", "Read", "$0.01", "/ MTok", "Write", "$0.125", "/ MTok",
        "Input", "$0.10", "/ MTok", "Output", "$0.50", "/ MTok",
    ]
    assert _parse_card(lines)[HAIKU] == {
        "prompt_micro_per_m": 100_000,
        "completion_micro_per_m": 500_000,
        "prompt_cached_micro_per_m": 10_000,
    }


def test_card_naming_only_the_low_tier_publishes_no_price() -> None:
    # One price per label, but the heading says it covers short prompts only:
    # a flat row would apply it to every prompt length.
    lines = [
        "Haiku 5.5", "Input", "Prompts ≤ 100K tokens", "$0.10", "/ MTok",
        "Output", "Prompts ≤ 100K tokens", "$0.50", "/ MTok",
    ]
    assert HAIKU not in _parse_card(lines)
