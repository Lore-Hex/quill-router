"""Regolo's authenticated chat and decision catalog, priced in EUR."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from defusedxml.ElementTree import fromstring

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_html, fetch_json
from scripts.pricing.manifest import apply_canary_results, models_requiring_canary
from scripts.pricing.openai_catalog import dollars_per_token_to_micro_per_m, positive_int
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "regolo"
BASE_URL = "https://api.regolo.ai/v1"
URL = "https://api.regolo.ai/model_group/info"
PRICING_URL = "https://docs.regolo.ai/models/catalog/"
ECB_FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/regolo.json"
MANIFEST_STALE_FALLBACK = True
DECISION_MODEL_ID = "cloudflare/clef-flash"

MODEL_MAP = {
    "clef-flash": DECISION_MODEL_ID,
    "apertus-70b": "swiss-ai/apertus-70b-instruct",
    "gemma4-31b": "google/gemma-4-31b-it",
    "glm5.2": "z-ai/glm-5.2",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "gpt-oss-20b": "openai/gpt-oss-20b",
    "Llama-3.3-70B-Instruct": "meta-llama/llama-3.3-70b-instruct",
    "mistral-small-4-119b": "mistralai/mistral-small-2603",
    "qwen3-coder-next": "qwen/qwen3-coder-next",
    "qwen3.5-122b": "qwen/qwen3.5-122b-a10b",
    "qwen3.5-9b": "qwen/qwen3.5-9b",
    "qwen3.8-27b": "qwen/qwen3.8-27b",
}


def usd_per_eur(xml: str, *, today: date | None = None) -> Decimal:
    root = fromstring(xml)
    today = today or datetime.now(UTC).date()
    for day in root.iter():
        if "time" not in day.attrib:
            continue
        observed = date.fromisoformat(day.attrib["time"])
        if not 0 <= (today - observed).days <= 7:
            raise RuntimeError("regolo: ECB exchange rate is stale or future-dated")
        for entry in day:
            if entry.attrib.get("currency") == "USD":
                rate = Decimal(entry.attrib["rate"])
                if rate.is_finite() and rate > 0:
                    return rate
    raise RuntimeError("regolo: ECB feed has no valid USD/EUR rate")


def normalize_catalog(payload: object, rate: Decimal) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("regolo: catalog has no data list")
    if not rate.is_finite() or rate <= 0:
        raise RuntimeError("regolo: invalid exchange rate")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload["data"]:
        if not isinstance(item, dict) or item.get("mode") != "chat":
            continue
        native_id = item.get("model_group")
        # Brick is itself a router, not a concrete inference model. It stays
        # outside this directly hosted, renewable-inference catalog.
        if not isinstance(native_id, str) or not native_id or native_id.startswith("brick-"):
            continue
        if native_id in seen:
            raise RuntimeError("regolo: duplicate model group")
        seen.add(native_id)
        try:
            prompt = Decimal(str(item["input_cost_per_token"]))
            completion = Decimal(str(item["output_cost_per_token"]))
        except (KeyError, InvalidOperation, TypeError, ValueError):
            continue
        if not all(value.is_finite() and value > 0 for value in (prompt, completion)):
            continue
        input_limit = positive_int(item.get("max_input_tokens"))
        output_limit = positive_int(item.get("max_output_tokens"))
        if not input_limit or not output_limit:
            continue
        context = min(input_limit, positive_int(item.get("max_tokens")) or input_limit)
        features = ["streaming"]
        if item.get("supports_function_calling") is True:
            features.append("function-calling")
        if item.get("supports_reasoning") is True:
            features.append("reasoning")
        if item.get("supports_response_schema") is True:
            features.append("structured-outputs")
        result.append({
            "id": native_id,
            "name": native_id,
            "context_length": context,
            "max_output_tokens": min(output_limit, context),
            "input_modalities": ["text", "image"] if item.get("supports_vision") is True else ["text"],
            "output_modalities": ["text"],
            "supported_features": features,
            # The API's supported_openai_params is a LiteLLM-wide list, even
            # on OCR/audio rows. Do not claim web search or state from it.
            "supported_sampling_parameters": ["temperature", "top_p", "max_tokens"],
            "pricing": {"prompt": str(prompt * rate), "completion": str(completion * rate)},
        })
    return result


def load_catalog(api_key: str) -> list[dict[str, Any]]:
    rate = usd_per_eur(fetch_html(ECB_FX_URL))
    payload = fetch_json(URL, extra_headers={"Authorization": f"Bearer {api_key}"})
    return normalize_catalog(payload, rate)


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG, base_url=BASE_URL, api_key_env="REGOLO_API_KEY",
        explicit_model_map=MODEL_MAP, namespace_unqualified="regolo",
        catalog_url=URL, pricing_source_url=PRICING_URL, catalog_loader=load_catalog,
        canary_max_tokens=512, canary_expected_content="PONG",
        canary_prompt="Reply with exactly PONG and nothing else.",
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
write_provider_manifest = CATALOG.write_provider_manifest


def discover_decisions(payload: object, rate: Decimal) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("regolo: catalog has no data list")
    if not rate.is_finite() or rate <= 0:
        raise RuntimeError("regolo: invalid exchange rate")
    matches = [row for row in payload["data"] if isinstance(row, dict) and row.get("model_group") == "clef-flash"]
    if not matches:
        return {}, {}
    if len(matches) != 1:
        raise RuntimeError("regolo: duplicate decision model")
    item = matches[0]
    try:
        prompt = Decimal(str(item["input_cost_per_token"]))
        output = Decimal(str(item["output_cost_per_token"]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("regolo: missing decision token price") from exc
    context = positive_int(item.get("max_input_tokens"))
    if (
        item.get("mode") != "decision" or not context
        or not prompt.is_finite() or prompt <= 0
        or not output.is_finite() or output != 0
    ):
        raise RuntimeError("regolo: unsupported decision billing contract")
    price = dollars_per_token_to_micro_per_m(prompt * rate)
    if price is None or price <= 0:
        raise RuntimeError("regolo: invalid decision input price")
    example = {
        "model": DECISION_MODEL_ID, "state": "Mia owns a red bicycle.",
        "questions": {"color": {"type": "choice", "instructions": "Which color is the bicycle?", "criteria": {"red": None, "blue": None}}},
    }
    row = {
        "id": DECISION_MODEL_ID, "upstream_id": "clef-flash", "display_name": "Cloudflare Clef-Flash",
        "model_type": "decision", "endpoints": ["decide"], "context_length": context,
        "input_modalities": ["text"], "output_modalities": ["decision"],
        "supported_features": [], "supported_sampling_parameters": [],
        "documentation": {
            "description": "Cloudflare's 9B decision model, hosted by Regolo. Returns typed answers and probabilities rather than chat text.",
            "input_format": "POST /v1/decide: text state and 1-64 boolean/noul, choice or score questions. Text-only on this route; images and streaming are not supported.",
            "output_format": "Verified answers and probabilities with inputTokens usage. Input-only billing; output tokens are free.",
            "example_input": json.dumps(example),
        },
    }
    return {DECISION_MODEL_ID: ModelPrice(price, 0)}, {DECISION_MODEL_ID: row}


def probe_decision(api_key: str) -> bool:
    try:
        response = httpx.post(
            f"{BASE_URL}/systemone", timeout=30,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "clef-flash", "state": "Mia owns a red bicycle.", "questions": {
                "color": {"type": "choice", "instructions": "Which color is the bicycle?", "criteria": {"red": None, "blue": None}},
            }},
        )
        response.raise_for_status()
        body = response.json()
        usage = body["usage"]
        return bool(
            body["model"] == "clef-flash" and body["answers"]["color"]["choice"] == "red"
            and type(usage["input_tokens"]) is int and usage["input_tokens"] > 0
            and type(usage["output_tokens"]) is int and usage["output_tokens"] == 0
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return False


def fetch() -> ProviderPricingResult:
    result = CATALOG.fetch()
    CATALOG._fetched = False
    key = os.environ["REGOLO_API_KEY"]
    prices, rows = discover_decisions(
        fetch_json(URL, extra_headers={"Authorization": f"Bearer {key}"}),
        usd_per_eur(fetch_html(ECB_FX_URL)),
    )
    checked = models_requiring_canary(CATALOG.manifest_path, set(rows))
    healthy = checked if checked and probe_decision(key) else set()
    apply_canary_results(rows, checked_model_ids=checked, healthy_model_ids=healthy)
    CATALOG.discovered_rows.update(rows)
    UPSTREAM_ID_MAP.update({model: row["upstream_id"] for model, row in rows.items()})
    result.prices.update(prices)
    result.notes.append(f"discovered {len(rows)} priced decision models; canaried {len(checked)}, {len(healthy)} passed")
    CATALOG._fetched = True
    return result
