"""TypeSafe AI: the Jev decision model, input-only pricing, read from the vendor.

TypeSafe publishes Jev's rate on its public models page: a table with one
column per current model (one until 2026-10, two since) and a row ``| Price (per Btok / per Mtok) | \\$42 / \\$0.042 |`` and one
sentence that fixes the shape ("Charged per input token. Output tokens are
free.").

The number this writes is the number customers are billed, so the parser does
not try to be a good markdown reader. Three attempts at "find the price row and
read it" were each beaten by a table it did not anticipate -- a second model as
a second column, an empty cell, a row with no closing pipe, GFM's implicit
cells. It now checks things that are true of the WHOLE page whatever syntax a
change arrives in, and refuses the page if any stops being true:

  * the price label appears exactly once, anywhere;
  * the page's only dollar amounts are the table's price pairs (so no
    per-request fee or stray price, in any table, list or HTML);
  * every versioned model id (``jev-1.14.0``) named anywhere is a priced column;
  * the page's alias table binds ``jev-latest`` -- the id this route actually
    calls -- to that same versioned model, and the pricing table names it too.
    Counting ids alone was not enough: a page could price a "legacy" table and
    describe the alias's new target in prose with no id and no dollar sign;
  * the table around the price row is the same width on every line, each of
    its value columns names exactly one versioned model, and the price read is
    the one in the column of the model the alias points to (the page priced
    one model until 2026-10 and prices two, one per column, since 2026-11);
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
# "Charged per input token." until 2026-10, "You pay per input token." since.
_INPUT_ONLY = re.compile(
    r"(?:charged|you pay) per input token\.\s+output tokens are free\.", re.IGNORECASE
)
# A versioned model id as a COMPLETE token. Matching `jev-1.13.0` with word
# boundaries also matched it inside `jev-1.13.0-pro`, so a page that priced
# Standard and pointed the alias at a dearer Pro variant read as one model. A
# token runs to the end of the identifier; trailing sentence punctuation is not
# part of it. An id has all three numeric parts, with or without a suffix: the
# page also links to `/model-jaggedness/jev-1.13`, a docs slug for the model
# FAMILY, which is not an id and is not counted. (`jev-latest` and
# `jev-preview` are aliases: no digit follows.)
_JEV_TOKEN = re.compile(r"(?<![A-Za-z0-9._-])jev-[0-9][A-Za-z0-9._-]*")
_ID_SHAPED = re.compile(r"jev-[0-9]+\.[0-9]+\.[0-9]+(?![0-9]).*")
_PLAIN_VERSION = re.compile(r"jev-[0-9]+\.[0-9]+\.[0-9]+")


def _model_versions(text: str) -> list[str]:
    """Every versioned model id in text, each as its whole identifier."""
    tokens = (token.rstrip("._-") for token in _JEV_TOKEN.findall(text))
    return [token for token in tokens if _ID_SHAPED.fullmatch(token)]


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
        named = _model_versions(cells[1])
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
    """(price cell of the column for the model UPSTREAM_ALIAS names, its block).

    The table may describe one model (a label column and a value column, as
    until 2026-10) or several, one per column ("| | Jev 1.14 | Jev 1.13 |"
    from 2026-11). Either way every line of the block has the same width,
    every value column names exactly one versioned model, no two columns name
    the same one, and the label column names none. The price read is the one
    in the column of the model the alias points to.
    """
    if page.count(_PRICE_LABEL) != 1:
        # Zero: the table moved. More: a second price table, in whatever
        # syntax, and which one applies is not certain.
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
    price_cells = _cells(lines[at])
    width = len(price_cells) if price_cells is not None else 0
    table: list[list[str]] = []
    for line in lines[first : last + 1]:
        cells = _cells(line)
        if cells is None or width < 2 or len(cells) != width:
            # A row narrower than its header is a model with no price:
            # markdown fills the missing cell in silently. A line without
            # both boundary pipes cannot be counted at all.
            raise RuntimeError(
                f"{SLUG}: the pricing table is not {width} columns on every line: {line.strip()[:60]!r}"
            )
        table.append(cells)
    assert price_cells is not None
    if price_cells[0] != _PRICE_LABEL:
        raise RuntimeError(f"{SLUG}: the price label is not the first cell of its row")
    if any(_model_versions(row[0]) for row in table):
        raise RuntimeError(f"{SLUG}: the pricing table's label column names a model")
    columns: dict[str, int] = {}
    for column in range(1, width):
        named = sorted({version for row in table for version in _model_versions(row[column])})
        if len(named) != 1:
            raise RuntimeError(
                f"{SLUG}: pricing column {column} names {named or 'no versioned model'}; "
                "its price belongs to exactly one"
            )
        if named[0] in columns:
            raise RuntimeError(f"{SLUG}: two pricing columns name {named[0]}")
        columns[named[0]] = column
    aliased = _aliased_version(page)
    if aliased not in columns:
        raise RuntimeError(
            f"{SLUG}: the pricing table does not name {aliased}, the model "
            f"{UPSTREAM_ALIAS} points at"
        )
    for column in range(1, width):
        cell = price_cells[column]
        if len(_PRICE_PAIR.findall(cell)) != 1 or _PRICE_PAIR.sub("", cell).strip():
            raise RuntimeError(f"{SLUG}: the price cell is not a single '$x / $y' pair")
    return price_cells[columns[aliased]], "\n".join(lines[first : last + 1])


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
    priced_columns = len(_PRICE_PAIR.findall(block))
    if len(_ANY_DOLLAR_AMOUNT.findall(page)) != 2 * priced_columns:
        # Each priced column holds one pair. Any other dollar figure on the
        # page is a price this parser does not understand: a fee, a tier.
        raise RuntimeError(f"{SLUG}: the page holds dollar amounts beyond the table's price pairs")
    named = set(_model_versions(block))
    versions = sorted(set(_model_versions(page)))
    if not named or set(versions) != named or not all(_PLAIN_VERSION.fullmatch(v) for v in versions):
        raise RuntimeError(
            f"{SLUG}: the page names {versions or 'no versioned model'} but prices {sorted(named)}; "
            "which one `jev-latest` costs is only certain when every named model is priced"
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
