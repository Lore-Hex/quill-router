# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses the concatenation of all Kimi chat-pricing pages discovered from
# Moonshot's official llms.txt index. The providers/kimi.py orchestrator
# allowlists each URL and joins the pages before calling parse(); we just need
# to handle the JSX-shaped table rows.
#
# Row format (5- and 4-cell variants):
#
#   ["kimi-k2.6", "1M tokens", <>{"$"}0.16</>, <>{"$"}0.95</>, <>{"$"}4.00</>, "262,144 tokens"]
#   ["moonshot-v1-8k", "1M tokens", <>{"$"}0.20</>, <>{"$"}2.00</>, "8,192 tokens"]
#
# K2 family has 5 cells before the context column: id, unit, cache
# hit input, cache MISS input (headline), output. V1 family has only
# 4: id, unit, input, output.
#
# We use the cache-MISS input price as the headline (matches what a
# fresh request pays). The cache-HIT price is emitted as the cached
# input rate — settle bills reported cache_read tokens at it.
"""Kimi/Moonshot pricing parser (multi-subpage markdown)."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

# Native model id → OR-canonical id.
_NAME_TO_OR_ID = {
    "kimi-k2.6": "moonshotai/kimi-k2.6",
    "kimi-k2.5": "moonshotai/kimi-k2.5",
    "kimi-k2-0905-preview": "moonshotai/kimi-k2-0905-preview",
    "kimi-k2-0711-preview": "moonshotai/kimi-k2-0711-preview",
    "kimi-k2-turbo-preview": "moonshotai/kimi-k2-turbo-preview",
    "kimi-k2-thinking": "moonshotai/kimi-k2-thinking",
    "kimi-k2-thinking-turbo": "moonshotai/kimi-k2-thinking-turbo",
    "moonshot-v1-8k": "moonshotai/moonshot-v1-8k",
    "moonshot-v1-32k": "moonshotai/moonshot-v1-32k",
    "moonshot-v1-128k": "moonshotai/moonshot-v1-128k",
}


def _or_id(native_id: str) -> str | None:
    mapped = _NAME_TO_OR_ID.get(native_id)
    if mapped is not None:
        return mapped
    lowered = native_id.strip().casefold()
    if lowered.startswith(("kimi-", "moonshot-")):
        return f"moonshotai/{lowered}"
    return None


# Match: ["model-id", "1M tokens", <>{"$"}X.X</>, <>{"$"}Y.Y</>, <>{"$"}Z.Z</>, "context"]
# OR  : ["model-id", "1M tokens", <>{"$"}X.X</>, <>{"$"}Y.Y</>, "context"]
_ROW_RE = re.compile(
    r'\["([^"]+)"\s*,\s*"1M tokens"\s*,'
    # K3 adds two cache-write TTL columns before cache hit/input/output.
    r'(?:\s*<>\{"\$"\}([\d.]+)</>\s*,'
    r'\s*<>\{"\$"\}([\d.]+)</>\s*,)?'
    r'\s*<>\{"\$"\}([\d.]+)</>\s*,'  # column A (cache hit OR input)
    r'\s*<>\{"\$"\}([\d.]+)</>\s*,'  # column B (cache miss OR output)
    r'(?:\s*<>\{"\$"\}([\d.]+)</>\s*,)?'  # column C (output, only K2 family)
    r'\s*"([^"]+)"\s*\]'  # context window
)


# Since 2026-11 the pricing lives on one page (docs/pricing/chat) whose .md
# export renders the tables empty; the rendered page carries them as compiled
# MDX DocTable props, one row per model, cells keyed by column title:
#
#   columns:[{title:`Model`,...},{title:`Input`,...},{title:`Output`,...},
#            {title:_jsx(Tooltip,{tip:`...`,children:`Cached Input`}),...}, ...],
#   rows:[[_jsx(_components.a,{href:`/guide/...`,children:`kimi-k3`}),
#          `$3.00 / MTok`,`$15.00 / MTok`,`$0.30 / MTok`,`$3.00 / MTok`,`$6.00 / MTok`], ...]
#
# Cache-write columns (5m / 1h writes) are not billed by this route and are
# ignored; every other column title must be one this parser knows.
_DOC_TABLE_RE = re.compile(r"columns:\[(.*?)\],rows:\[(.*?)\]\]", re.S)
_COLUMN_RE = re.compile(
    r"\{title:(?:`([^`]*)`|_jsx\(Tooltip,\{tip:`[^`]*`,children:`([^`]*)`\}\))\s*,\s*width:`[^`]*`\}"
)
_DOC_ROW_RE = re.compile(
    r"\[(?:_jsx\(_components\.a,\{href:`[^`]*`,children:`([^`]+)`\}\)|`([^`]+)`)((?:,`[^`]*`)*)\]"
)
_DOC_CELL_RE = re.compile(r",`([^`]*)`")
_DOC_PRICE_RE = re.compile(r"\$([\d]+(?:\.[\d]+)?) / MTok")
_DOC_COLUMNS = {
    "Input": "prompt_micro_per_m",
    "Output": "completion_micro_per_m",
    "Cached Input": "prompt_cached_micro_per_m",
}
_DOC_IGNORED_COLUMNS = frozenset({"5m writes", "1h writes"})


def _doc_table_rows(text: str):
    for table in _DOC_TABLE_RE.finditer(text):
        columns_src, rows_src = table.group(1), table.group(2) + "]"
        titles = [plain or tooltip for plain, tooltip in _COLUMN_RE.findall(columns_src)]
        if len(titles) != columns_src.count("{title:") or not titles or titles[0] != "Model":
            raise ValueError(f"Unrecognized Kimi pricing table columns: {titles!r}")
        unknown = set(titles[1:]) - set(_DOC_COLUMNS) - _DOC_IGNORED_COLUMNS
        if unknown or not {"Input", "Output"} <= set(titles) or len(set(titles)) != len(titles):
            raise ValueError(f"Unrecognized Kimi pricing table columns: {titles!r}")
        rows = list(_DOC_ROW_RE.finditer(rows_src))
        if not rows or sum(len(row.group(0)) for row in rows) + len(rows) - 1 != len(rows_src):
            raise ValueError("Unrecognized Kimi pricing table rows")
        for row in rows:
            native_id = row.group(1) or row.group(2)
            cells = _DOC_CELL_RE.findall(row.group(3))
            if len(cells) != len(titles) - 1:
                raise ValueError(f"Kimi pricing row for {native_id} does not match its columns")
            prices: dict = {}
            for title, cell in zip(titles[1:], cells, strict=True):
                if title in _DOC_IGNORED_COLUMNS:
                    continue
                match = _DOC_PRICE_RE.fullmatch(cell.strip())
                if match is None:
                    raise ValueError(f"Unrecognized Kimi price {cell!r} for {native_id}")
                prices[_DOC_COLUMNS[title]] = _to_micro_per_m(match.group(1))
            yield native_id, prices


def _to_micro_per_m(usd: str) -> int:
    return int((Decimal(usd) * Decimal(1_000_000)).quantize(Decimal("1"), ROUND_HALF_UP))


def parse(text: str) -> dict:
    out: dict = {}
    for match in _ROW_RE.finditer(text):
        native_id, write_5min, _write_1h, col_a, col_b, col_c, _context = match.groups()
        if write_5min is not None and col_c is None:
            raise ValueError(f"Unrecognized Kimi price column count for {native_id}")
        or_id = _or_id(native_id)
        if or_id is None:
            continue
        if col_c is not None:
            # 5-column shape: cache_hit, cache_miss, output. Use
            # cache_miss as the headline (uncached) input rate, and
            # cache_hit as the prompt_cached rate.
            cached_micro = _to_micro_per_m(col_a)
            input_micro = _to_micro_per_m(col_b)
            output_micro = _to_micro_per_m(col_c)
            row_out: dict = {
                "prompt_micro_per_m": input_micro,
                "completion_micro_per_m": output_micro,
                "prompt_cached_micro_per_m": cached_micro,
            }
        else:
            # 4-column shape (V1 family): just input, output. No cache
            # discount documented for the V1 family.
            input_micro = _to_micro_per_m(col_a)
            output_micro = _to_micro_per_m(col_b)
            row_out = {
                "prompt_micro_per_m": input_micro,
                "completion_micro_per_m": output_micro,
            }
        _merge(out, or_id, row_out)
    for native_id, row_out in _doc_table_rows(text):
        or_id = _or_id(native_id)
        if or_id is not None:
            _merge(out, or_id, row_out)
    return out


def _merge(out: dict, or_id: str, row_out: dict) -> None:
    previous = out.get(or_id, {})
    if any(previous[key] != row_out[key] for key in previous.keys() & row_out.keys()):
        raise ValueError(f"Conflicting Kimi prices for {or_id}")
    out[or_id] = {**previous, **row_out}
