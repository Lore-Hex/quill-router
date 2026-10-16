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

from trusted_router.transcription import TRANSCRIPTION_MODELS

SNAPSHOT = Path(__file__).resolve().parents[2] / "src/trusted_router/data/transcription_pricing.json"


def parse_price(model: str, raw: str) -> int:
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
    after: dict[str, Any] = {"unit": "microdollars", "minute_prices": {}, "request_prices": {}}
    for model, spec in TRANSCRIPTION_MODELS.items():
        response = requests.get(spec.pricing_source, timeout=30)
        response.raise_for_status()
        price = parse_price(model, response.text)
        kind = "request_prices" if spec.request_cost_microdollars else "minute_prices"
        previous = before[kind][model]
        if price >= previous * 2 or price * 2 <= previous:
            raise ValueError(f"Transcription price change requires review: {model}")
        after[kind][model] = price
    after["checked_on"] = datetime.now(UTC).date().isoformat()
    SNAPSHOT.write_text(json.dumps(after, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    refresh()
