"""Exact US Claude Runtime prices; independent of Mantle discovery/eligibility."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, PriceTier, fetch_html, fetch_json
from trusted_router.provider_contracts import BEDROCK_RUNTIME_PROFILES

PAGE_URL = "https://aws.amazon.com/bedrock/pricing/"
CARD_URL = "https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html"
ARTIFACT_BASE = "https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps"
REGION = "US East (N. Virginia)"
SCOPE = "Geo and In-region Cross-region Inference"
MODELS = {
    "Claude Haiku 5.5": "anthropic/claude-haiku-5.5",
    "Claude Sonnet 5.5": "anthropic/claude-sonnet-5.5",
    "Claude Opus 5.5": "anthropic/claude-opus-5.5",
    "Claude Sonnet 5": "anthropic/claude-sonnet-5",
    "Claude Opus 4.8": "anthropic/claude-opus-4.8",
}
PROFILES = BEDROCK_RUNTIME_PROFILES
RETENTION_REGIONS = ("us-east-1", "us-east-2", "us-west-2")
LONG_CONTEXT_ROW = "Claude Haiku 5.5 - Long Context"
HEADERS = (
    "Anthropic models", "Price per 1M input tokens", "Price per 1M output tokens",
    "Price per 1M input tokens (batch)", "Price per 1M output tokens (batch)",
    "Price per 1M input tokens (5m cache write)", "Price per 1M input tokens (1h cache write)",
    "Price per 1M input tokens (cache read)",
)
_TOKEN = re.compile(r"\{priceOf!bedrockfoundationmodels/([A-Za-z0-9_-]+)!([A-Za-z0-9_-]+)(?:!opt)?\}")


def price_references(html: str) -> dict[str, list[tuple[str, str]]]:
    """Read only exact standard US-scope table cells, never global/batch prices."""
    selected: list[Any] = []
    for widget in BeautifulSoup(html, "html.parser").select("[data-pricing-markup]"):
        markup = BeautifulSoup(widget["data-pricing-markup"], "html.parser")
        heading = markup.find("h2")
        headers = tuple(cell.get_text(" ", strip=True) for cell in markup.select("th"))
        if (
            heading is not None and heading.get_text(" ", strip=True) == SCOPE
            and headers[:2] == HEADERS[:2]
        ):
            selected.append(markup)
    if len(selected) != 1:
        raise RuntimeError("bedrock-runtime: exact US price table missing or ambiguous")
    table = selected[0]
    if tuple(cell.get_text(" ", strip=True) for cell in table.select("th")) != HEADERS:
        raise RuntimeError("bedrock-runtime: price columns or units changed")
    rows: dict[str, list[tuple[str, str]]] = {}
    expected = {*MODELS, LONG_CONTEXT_ROW}
    for row in table.select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td", recursive=False)]
        if not cells or cells[0] not in expected:
            continue
        if len(cells) != len(HEADERS) or cells[0] in rows:
            raise RuntimeError("bedrock-runtime: malformed or duplicate model row")
        refs: list[tuple[str, str]] = []
        for index in (1, 2, 5, 7):
            match = _TOKEN.fullmatch(cells[index])
            if match is None:
                raise RuntimeError("bedrock-runtime: missing exact price reference")
            refs.append((match[1], match[2]))
        rows[cells[0]] = refs
    if set(rows) != expected:
        raise RuntimeError("bedrock-runtime: reviewed model prices incomplete")
    return rows


def _micro_price(artifact: object, key: str) -> int:
    try:
        if not isinstance(artifact, dict):
            raise ValueError("invalid artifact")
        row = artifact["regions"][REGION][key]
        if row["RegionlessRateCode"] != key or not isinstance(row["price"], str):
            raise ValueError("invalid rate identity")
        value = Decimal(row["price"]) * 1_000_000
        if not value.is_finite() or not 0 < value <= 1_000_000_000 or value != value.to_integral_value():
            raise ValueError("invalid rate")
        return int(value)
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise RuntimeError("bedrock-runtime: missing or invalid regional USD price") from exc


def parse_prices(html: str, artifacts: dict[str, object], card: str) -> dict[str, ModelPrice]:
    text = BeautifulSoup(card, "html.parser").get_text(" ", strip=True)
    if "Claude Haiku 5.5" not in text or (
        "Prompts of 100,000 tokens or fewer are billed at the standard per-token rates." not in text
        or "Prompts that exceed 100,000 tokens are billed at higher rates for input, output, and cache read and write operations." not in text
    ):
        raise RuntimeError("bedrock-runtime: Haiku context-price boundary requires review")
    rates: dict[str, tuple[int, int, int]] = {}
    for name, refs in price_references(html).items():
        prompt, completion, write, read = [_micro_price(artifacts.get(path), key) for path, key in refs]
        # The shared five-minute cache-write ledger uses exactly 1.25x input.
        # A changed upstream tariff must block publication, not lose margin.
        if write * 4 != prompt * 5 or read > prompt:
            raise RuntimeError("bedrock-runtime: cache tariff requires billing review")
        rates[name] = (prompt, completion, read)
    result = {
        model: ModelPrice(rates[name][0], rates[name][1], prompt_cached_micro_per_m=rates[name][2])
        for name, model in MODELS.items()
    }
    standard, high = rates["Claude Haiku 5.5"], rates[LONG_CONTEXT_ROW]
    if any(h < low for low, h in zip(standard, high, strict=True)):
        raise RuntimeError("bedrock-runtime: inverted Haiku long-context prices")
    result[MODELS["Claude Haiku 5.5"]] = ModelPrice(tiers=[
        PriceTier(100_000, *standard), PriceTier(None, *high),
    ])
    return result


def fetch_runtime_prices() -> dict[str, ModelPrice]:
    html = fetch_html(PAGE_URL)
    paths = {path for refs in price_references(html).values() for path, _ in refs}
    # Paths are restricted identifiers from the public widget, not arbitrary URLs.
    artifacts = {path: fetch_json(f"{ARTIFACT_BASE}/bedrockfoundationmodels/USD/current/{path}.json") for path in paths}
    return parse_prices(html, artifacts, fetch_html(CARD_URL))


def model_metadata(html: str, model: str) -> dict[str, Any]:
    profile = PROFILES[model]
    soup = BeautifulSoup(html, "html.parser")
    if not any(
        "bedrock-runtime" in [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
        and profile in row.get_text(" ", strip=True).split()
        for row in soup.select("tr")
    ):
        raise RuntimeError("bedrock-runtime: exact US profile absent from model card")
    fields = {"Context window": "context_length", "Max output tokens": "max_output_tokens"}
    limits: dict[str, int] = {}
    for item in soup.select("li"):
        match = re.fullmatch(r"(Context window|Max output tokens):\s*([0-9,]+(?:\.[0-9]+)?)\s*([KM]?)(?:\s+tokens)?", item.get_text(" ", strip=True))
        if match:
            field = fields[match[1]]
            value = Decimal(match[2].replace(",", "")) * {"": 1, "K": 1000, "M": 1_000_000}[match[3]]
            if value != value.to_integral_value() or (field in limits and limits[field] != value):
                raise RuntimeError("bedrock-runtime: ambiguous model limits")
            limits[field] = int(value)
    if not 0 < limits.get("max_output_tokens", 0) <= limits.get("context_length", 0) <= 10_000_000:
        raise RuntimeError("bedrock-runtime: missing or invalid model limits")
    return {
        "id": model, "upstream_id": profile,
        "display_name": next(name for name, model_id in MODELS.items() if model_id == model),
        **limits, "input_modalities": ["text"], "output_modalities": ["text"],
        "supported_features": ["streaming"], "endpoints": ["chat/completions"],
        "routable": False, "routable_reason": "runtime-enclave-rollout-pending",
    }


def fetch_runtime_rows(api_key: str) -> dict[str, dict[str, Any]]:
    # The serving adapter repeats this check on every request. A catalog refresh
    # is evidence for discovery, never a replacement for runtime enforcement.
    for region in RETENTION_REGIONS:
        policy = fetch_json(
            f"https://bedrock.{region}.amazonaws.com/data-retention",
            extra_headers={"Authorization": f"Bearer {api_key}"},
        )
        if not isinstance(policy, dict) or policy.get("mode") != "none":
            raise RuntimeError("bedrock-runtime: all US destinations must have retention none")
    return {
        model: model_metadata(fetch_html(
            "https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-"
            + profile.removeprefix("us.").replace(".", "-") + ".html"
        ), model)
        for model, profile in PROFILES.items()
    }


def probe_runtime(api_key: str, model: str) -> bool:
    profile = PROFILES[model]
    try:
        # A bounded synthetic request, once only; never test a Runtime profile
        # against Mantle's OpenAI-compatible endpoint.
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            with client.stream(
                "POST", f"https://bedrock-runtime.us-east-1.amazonaws.com/model/{profile}/invoke",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"anthropic_version": "bedrock-2023-05-31", "max_tokens": 128,
                      "messages": [{"role": "user", "content": "Reply exactly PONG."}]},
            ) as response:
                if response.status_code != 200:
                    return False
                data = bytearray()
                for part in response.iter_bytes():
                    data.extend(part)
                    if len(data) > 65536:
                        return False
        body = json.loads(data)
        usage = body.get("usage", {})
        return (
            body.get("stop_reason") == "end_turn"
            and all(type(usage.get(key)) is int and usage[key] > 0 for key in ("input_tokens", "output_tokens"))
            and any(block.get("type") == "text" and block.get("text", "").strip() == "PONG"
                    for block in body.get("content", []))
        )
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return False
