"""Upstage Solar authenticated catalog joined to official token prices."""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_html
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "upstage"
BASE_URL = "https://api.upstage.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://www.upstage.ai/pricing/api"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/upstage.json"
)
MANIFEST_STALE_FALLBACK = True
EXPLICIT_MODEL_MAP = {
    "solar-pro2": "upstage/solar-pro2",
    "solar-pro3": "upstage/solar-pro3",
    "solar-pro4": "upstage/solar-pro4",
}

_DISPLAY_MODEL_MAP = {
    "Solar Pro 2": "upstage/solar-pro2",
    "Solar Pro 3": "upstage/solar-pro3",
    "Solar Pro 4": "upstage/solar-pro4",
}
_PRICING_VALID_UNTIL: datetime | None = None


def _micro_per_m(raw: str) -> int:
    if re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", raw) is None:
        raise RuntimeError("upstage: invalid token price")
    return int(Decimal(raw) * Decimal(1_000_000))


def _promotion(
    source: str, now: datetime
) -> tuple[dict[str, int] | None, datetime | None]:
    if not source.strip():
        return None, None
    active = None
    deadline = None
    previous_end = None
    for line in source.strip().splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            raise RuntimeError("upstage: malformed promotion schedule")
        try:
            start, end = (datetime.fromisoformat(value.replace("Z", "+00:00")) for value in parts[:2])
        except ValueError as exc:
            raise RuntimeError("upstage: malformed promotion dates") from exc
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise RuntimeError("upstage: invalid promotion dates")
        if previous_end is not None and start < previous_end:
            raise RuntimeError("upstage: overlapping promotion schedules")
        previous_end = end
        if parts[2:] == ["free"]:
            axes = dict.fromkeys(("input", "cached", "output"), 0)
        else:
            axes = {}
            for part in parts[2:]:
                key, sep, value = part.partition("=")
                if not sep or key not in {"input", "cached", "output"} or key in axes:
                    raise RuntimeError("upstage: invalid promotion prices")
                axes[key] = _micro_per_m(value)
            if set(axes) != {"input", "cached", "output"}:
                raise RuntimeError("upstage: incomplete promotion prices")
        if axes["cached"] > axes["input"]:
            raise RuntimeError("upstage: cached price exceeds input price")
        if start <= now < end:
            active = axes
        for transition in (start, end):
            if transition > now and (deadline is None or transition < deadline):
                deadline = transition
    return active, deadline


def _parse_pricing_document(
    source: str, *, now: datetime
) -> tuple[dict[str, ModelPrice], datetime | None]:
    soup = BeautifulSoup(source, "html.parser")
    prices: dict[str, ModelPrice] = {}
    deadline = None
    for card in soup.select(".pricing-card-v2"):
        heading = card.find("h4")
        if heading is None:
            continue
        model_id = _DISPLAY_MODEL_MAP.get(heading.get_text(" ", strip=True))
        if model_id is None:
            continue
        axes: dict[str, int] = {}
        for feature in card.select(".pricing-feature-v2"):
            rate = feature.select_one("[data-rate]")
            if rate is not None:
                axis = rate.get("data-rate")
                unit = feature.select_one("[data-rate-unit]")
                if axis not in {"input", "cached", "output"} or unit is None or unit.get_text(" ", strip=True) != "1M tokens":
                    raise RuntimeError(f"upstage: invalid rate axis or unit for {model_id}")
                if axis in axes:
                    raise RuntimeError(f"upstage: duplicate rate axis for {model_id}")
                axes[str(axis)] = _micro_per_m(rate.get_text(strip=True))
                continue
            text = feature.get_text(" ", strip=True)
            match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)\s*/\s*1M tokens", text)
            if match is None:
                continue
            if text.startswith("Input(Cached)"):
                axes["cached"] = _micro_per_m(match.group(1))
            elif text.startswith("Input"):
                axes["input"] = _micro_per_m(match.group(1))
            elif text.startswith("Output"):
                axes["output"] = _micro_per_m(match.group(1))
        if set(axes) != {"input", "cached", "output"}:
            raise RuntimeError(f"upstage: incomplete pricing card for {model_id}")
        schedules = card.select('[data-promo="schedule"]')
        if len(schedules) > 1:
            raise RuntimeError(f"upstage: duplicate promotion schedules for {model_id}")
        if schedules:
            promoted, next_change = _promotion(schedules[0].get_text(), now)
            if promoted is not None:
                axes = promoted
            if next_change is not None and (deadline is None or next_change < deadline):
                deadline = next_change
        if axes["cached"] > axes["input"]:
            raise RuntimeError(f"upstage: cached price exceeds input price for {model_id}")
        price = ModelPrice(
            axes["input"],
            axes["output"],
            prompt_cached_micro_per_m=axes["cached"],
        )
        if model_id in prices and prices[model_id] != price:
            raise RuntimeError(f"upstage: conflicting prices for {model_id}")
        prices[model_id] = price
    if not prices:
        raise RuntimeError("upstage: no documented token prices")
    return prices, deadline


def _parse_pricing(source: str) -> dict[str, ModelPrice]:
    return _parse_pricing_document(source, now=datetime.now(UTC))[0]


def _load_prices() -> dict[str, ModelPrice]:
    global _PRICING_VALID_UNTIL
    _PRICING_VALID_UNTIL = None
    prices, _PRICING_VALID_UNTIL = _parse_pricing_document(
        fetch_html(PRICING_URL), now=datetime.now(UTC)
    )
    return prices


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="UPSTAGE_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        price_loader=_load_prices,
        expected_models=("upstage/solar-pro4",),
        pricing_source_url=PRICING_URL,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    notes = CATALOG.write_provider_manifest(result)
    raw = json.loads(CATALOG.manifest_path.read_text(encoding="utf-8"))
    if _PRICING_VALID_UNTIL is None:
        raw.pop("pricing_valid_until", None)
    else:
        raw["pricing_valid_until"] = _PRICING_VALID_UNTIL.isoformat()
    CATALOG.manifest_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return notes
