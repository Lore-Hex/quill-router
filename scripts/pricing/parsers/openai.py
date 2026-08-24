# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses the normalized projection of OpenAI's official developer pricing page.
# Captured fixture lives at tests/fixtures/pricing/openai.html.
#
# The page format changed in mid-2026: the flagship Standard table now has
# 8 data columns instead of 6 (a "Cache writes" column was added for both
# Short context and Long context). The header now looks like:
#
#   | Model | Input | Cached input | Cache writes | Output |
#          Input | Cached input | Cache writes | Output |
#
# So a data row like gpt-5.5 has 9 pipe-delimited cells:
#   [name, short_in, short_cached, short_cache_writes, short_out,
#          long_in,  long_cached,  long_cache_writes,  long_out]
#
# Short-context-only rows (gpt-5.4-mini/nano) have "-" in the long columns.
# Some rows (gpt-5.5-pro, gpt-5.4-pro) don't support caching and put "-" in
# cached / cache-writes columns.
#
# We only extract the Standard tier (first flagship table on the page);
# Batch/Flex/Priority variants come later in the doc and are skipped via
# the `_live_seen` guard.
"""OpenAI pricing parser for provider-owned HTML or normalized Markdown."""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

# Display name on platform.openai.com/docs/pricing → OR-canonical id.
_NAME_TO_OR_ID = {
    "gpt-5.6-sol": "openai/gpt-5.6-sol",
    "gpt-5.6-terra": "openai/gpt-5.6-terra",
    "gpt-5.6-luna": "openai/gpt-5.6-luna",
    "gpt-5.5": "openai/gpt-5.5",
    "gpt-5.5-pro": "openai/gpt-5.5-pro",
    "gpt-5.4": "openai/gpt-5.4",
    "gpt-5.4-mini": "openai/gpt-5.4-mini",
    "gpt-5.4-nano": "openai/gpt-5.4-nano",
    "gpt-5.4-pro": "openai/gpt-5.4-pro",
    "gpt-5.3-codex": "openai/gpt-5.3-codex",
    "chat-latest": "openai/chat-latest",
}

# Legacy OpenAI models still served by the API but no longer on the
# current pricing page.
_LEGACY_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4o": (2.50, 10.00),
    "openai/gpt-4.1": (2.00, 8.00),
    "openai/gpt-4.1-mini": (0.40, 1.60),
    "openai/gpt-4.1-nano": (0.10, 0.40),
    "openai/o1": (15.00, 60.00),
    "openai/o1-mini": (1.10, 4.40),
    "openai/o3": (2.00, 8.00),
    "openai/o3-mini": (1.10, 4.40),
    "openai/o4-mini": (1.10, 4.40),
}

# Threshold for Short vs Long context tiers, in tokens. See gpt-5.5
# model card ("prompts with >272K input tokens are priced at 2x input
# and 1.5x output"). The gpt-5.6 family follows the same convention.
_SHORT_CONTEXT_THRESHOLD = 272_000


_DOLLAR_RE = re.compile(r"\$([\d.]+)")
_CHAT_PRICE_NAME_RE = re.compile(
    r"^(?:gpt-(?:[0-9][a-z0-9.-]*|oss-[0-9]+b)|o[0-9][a-z0-9.-]*|chat-latest)$",
    re.I,
)
_SHORT_CONTEXT_SUFFIX_RE = re.compile(
    r"\s*\(<\s*272k\s+context\s+length\)\s*$",
    re.I,
)


def _canonical_id(name: str) -> str | None:
    normalized = _SHORT_CONTEXT_SUFFIX_RE.sub("", name.strip().strip("*")).casefold()
    mapped = _NAME_TO_OR_ID.get(normalized)
    if mapped is not None:
        return mapped
    if not _CHAT_PRICE_NAME_RE.fullmatch(normalized):
        return None
    return f"openai/{normalized}"


def _astro_unwrap(value):
    """Decode Astro's compact JSON serialization used by hidden table rows."""

    if isinstance(value, list) and len(value) == 2 and value[0] in (0, 1):
        return _astro_unwrap(value[1])
    if isinstance(value, list):
        return [_astro_unwrap(item) for item in value]
    if isinstance(value, dict):
        return {key: _astro_unwrap(item) for key, item in value.items()}
    return value


def _embedded_standard_rows(source: str) -> list[list]:
    """Return official Standard rows hidden behind the page's All models UI."""

    rows: list[list] = []
    soup = BeautifulSoup(source, "html.parser")
    for island in soup.find_all("astro-island"):
        props = island.get("props")
        if not isinstance(props, str) or '"tier"' not in props or '"rows"' not in props:
            continue
        try:
            decoded = _astro_unwrap(json.loads(props))
        except (TypeError, ValueError):
            continue
        if not isinstance(decoded, dict) or decoded.get("tier") != "standard":
            continue
        candidate_rows = decoded.get("rows")
        if not isinstance(candidate_rows, list):
            continue
        rows.extend(row for row in candidate_rows if isinstance(row, list))
    return rows


def _to_micro_per_m(text: str | None) -> int | None:
    if text is None:
        return None
    stripped = text.strip()
    if stripped in {"-", "", "—"}:
        return None
    match = _DOLLAR_RE.search(stripped)
    if not match:
        return None
    try:
        return int(round(float(match.group(1)) * 1_000_000))
    except (TypeError, ValueError):
        return None


def _embedded_to_micro_per_m(value) -> int | None:
    if isinstance(value, (int, float)):
        return int(round(float(value) * 1_000_000))
    return _to_micro_per_m(str(value))


def _flat_row(prompt: int, completion: int,
              cached: int | None) -> dict:
    row: dict = {
        "prompt_micro_per_m": prompt,
        "completion_micro_per_m": completion,
    }
    if cached is not None:
        row["prompt_cached_micro_per_m"] = cached
    return row


def _tiered_row(short_in: int, short_out: int, short_cached: int | None,
                long_in: int, long_out: int, long_cached: int | None) -> dict:
    tier_low: dict = {
        "max_prompt_tokens": _SHORT_CONTEXT_THRESHOLD,
        "prompt_micro_per_m": short_in,
        "completion_micro_per_m": short_out,
    }
    if short_cached is not None:
        tier_low["prompt_cached_micro_per_m"] = short_cached
    tier_high: dict = {
        "max_prompt_tokens": None,
        "prompt_micro_per_m": long_in,
        "completion_micro_per_m": long_out,
    }
    if long_cached is not None:
        tier_high["prompt_cached_micro_per_m"] = long_cached
    return {"tiers": [tier_low, tier_high]}


def parse(md: str) -> dict:
    out: dict = {}
    _live_seen: set[str] = set()

    for line in md.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        name = cells[0]
        or_id = _canonical_id(name)
        if or_id is None:
            continue
        if or_id in _live_seen:
            # Ignore Batch/Flex/Priority tables that appear later in the
            # page — Standard tier is canonical.
            continue

        # New 8-column flagship layout (Short+Long context, each with
        # Input / Cached / Cache-writes / Output). Data row has 9 cells:
        # [name] + 8 numeric columns.
        if len(cells) >= 9:
            short_in = _to_micro_per_m(cells[1])
            short_cached = _to_micro_per_m(cells[2])
            # cells[3] = short cache writes  (ignored)
            short_out = _to_micro_per_m(cells[4])
            long_in = _to_micro_per_m(cells[5])
            long_cached = _to_micro_per_m(cells[6])
            # cells[7] = long cache writes   (ignored)
            long_out = _to_micro_per_m(cells[8])
            if short_in is None or short_out is None:
                continue
            if long_in is not None and long_out is not None:
                out[or_id] = _tiered_row(
                    short_in, short_out, short_cached,
                    long_in, long_out, long_cached,
                )
            else:
                out[or_id] = _flat_row(short_in, short_out, short_cached)
            _live_seen.add(or_id)
            continue

        # Older 6-column layout (Short+Long, no cache-writes). 7 cells.
        if len(cells) >= 7:
            short_in = _to_micro_per_m(cells[1])
            short_cached = _to_micro_per_m(cells[2])
            short_out = _to_micro_per_m(cells[3])
            long_in = _to_micro_per_m(cells[4])
            long_cached = _to_micro_per_m(cells[5])
            long_out = _to_micro_per_m(cells[6])
            if short_in is None or short_out is None:
                continue
            if long_in is not None and long_out is not None:
                out[or_id] = _tiered_row(
                    short_in, short_out, short_cached,
                    long_in, long_out, long_cached,
                )
            else:
                out[or_id] = _flat_row(short_in, short_out, short_cached)
            _live_seen.add(or_id)
            continue

        # 4-column single-context layout (Codex / specialized / chat-latest
        # in the specialized-models table): Input | Cached | Output.
        if len(cells) >= 4:
            single_in = _to_micro_per_m(cells[1])
            single_cached = _to_micro_per_m(cells[2])
            single_out = _to_micro_per_m(cells[3])
            if single_in is not None and single_out is not None:
                out[or_id] = _flat_row(single_in, single_out, single_cached)
                _live_seen.add(or_id)
                continue

        # Specialized-models table has an extra leading "Category" column,
        # so a data row is 5 cells: [category, model, input, cached, output].
        # In that case cells[0] is the category, not the model name — this
        # branch is only reached when name matched, meaning the table has
        # already dropped the category prefix (e.g. rows without a rowspan
        # continuation). Handled by the 4-column branch above via the
        # secondary lookup below.

    # OpenAI collapses older rows behind an "All models" client control. The
    # DOM contains those rows in an Astro JSON attribute even though they are
    # absent from the rendered table. Parse only the Standard processing tier;
    # visible rows already won above and retain their explicit long-context
    # columns. Models labelled <272K use OpenAI's documented 2x input/cache and
    # 1.5x output rates above that threshold.
    for cells in _embedded_standard_rows(md):
        if len(cells) < 4:
            continue
        name = str(cells[0])
        or_id = _canonical_id(name)
        if or_id is None or or_id in _live_seen:
            continue
        short_in = _embedded_to_micro_per_m(cells[1])
        short_cached = _embedded_to_micro_per_m(cells[2])
        short_out = _embedded_to_micro_per_m(cells[4] if len(cells) >= 5 else cells[3])
        if short_in is None or short_out is None:
            continue
        if _SHORT_CONTEXT_SUFFIX_RE.search(name):
            out[or_id] = _tiered_row(
                short_in,
                short_out,
                short_cached,
                short_in * 2,
                short_out * 3 // 2,
                short_cached * 2 if short_cached is not None else None,
            )
        else:
            out[or_id] = _flat_row(short_in, short_out, short_cached)
        _live_seen.add(or_id)

    # Also handle the specialized-models table where the row is prefixed
    # with a category column: | Codex | gpt-5.3-codex | $1.75 | $0.175 | $14.00 |
    # That yields 5 cells with cells[1] as the model name.
    for line in md.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        name = cells[1]
        or_id = _canonical_id(name)
        if or_id is None or or_id in _live_seen:
            continue
        single_in = _to_micro_per_m(cells[2])
        single_cached = _to_micro_per_m(cells[3])
        single_out = _to_micro_per_m(cells[4])
        if single_in is not None and single_out is not None:
            out[or_id] = _flat_row(single_in, single_out, single_cached)
            _live_seen.add(or_id)

    # Fall-back legacy prices for ids the live page no longer lists.
    for or_id, (prompt_usd, completion_usd) in _LEGACY_PRICES.items():
        if or_id in out:
            continue
        out[or_id] = {
            "prompt_micro_per_m": int(round(prompt_usd * 1_000_000)),
            "completion_micro_per_m": int(round(completion_usd * 1_000_000)),
        }
    return out
