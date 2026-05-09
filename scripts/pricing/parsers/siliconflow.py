# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses siliconflow.com/pricing as rendered by r.jina.ai.
# Captured fixture lives at tests/fixtures/pricing/siliconflow.html.
#
# The Framer-built page renders each pricing row as a stack of lines:
#
#     Qwen3-VL-32B-Instruct
#     262K                       <- context length
#     $
#     0.2                        <- input $ / M tokens
#     $
#     0.6                        <- output $ / M tokens
#     [Details](https://siliconflow.com/models/qwen3-vl-32b-instruct)
#
# We anchor on the [Details] link (which carries the canonical model
# slug) and walk back to find the model name + the two $-numbers.
"""SiliconFlow pricing parser (Jina-rendered Framer markdown)."""
from __future__ import annotations

import re

# Native SiliconFlow slug → OR-canonical id. Most native ids match
# OR's `vendor/model-name` convention but lowercased. We pass through
# unmodified and let the catalog reject anything we don't recognize.
_NAME_TO_OR_ID: dict[str, str] = {}


# Match the [Details](https://siliconflow.com/models/<slug>) anchor at
# the end of each row, capturing the slug. We then look UP in the
# preceding ~400 chars for the input/output $ amounts.
_DETAILS_RE = re.compile(
    r"\[Details\]\(https://(?:www\.)?siliconflow\.com/models/([\w.\-]+)\)"
)
_PRICE_RE = re.compile(r"\$\s*\n?\s*([\d.]+)")


def parse(md: str) -> dict:
    out: dict = {}
    for m in _DETAILS_RE.finditer(md):
        slug = m.group(1)
        # Look back in a 600-char window for the two $-prefixed numbers
        # that come right before this [Details] link.
        window_start = max(0, m.start() - 600)
        window = md[window_start : m.start()]
        prices = _PRICE_RE.findall(window)
        if len(prices) < 2:
            continue
        # Some rows have 2 prices (Input, Output), some have 3 (Input,
        # Cached Input, Output). Take prices[0] = Input and prices[-1]
        # = Output regardless. We need to be careful not to grab
        # leftovers from the previous row though — the regex window
        # walks back 600 chars which can span multiple rows. Trim to
        # only the prices that come AFTER the most recent
        # context-length marker (e.g., "262K", "1049K", "33K"),
        # which is the boundary between rows.
        # Find the LAST context marker (e.g., "262K\n$") before this
        # Details link — that's the start of THIS row's data. Earlier
        # markers belong to previous rows that are still in the window.
        ctx_matches = list(re.finditer(r"\b(\d+[KMkm])\s*\n+\s*\$", window))
        if not ctx_matches:
            continue
        ctx_match = ctx_matches[-1]
        row_window = window[ctx_match.end() - 1 :]
        row_prices = _PRICE_RE.findall(row_window)
        if len(row_prices) < 2:
            continue
        try:
            input_usd = float(row_prices[0])
            output_usd = float(row_prices[-1])
        except ValueError:
            continue
        # Skip image/video models (their slugs include 'flux', 'image',
        # 'video', 'sora', etc., and their prices aren't per-token).
        lowered = slug.lower()
        if any(
            tag in lowered
            for tag in ("flux", "z-image", "wan2", "vidu", "sora", "kling")
        ):
            continue
        or_id = _normalize(slug)
        if or_id in out:
            continue
        out[or_id] = {
            "prompt_micro_per_m": int(round(input_usd * 1_000_000)),
            "completion_micro_per_m": int(round(output_usd * 1_000_000)),
        }
    return out


def _normalize(native_slug: str) -> str:
    """Convert SiliconFlow's slug ('qwen3-vl-32b-instruct') to OR-canonical
    ('qwen/qwen3-vl-32b-instruct'). Adds vendor prefix when missing."""
    if "/" in native_slug or native_slug in _NAME_TO_OR_ID:
        return _NAME_TO_OR_ID.get(native_slug, native_slug)
    lower = native_slug.lower()
    if lower.startswith("deepseek"):
        return f"deepseek/{native_slug}"
    if lower.startswith("qwen"):
        return f"qwen/{native_slug}"
    if lower.startswith("glm"):
        return f"z-ai/{native_slug.lower()}"
    if lower.startswith("kimi") or lower.startswith("moonshot"):
        return f"moonshotai/{native_slug}"
    if lower.startswith("llama") or lower.startswith("meta"):
        return f"meta-llama/{native_slug}"
    if lower.startswith("hy") or lower.startswith("hunyuan"):
        return f"tencent/{native_slug}"
    if lower.startswith("minimax"):
        return f"minimax/{native_slug}"
    return native_slug
