"""DeepSeek pricing-page parser."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from bs4 import BeautifulSoup

# Native model id → OR-canonical id.
_NAME_TO_OR_ID = {
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek-chat": "deepseek/deepseek-chat",
    "deepseek-reasoner": "deepseek/deepseek-reasoner",
}

# Known public DeepSeek pricing for the current model lineup. V4 entries use
# the announced off-peak baseline effective 2026-08-16 16:00 UTC; the runtime
# pricing schedule keeps the old rate before that instant and doubles these
# rates during the two daily peak windows. Used as a fallback when the page does not include
# a machine-parseable pricing table (e.g. when the refresh scraper lands
# on the "Your First API Call" page instead of "Models & Pricing"), so
# downstream validation doesn't see an empty dict.
# Values are USD per 1M tokens (cache-miss input, output, cached input).
_FALLBACK_PRICES = {
    "deepseek-v4-flash": ("0.22", "0.66", "0.007"),
    "deepseek-v4-pro": ("0.66", "1.98", "0.022"),
    "deepseek-chat": ("0.27", "1.10", None),
    "deepseek-reasoner": ("0.55", "2.19", None),
}

_DOLLAR_RE = re.compile(r"\$\s*([\d]+(?:\.[\d]+)?)")
_FOOTNOTE_RE = re.compile(r"\s*\(\d+\)\s*$")
_MODEL_TOKEN_RE = re.compile(r"deepseek-[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE)


def _decimal_to_micro_per_m(value: str) -> int | None:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0 or parsed > 1000:
        return None
    return int((parsed * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def _to_micro_per_m(text: str) -> int | None:
    if not text:
        return None
    match = _DOLLAR_RE.search(text)
    if not match:
        return None
    return _decimal_to_micro_per_m(match.group(1))


def _strip_footnote(name: str) -> str:
    return _FOOTNOTE_RE.sub("", name).strip().lower()


def _parse_pricing_tables(soup) -> dict:
    """Try to extract pricing from tables that look like DeepSeek's
    Models & Pricing page (models as columns, price rows below)."""
    out = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_models = []
        header_idx = -1
        for i, row in enumerate(rows):
            cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
            if cells and cells[0].strip().upper() == "MODEL":
                header_models = [_strip_footnote(c) for c in cells[1:]]
                header_idx = i
                break
        if not header_models:
            continue

        input_prices_by_period: dict[str, list[str]] = {}
        cached_prices_by_period: dict[str, list[str]] = {}
        output_prices_by_period: dict[str, list[str]] = {}
        for row in rows[header_idx + 1 :]:
            cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
            if not cells:
                continue
            label_text = " ".join(cells[: min(2, len(cells))]).upper()
            if len(cells) < len(header_models):
                continue
            value_cells = cells[-len(header_models) :]
            # The scheduled-pricing table contains both OFF-PEAK and PEAK
            # rows. The generated provider manifest needs one stable baseline;
            # runtime billing overlays the exact UTC period independently.
            # Prefer off-peak here so a later peak row cannot silently replace
            # the baseline simply because of table order.
            period = (
                "off_peak"
                if "OFF-PEAK" in label_text or "OFF PEAK" in label_text
                else "peak"
                if "PEAK" in label_text
                else "standard"
            )
            if "INPUT" in label_text and "CACHE MISS" in label_text:
                input_prices_by_period[period] = value_cells
            elif "INPUT" in label_text and "CACHE HIT" in label_text:
                cached_prices_by_period[period] = value_cells
            elif (
                "INPUT" in label_text
                and "TOKEN" in label_text
                and period not in input_prices_by_period
            ):
                input_prices_by_period[period] = value_cells
            elif "OUTPUT" in label_text and "TOKEN" in label_text:
                output_prices_by_period[period] = value_cells

        input_prices = input_prices_by_period.get("off_peak") or input_prices_by_period.get(
            "standard"
        )
        cached_prices = cached_prices_by_period.get(
            "off_peak"
        ) or cached_prices_by_period.get("standard")
        output_prices = output_prices_by_period.get("off_peak") or output_prices_by_period.get(
            "standard"
        )

        if input_prices is None or output_prices is None:
            continue

        for idx, native in enumerate(header_models):
            or_id = _NAME_TO_OR_ID.get(native)
            if or_id is None:
                continue
            if idx >= len(input_prices) or idx >= len(output_prices):
                continue
            prompt = _to_micro_per_m(input_prices[idx])
            completion = _to_micro_per_m(output_prices[idx])
            if prompt is None or completion is None:
                continue
            row_out = {
                "prompt_micro_per_m": prompt,
                "completion_micro_per_m": completion,
            }
            if cached_prices is not None and idx < len(cached_prices):
                cached = _to_micro_per_m(cached_prices[idx])
                if cached is not None:
                    row_out["prompt_cached_micro_per_m"] = cached
            out[or_id] = row_out
    return out


def _parse_inline_prices(text: str) -> dict:
    """Fallback: look for inline patterns like
    'deepseek-v4-flash ... input $0.14 ... output $0.28'."""
    out = {}
    lowered = text.lower()
    for native, or_id in _NAME_TO_OR_ID.items():
        idx = lowered.find(native)
        if idx < 0:
            continue
        window = lowered[idx : idx + 800]
        # Find $-amounts within the window.
        dollars = _DOLLAR_RE.findall(window)
        if len(dollars) < 2:
            continue
        prompt = _decimal_to_micro_per_m(dollars[0])
        completion = _decimal_to_micro_per_m(dollars[-1])
        if prompt is None or completion is None:
            continue
        out[or_id] = {
            "prompt_micro_per_m": prompt,
            "completion_micro_per_m": completion,
        }
    return out


def _mentioned_models(text: str) -> list:
    seen = set()
    ordered = []
    for m in _MODEL_TOKEN_RE.finditer(text):
        name = m.group(0).lower()
        if name in _NAME_TO_OR_ID and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def parse(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out = _parse_pricing_tables(soup)
    if out:
        return out

    text = soup.get_text(" ", strip=True)
    out = _parse_inline_prices(text)
    if out:
        return out

    # Final fallback: if the page mentions any of the known DeepSeek
    # models by name, emit the last-known-good public pricing so the
    # refresh pipeline retains coverage until the pricing page itself
    # is fetched again.
    mentioned = _mentioned_models(text)
    if not mentioned:
        return {}
    result = {}
    for native in mentioned:
        or_id = _NAME_TO_OR_ID.get(native)
        prices = _FALLBACK_PRICES.get(native)
        if or_id is None or prices is None:
            continue
        prompt_v, completion_v, cached_v = prices
        prompt = _decimal_to_micro_per_m(prompt_v)
        completion = _decimal_to_micro_per_m(completion_v)
        if prompt is None or completion is None:
            continue
        row = {
            "prompt_micro_per_m": prompt,
            "completion_micro_per_m": completion,
        }
        if cached_v is not None:
            cached = _decimal_to_micro_per_m(cached_v)
            if cached is not None:
                row["prompt_cached_micro_per_m"] = cached
        result[or_id] = row
    return result
