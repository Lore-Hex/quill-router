"""Refresh speech tariffs independently of the language-model pricing pipeline.

Every price page is one source. A source that fails to fetch, parse, or pass
the price-change review keeps its last published prices and is reported under
the same "Held for price review" heading refresh.py uses, while the other
sources publish. A strict majority of sources failing publishes nothing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag

from scripts.pricing.base import safe_exception_summary
from trusted_router.pricing_approvals import approved_dated_price_transition

HELD_FOR_REVIEW_HEADING = "Held for price review (last published prices kept):"
SOURCES = {
    "x-ai/grok-voice-tts-1.0": "https://docs.x.ai/developers/models/text-to-speech",
    "mistralai/voxtral-mini-tts-2603": "https://docs.mistral.ai/inference/pricing",
}
SNAPSHOT = Path(__file__).resolve().parents[2] / "src/trusted_router/data/speech_pricing.json"
GEMINI_URL = "https://ai.google.dev/gemini-api/docs/pricing"
ELEVENLABS_URL = "https://elevenlabs.io/pricing/api"
MICROSOFT_URL = "https://microsoft.ai/news/our-first-streaming-transcription-model/"
TINFOIL_URL = "https://inference.tinfoil.sh/v1/models"


def parse_tinfoil_request_prices(raw: str) -> dict[str, int]:
    payload = json.loads(raw, parse_float=Decimal)
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Missing Tinfoil speech catalog")
    names = {"qwen3-tts": "qwen/qwen3-tts", "voxtral-tts": "mistralai/voxtral-4b-tts-2603"}
    prices = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("id") not in names:
            continue
        model = names[row["id"]]
        pricing = row.get("pricing")
        if (
            model in prices or row.get("type") != "tts"
            or row.get("endpoints") != ["/v1/audio/speech"]
            or not isinstance(pricing, dict)
            or pricing.get("inputTokenPricePer1M") != 0
            or pricing.get("outputTokenPricePer1M") != 0
        ):
            raise ValueError("Conflicting Tinfoil speech billing contract")
        price = pricing.get("requestPrice")
        if isinstance(price, bool) or not isinstance(price, (int, Decimal)):
            raise ValueError("Missing Tinfoil per-request tariff")
        micro = Decimal(price) * 1_000_000
        if not micro.is_finite() or micro <= 0 or micro != micro.to_integral_value():
            raise ValueError("Invalid Tinfoil per-request tariff")
        prices[model] = int(micro)
    if set(prices) != set(names.values()):
        raise ValueError("Incomplete Tinfoil speech tariffs")
    return prices


def parse_elevenlabs_prices(html: str) -> dict[str, int]:
    soup = BeautifulSoup(html, "html.parser")
    prices = {}
    for name, models in (
        ("v3", ("eleven-v3",)),
        ("v2 Multilingual", ("eleven-multilingual-v2",)),
        ("Flash / Turbo", ("eleven-flash-v2.5", "eleven-turbo-v2.5")),
    ):
        rates = set()
        for heading in soup.find_all("h3", string=name):
            for parent in heading.parents:
                text = parent.get_text(" ", strip=True)
                if "Price per 1K characters" not in text:
                    continue
                # Use the smallest price card, not the whole pricing page or
                # promotional v4 card. Multiple dollar amounts are ambiguous.
                matches = re.findall(r"\$(\d+(?:\.\d+)?)", text)
                if len(matches) != 1 or "Text to Speech" not in text:
                    raise ValueError(f"Conflicting ElevenLabs tariff: {name}")
                rate = Decimal(matches[0]) * 1000 * 1_000_000
                if rate <= 0 or rate != rate.to_integral_value():
                    raise ValueError("Invalid ElevenLabs tariff")
                rates.add(int(rate))
                break
        if len(rates) != 1:
            raise ValueError(f"Missing ElevenLabs character tariff: {name}")
        rate = rates.pop()
        prices.update({"elevenlabs/" + model: rate for model in models})
    return prices


def parse_microsoft_prices(html: str) -> dict[str, int]:
    # Bind each rate to its own article section, not a neighboring model.
    soup = BeautifulSoup(html, "html.parser")
    prices = {}
    for label, model in (
        ("MAI-Voice-2.1", "mai-voice-2.1"),
        ("MAI-Voice-2.1-Flash", "mai-voice-2.1-flash"),
    ):
        amounts = []
        for heading in soup.select("p.text-lead"):
            if heading.get_text(" ", strip=True).split(":", 1)[0] != label:
                continue
            for element in heading.next_siblings:
                if not isinstance(element, Tag):
                    continue
                if element.name in {"h2", "h3", "h4"} or "text-lead" in element.get("class", []):
                    break
                amounts.extend(re.findall(
                    r"\$(\d+(?:\.\d+)?) per 1M characters\b",
                    element.get_text(" ", strip=True),
                ))
        if len(amounts) != 1:
            raise ValueError(f"Missing or conflicting Microsoft speech tariff: {model}")
        rate = Decimal(amounts.pop()) * 1_000_000
        if rate <= 0 or rate != rate.to_integral_value():
            raise ValueError("Invalid Microsoft speech tariff")
        prices["microsoft/" + model] = int(rate)
    return prices


def parse_gemini_token_prices(html: str, *, today: date | None = None) -> dict[str, dict[str, int]]:
    """Read only Standard TTS tables, respecting Google's dated price schedule."""
    today = today or datetime.now(UTC).date()
    soup = BeautifulSoup(html, "html.parser")
    prices: dict[str, dict[str, int]] = {}
    for name, model in (
        ("Gemini 3.8 Flash TTS", "gemini-3.8-flash-tts"),
        ("Gemini 3.8 Flash-Lite TTS", "gemini-3.8-flash-lite-tts"),
    ):
        heading = next(
            (h for h in soup.find_all("h2") if h.get_text(" ", strip=True) == name), None
        )
        if heading is None:
            raise ValueError(f"Missing Gemini speech section: {model}")
        table = None
        standard = False
        for element in heading.find_all_next(["h2", "h3", "table"]):
            if element.name == "h2":
                break
            if element.name == "h3":
                standard = element.get_text(" ", strip=True) == "Standard"
            if element.name == "table" and standard:
                table = element
                break
        if table is None or "per 1M tokens in USD" not in table.get_text(" ", strip=True):
            raise ValueError(f"Missing Standard per-token speech pricing: {model}")
        rates: dict[str, int] = {}
        for row in table.select("tr"):
            cells = [
                c.get_text(" ", strip=True) for c in row.find_all(["td", "th"], recursive=False)
            ]
            if len(cells) != 3 or cells[0] not in {"Input price", "Output price"}:
                continue
            kind = "input" if cells[0] == "Input price" else "output"
            modality = "text" if kind == "input" else "audio"
            candidates: set[int] = set()
            pattern = rf"\$(\d+(?:\.\d+)?)\s*\({modality}\)(?:\s+(through|starting)\s+([A-Z][a-z]+ \d{{1,2}}, \d{{4}})\.)?"
            matches = list(re.finditer(pattern, cells[2]))
            if not matches:
                raise ValueError(f"Unrecognized Gemini speech tariff: {model}")
            for match in matches:
                direction, boundary = match[2], match[3]
                when = datetime.strptime(boundary, "%B %d, %Y").date() if boundary else None
                if when and (
                    (direction == "through" and today > when)
                    or (direction == "starting" and today < when)
                ):
                    continue
                micro = Decimal(match[1]) * 1_000_000
                if micro <= 0 or micro != micro.to_integral_value():
                    raise ValueError("Invalid Gemini speech tariff")
                candidates.add(int(micro))
            if len(candidates) != 1 or kind in rates:
                raise ValueError(f"Conflicting or expired Gemini speech tariff: {model}")
            rates[kind] = candidates.pop()
        if set(rates) != {"input", "output"}:
            raise ValueError(f"Incomplete Gemini speech tariff: {model}")
        prices["google/" + model] = rates
    return prices


def parse_character_price(model: str, html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    found: set[int] = set()
    for row in soup.select("table tr"):
        cells = [
            cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"], recursive=False)
        ]
        if model == "mistralai/voxtral-mini-tts-2603":
            if len(cells) != 4 or cells[0].removesuffix(" \u2197") != "Voxtral TTS":
                continue
            table = row.find_parent("table")
            headers = [cell.get_text(" ", strip=True) for cell in table.find_all("th")] if table else []
            if headers:
                if len(headers) != 4 or set(headers) != {"Model", "Input", "Cached input", "Output"}:
                    raise ValueError("Unrecognized Mistral speech pricing columns")
                text = cells[headers.index("Input")]
            else:
                # Legacy table placed the character tariff in Output.
                text = cells[3]
        elif model == "x-ai/grok-voice-tts-1.0":
            if len(cells) != 2 or cells[0] not in {"Pricing", "Per 1M chars"}:
                continue
            text = cells[1]
        else:
            raise ValueError("unimplemented speech tariff")
        match = re.fullmatch(r"\$(\d+(?:\.\d+)?)\s*/\s*(?:1M|M)\s+[Cc]hars", text)
        if match is None:
            # Name the cell so the hold reason says what the page served.
            raise ValueError(f"Unrecognized speech billing unit for {model}: {text!r}")
        micro = Decimal(match[1]) * 1_000_000
        if micro <= 0 or micro != micro.to_integral_value():
            raise ValueError(f"Invalid speech tariff for {model}")
        found.add(int(micro))
    if len(found) != 1:
        raise ValueError(f"Missing or conflicting speech tariffs for {model}")
    return found.pop()


# Section of the snapshot each source refreshes, keyed by model id.
Updates = dict[str, dict[str, Any]]
_SECTIONS = ("prices", "token_prices", "request_prices")


def _fetch_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"} if url == ELEVENLABS_URL else {}
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def _character_source(model: str) -> Updates:
    return {"prices": {model: parse_character_price(model, _fetch_text(SOURCES[model]))}}


def _sources(today: date) -> list[tuple[str, Callable[[], Updates]]]:
    return [
        ("mistral", lambda: _character_source("mistralai/voxtral-mini-tts-2603")),
        ("xai", lambda: _character_source("x-ai/grok-voice-tts-1.0")),
        ("elevenlabs", lambda: {"prices": parse_elevenlabs_prices(_fetch_text(ELEVENLABS_URL))}),
        ("microsoft", lambda: {"prices": parse_microsoft_prices(_fetch_text(MICROSOFT_URL))}),
        ("gemini", lambda: {
            "token_prices": parse_gemini_token_prices(_fetch_text(GEMINI_URL), today=today),
        }),
        ("tinfoil", lambda: {
            "request_prices": parse_tinfoil_request_prices(_fetch_text(TINFOIL_URL)),
        }),
    ]


def _review(before: dict[str, Any], updates: Updates, today: date) -> None:
    """Reject a 2x move in either direction unless a dated transition approves it."""
    for section, rows in updates.items():
        for model, price in rows.items():
            previous = before[section].get(model)
            if previous is None:
                raise ValueError(f"No committed speech price for {model}")
            if section != "token_prices":
                if price >= previous * 2 or price * 2 <= previous:
                    raise ValueError(f"Speech price change requires review: {model}")
                continue
            approved_transition = approved_dated_price_transition(
                f"{model} [google-ai-studio:google-ai-studio:{model.removeprefix('google/')}]",
                (previous["input"], previous["output"]),
                (price["input"], price["output"]),
                today=today,
            )
            for kind, rate in price.items():
                old = previous[kind]
                if (rate >= old * 2 or rate * 2 <= old) and not approved_transition:
                    raise ValueError(f"Speech price change requires review: {model} {kind}")


def _hold_reason(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        # Parser and review messages are this module's own text and every
        # speech price page is public, so say exactly what did not parse.
        return f"ValueError: {exc}"
    return safe_exception_summary(exc)


def refresh() -> int:
    today = datetime.now(UTC).date()
    before = json.loads(SNAPSHOT.read_text())
    if before.get("request_price_unit") != "microdollars_per_request":
        raise ValueError("Wrong speech request price unit")
    sources = _sources(today)
    after: dict[str, dict[str, Any]] = {section: dict(before[section]) for section in _SECTIONS}
    refreshed: dict[str, list[str]] = {}
    held: dict[str, str] = {}
    for slug, load in sources:
        try:
            updates = load()
            _review(before, updates, today)
        except Exception as exc:  # noqa: BLE001 - one source's failure holds only its own models
            held[slug] = f"refresh failed: {_hold_reason(exc)}"
            continue
        for section, rows in updates.items():
            after[section].update(rows)
        refreshed[slug] = sorted(model for rows in updates.values() for model in rows)

    if len(held) * 2 > len(sources):
        print(
            f"Systemic speech refresh failure: {len(held)}/{len(sources)} sources failed; "
            "nothing published."
        )
        _print_held(held)
        return 1

    snapshot = {
        "checked_on": today.isoformat(),
        "unit": "microdollars_per_million_input_characters",
        **after,
        "request_price_unit": "microdollars_per_request",
    }
    SNAPSHOT.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    print(f"Speech price refresh — {len(sources)} sources, {len(held)} held")
    for slug, models in refreshed.items():
        print(f"  {slug}: refreshed {', '.join(models)}")
    if held:
        _print_held(held)
        print()
        print(
            "::warning title=Speech prices held for review::"
            f"{', '.join(sorted(held))} kept their committed prices. Repair the fetch or "
            "parser, or review the price change."
        )
    return 0


def _print_held(held: dict[str, str]) -> None:
    print()
    print(HELD_FOR_REVIEW_HEADING)
    for slug in sorted(held):
        print(f"  {slug}:")
        print(f"    {held[slug]}")


if __name__ == "__main__":
    raise SystemExit(refresh())
