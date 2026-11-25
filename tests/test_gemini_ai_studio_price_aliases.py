"""Google AI Studio rows priced from Google's published tables, not by hand.

Robotics ER 2 Preview has its own dated Standard table. The customtools
endpoint and the ``-latest`` pointers are billed at another model's published
rate, through the provider-owned alias map. Every newly priced row still has
to pass the provider canary before it is routable.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing.base import (
    ModelPrice,
    PriceTier,
    ProviderPricingResult,
    normalize_parser_input,
)
from scripts.pricing.gemini_prices import current_price_source
from scripts.pricing.parsers import gemini as parser
from scripts.pricing.providers import gemini as provider
from tests.test_gemini_markdoc_pricing import _tab, _table
from trusted_router.pricing_approvals import approved_dated_price_transition

ROBOTICS = "google/gemini-robotics-er-2-preview"
FIXTURE = Path(__file__).parent / "fixtures/pricing/gemini_aistudio_markdoc_2026-11-07.md"


def _robotics_page(heading: str = "Gemini Robotics ER 2 Preview") -> str:
    # Official Markdoc cells: a modality qualifier and "\\" hard breaks.
    return f'## {heading}  {{% id="gemini-robotics-er-2" %}}\n' + _tab(
        "Standard",
        _table(
            "$1.00 (text / image / video / audio) through December 31, 2026. \\\\\n"
            "  $2.00 (text / image / video / audio) starting January 1, 2027.",
            "$5.00 through December 31, 2026. \\\\\n  $10.00 starting January 1, 2027.",
            "$0.10 through December 31, 2026. \\\\\n  $0.20 starting January 1, 2027. \\\\\n"
            "  $0.50 / 1,000,000 tokens per hour (storage price) through December 31, 2026. \\\\\n"
            "  $1.00 / 1,000,000 tokens per hour (storage price) starting January 1, 2027.",
        ),
    )


def _parse(source: str, day: date) -> dict[str, Any]:
    return parser.parse(normalize_parser_input(current_price_source(source, today=day)))


@pytest.mark.parametrize(
    "day,factor",
    [(date(2026, 11, 24), 1), (date(2026, 12, 31), 1), (date(2027, 1, 1), 2)],
)
def test_robotics_er_2_standard_rates_follow_the_dated_schedule(day: date, factor: int) -> None:
    assert _parse(_robotics_page(), day)[ROBOTICS] == {
        "prompt_micro_per_m": factor * 1_000_000,
        "completion_micro_per_m": factor * 5_000_000,
        "prompt_cached_micro_per_m": factor * 100_000,
    }


def test_robotics_er_2_streaming_variant_stays_unpriced() -> None:
    page = _robotics_page("Gemini Robotics ER 2 Streaming Preview")
    assert _parse(page, date(2026, 11, 24)) == {}


def test_robotics_er_2_discontinuous_schedule_fails_closed() -> None:
    page = _robotics_page().replace("January 1, 2027", "January 2, 2027")
    with pytest.raises(ValueError, match="gemini:"):
        _parse(page, date(2026, 11, 24))


def test_official_page_prices_robotics_er_2_and_keeps_media_out() -> None:
    prices = _parse(FIXTURE.read_text(encoding="utf-8"), date(2026, 11, 7))
    assert prices[ROBOTICS] == {
        "prompt_micro_per_m": 1_000_000,
        "completion_micro_per_m": 5_000_000,
        "prompt_cached_micro_per_m": 100_000,
    }
    assert not [
        model_id for model_id in prices
        if any(media in model_id for media in ("omni", "tts", "image", "transcribe", "streaming"))
    ]


@pytest.mark.parametrize("suffix,before,after", [
    ("", (1_000_000, 5_000_000), (2_000_000, 10_000_000)),
    (" cached-input", (100_000, 0), (200_000, 0)),
])
def test_robotics_er_2_cutover_is_approved_only_from_january_2027(
    suffix: str, before: tuple[int, int], after: tuple[int, int],
) -> None:
    route = f"{ROBOTICS} [google-ai-studio:google-ai-studio:gemini-robotics-er-2-preview]{suffix}"
    assert not approved_dated_price_transition(route, before, after, today=date(2026, 12, 31))
    assert approved_dated_price_transition(route, before, after, today=date(2027, 1, 1))
    assert not approved_dated_price_transition(
        route, before, (after[0] * 2, after[1] * 2), today=date(2027, 1, 1),
    )


PRO = ModelPrice(tiers=[
    PriceTier(200_000, 2_000_000, 12_000_000, 200_000),
    PriceTier(None, 4_000_000, 18_000_000, 400_000),
])
FLASH = ModelPrice(500_000, 3_000_000, prompt_cached_micro_per_m=50_000)


def _fetch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    live: set[str],
    parsed: dict[str, ModelPrice],
) -> tuple[ProviderPricingResult, dict[str, dict[str, Any]]]:
    manifest = tmp_path / "google-ai-studio.json"
    manifest.write_text(json.dumps({"models": [
        {"id": model_id, "routable": False, "routable_reason": "awaiting-price"}
        for model_id in provider.PRICE_ALIASES
    ]}))
    monkeypatch.setattr(provider, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(provider, "_live_model_rows", lambda: {
        model_id: {"id": model_id, "upstream_id": model_id.removeprefix("google/")}
        for model_id in live
    })
    monkeypatch.setattr(provider, "fetch_provider", lambda **_kwargs: ProviderPricingResult(
        slug="gemini", prices=dict(parsed), source="deterministic",
    ))
    probed: list[str] = []

    def probe(*, api_key: str | None, native_id: str) -> bool:
        probed.append(native_id)
        return False

    monkeypatch.setattr(provider, "_probe_generate_content", probe)
    result = provider.fetch()
    return result, provider._DISCOVERED_MANIFEST_ROWS


def test_aliases_take_the_published_source_price_and_still_need_a_canary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    live = {
        "google/gemini-3.1-pro-preview", "google/gemini-3-flash-preview",
        *provider.PRICE_ALIASES, "google/gemini-flash-lite-latest",
    }
    parsed = {"google/gemini-3.1-pro-preview": PRO, "google/gemini-3-flash-preview": FLASH}
    result, rows = _fetch(monkeypatch, tmp_path, live=live, parsed=parsed)

    assert result.prices["google/gemini-3.1-pro-preview-customtools"].tiers == PRO.tiers
    assert result.prices["google/gemini-pro-latest"].tiers == PRO.tiers
    assert result.prices["google/gemini-flash-latest"].tiers == FLASH.tiers
    # Google publishes no target for this pointer, so it gets no price.
    assert "google/gemini-flash-lite-latest" not in result.prices
    assert any(note.startswith("applied approved price aliases:") for note in result.notes)
    # A first price never makes an awaiting-price row routable by itself.
    for alias in provider.PRICE_ALIASES:
        assert rows[alias]["routable"] is False
        assert rows[alias]["routable_reason"] == "provider-canary-failed"


def test_alias_needs_a_live_target_row_and_a_live_source_price(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # The flash pointer is not in Google's feed; the Pro source has no price.
    live = {"google/gemini-3-flash-preview", "google/gemini-pro-latest",
            "google/gemini-3.1-pro-preview"}
    result, _rows = _fetch(
        monkeypatch, tmp_path, live=live,
        parsed={"google/gemini-3-flash-preview": FLASH},
    )
    assert set(result.prices) == {"google/gemini-3-flash-preview"}


def test_alias_map_targets_only_rows_google_bills_as_chat() -> None:
    # Every source must be a parsed chat price and no alias may chain.
    assert set(provider.PRICE_ALIASES.values()).isdisjoint(provider.PRICE_ALIASES)
    for alias, source in provider.PRICE_ALIASES.items():
        assert alias.startswith("google/gemini-") and source.startswith("google/gemini-")
        assert parser._model_id_from_heading(
            {"google/gemini-3.1-pro-preview": "Gemini 3.1 Pro Preview",
             "google/gemini-3-flash-preview": "Gemini 3 Flash Preview"}[source]
        ) == source
