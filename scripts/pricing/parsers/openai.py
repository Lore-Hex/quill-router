# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses platform.openai.com/docs/pricing as rendered by r.jina.ai.
# Captured fixture lives at tests/fixtures/pricing/openai.html.
#
# Page format: Jina returns clean markdown tables. The Standard tier
# tables look like:
#
#   |  | Short context | Long context |
#   | --- | --- | --- |
#   | Model | Input | Cached input | Output | Input | Cached input | Output |
#   | gpt-5.5 | $5.00 | $0.50 | $30.00 | $10.00 | $1.00 | $45.00 |
#   | gpt-5.4 | $2.50 | $0.25 | $15.00 | $5.00 | $0.50 | $22.50 |
#   ...
#
# Columns 1+3 are Short context (≤ some threshold) input/output;
# columns 4+6 are Long context input/output. We pull the Standard
# tier (the headline rate) and ignore Batch/Flex/Priority for now.
#
# OpenAI doesn't explicitly state the Short/Long threshold on the page,
# but their docs convention is 200k for the GPT-5 family. We emit a
# tiered ModelPrice when both columns are populated; otherwise flat.
"""OpenAI pricing parser (Jina-rendered markdown)."""
from __future__ import annotations

import re

# Display name on platform.openai.com/docs/pricing → OR-canonical id.
_NAME_TO_OR_ID = {
    "gpt-5.5": "openai/gpt-5.5",
    "gpt-5.5-pro": "openai/gpt-5.5-pro",
    "gpt-5.4": "openai/gpt-5.4",
    "gpt-5.4-mini": "openai/gpt-5.4-mini",
    "gpt-5.4-nano": "openai/gpt-5.4-nano",
    "gpt-5.4-pro": "openai/gpt-5.4-pro",
    "gpt-5.3-codex": "openai/gpt-5.3-codex",
}

# Legacy OpenAI models that the API still serves but that no longer
# appear on the current pricing page (5.5/5.4 family is what's
# advertised). We keep them so back-compat callers can still route
# to model ids that have been around for years.
#
# Prices captured manually from openai.com/api/pricing/ via Wayback
# Machine on 2026-05-08; OpenAI hasn't published updated rates for
# these since the GPT-5 launch. The cross-check vs OR will surface
# any drift the LLM self-heal didn't catch.
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

# Threshold for OpenAI's Short context vs Long context tiers, in tokens.
# Verified from platform.openai.com/docs/models/gpt-5.5:
# "For GPT-5.5, prompts with >272K input tokens are priced at 2x input
# and 1.5x output for the full session for standard, batch, and flex."
# Same rule applies to gpt-5.4 / gpt-5.4-pro / gpt-5.5-pro (the table
# values verify: 10/5=2x input, 45/30=1.5x output).
_SHORT_CONTEXT_THRESHOLD = 272_000


_DOLLAR_RE = re.compile(r"\$([\d.]+)")


def _to_micro_per_m(text: str | None) -> int | None:
    if not text or text.strip() in {"-", ""}:
        return None
    match = _DOLLAR_RE.search(text)
    if not match:
        return None
    try:
        return int(round(float(match.group(1)) * 1_000_000))
    except (TypeError, ValueError):
        return None


def parse(md: str) -> dict:
    out: dict = {}
    # Track which ids have been populated by the live page in this run.
    # Live values override the legacy seed on collision; later
    # Batch/Flex/Priority sections of the live page do NOT override
    # the Standard tier (first table in the page is the canonical rate).
    _live_seen: set[str] = set()
    # Walk every markdown table row that starts with "| " — Jina renders
    # OpenAI's HTML tables as pipe-delimited markdown.
    for line in md.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        name = cells[0]
        or_id = _NAME_TO_OR_ID.get(name)
        if or_id is None:
            continue
        # The legacy seed populated `out` with flat back-compat prices
        # for some ids; we DO want live-page data to overwrite legacy
        # since live is fresher. But we don't want a Batch/Flex/Priority
        # section to overwrite the Standard rate from earlier in the
        # same page. Track which ids we've already populated from the
        # live data via a separate set.
        if or_id in _live_seen:
            continue
        # 6-column table (Standard / chat models): Input | Cached |
        # Output | Long Input | Long Cached | Long Output. The cached
        # columns are real prices (not "-") for the chat models that
        # support cache reads (gpt-5.5, gpt-5.4 family).
        if len(cells) >= 7:
            short_input = _to_micro_per_m(cells[1])
            short_cached = _to_micro_per_m(cells[2])
            short_output = _to_micro_per_m(cells[3])
            long_input = _to_micro_per_m(cells[4])
            long_cached = _to_micro_per_m(cells[5])
            long_output = _to_micro_per_m(cells[6])
            if short_input is None or short_output is None:
                continue
            if long_input is not None and long_output is not None:
                tier_low: dict = {
                    "max_prompt_tokens": _SHORT_CONTEXT_THRESHOLD,
                    "prompt_micro_per_m": short_input,
                    "completion_micro_per_m": short_output,
                }
                if short_cached is not None:
                    tier_low["prompt_cached_micro_per_m"] = short_cached
                tier_high: dict = {
                    "max_prompt_tokens": None,
                    "prompt_micro_per_m": long_input,
                    "completion_micro_per_m": long_output,
                }
                if long_cached is not None:
                    tier_high["prompt_cached_micro_per_m"] = long_cached
                out[or_id] = {"tiers": [tier_low, tier_high]}
            else:
                row_out: dict = {
                    "prompt_micro_per_m": short_input,
                    "completion_micro_per_m": short_output,
                }
                if short_cached is not None:
                    row_out["prompt_cached_micro_per_m"] = short_cached
                out[or_id] = row_out
            _live_seen.add(or_id)
            continue
        # 4-column table (single-context models like Codex /
        # transcription): Input | Cached | Output. Skip if shape
        # doesn't match what we expect.
        if len(cells) >= 4:
            single_input = _to_micro_per_m(cells[1])
            single_cached = _to_micro_per_m(cells[2])
            single_output = _to_micro_per_m(cells[3])
            if single_input is not None and single_output is not None:
                row_out = {
                    "prompt_micro_per_m": single_input,
                    "completion_micro_per_m": single_output,
                }
                if single_cached is not None:
                    row_out["prompt_cached_micro_per_m"] = single_cached
                out[or_id] = row_out
                _live_seen.add(or_id)
    # Add legacy back-compat entries for ids the live page didn't list.
    # See _LEGACY_PRICES docstring for why these stay even though
    # they're not on platform.openai.com/docs/pricing anymore.
    for or_id, (prompt_usd, completion_usd) in _LEGACY_PRICES.items():
        if or_id in out:
            continue
        out[or_id] = {
            "prompt_micro_per_m": int(round(prompt_usd * 1_000_000)),
            "completion_micro_per_m": int(round(completion_usd * 1_000_000)),
        }
    return out
