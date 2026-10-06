"""BytePlus ModelArk native inventory and exact standard-inference tariffs."""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from scripts.pricing.base import (
    ModelPrice,
    PriceTier,
    ProviderPricingResult,
    fetch_html,
    fetch_json,
    validate,
)
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.openai_catalog import probe_openai_chat

SLUG = "byteplus"
BASE_URL = "https://ark.ap-southeast.bytepluses.com/api/v3"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://docs.byteplus.com/en/docs/modelark/model-pricing"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/byteplus.json"
MANIFEST_STALE_FALLBACK = True
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}

# Flip only after the native worker and current key are deployed in every cloud.
# Discovery must not publish routes an older attested worker cannot fulfill.
NATIVE_ROUTES_DEPLOYED = True
VIDEO_MODELS = {
    "dreamina-seedance-2-5-260628": "bytedance/seedance-2.5",
    "dreamina-seedance-2-0-260128": "bytedance/seedance-2.0",
    "dreamina-seedance-2-0-fast-260128": "bytedance/seedance-2.0-fast",
}
MODEL_IDS = {
    **VIDEO_MODELS,
    "glm-5-3-flash-260828": "z-ai/glm-5.3-flash",
    "glm-5-2-260617": "z-ai/glm-5.2",
    "deepseek-v4-pro-ga-260813": "deepseek/deepseek-v4-pro-0813",
    "deepseek-v4-pro-260425": "deepseek/deepseek-v4-pro-0423",
    "deepseek-v4-flash-ga-260731": "deepseek/deepseek-v4-flash-0731",
    "deepseek-v4-flash-260425": "deepseek/deepseek-v4-flash",
    "deepseek-v4-1-flash-260910": "deepseek/deepseek-v4.1-flash",
}
PRICE_ALIASES = {"dola-seed-2-1-turbo-260628": "dola-seed-2-1-turbo"}


def canonical_model_id(native_id: str) -> str | None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", native_id):
        return None
    return MODEL_IDS.get(native_id, f"bytedance/{native_id}")


def pricing_markdown(html: str) -> str:
    for script in BeautifulSoup(html, "html.parser").find_all("script"):
        text = script.string or ""
        if re.match(r"\s*window\._ROUTER_DATA\s*=", text):
            data, _ = json.JSONDecoder().raw_decode(text.split("=", 1)[1].strip())
            md = data["loaderData"]["(lang)/docs/(libcode)/(doccode$)/page"]["curDoc"]["MDContent"]
            if isinstance(md, str) and md:
                return md
    raise RuntimeError("byteplus: authoritative pricing document missing")


def _usd(value: str) -> int:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise RuntimeError("byteplus: ambiguous token price")
    amount = Decimal(value) * 1_000_000
    if amount != amount.to_integral_value():
        raise RuntimeError("byteplus: unsupported price precision")
    return int(amount)


def _table(section: str, columns: int) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip().replace("\\-", "-") for c in line.strip()[1:-1].split("|")]
        if cells[0].startswith("**Model") or all(c == "---" for c in cells):
            continue
        if len(cells) != columns:
            raise RuntimeError("byteplus: pricing table shape changed")
        rows.append(cells)
    return rows


def video_resolution_prices(md: str) -> dict[str, dict[str, int]]:
    """Parse no-video-input list tariffs, never promotional or video-input rates."""
    try:
        video = md.split("# Video generation models", 1)[1].split("## Pricing", 1)[1].split("## Price examples", 1)[0]
    except IndexError as exc:
        raise RuntimeError("byteplus: pricing sections changed") from exc
    prices: dict[str, dict[str, int]] = {}
    for cells in _table(video, 3):
        native = cells[0].split("<br>", 1)[0].strip()
        if native not in VIDEO_MODELS:
            continue
        if native in prices:
            raise RuntimeError("byteplus: conflicting video tariff")
        text = BeautifulSoup(cells[1], "html.parser").get_text(" ", strip=True)
        rates = {}
        for label, resolutions in (("480p and 720p", ("480p", "720p")), ("1080p", ("1080p",))):
            heading = f"For {label} outputs:"
            if label == "1080p" and heading not in text:
                continue
            matches = re.findall(
                re.escape(heading) + r"\s*\*?\s*Input without video:\s*(?:\(Original\)\s*)?([0-9.]+)(?=\s|$)",
                text,
            )
            if text.count(heading) != 1 or len(matches) != 1:
                raise RuntimeError("byteplus: missing or conflicting video tariff")
            rate = _usd(matches[0])
            if rate <= 0:
                raise RuntimeError("byteplus: invalid video tariff")
            rates.update(dict.fromkeys(resolutions, rate))
        prices[native] = rates
    if not VIDEO_MODELS.keys() <= prices.keys():
        raise RuntimeError("byteplus: required video prices missing")
    return prices


def parse_prices(md: str) -> dict[str, ModelPrice]:
    try:
        standard = md.split("## Online inference (standard)", 1)[1].split("## Online inference (Flex)", 1)[0]
    except IndexError as exc:
        raise RuntimeError("byteplus: pricing sections changed") from exc
    tiers: dict[str, list[PriceTier]] = {}
    native = ""
    for cells in _table(standard, 8):
        native = cells[0] or native
        if not native:
            raise RuntimeError("byteplus: orphan pricing tier")
        # Time-based rates need a runtime clock-aware tariff, not a fake
        # context tier or an always-off-peak charge.
        if cells[1] in {"Off-peak hours", "Peak hours"}:
            continue
        match = re.fullmatch(r"Prompt length [\[(]([0-9]+), ([0-9]+)\]", cells[1])
        if cells[1] != "-" and match is None:
            raise RuntimeError("byteplus: unknown pricing tier")
        limit = int(match[2]) * 1_000 if match else None
        tiers.setdefault(native, []).append(PriceTier(
            limit, _usd(cells[2]), _usd(cells[7]),
            None if cells[5] == "-" else _usd(cells[5]),
        ))
    prices = {key: ModelPrice(tiers=[*rows[:-1], replace(rows[-1], max_prompt_tokens=None)]) for key, rows in tiers.items()}
    for native, rates in video_resolution_prices(md).items():
        if native in prices:
            raise RuntimeError("byteplus: missing or conflicting video tariff")
        prices[native] = ModelPrice(0, rates["720p"])
    if not VIDEO_MODELS.keys() <= prices.keys() or not tiers:
        raise RuntimeError("byteplus: required standard/video prices missing")
    if errors := validate(prices, []):
        raise RuntimeError("; ".join(errors))
    return prices


def discover(payload: object, md: str) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("byteplus: catalog data missing")
    tariffs = parse_prices(md)
    video_tariffs = video_resolution_prices(md)
    prices: dict[str, ModelPrice] = {}
    rows: dict[str, dict[str, Any]] = {}
    for source in payload["data"]:
        native = source.get("id") if isinstance(source, dict) else None
        model_id = canonical_model_id(native) if isinstance(native, str) else None
        if model_id is None or model_id in rows:
            raise RuntimeError("byteplus: invalid or duplicate catalog identity")
        row: dict[str, Any] = {"id": model_id, "upstream_id": native, "display_name": source.get("name", native)}
        rows[model_id] = row
        reason = None
        if source.get("status") in {"Shutdown", "Retiring"}:
            reason = "provider-retired"
        elif source.get("status") not in {None, ""}:
            reason = "unknown-provider-status"
        elif native in VIDEO_MODELS:
            row.update(model_type="video", billing_unit="output_tokens", endpoints=["videos"], input_modalities=["text", "image"], output_modalities=["video"], context_length=0)
            row["output_token_price_per_m_by_resolution"] = video_tariffs[native]
            prices[model_id] = tariffs[native]
            if not NATIVE_ROUTES_DEPLOYED:
                reason = "gateway-upgrade-required"
            else:
                row["routable"] = True
        elif source.get("domain") in {"LLM", "VLM"}:
            price = tariffs.get(PRICE_ALIASES.get(native, native))
            if price is None:
                reason = "time-dependent-pricing-unsupported" if native == "deepseek-v4-1-flash-260910" else "awaiting-price"
            else:
                limits = source.get("token_limits", {})
                row.update(model_type="chat", endpoints=["chat/completions"],
                           input_modalities=["text"], output_modalities=["text"],
                           context_length=limits.get("context_window", 0),
                           max_output_tokens=limits.get("max_output_token_length", 0))
                features = source.get("features", {})
                row["supported_features"] = ["streaming"]
                if features.get("tools", {}).get("function_calling") is True:
                    row["supported_features"].append("tools")
                if features.get("cache", {}).get("prefix_cache") is True:
                    row["supported_features"].append("prompt_caching")
                prices[model_id] = price
        else:
            reason = "unsupported-api"
        if reason:
            row.update(routable=False, routable_reason=reason)
    if not prices:
        raise RuntimeError("byteplus: no live priced models")
    return prices, rows


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_ROWS  # noqa: PLW0603
    _DISCOVERED_ROWS = {}
    UPSTREAM_ID_MAP.clear()
    key = os.environ.get("BYTEPLUS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("byteplus: BYTEPLUS_API_KEY is required")
    prices, rows = discover(fetch_json(URL, extra_headers={"Authorization": f"Bearer {key}"}), pricing_markdown(fetch_html(PRICING_URL)))
    chats = {key for key in prices if rows[key].get("model_type") == "chat"}
    needs_canary = models_requiring_canary(MANIFEST_PATH, chats)
    if NATIVE_ROUTES_DEPLOYED:
        needs_canary |= models_requiring_canary(
            MANIFEST_PATH, chats, failure_reason="gateway-upgrade-required",
        )
    checked = sorted(needs_canary)
    def probe(model_id: str) -> bool:
        return probe_openai_chat(base_url=BASE_URL, api_key=key, model=rows[model_id]["upstream_id"],
                                 max_tokens=512, expected_content="PONG", prompt="Reply with exactly PONG and nothing else.")
    with ThreadPoolExecutor(max_workers=3) as pool:
        healthy = {model_id for model_id, passed in zip(checked, pool.map(probe, checked), strict=True) if passed}
    apply_canary_results(rows, checked_model_ids=checked, healthy_model_ids=healthy)
    UPSTREAM_ID_MAP.update({model_id: row["upstream_id"] for model_id, row in rows.items()})
    _DISCOVERED_ROWS = rows
    return ProviderPricingResult(slug=SLUG, prices=prices, source="api", fetched_url=PRICING_URL,
                                 price_index_model_ids=frozenset(chats), notes=[f"{len(healthy)}/{len(checked)} new chat canaries passed"])


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if not _DISCOVERED_ROWS:
        raise RuntimeError("byteplus: fetch must succeed before writing manifest")
    holds = {} if NATIVE_ROUTES_DEPLOYED else {
        model_id: "gateway-upgrade-required" for model_id in result.prices
        if _DISCOVERED_ROWS[model_id].get("routable_reason") in {None, "gateway-upgrade-required"}
    }
    return write_discovered_chat_manifest(result, manifest_path=MANIFEST_PATH, discovered_rows=_DISCOVERED_ROWS,
                                          source_url=URL, pricing_source_url=PRICING_URL, operator_hold_reasons=holds)
