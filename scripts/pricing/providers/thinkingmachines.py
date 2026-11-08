"""Thinking Machines Lab Tinker model pricing refresh."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json
from scripts.pricing.manifest import set_manifest_model_canary_states
from scripts.pricing.openai_catalog import probe_openai_chat
from scripts.pricing.parsers.thinkingmachines import _SAMPLER_IDS, _SERVERLESS_IDS

SLUG = "thinkingmachines"
URL = "https://tinker-docs.thinkingmachines.ai/tinker/models/"
SERVERLESS_URL = "https://tinker-docs.thinkingmachines.ai/tinker/serverless.json"
SAMPLER_URL = "https://tinker-docs.thinkingmachines.ai/tinker/models.json"
EXPECTED_MODELS = [
    "thinkingmachines/inkling",
    "thinkingmachines/inkling-2-small",
    "thinkingmachines/inkling-small",
    "z-ai/glm-5.3",
]
BASE_URL = "https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1"
API_KEY_ENVS = ("THINKING_MACHINES_API_KEY", "TINKER_API_KEY")
# Listed in serverless.json, but TR's Tinker key gets a model-level 4xx for it:
# the configured-route probe classified six straight attempts as
# unsupported_route (Sentry QUILL-ROUTER-FD, 2026-11-08). Serverless inference
# is a per-model beta, so this is account access, not a TR adapter defect.
# Inkling and Inkling-Small serve on the same key and stay routable.
#
# The hold lives in the committed manifest, so price refreshes keep it. It
# lifts by itself the first time an authenticated canary to the exact upstream
# ID returns 200: run the refresh with THINKING_MACHINES_API_KEY set (the
# hourly workflow does not receive that key today). To lift it by hand once
# access is confirmed, delete the row's "routable"/"routable_reason" fields.
ACCESS_HOLD_REASON = "provider-access-unavailable"
OPERATOR_HOLDS = {"thinkingmachines/inkling-2-small": ACCESS_HOLD_REASON}
_ACCESS_CANARY_CHECKED: set[str] = set()
_ACCESS_CANARY_HEALTHY: set[str] = set()
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "thinkingmachines.json"
)


def _json_prices(
    payload: object, model_ids: dict[str, str], fields: tuple[str, str, str]
) -> dict[str, ModelPrice]:
    if not isinstance(payload, list):
        raise RuntimeError("thinkingmachines: pricing feed must be a list")
    prices: dict[str, ModelPrice] = {}
    for row in payload:
        if not isinstance(row, dict):
            raise RuntimeError("thinkingmachines: malformed pricing row")
        native_id = row.get("tinker_id")
        if not isinstance(native_id, str) or native_id not in model_ids:
            continue
        amounts = []
        for field in fields:
            value = row.get(field)
            if not isinstance(value, str) or re.fullmatch(r"\$\d+(?:\.\d{1,6})?", value) is None:
                raise RuntimeError(f"thinkingmachines: invalid {field} price for {native_id}")
            amounts.append(int(Decimal(value[1:]) * 1_000_000))
        prompt, cached, completion = amounts
        if prompt <= 0 or completion <= 0 or cached > prompt:
            raise RuntimeError(f"thinkingmachines: invalid token prices for {native_id}")
        model_id = model_ids[native_id]
        price = ModelPrice(prompt, completion, prompt_cached_micro_per_m=cached)
        if model_id in prices and prices[model_id] != price:
            raise RuntimeError(f"thinkingmachines: conflicting prices for {native_id}")
        prices[model_id] = price
    missing = set(model_ids.values()) - prices.keys()
    if missing:
        raise RuntimeError(f"thinkingmachines: missing exact model prices: {sorted(missing)}")
    return prices


def fetch() -> ProviderPricingResult:
    # These are the provider's documented stable interfaces. HTML columns can
    # move when presentation-only fields such as active parameters are added.
    prices = _json_prices(
        fetch_json(SERVERLESS_URL), _SERVERLESS_IDS, ("input", "cached_input", "output")
    )
    prices.update(
        _json_prices(fetch_json(SAMPLER_URL), _SAMPLER_IDS, ("prefill", "cached_prefill", "sample"))
    )
    notes = ["verified exact deployed IDs against official serverless.json and models.json"]
    notes.extend(_run_access_canary())
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=notes,
    )


def _api_key() -> str | None:
    for name in API_KEY_ENVS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _held_upstream_ids() -> dict[str, str]:
    """Rows currently under the access hold, keyed to their exact upstream ID."""

    if not MANIFEST_PATH.exists():
        return {}
    rows = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")).get("models")
    if not isinstance(rows, list):
        return {}
    return {
        row["id"]: row["upstream_id"]
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and isinstance(row.get("upstream_id"), str)
        and row.get("routable") is False
        and row.get("routable_reason") == ACCESS_HOLD_REASON
    }


def _run_access_canary() -> list[str]:
    """Recheck held routes only; healthy siblings are never probed or touched."""

    _ACCESS_CANARY_CHECKED.clear()
    _ACCESS_CANARY_HEALTHY.clear()
    key = _api_key()
    held = _held_upstream_ids()
    if key is None or not held:
        return []
    for model_id, upstream_id in sorted(held.items()):
        _ACCESS_CANARY_CHECKED.add(model_id)
        if probe_openai_chat(
            base_url=BASE_URL, api_key=key, model=upstream_id, max_tokens=64
        ):
            _ACCESS_CANARY_HEALTHY.add(model_id)
    return [
        f"access canary passed {len(_ACCESS_CANARY_HEALTHY)}/"
        f"{len(_ACCESS_CANARY_CHECKED)} held route(s)"
    ]


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("thinkingmachines manifest has no models list")

    updated: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str):
            continue
        price = result.prices.get(model_id)
        if price is None:
            continue
        tier = price.tiers[0]
        row["input_token_price_per_m"] = tier.prompt_micro_per_m
        row["output_token_price_per_m"] = tier.completion_micro_per_m
        if tier.prompt_cached_micro_per_m is not None:
            row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
        else:
            row.pop("cached_input_token_price_per_m", None)
        updated.append(model_id)

    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"thinkingmachines manifest did not update expected model(s): {missing}")

    raw["source"] = URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(rows)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if _ACCESS_CANARY_CHECKED:
        set_manifest_model_canary_states(
            MANIFEST_PATH,
            checked_model_ids=_ACCESS_CANARY_CHECKED,
            healthy_model_ids=_ACCESS_CANARY_HEALTHY,
            failure_reason=ACCESS_HOLD_REASON,
        )
    return [
        "thinkingmachines: refreshed provider_models/thinkingmachines.json "
        f"({len(updated)} priced rows)"
    ]
