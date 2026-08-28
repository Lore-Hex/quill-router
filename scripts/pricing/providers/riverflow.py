"""Riverflow fixed-price asynchronous image generation."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import ModelPrice, ProviderPricingResult, validate
from scripts.pricing.manifest import (
    apply_canary_results,
    guard_fixed_output_prices,
    write_discovered_chat_manifest,
)

SLUG = "riverflow"
BASE_URL = "https://design-api.sourceful.com"
DOCS_URL = "https://www.riverflow.ai/developers/docs"
MODEL_ID = "riverflow/riverflow-2-fast"
UPSTREAM_ID = "riverflow-2-fast"
FIXED_PRICE_MICRODOLLARS = 20_000
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/riverflow.json"
)
MANIFEST_STALE_FALLBACK = True
INCLUDE_IN_PRICE_INDEX = False
CANARY_TIMEOUT_SECONDS = 300
FAILED_CANARY_RETRY_INTERVAL = timedelta(hours=24)
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}


def _microdollars(value: object) -> int | None:
    try:
        parsed = Decimal(str(value)) * Decimal(1_000_000)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0 or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _models_requiring_paid_canary(
    manifest_path: Path,
    discovered_model_ids: set[str],
    *,
    now: datetime | None = None,
) -> frozenset[str]:
    """Retry a paid failed canary at most daily, while checking new routes now."""

    if not manifest_path.exists():
        return frozenset(discovered_model_ids)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return frozenset(discovered_model_ids)
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return frozenset(discovered_model_ids)
    existing = {
        row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    checked_at = now or datetime.now(UTC)
    due: set[str] = set()
    for model_id in discovered_model_ids:
        row = existing.get(model_id)
        if row is None:
            due.add(model_id)
            continue
        if row.get("routable_reason") != "provider-canary-failed":
            continue
        raw_last_checked = row.get("canary_checked_at")
        try:
            last_checked = datetime.fromisoformat(str(raw_last_checked).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            due.add(model_id)
            continue
        if last_checked.tzinfo is None:
            last_checked = last_checked.replace(tzinfo=UTC)
        if checked_at - last_checked >= FAILED_CANARY_RETRY_INTERVAL:
            due.add(model_id)
    return frozenset(due)


def _probe_generation(api_key: str) -> bool:
    idempotency_key = f"trustedrouter-catalog-{uuid.uuid4()}"
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            submitted = client.post(
                f"{BASE_URL}/v2/generations/t2i",
                headers={"X-API-Key": api_key},
                json={
                    "model": UPSTREAM_ID,
                    "instruction": "A single blue circle on a white background",
                    "idempotencyKey": idempotency_key,
                    "resolution": "1K",
                },
            )
            if submitted.status_code not in {200, 201}:
                return False
            payload = submitted.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            job_id = data.get("jobId") if isinstance(data, dict) else None
            if not isinstance(job_id, str) or not job_id:
                return False
            deadline = time.monotonic() + CANARY_TIMEOUT_SECONDS
            delay = 1.0
            while time.monotonic() < deadline:
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                response = client.get(
                    f"{BASE_URL}/v2/generations/{job_id}", headers={"X-API-Key": api_key}
                )
                if response.status_code != 200:
                    if response.status_code == 429 or response.status_code >= 500:
                        delay = min(delay * 2, 30.0)
                        continue
                    return False
                state = response.json(parse_float=Decimal)
                data = state.get("data") if isinstance(state, dict) else None
                job = data.get("job") if isinstance(data, dict) else None
                status = str(job.get("status") if isinstance(job, dict) else "").casefold()
                if status in {"completed", "succeeded", "success"}:
                    cost = job.get("cost") if isinstance(job, dict) else None
                    task_cost = cost.get("taskCost") if isinstance(cost, dict) else None
                    currency = cost.get("currency") if isinstance(cost, dict) else None
                    artifacts = data.get("artifacts") if isinstance(data, dict) else None
                    return (
                        str(currency).upper() == "USD"
                        and _microdollars(task_cost) == FIXED_PRICE_MICRODOLLARS
                        and isinstance(artifacts, list)
                        and any(
                            isinstance(item, dict)
                            and item.get("type") == "image"
                            and item.get("status") == "ready"
                            and isinstance(item.get("url"), str)
                            and item["url"]
                            for item in artifacts
                        )
                    )
                if status in {"failed", "cancelled", "canceled", "error"}:
                    return False
                delay = min(delay * 2, 30.0)
    except (httpx.HTTPError, TypeError, ValueError):
        return False
    return False


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_ROWS  # noqa: PLW0603
    api_key = os.environ.get("RIVERFLOW_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("riverflow: RIVERFLOW_API_KEY is required")
    rows = {
        MODEL_ID: {
            "id": MODEL_ID,
            "upstream_id": UPSTREAM_ID,
            "display_name": "Riverflow 2 Fast",
            "model_type": "image",
            "input_modalities": ["text"],
            "output_modalities": ["image"],
            "endpoints": ["images"],
            "supported_features": ["image-generation"],
            "fixed_output_price_microdollars": {"1k": FIXED_PRICE_MICRODOLLARS},
            "status": 1,
        }
    }
    checked = _models_requiring_paid_canary(MANIFEST_PATH, set(rows))
    healthy = {MODEL_ID} if MODEL_ID in checked and _probe_generation(api_key) else set()
    apply_canary_results(rows, checked_model_ids=checked, healthy_model_ids=healthy)
    checked_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for model_id in checked:
        rows[model_id]["canary_checked_at"] = checked_at
    guard_fixed_output_prices(MANIFEST_PATH, rows)
    _DISCOVERED_ROWS = rows
    prices = {MODEL_ID: ModelPrice(0, 0)}
    errors = validate(prices, [MODEL_ID], allow_all_zero=True)
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=DOCS_URL,
        include_in_price_index=INCLUDE_IN_PRICE_INDEX,
        notes=[f"canaried {len(checked)} new or unhealthy routes; {len(healthy)} passed"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_ROWS,
        source_url=DOCS_URL,
        pricing_source_url=DOCS_URL,
    )
