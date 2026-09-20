"""TypeSafe AI: the Jev decision model, input-only pricing, read from the vendor.

TypeSafe publishes Jev's rate on its public models page as one markdown table
row (``| Price (per Btok / per Mtok) | \\$42 / \\$0.042 |``) plus one sentence
that fixes the shape ("Charged per input token. Output tokens are free.").

This parser reads both and refuses to guess. The per-billion and per-million
figures must agree with each other, the sentence must still be there, and the
page must price exactly one model. If TypeSafe starts metering output, prices a
second model, or moves the table, the refresh fails loudly and the last-known-
good price in the manifest stays in force.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from scripts.pricing.base import ModelPrice, ProviderPricingResult

SLUG = "typesafe"
URL = "https://docs.typesafe.ai/models.md"
EXPECTED_MODELS = ["typesafe-ai/jev"]
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/typesafe.json"
)
# Fixed-shape input-only rows are not comparable with chat token prices.
INCLUDE_IN_PRICE_INDEX = False

_AMOUNT = r"\\?\$([0-9][0-9,]*(?:\.[0-9]+)?)"
_PRICE_LABEL = "Price (per Btok / per Mtok)"
_PRICE_PAIR = re.compile(_AMOUNT + r"\s*/\s*" + _AMOUNT)
_INPUT_ONLY = re.compile(r"charged per input token\.\s+output tokens are free\.", re.IGNORECASE)


def _price_cells(page: str) -> list[str]:
    """The value cells of every table row labelled as the price row."""
    rows: list[list[str]] = []
    for line in page.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == _PRICE_LABEL:
            rows.append(cells[1:])
    if len(rows) != 1:
        # Zero rows: the table moved. Two or more: TypeSafe now prices more
        # than one model and "the" price no longer exists.
        raise RuntimeError(f"{SLUG}: expected exactly one price row, found {len(rows)}")
    return rows[0]


def _dollars(text: str) -> Decimal:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation as exc:
        raise RuntimeError(f"{SLUG}: unparseable price {text!r}") from exc


def parse(page: object) -> dict[str, ModelPrice]:
    if not isinstance(page, str):
        raise RuntimeError(f"{SLUG}: models page is not text")
    cells = _price_cells(page)
    # One value cell holding one "$x / $y" pair, and nothing else in it. A
    # second model arrives as a second COLUMN as easily as a second row
    # (`| Price | $42 / $0.042 | $84 / $0.084 |`), and reading only the first
    # cell would publish the old model's price for whichever one `jev-latest`
    # now points at.
    if len(cells) != 1:
        raise RuntimeError(
            f"{SLUG}: the price row has {len(cells)} value cells; it prices more than one model"
        )
    pairs = _PRICE_PAIR.findall(cells[0])
    if len(pairs) != 1 or _PRICE_PAIR.sub("", cells[0]).strip():
        raise RuntimeError(f"{SLUG}: the price cell is not a single '$x / $y' pair")
    rows = pairs
    if not _INPUT_ONLY.search(page):
        # The route bills input only. A vendor that starts metering output
        # must fail the refresh loudly, not be billed at zero.
        raise RuntimeError(f"{SLUG}: the page no longer says output tokens are free")
    per_billion, per_million = (_dollars(amount) for amount in rows[0])
    if per_billion != per_million * 1000:
        raise RuntimeError(
            f"{SLUG}: per-Btok {per_billion} and per-Mtok {per_million} disagree; "
            "refusing to pick one"
        )
    micro = per_million * Decimal(1_000_000)
    if micro <= 0 or micro != micro.to_integral_value():
        raise RuntimeError(f"{SLUG}: {per_million} is not a whole, positive microdollar/M rate")
    return {model_id: ModelPrice(int(micro), 0) for model_id in EXPECTED_MODELS}


def fetch() -> ProviderPricingResult:
    response = httpx.get(URL, timeout=30, headers={"Accept": "text/markdown, text/plain"})
    response.raise_for_status()
    return ProviderPricingResult(
        slug=SLUG,
        prices=parse(response.text),
        source="deterministic",
        fetched_url=URL,
        include_in_price_index=INCLUDE_IN_PRICE_INDEX,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    updated: list[str] = []
    for row in raw["models"]:
        price = result.prices.get(row["id"])
        if price is None:
            continue
        row["input_token_price_per_m"] = price.prompt_micro_per_m
        row["output_token_price_per_m"] = 0
        row["pricing_source"] = result.fetched_url
        updated.append(row["id"])
    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"{SLUG} manifest did not update required model(s): {missing}")
    raw["generated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    MANIFEST_PATH.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return [f"{SLUG}: refreshed provider_models/{MANIFEST_PATH.name} ({len(updated)} priced rows)"]
