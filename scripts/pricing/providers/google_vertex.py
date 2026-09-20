"""Discover Gemini on Vertex itself and admit only priced, canaried routes."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import google.auth
import httpx
from google.auth.transport.requests import Request

from scripts.pricing.base import ProviderPricingResult, fetch_html, fetch_json, validate
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.vertex_prices import parse

SLUG = "google-vertex"
URL = "https://cloud.google.com/vertex-ai/generative-ai/pricing"
MODELS_URL = "https://aiplatform.googleapis.com/v1beta1/publishers/google/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/trusted_router/data/provider_models/google-vertex.json"
)
_CHAT_ID = re.compile(r"^gemini-\d+(?:\.\d+)?-(?:pro|flash(?:-lite)?)(?:-preview)?$")
_RECOVERABLE_HOLDS = frozenset(
    {
        "provider-canary-failed",
        "price-unavailable",
        "awaiting-price",
        "vertex-metadata-unavailable",
        "delisted-upstream",
    }
)
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _authorization() -> tuple[dict[str, str], str]:
    credentials, default_project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(Request())
    project = os.environ.get("VERTEX_PROJECT_ID") or default_project
    if not project or not credentials.token:
        raise RuntimeError(
            "google-vertex: ADC token and project are required for discovery/canaries"
        )
    return {"Authorization": f"Bearer {credentials.token}"}, project


def _live_model_ids(headers: dict[str, str]) -> set[str]:
    models: set[str] = set()
    token: str | None = None
    seen: set[str] = set()
    for _ in range(20):
        params = {"pageSize": "100", "listAllVersions": "true"}
        if token:
            params["pageToken"] = token
        payload = fetch_json(
            f"{MODELS_URL}?{urlencode(params)}", extra_headers=headers, follow_redirects=False
        )
        rows = payload.get("publisherModels") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("google-vertex: publisherModels list missing")
        for row in rows:
            name = row.get("name") if isinstance(row, dict) else None
            if isinstance(name, str) and name.startswith("publishers/google/models/gemini-"):
                models.add(name.removeprefix("publishers/google/models/"))
        token = payload.get("nextPageToken")
        if not token:
            if not models:
                raise RuntimeError("google-vertex: empty Gemini publisher catalog")
            return models
        if not isinstance(token, str) or token in seen:
            raise RuntimeError("google-vertex: pagination token did not advance")
        seen.add(token)
    raise RuntimeError("google-vertex: publisher catalog exceeded 20 pages")


def _manifest_rows(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return {row["id"]: row for row in json.loads(path.read_text())["models"]}


def _metadata() -> dict[str, dict[str, Any]]:
    # AI Studio provides model limits, never evidence of Vertex availability.
    result: dict[str, dict[str, Any]] = {}
    for name in ("google-vertex.json", "google-ai-studio.json"):
        path = MANIFEST_PATH.with_name(name)
        for model_id, row in _manifest_rows(path).items():
            result.setdefault(model_id, {}).update(row)
    return result


def _probe(headers: dict[str, str], project: str, native_id: str) -> tuple[bool, int | None]:
    config: dict[str, Any] = {"maxOutputTokens": 128}
    if native_id.startswith("gemini-2.5-flash"):
        config["thinkingConfig"] = {"thinkingBudget": 0}
    elif native_id.startswith("gemini-3"):
        config["thinkingConfig"] = {"thinkingLevel": "low"}
    try:
        response = httpx.post(
            f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/models/{native_id}:generateContent",
            headers=headers,
            json={
                "contents": [{"role": "user", "parts": [{"text": "Reply exactly PONG"}]}],
                "generationConfig": config,
            },
            timeout=60,
        )
        if response.status_code != 200:
            return False, response.status_code
        payload = response.json()
        candidates = payload.get("candidates", [])
        if not candidates:
            return False, 200
        visible = "".join(
            part.get("text", "")
            for part in candidates[0].get("content", {}).get("parts", [])
            if part.get("thought") is not True
        )
        usage = payload.get("usageMetadata", {})
        valid_usage = all(
            type(usage.get(key)) is int and usage[key] >= 0
            for key in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount")
        )
        return visible.strip() == "PONG" and valid_usage and candidates[0].get(
            "finishReason"
        ) == "STOP", 200
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return False, None


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603
    _DISCOVERED_MANIFEST_ROWS = {}
    headers, project = _authorization()
    native_ids = _live_model_ids(headers)
    prices = parse(fetch_html(URL))
    unpriced = sorted(
        f"google/{mid}"
        for mid in native_ids
        if _CHAT_ID.fullmatch(mid) and f"google/{mid}" not in prices
    )
    metadata = _metadata()
    existing = _manifest_rows(MANIFEST_PATH)
    rows: dict[str, dict[str, Any]] = {}
    for native_id in sorted(native_ids):
        model_id = f"google/{native_id}"
        row: dict[str, Any] = {"id": model_id, "upstream_id": native_id}
        if not _CHAT_ID.fullmatch(native_id):
            # Media/live/specialized APIs require their own adapters and billing.
            row.update(routable=False, routable_reason="unsupported-vertex-api")
        else:
            source = metadata.get(model_id, {})
            for key in (
                "display_name",
                "context_length",
                "max_output_tokens",
                "features",
                "input_modalities",
            ):
                if key in source:
                    row[key] = source[key]
            if not row.get("context_length") or not row.get("max_output_tokens"):
                row.update(routable=False, routable_reason="vertex-metadata-unavailable")
        old = existing.get(model_id, {})
        if old.get("routable") is False and old.get("routable_reason") not in _RECOVERABLE_HOLDS:
            row.update(routable=False, routable_reason=old.get("routable_reason"))
        rows[model_id] = row
    candidates = {
        mid for mid, row in rows.items() if mid in prices and row.get("routable") is not False
    }
    checked = frozenset().union(
        *(
            models_requiring_canary(MANIFEST_PATH, candidates, failure_reason=reason)
            for reason in _RECOVERABLE_HOLDS
        )
    )
    healthy: set[str] = set()
    for mid in sorted(checked):
        ok, status = _probe(headers, project, rows[mid]["upstream_id"])
        rows[mid]["canary_status_code"] = status
        if ok:
            healthy.add(mid)
            rows[mid]["canary_verified_at"] = datetime.now(UTC).isoformat()
    apply_canary_results(rows, checked_model_ids=checked, healthy_model_ids=healthy)
    priced = {
        mid: value
        for mid, value in prices.items()
        if mid in rows and _CHAT_ID.fullmatch(rows[mid]["upstream_id"])
    }
    errors = validate(priced, [])
    if errors or not priced:
        raise RuntimeError(f"google-vertex: invalid or empty current prices: {errors}")
    _DISCOVERED_MANIFEST_ROWS = rows
    return ProviderPricingResult(
        slug=SLUG,
        prices=priced,
        source="api",
        fetched_url=URL,
        price_index_model_ids=frozenset(
            mid for mid in candidates if rows[mid].get("routable") is not False
        ),
        notes=[
            f"Vertex-native discovery: {len(rows)} models; {len(healthy)}/{len(checked)} new/held canaries passed",
            f"Held: native chat models missing complete official prices: {unpriced}",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=MODELS_URL,
        pricing_source_url=URL,
    )
