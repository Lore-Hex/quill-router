"""Moonshot/Kimi — human-only provider config.

Kimi's pricing is published as raw .md files at predictable URLs:

    https://platform.kimi.ai/docs/pricing/chat-k26.md  → K2.6 family
    https://platform.kimi.ai/docs/pricing/chat-k25.md  → K2.5 family
    https://platform.kimi.ai/docs/pricing/chat-k2.md   → K2 family (5 SKUs)
    https://platform.kimi.ai/docs/pricing/chat-v1.md   → Moonshot V1 family

Each .md returns a JSX-shaped table embedded in markdown:

    ["kimi-k2.6", "1M tokens", <>{"$"}0.16</>, <>{"$"}0.95</>, <>{"$"}4.00</>, "262,144 tokens"]

Columns are: model_id | unit | cache_hit | cache_miss_input | output | context

The cache_miss_input is the headline (non-cached) input rate; we use
that for billing.

Different from the other providers because we fetch FOUR separate
URLs, concatenate, then parse. The parser layer's contract stays
`parse(combined_text)`; concatenation is how we present the union of
the family pages as a single input.
"""
from __future__ import annotations

from typing import Any

from scripts.pricing.base import (
    ProviderPricingResult,
    _coerce_to_model_prices,
    fetch_html,
    log,
    parser_path,
    validate,
)

SLUG = "kimi"

# Sub-pages of platform.kimi.ai/docs/pricing/chat. Fetched directly
# (no Jina proxy needed — these .md URLs serve plain text). Listed in
# order; the K2 page is the canonical "headline" and goes first so
# its URL ends up logged.
SUBPAGES = [
    "https://platform.kimi.ai/docs/pricing/chat-k26.md",
    "https://platform.kimi.ai/docs/pricing/chat-k25.md",
    "https://platform.kimi.ai/docs/pricing/chat-k2.md",
    "https://platform.kimi.ai/docs/pricing/chat-v1.md",
]
URL = SUBPAGES[0]  # for log/diagnostic purposes only

EXPECTED_MODELS = [
    "moonshotai/kimi-k2.6",
]


def _combined_html() -> str:
    """Fetch all 4 sub-pages and concatenate so the parser sees the
    union as a single document. Failure of one sub-page is non-fatal —
    we keep what we got."""
    chunks: list[str] = []
    for url in SUBPAGES:
        try:
            chunks.append(fetch_html(url))
        except Exception as exc:  # noqa: BLE001
            log.warning("kimi.subpage_fetch_failed url=%s err=%s", url, exc)
    return "\n\n--- PAGE BREAK ---\n\n".join(chunks)


def fetch() -> ProviderPricingResult:
    """Custom fetch path: fetches multiple sub-pages, parses the
    combined text via parsers/kimi.py. Self-heal still works — if the
    parser breaks, the LLM rewrites parsers/kimi.py and we retry on
    the same combined text."""
    html = _combined_html()
    if not html:
        raise RuntimeError("kimi: all sub-pages failed to fetch")

    # Run parsers/kimi.py against the combined text.
    src = parser_path(SLUG).read_text(encoding="utf-8")
    namespace: dict[str, Any] = {}
    exec(compile(src, str(parser_path(SLUG)), "exec"), namespace)  # noqa: S102
    parse_fn = namespace.get("parse")
    if not callable(parse_fn):
        raise RuntimeError(f"{SLUG}: parsers/{SLUG}.py has no callable `parse`")
    raw = parse_fn(html)

    prices, schema_errors = _coerce_to_model_prices(raw)
    if schema_errors:
        raise RuntimeError(f"{SLUG}: parser schema errors: {schema_errors}")
    if prices is None:
        raise RuntimeError(f"{SLUG}: parser returned None unexpectedly")
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        # No self-heal path here — Kimi's structure is too custom for
        # the standard fetch_provider flow. If validation fails, we
        # fail loudly and a human updates parsers/kimi.py.
        raise RuntimeError(f"{SLUG}: validation failed: {errors}")
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="multi_page_md",
        fetched_url=URL,
    )
