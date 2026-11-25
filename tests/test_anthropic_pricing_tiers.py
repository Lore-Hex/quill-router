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


FIXTURES = Path(__file__).parent / "fixtures" / "pricing"
CSS = "ApiTab-module-scss-module__YQdvya__"


def _price_row(label: str, *values: str) -> str:
    return (
        f'<div class="{CSS}priceRow"><p class="{CSS}priceLabel">{escape(label)}</p>'
        + "".join(f'<p class="{CSS}priceValue">{escape(value)}<!-- --> / MTok</p>'
                  for value in values)
        + '</div>'
    )


def _structured_card(body: str) -> str:
    return (
        f'<div class="{CSS}modelCard"><div class="{CSS}modelTitle">'
        f'<h3 class="{CSS}modelName">Haiku 5.5</h3></div>'
        f'<div class="{CSS}modelBody">{body}</div></div>'
    )


@pytest.mark.parametrize("complete", [True, False])
def test_structured_headline_above_tier_breakdown(complete: bool) -> None:
    body = ""
    for label, low, high in [("Input", "$0.10", "$0.50"), ("Output", "$0.50", "$2.50")]:
        body += f'<div class="{CSS}priceBlock">' + _price_row(label, low)
        body += f'<div class="{CSS}priceBreakdown">'
        body += _price_row("Prompts ≤ 100K tokens", low)
        if complete or label == "Input":
            body += _price_row("Prompts > 100K tokens", high)
        body += '</div></div>'
    html = _structured_card(body)
    assert "tiers" not in parser._parse_structured(BeautifulSoup(html, "html.parser"))[HAIKU]
    rows = parser.parse(html)
    if complete:
        expected = [{k: v for k, v in tier.items() if k != "prompt_cached_micro_per_m"}
                    for tier in EXPECTED_TIERS]
        assert rows == {HAIKU: {"tiers": expected}}
    else:
        assert rows == {}


def test_structured_each_tier_repeats_input_and_output() -> None:
    body = ""
    for heading, prompt, completion in [
        ("Prompts ≤ 100K tokens", "$0.10", "$0.50"),
        ("Prompts > 100K tokens", "$0.50", "$2.50"),
    ]:
        body += f'<div class="{CSS}priceBlock">' + _price_row(heading)
        body += _price_row("Input", prompt) + _price_row("Output", completion) + '</div>'
    html = _structured_card(body)
    assert HAIKU in parser._parse_structured(BeautifulSoup(html, "html.parser"))
    assert parser.parse(html) == {}


def test_structured_only_cache_prices_are_tiered() -> None:
    body = _price_row("Input", "$0.10") + _price_row("Output", "$0.50")
    body += _price_row("Prompt caching")
    for heading, price in [("Prompts ≤ 100K tokens", "$0.01"),
                           ("Prompts > 100K tokens", "$0.05")]:
        body += _price_row(heading) + _price_row("Read", price)
    html = _structured_card(body)
    assert HAIKU in parser._parse_structured(BeautifulSoup(html, "html.parser"))
    assert parser.parse(html) == {}


@pytest.mark.parametrize("label", ["Input", "Output", "Read", "Write"])
def test_structured_repeated_labels_without_tier_text(label: str) -> None:
    body = _price_row("Input", "$1") + _price_row("Output", "$5")
    if label in {"Read", "Write"}:
        body += _price_row(label, "$0.1")
    body += '<p>Unfamiliar category</p>' + _price_row(label, "$2")
    assert parser.parse(_structured_card(body)) == {}


def test_structured_multiple_values_in_one_row() -> None:
    body = _price_row("Input", "$1", "$2") + _price_row("Output", "$5")
    assert parser.parse(_structured_card(body)) == {}


def test_flat_repeated_labels_under_unknown_headings() -> None:
    lines = ["Haiku 5.5", "Category A", "Input", "$1", "Category B", "Input", "$2",
             "Output", "$5"]
    assert HAIKU not in _parse_card(lines)


@pytest.mark.parametrize("gap", [4, 6, 10])
@pytest.mark.parametrize("label", ["Input", "Output", "Read", "Write"])
def test_flat_prices_beyond_old_window(gap: int, label: str) -> None:
    # Split context headings and unit text need no fixed lookahead window.
    fragments = ["/ MTok", "Long", "context", *["tokens"] * (gap - 3)]
    prices = {"Input": "$1", "Output": "$5", "Read": "$0.1", "Write": "$1.25"}
    lines = ["Haiku 5.5"]
    for quantity, value in prices.items():
        lines.extend([quantity, value])
        if label == quantity:
            lines.extend([*fragments, "$2"])
    assert HAIKU not in _parse_card(lines)


@pytest.mark.parametrize("text", [
    "Prompts are shorter", "Prompts ≤ 100K tokens", "≤ 200K", "<= 100K tokens",
    "< 1M tokens", "> 100K tokens", ">= 200K", "up to 100K tokens", "over 200K",
    "above 1M tokens", "under 100K tokens", "more than 200K", "fewer than 1M tokens",
    "100K tokens >", "up to 100000 tokens",
])
def test_narrow_tier_text_rejects_flat_card(text: str) -> None:
    html = _structured_card('<p>' + escape(text) + '</p>'
                            + _price_row("Input", "$1") + _price_row("Output", "$5"))
    assert parser.parse(html) == {}
    assert HAIKU not in _parse_card(["Haiku 5.5", text, "Input", "$1", "Output", "$5"])


@pytest.mark.parametrize("text", [
    "100K tokens of context", "200K context window", "1M tokens", "up to 5x faster",
    "over 100 customers", "Input for all prompts", "Prompt caching", "Haiku 5.5",
])
def test_non_threshold_text_does_not_mark_card_multi_priced(text: str) -> None:
    html = _structured_card('<p>' + escape(text) + '</p>'
                            + _price_row("Input", "$1") + _price_row("Output", "$5"))
    assert parser.parse(html)[HAIKU] == {
        "prompt_micro_per_m": 1_000_000, "completion_micro_per_m": 5_000_000,
    }


def test_threshold_split_by_inline_elements() -> None:
    body = '<p>up to <span>100K</span> tokens</p>'
    body += _price_row("Input", "$1") + _price_row("Output", "$5")
    assert parser.parse(_structured_card(body)) == {}


@pytest.mark.parametrize("structured", [True, False])
def test_last_card_does_not_include_unrelated_priced_text(structured: bool) -> None:
    card = (_structured_card(_price_row("Input", "$1") + _price_row("Output", "$5"))
            if structured else _html(["Haiku 5.5", "Input", "$1", "/ MTok", "Output", "$5",
                                      "/ MTok"]))
    tail = _html(["FAQ", "Input", "$99", "Prompts over 200K tokens", "$999", "Output", "$100"])
    assert parser.parse(card + tail) == parser.parse(card)
    assert HAIKU in parser.parse(card + tail)


def test_captured_fixture_old_rows_unchanged_and_new_tiers_exact() -> None:
    expected = json.loads((FIXTURES / "anthropic.expected.json").read_text())
    actual = parser.parse((FIXTURES / "anthropic.html").read_text())
    assert len(expected) == 14
    assert len(expected[HAIKU]["tiers"]) == 2
    # Other 13 rows are frozen pre-change output, not checked-in manifest prices.
    assert {k: v for k, v in actual.items() if k != HAIKU} == {
        k: v for k, v in expected.items() if k != HAIKU
    }
    assert actual == expected


def test_self_heal_fixture_validation_rejects_flat_tier_stub() -> None:
    from scripts.pricing.base import _assert_fixture_compatibility

    expected = json.loads((FIXTURES / "anthropic.expected.json").read_text())
    # Run the actual sandbox and compatibility check, not a stand-in validator.
    _assert_fixture_compatibility(
        slug="anthropic", current_parse_fn=parser.parse,
        candidate_source=f"def parse(html):\n    return {expected!r}\n",
    )
    expected[HAIKU] = {key: value for key, value in expected[HAIKU]["tiers"][0].items()
                       if key != "max_prompt_tokens"}
    with pytest.raises(RuntimeError, match=r"self-heal fixture regression:.*changed=.*haiku-5.5"):
        _assert_fixture_compatibility(
            slug="anthropic", current_parse_fn=parser.parse,
            candidate_source=f"def parse(html):\n    return {expected!r}\n",
        )


def _manifest_profile(tiered: bool, price: int) -> dict:
    profile = {
        "input_token_price_per_m": price,
        "output_token_price_per_m": price * 5,
        "cached_input_token_price_per_m": price // 10,
    }
    if tiered:
        profile["price_tiers"] = [
            {"max_prompt_tokens": 100_000, **profile},
            {"max_prompt_tokens": None, "input_token_price_per_m": price * 2,
             "output_token_price_per_m": price * 10, "cached_input_token_price_per_m": price // 5},
        ]
    return profile


def _new_price(tiered: bool) -> ModelPrice:
    from scripts.pricing.base import PriceTier as ParsedTier

    tiers = [ParsedTier(100_000 if tiered else None, 2_000_000, 10_000_000, 200_000)]
    if tiered:
        tiers.append(ParsedTier(None, 4_000_000, 20_000_000, 400_000))
    return ModelPrice(tiers=tiers)


@pytest.mark.parametrize("slug", ["anthropic", "openai", "grok", "streamlake"])
@pytest.mark.parametrize("old_tiered,new_tiered", [(True, False), (True, True),
                                                  (False, True), (False, False)])
def test_shared_writer_price_transitions(
    tmp_path: Path, slug: str, old_tiered: bool, new_tiered: bool,
) -> None:
    from scripts.pricing import refresh
    from scripts.pricing.manifest import write_discovered_chat_manifest

    manifest = tmp_path / f"{slug}.json"
    row = {"id": HAIKU, "display_name": "Saved model", **_manifest_profile(old_tiered, 1_000_000)}
    raw = {"provider": slug, "models": [row]}
    manifest.write_text(json.dumps(raw, indent=2) + "\n")
    old_row_bytes = json.dumps(row, indent=2).encode()
    result = ProviderPricingResult(slug=slug, source="deterministic",
                                   prices={HAIKU: _new_price(new_tiered)})
    # Discovery attempts to replace prices too: committed prices win a hold.
    discovered = {HAIKU: {"id": HAIKU, "display_name": "Saved model",
                           **_manifest_profile(False, 9_000_000)}}
    write_discovered_chat_manifest(result, manifest_path=manifest,
                                   discovered_rows=discovered, source_url="https://example.test")
    actual = json.loads(manifest.read_text())["models"][0]
    if old_tiered and not new_tiered:
        assert json.dumps(actual, indent=2).encode() == old_row_bytes
        note = (f"kept tiered prices for {HAIKU}: new parse returned a single price; "
                "if the tiers are really gone, remove price_tiers from the manifest by hand")
        assert result.notes == [note]
        assert result.price_holds == {HAIKU: note}
        # This is the structure printed under the alert workflow's heading.
        assert refresh._price_review_holds({slug: result}, {}) == {slug: [note]}
        assert note in "\n".join(refresh._summary_lines({slug: result}, [], [], [], []))
    else:
        assert actual == {"id": HAIKU, "display_name": "Saved model",
                          **_manifest_profile(new_tiered, 2_000_000)}
        assert result.notes == []
        assert result.price_holds == {}


@pytest.mark.parametrize("discovered", [True, False])
def test_shared_writer_missing_price_keeps_existing_behavior(tmp_path: Path, discovered: bool) -> None:
    from scripts.pricing.manifest import write_discovered_chat_manifest

    manifest = tmp_path / "test.json"
    row = {"id": HAIKU, **_manifest_profile(True, 1_000_000)}
    siblings = [{"id": f"example/sibling-{i}", **_manifest_profile(False, 1_000_000)}
                for i in range(3)]
    manifest.write_text(json.dumps({"provider": "anthropic", "models": [row, *siblings]}))
    result = ProviderPricingResult(slug="anthropic", source="deterministic",
                                   prices={sibling["id"]: _new_price(False) for sibling in siblings})
    discovery = {sibling["id"]: {"id": sibling["id"]} for sibling in siblings}
    if discovered:
        discovery[HAIKU] = {"id": HAIKU}
    write_discovered_chat_manifest(result, manifest_path=manifest,
                                   discovered_rows=discovery, source_url="https://example.test")
    actual = json.loads(manifest.read_text())["models"][0]
    if discovered:
        assert actual == {"id": HAIKU, "routable": False, "routable_reason": "price-unavailable"}
    else:
        assert actual == row
    assert result.notes == []
    assert result.price_holds == {}


def test_tier_hold_reaches_refresh_alert_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    from scripts.pricing import refresh
    from scripts.pricing.manifest import write_discovered_chat_manifest

    manifests = tmp_path / "provider_models"
    manifests.mkdir()
    manifest = manifests / "anthropic.json"
    old = {"id": HAIKU, **_manifest_profile(True, 1_000_000)}
    manifest.write_text(json.dumps({"provider": "anthropic", "models": [old]}))
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"models": []}))
    parsers = tmp_path / "parsers"
    parsers.mkdir()
    result = ProviderPricingResult(slug="anthropic", source="deterministic",
                                   prices={HAIKU: _new_price(False)},
                                   notes=["prior note"] * 4)

    def write(res: ProviderPricingResult) -> list[str]:
        return write_discovered_chat_manifest(
            res, manifest_path=manifest, discovered_rows={HAIKU: {"id": HAIKU}},
            source_url="https://example.test",
        )

    module = SimpleNamespace(MANIFEST_PATH=manifest, write_provider_manifest=write)
    monkeypatch.setattr(refresh, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(refresh, "PROVIDER_MANIFEST_DIR", manifests)
    monkeypatch.setattr(refresh, "PARSERS_DIR", parsers)
    monkeypatch.setattr(refresh, "parser_path", lambda slug: parsers / f"{slug}.py")
    monkeypatch.setattr(refresh, "PROVIDER_SLUGS", ("anthropic",))
    monkeypatch.setattr(refresh, "_import_provider", lambda slug: module)
    monkeypatch.setattr(refresh, "_fetch_all_providers", lambda: ({"anthropic": result}, []))
    monkeypatch.setattr(refresh, "build_openrouter_snapshot", lambda: {"models": []})
    monkeypatch.setattr(refresh, "_new_parser_requirements", lambda *_args: {})
    monkeypatch.setattr(refresh, "configure_runtime_required_models", lambda *_args: None)
    monkeypatch.setattr(refresh, "quarantined_providers", lambda: {})
    assert refresh.main([]) == 0
    output = capsys.readouterr().out
    # The workflow uses sed from this heading to the next blank line. Check
    # the actual emitted section even when ordinary notes are truncated.
    alert_section = output.split(refresh.HELD_FOR_REVIEW_HEADING + "\n", 1)[1].split("\n\n", 1)[0]
    assert result.price_holds[HAIKU] in alert_section
    assert json.loads(manifest.read_text())["models"][0] == old



def test_structured_card_boundary_precedes_unrelated_labels() -> None:
    html = _structured_card(_price_row("Input", "$1") + _price_row("Output", "$5"))
    assert parser.parse(html + _html(["Input", "$99", "Output", "$999"])) == parser.parse(html)


def test_flat_threshold_split_by_inline_markup() -> None:
    html = _html(["Haiku 5.5", "Input", "$1", "Output", "$5"])
    html += '<p>up <b>to</b> <span>100K</span> tokens</p>'
    assert parser.parse(html) == {}


def test_structured_data_values_without_visible_dollars_are_counted() -> None:
    html = '''<div><div><h3 class="card_pricing_title_text">Haiku 5.5</h3></div>
      <div class="tokens_main_wrap"><span class="tokens_main_label">Input</span>
        <span class="tokens_main_val_number" data-value="1"></span></div>
      <div class="tokens_main_wrap">
        <span class="tokens_main_val_number" data-value="2"></span></div>
      <div class="tokens_main_wrap"><span class="tokens_main_label">Output</span>
        <span class="tokens_main_val_number" data-value="5"></span></div></div>'''
    assert HAIKU in parser._parse_structured(BeautifulSoup(html, "html.parser"))
    assert parser.parse(html) == {}



@pytest.mark.parametrize("reason", ["provider-canary-failed", "operator-safety-hold"])
def test_price_hold_does_not_override_discovery_or_operator_safety_holds(
    tmp_path: Path, reason: str,
) -> None:
    from scripts.pricing.manifest import write_discovered_chat_manifest

    manifest = tmp_path / "test.json"
    profile = _manifest_profile(True, 1_000_000)
    rows = [{"id": HAIKU, **profile},
            *[{"id": f"example/sibling-{i}", **_manifest_profile(False, 1_000_000)}
              for i in range(3)]]
    manifest.write_text(json.dumps({"provider": "anthropic", "models": rows}))
    discovered = {row["id"]: {"id": row["id"]} for row in rows}
    if reason == "provider-canary-failed":
        discovered[HAIKU].update(routable=False, routable_reason=reason)
    result = ProviderPricingResult(slug="anthropic", source="api",
                                   prices={row["id"]: _new_price(False) for row in rows})
    write_discovered_chat_manifest(
        result, manifest_path=manifest, discovered_rows=discovered,
        source_url="https://example.test",
        operator_hold_reasons={HAIKU: reason} if reason == "operator-safety-hold" else None,
    )
    actual = json.loads(manifest.read_text())["models"][0]
    assert actual == {"id": HAIKU, **profile, "routable": False, "routable_reason": reason}


def test_flat_repeated_input_output_blocks_under_unknown_context_headings() -> None:
    lines = ["Haiku 5.5", "Standard context", "Input", "$1", "Output", "$5",
             "Long context", "Input", "$2", "Output", "$10"]
    assert parser.parse(_html(lines)) == {}


@pytest.mark.parametrize("text", [
    "Our fastest model, with up to 1M tokens of context for long documents",
    "Handles codebases over 200K tokens and still answers in real time",
    "Best for agents: more than 100K tokens of working memory in every request",
])
def test_a_description_mentioning_a_context_size_keeps_the_row(text: str) -> None:
    # A sentence about the model's context is not a price tier. Dropping the
    # row for it would leave the model with stale prices after a copy change.
    expected = {"prompt_micro_per_m": 1_000_000, "completion_micro_per_m": 5_000_000}
    html = _structured_card('<p>' + escape(text) + '</p>'
                            + _price_row("Input", "$1") + _price_row("Output", "$5"))
    assert parser.parse(html)[HAIKU] == expected
    assert _parse_card(["Haiku 5.5", text, "Input", "$1", "/ MTok", "Output", "$5", "/ MTok"])[HAIKU] == expected


@pytest.mark.parametrize("text", [
    "Our fastest model, with up to 1M tokens of context for long documents",
    "Applies to prompts over 200K tokens in a single request to this model",
])
def test_the_same_sentence_inside_the_price_area_drops_the_row(text: str) -> None:
    # After the first label the card is describing prices, so threshold
    # wording of any length means the single price cannot be trusted.
    assert HAIKU not in _parse_card(["Haiku 5.5", "Input", "$1", "/ MTok", text, "Output", "$5", "/ MTok"])
    html = _structured_card(_price_row("Input", "$1") + '<p>' + escape(text) + '</p>'
                            + _price_row("Output", "$5"))
    assert HAIKU not in parser.parse(html)
