"""Parse Cohere's embedded first-party pricing data."""

from __future__ import annotations

import re
from decimal import Decimal


def _microdollars_per_million(value: str) -> int:
    return int((Decimal(value) * Decimal(1_000_000)).to_integral_value())


def parse(html: str) -> dict[str, dict[str, int]]:
    # Next.js serializes the Sanity pricing payload into script text with
    # escaped quotes. Normalize only that representation, then scope the
    # price lookup to the Embed 4 record so Model Vault instance rates and
    # image prices cannot be mistaken for text-token pricing.
    text = html.replace(r'\"', '"')
    record = re.search(
        r'"modelName":"Embed 4","per":"1M tokens"'
        r'.{0,8000}?"pricings":\[(.*?)\]',
        text,
        re.DOTALL,
    )
    if record is None:
        return {}
    price = re.search(
        r'"inputLabel":"Cost","inputPrice":([0-9]+(?:\.[0-9]+)?)',
        record.group(1),
    )
    if price is None:
        return {}
    return {
        "cohere/embed-v4.0": {
            "prompt_micro_per_m": _microdollars_per_million(price.group(1)),
            "completion_micro_per_m": 0,
        }
    }
