"""TypeSafe AI: the Jev decision model, input-only pricing, read from the vendor.

TypeSafe publishes Jev's rate on its public models page: one two-column table
with a row ``| Price (per Btok / per Mtok) | \\$42 / \\$0.042 |`` and one
sentence that fixes the shape ("Charged per input token. Output tokens are
free.").

The number this writes is the number customers are billed, so the parser does
not try to be a good markdown reader. Three attempts at "find the price row and
read it" were each beaten by a table it did not anticipate -- a second model as
a second column, an empty cell, a row with no closing pipe, GFM's implicit
cells. It now checks things that are true of the WHOLE page whatever syntax a
change arrives in, and refuses the page if any stops being true:

  * the price label appears exactly once, anywhere;
  * the page holds exactly two dollar amounts, and they are that one pair (so
    no second model's price, no per-request fee, in any table, list or HTML);
  * exactly one versioned model id (``jev-1.13.0``) is named anywhere;
  * the page's alias table binds ``jev-latest`` -- the id this route actually
    calls -- to that same versioned model, and the pricing table names it too.
    Counting ids alone was not enough: a page could price a "legacy" table and
    describe the alias's new target in prose with no id and no dollar sign;
  * the table around the price row is two columns wide on every line;
  * the per-billion and per-million figures agree, and output is still free.

A refusal fails the refresh loudly and leaves the last-known-good price in
force. That is the point: a human looks at the page.

The threat model is a vendor EDITING its page in a way nobody here anticipated,
not a vendor writing a page to deceive this parser: a page that binds the alias
to one model in its table and to another in its prose contradicts itself, and
no parser can say which half is true.
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
# The upstream id the catalog sends to TypeSafe for this model. The price that
# matters is the price of whatever THIS names, so the page must say what it is.
UPSTREAM_ALIAS = "jev-latest"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/typesafe.json"
)
# Fixed-shape input-only rows are not comparable with chat token prices.
INCLUDE_IN_PRICE_INDEX = False

_AMOUNT = r"\\?\$([0-9][0-9,]*(?:\.[0-9]+)?)"
_PRICE_LABEL = "Price (per Btok / per Mtok)"
_PRICE_PAIR = re.compile(_AMOUNT + r"\s*/\s*" + _AMOUNT)
_ANY_DOLLAR_AMOUNT = re.compile(r"\$\s*[0-9]")
_MODEL_VERSION = re.compile(r"\bjev-[0-9]+\.[0-9]+\.[0-9]+\b")
_INPUT_ONLY = re.compile(r"charged per input token\.\s+output tokens are free\.", re.IGNORECASE)


def _cells(line: str) -> list[str] | None:
    """The cells of a table line written with both boundary pipes, else None."""
    row = line.strip()
    if len(row) < 2 or not (row.startswith("|") and row.endswith("|")):
        return None
    return [cell.strip() for cell in row[1:-1].split("|")]


def _aliased_version(page: str) -> str:
    """The versioned model the page's alias table binds UPSTREAM_ALIAS to."""
    targets: set[str] = set()
    for line in page.splitlines():
        cells = _cells(line)
        if not cells or len(cells) < 2 or cells[0].strip("` ") != UPSTREAM_ALIAS:
            continue
        named = _MODEL_VERSION.findall(cells[1])
        if len(named) != 1:
            raise RuntimeError(
                f"{SLUG}: the {UPSTREAM_ALIAS} row does not name one versioned model"
            )
        targets.add(named[0])
    if len(targets) != 1:
        raise RuntimeError(
            f"{SLUG}: the page does not bind {UPSTREAM_ALIAS} to exactly one versioned model "
            f"(found {sorted(targets) or 'no alias row'})"
        )
    return targets.pop()


def _price_block(page: str) -> tuple[str, str]:
    """(value cell of the price row, text of the table block it sits in)."""
    if page.count(_PRICE_LABEL) != 1:
        # Zero: the table moved. More: TypeSafe prices more than one model, in
        # whatever syntax, and "the" price no longer exists.
        raise RuntimeError(
            f"{SLUG}: expected the price label exactly once, found {page.count(_PRICE_LABEL)}"
        )
    lines = page.splitlines()
    at = next(index for index, line in enumerate(lines) if _PRICE_LABEL in line)
    first, last = at, at
    while first > 0 and lines[first - 1].strip().startswith("|"):
        first -= 1
    while last + 1 < len(lines) and lines[last + 1].strip().startswith("|"):
        last += 1
    for line in lines[first : last + 1]:
        cells = _cells(line)
        if cells is None or len(cells) != 2:
            # A third column is a second model. A row narrower than its header
            # is one too: markdown fills the missing cell in silently.
            raise RuntimeError(
                f"{SLUG}: the pricing table is not two columns on every line: {line.strip()[:60]!r}"
            )
    label, value = _cells(lines[at]) or ("", "")
    if label != _PRICE_LABEL:
        raise RuntimeError(f"{SLUG}: the price label is not the first cell of its row")
    return value, "\n".join(lines[first : last + 1])


def _dollars(text: str) -> Decimal:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation as exc:
        raise RuntimeError(f"{SLUG}: unparseable price {text!r}") from exc


def parse(page: object) -> dict[str, ModelPrice]:
    if not isinstance(page, str):
        raise RuntimeError(f"{SLUG}: models page is not text")
    cell, block = _price_block(page)
    pairs = _PRICE_PAIR.findall(cell)
    if len(pairs) != 1 or _PRICE_PAIR.sub("", cell).strip():
        raise RuntimeError(f"{SLUG}: the price cell is not a single '$x / $y' pair")
    if len(_ANY_DOLLAR_AMOUNT.findall(page)) != 2:
        # The pair is two amounts. Any other dollar figure on the page is a
        # price this parser does not understand: a fee, a tier, another model.
        raise RuntimeError(f"{SLUG}: the page holds dollar amounts beyond the one price pair")
    versions = sorted(set(_MODEL_VERSION.findall(page)))
    if len(versions) != 1:
        raise RuntimeError(
            f"{SLUG}: the page names {versions or 'no versioned model'}; which one "
            "`jev-latest` costs is only certain when there is exactly one"
        )
    # The price belongs to the model the pricing table names; the route calls
    # UPSTREAM_ALIAS. Those must be the same model, said so by the page itself.
    aliased = _aliased_version(page)
    if _MODEL_VERSION.findall(block) != [aliased]:
        raise RuntimeError(
            f"{SLUG}: the pricing table does not name {aliased}, the model "
            f"{UPSTREAM_ALIAS} points at"
        )
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
