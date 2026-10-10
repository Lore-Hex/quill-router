"""Refresh speech tariffs independently of the language-model pricing pipeline."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SOURCES = {
    "x-ai/grok-voice-tts-1.0": "https://docs.x.ai/developers/models/text-to-speech",
    "mistralai/voxtral-mini-tts-2603": "https://docs.mistral.ai/inference/pricing",
}
SNAPSHOT = Path(__file__).resolve().parents[2] / "src/trusted_router/data/speech_pricing.json"
GEMINI_URL = "https://ai.google.dev/gemini-api/docs/pricing"
ELEVENLABS_URL = "https://elevenlabs.io/pricing/api"
MICROSOFT_URL = "https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/build-expressive-voice-experiences-with-new-mai-models-in-microsoft-foundry/4524637"

# Reviewed Standard TTS transition published at GEMINI_URL. Unlike the
# provider-specific runtime schedules in provider_lifecycle, speech snapshots
# store only current rates. Approve these exact old/new pairs from the dated
# cutover onward (including a delayed refresh), never arbitrary 2x changes.
_GEMINI_2027_EFFECTIVE_ON = date(2027, 1, 1)
_GEMINI_2027_TRANSITIONS = {
    "google/gemini-3.8-flash-tts": (
        {"input": 500_000, "output": 9_000_000},
        {"input": 1_000_000, "output": 18_000_000},
    ),
    "google/gemini-3.8-flash-lite-tts": (
        {"input": 500_000, "output": 6_000_000},
        {"input": 1_000_000, "output": 12_000_000},
    ),
}


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
    # The official announcement embeds its article in structured page state.
    # Match the explicit model + billing unit, not unrelated dollar figures.
    prices = {}
    for label, model in (
        ("MAI-Voice-2.1", "mai-voice-2.1"),
        ("MAI-Voice-2.1 Flash", "mai-voice-2.1-flash"),
    ):
        amounts = set(
            re.findall(
                re.escape(label) + r" is available at \$(\d+(?:\.\d+)?) per 1M characters", html
            )
        )
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
            if len(cells) != 4 or not cells[0].startswith("Voxtral TTS"):
                continue
            text = cells[3]
        elif model == "x-ai/grok-voice-tts-1.0":
            if len(cells) != 2 or cells[0] not in {"Pricing", "Per 1M chars"}:
                continue
            text = cells[1]
        else:
            raise ValueError("unimplemented speech tariff")
        match = re.fullmatch(r"\$(\d+(?:\.\d+)?)\s*/\s*(?:1M|M)\s+[Cc]hars", text)
        if match is None:
            raise ValueError(f"Unrecognized speech billing unit for {model}")
        micro = Decimal(match[1]) * 1_000_000
        if micro <= 0 or micro != micro.to_integral_value():
            raise ValueError(f"Invalid speech tariff for {model}")
        found.add(int(micro))
    if len(found) != 1:
        raise ValueError(f"Missing or conflicting speech tariffs for {model}")
    return found.pop()


def refresh() -> None:
    today = datetime.now(UTC).date()
    before = json.loads(SNAPSHOT.read_text())
    prices = {}
    for model, url in SOURCES.items():
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        price = parse_character_price(model, response.text)
        previous = before["prices"][model]
        if price >= previous * 2 or price * 2 <= previous:
            raise ValueError(f"Speech price change requires review: {model}")
        prices[model] = price
    for url, parser in (
        (ELEVENLABS_URL, parse_elevenlabs_prices),
        (MICROSOFT_URL, parse_microsoft_prices),
    ):
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        response.raise_for_status()
        for model, price in parser(response.text).items():
            previous = before["prices"][model]
            if price >= previous * 2 or price * 2 <= previous:
                raise ValueError(f"Speech price change requires review: {model}")
            prices[model] = price
    response = requests.get(GEMINI_URL, timeout=30)
    response.raise_for_status()
    token_prices = parse_gemini_token_prices(response.text, today=today)
    for model, rates in token_prices.items():
        approved_transition = (
            today >= _GEMINI_2027_EFFECTIVE_ON
            and (before["token_prices"][model], rates) == _GEMINI_2027_TRANSITIONS.get(model)
        )
        for kind, price in rates.items():
            previous = before["token_prices"][model][kind]
            if (price >= previous * 2 or price * 2 <= previous) and not approved_transition:
                raise ValueError(f"Speech price change requires review: {model} {kind}")
    # Fetch and validate every rate before touching the last-known-good file.
    snapshot = {
        "checked_on": today.isoformat(),
        "unit": "microdollars_per_million_input_characters",
        "prices": prices,
        "token_prices": token_prices,
    }
    SNAPSHOT.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    refresh()
