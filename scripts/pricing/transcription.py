"""Guarded, source-isolated transcription tariffs with explicit review holds."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from scripts.pricing.base import safe_exception_summary
from scripts.pricing.currency import ECB_FX_URL, eur_microdollars_per_million, usd_per_eur
from trusted_router.transcription import TRANSCRIPTION_MODELS

SNAPSHOT = Path(__file__).resolve().parents[2] / "src/trusted_router/data/transcription_pricing.json"


def parse_token_prices(raw: str) -> dict[str, int]:
    payload = json.loads(raw, parse_float=Decimal)
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Invalid Tinfoil model list")
    matches = [row for row in rows if isinstance(row, dict) and row.get("id") == "voxtral-small-24b"]
    if len(matches) != 1:
        raise ValueError("Missing or duplicate Tinfoil audio model")
    row = matches[0]
    price = row["pricing"]
    if row.get("type") != "audio" or "/v1/chat/completions" not in row.get("endpoints", []) or price.get("requestPrice") != 0:
        raise ValueError("Changed Tinfoil audio token contract")
    result = {}
    for kind, field in (("input", "inputTokenPricePer1M"), ("output", "outputTokenPricePer1M")):
        amount = price[field]
        if isinstance(amount, bool) or not isinstance(amount, (int, Decimal)):
            raise ValueError("Invalid audio token price")
        micro = Decimal(amount) * 1_000_000
        if not micro.is_finite() or micro <= 0 or micro != micro.to_integral_value():
            raise ValueError("Invalid audio token price")
        result[kind] = int(micro)
    return result


def parse_price(model: str, raw: str, *, usd_eur_rate: Decimal | None = None) -> int:
    if model in {"openai/whisper-large-v3", "mistralai/voxtral-mini-3b-2507"}:
        if usd_eur_rate is None:
            raise ValueError("Privatemode audio requires an explicit EUR/USD reference rate")
        name = "Whisper large-v3" if model == "openai/whisper-large-v3" else "Voxtral Mini 3B"
        amounts = []
        for table in BeautifulSoup(raw, "html.parser").find_all("table"):
            headers = [cell.get_text(" ", strip=True) for cell in table.find_all("th")]
            if headers != ["Model", "Audio"]:
                continue
            for row in table.find_all("tr"):
                cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
                if cells and cells[0] == name:
                    if len(cells) != 2 or re.fullmatch(r"EUR [0-9]+(?:\.[0-9]+)?", cells[1]) is None:
                        raise ValueError("Invalid Privatemode audio tariff")
                    amounts.append(Decimal(cells[1].split()[1]))
        if len(amounts) != 1 or amounts[0] <= 0 or "per audio minute" not in BeautifulSoup(raw, "html.parser").get_text(" ", strip=True):
            raise ValueError("Missing or duplicate per-minute audio tariff")
        return eur_microdollars_per_million(amounts[0], usd_eur_rate)
    if model == "openai/whisper-large-v3-turbo":
        payload = json.loads(raw, parse_float=Decimal)
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("Invalid Tinfoil model list")
        matches = [row for row in rows if isinstance(row, dict) and row.get("id") == "whisper-large-v3-turbo"]
        if len(matches) != 1:
            raise ValueError("Missing or duplicate Tinfoil transcription model")
        row = matches[0]
        pricing = row["pricing"]
        if row.get("type") != "audio" or row.get("endpoints") != ["/v1/audio/transcriptions"] or pricing.get("inputTokenPricePer1M") != 0 or pricing.get("outputTokenPricePer1M") != 0:
            raise ValueError("Changed Tinfoil transcription contract")
        amount = pricing["requestPrice"]
        if isinstance(amount, bool) or not isinstance(amount, (int, Decimal)):
            raise ValueError("Invalid request price")
        values = {Decimal(amount)}
    else:
        text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
        if model == "openai/whisper-1":
            matches = re.findall(r"Transcription\s+Cost\s*/\s*minute\s+\$(\d+(?:\.\d+)?)", text)
        elif model == "mistralai/voxtral-mini-2602" and "voxtral-mini-2602" in text:
            matches = re.findall(r"\$(\d+(?:\.\d+)?)\s*/\s*Min\b", text)
        else:
            raise ValueError("Unknown transcription tariff")
        values = {Decimal(value) for value in matches}
    if len(values) != 1:
        raise ValueError("Missing or conflicting transcription tariff")
    micro = values.pop() * 1_000_000
    if not micro.is_finite() or micro <= 0 or micro != micro.to_integral_value():
        raise ValueError("Invalid transcription tariff")
    return int(micro)


def _fetch_text(url: str) -> str:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def _source_prices(provider: str, before: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {"minute_prices": {}, "request_prices": {}, "token_prices": {}}
    rate = usd_per_eur(_fetch_text(ECB_FX_URL)) if provider == "privatemode" else None
    pages: dict[str, str] = {}
    for model, spec in TRANSCRIPTION_MODELS.items():
        if spec.provider != provider:
            continue
        if spec.pricing_source not in pages:
            pages[spec.pricing_source] = _fetch_text(spec.pricing_source)
        raw = pages[spec.pricing_source]
        if spec.token_billed:
            token_prices = parse_token_prices(raw)
            for kind, price in token_prices.items():
                previous = before["token_prices"][model][kind]
                if price >= previous * 2 or price * 2 <= previous:
                    raise ValueError(f"Transcription token price change requires review: {model} {kind}")
            updates["token_prices"][model] = token_prices
            continue
        price = parse_price(model, raw, usd_eur_rate=rate)
        kind = "request_prices" if spec.request_cost_microdollars else "minute_prices"
        previous = before[kind][model]
        if price >= previous * 2 or price * 2 <= previous:
            raise ValueError(f"Transcription price change requires review: {model}")
        updates[kind][model] = price
    return updates


def refresh() -> int:
    before = json.loads(SNAPSHOT.read_text())
    if before.get("unit") != "microdollars":
        raise ValueError("Invalid transcription price unit")
    providers = sorted({spec.provider for spec in TRANSCRIPTION_MODELS.values()})
    after = deepcopy(before)
    held: dict[str, str] = {}
    for provider in providers:
        try:
            updates = _source_prices(provider, before)
        except Exception as exc:  # noqa: BLE001 - hold the entire source, never partially publish it
            reason = f"ValueError: {exc}" if isinstance(exc, ValueError) else safe_exception_summary(exc)
            held[provider] = f"refresh failed: {reason}"
            continue
        for section, rows in updates.items():
            after[section].update(rows)

    systemic = len(held) * 2 > len(providers)
    if systemic:
        print(f"Systemic transcription refresh failure: {len(held)}/{len(providers)} sources failed; nothing published.")
    else:
        print(f"Transcription price refresh: {len(providers)} sources, {len(held)} held")
    if held:
        print("Held for price review (last published prices kept):")
        for provider, reason in held.items():
            print(f"  {provider}: {reason}")
        print()
        print("::warning title=Transcription prices held for review::"
              f"{', '.join(held)} kept their committed prices. Repair the source or review the price change.")
    if systemic:
        return 1
    # A successful partial run must not re-date the held tariffs as fresh.
    if not held:
        after["checked_on"] = datetime.now(UTC).date().isoformat()
    SNAPSHOT.write_text(json.dumps(after, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(refresh())
