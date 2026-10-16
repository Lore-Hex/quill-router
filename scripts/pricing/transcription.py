"""Guarded transcription tariffs; never reinterpret audio prices as token rates."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

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


def refresh() -> None:
    before = json.loads(SNAPSHOT.read_text())
    if before.get("unit") != "microdollars":
        raise ValueError("Invalid transcription price unit")
    after: dict[str, Any] = {"unit": "microdollars", "minute_prices": {}, "request_prices": {}, "token_prices": {}}
    fx_response = requests.get(ECB_FX_URL, timeout=30)
    fx_response.raise_for_status()
    rate = usd_per_eur(fx_response.text)
    for model, spec in TRANSCRIPTION_MODELS.items():
        response = requests.get(spec.pricing_source, timeout=30)
        response.raise_for_status()
        if spec.token_billed:
            token_prices = parse_token_prices(response.text)
            for kind, price in token_prices.items():
                previous = before["token_prices"][model][kind]
                if price >= previous * 2 or price * 2 <= previous:
                    raise ValueError(f"Transcription token price change requires review: {model} {kind}")
            after["token_prices"][model] = token_prices
            continue
        price = parse_price(model, response.text, usd_eur_rate=rate)
        kind = "request_prices" if spec.request_cost_microdollars else "minute_prices"
        previous = before[kind][model]
        if price >= previous * 2 or price * 2 <= previous:
            raise ValueError(f"Transcription price change requires review: {model}")
        after[kind][model] = price
    after["checked_on"] = datetime.now(UTC).date().isoformat()
    SNAPSHOT.write_text(json.dumps(after, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    refresh()
