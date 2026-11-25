"""Parse Cohere's embedded first-party pricing data."""

from __future__ import annotations

import json
import re
from decimal import Decimal


def _microdollars_per_million(value: str) -> int:
    return int((Decimal(value) * Decimal(1_000_000)).to_integral_value())


def parse_rerank(html: str) -> dict[str, dict[str, int]]:
    """Read token prices from individual Rerank records."""
    text = html.replace(r'\"', '"')
    prices: dict[str, dict[str, int]] = {}
    decoder = json.JSONDecoder(parse_float=Decimal)
    for match in re.finditer(r'"modelName":"(Rerank [^"]+)"', text):
        # Stop before the next model even if this record is incomplete.
        # Decoding the object also stops at its own closing brace, so a
        # neighbouring price cannot supply a missing pricings member.
        end = text.find('"modelName":', match.end())
        record_text = text[match.start():end if end != -1 else len(text)]
        try:
            record, _ = decoder.raw_decode("{" + record_text)
        except ValueError:
            continue
        if record.get("per") != "1M tokens" or "overridePer" in record:
            continue
        rows = record.get("pricings")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or "overridePer" in row for row in rows
        ):
            continue
        for row in rows:
            cost = row.get("inputPrice")
            if row.get("inputLabel") != "Cost" or isinstance(cost, bool) or not isinstance(cost, (int, Decimal)):
                continue
            amount = _microdollars_per_million(str(cost))
            if amount > 0:
                model_id = "cohere/" + match.group(1).lower().replace(" ", "-")
                prices[model_id] = {"prompt_micro_per_m": amount, "completion_micro_per_m": 0}
            break
    return prices


def parse(html: str) -> dict[str, dict[str, int]]:
    # Next.js serializes the Sanity pricing payload into script text with
    # escaped quotes. Normalize only that representation, then scope the
    # price lookup to token-priced Embed 4 and Rerank records so instance rates and
    # image prices cannot be mistaken for text-token pricing.
    text = html.replace(r'\"', '"')
    record = re.search(
        r'"modelName":"Embed 4","per":"1M tokens"'
        r'.{0,8000}?"pricings":\[(.*?)\]',
        text,
        re.DOTALL,
    )
    prices = parse_rerank(html)
    if record is None:
        priced = re.findall(r'"modelName":"([^"]+)","per":"1M tokens"', text)
        if priced and "Embed 4" not in priced:
            # A missing published rate cannot be recovered by self-heal.
            # The provider's legacy pin calls parse_rerank separately.
            raise ValueError(
                "cohere: cohere.com/pricing no longer publishes an Embed 4 rate "
                f"(priced per 1M tokens: {sorted(set(priced))[:6]})"
            )
        return prices
    price = re.search(
        r'"inputLabel":"Cost","inputPrice":([0-9]+(?:\.[0-9]+)?)',
        record.group(1),
    )
    if price is None:
        return prices
    prices["cohere/embed-v4.0"] = {
        "prompt_micro_per_m": _microdollars_per_million(price.group(1)),
        "completion_micro_per_m": 0,
    }
    return prices
