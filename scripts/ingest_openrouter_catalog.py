#!/usr/bin/env python3
"""Ingest OpenRouter's public model catalog into a checked-in snapshot.

Run locally (no auth required); commit the resulting JSON under
src/trusted_router/data/openrouter_snapshot.json. PR review covers the
diff. Production reads the snapshot — no live OpenRouter calls at
serve time.

Usage:
    python scripts/ingest_openrouter_catalog.py            # refresh snapshot
    python scripts/ingest_openrouter_catalog.py --check    # CI guard: exits 1 if the file would change

The filter keeps models whose endpoints[].provider_name resolves to one
of TR's 8 keyed providers (anthropic, openai, gemini, cerebras, deepseek,
mistral, kimi, zai). Within each kept model we keep only those endpoints
— secondary inference providers (DeepInfra, Together, etc.) are dropped
because TR has no key for them. Vertex is intentionally excluded until
TR's GCP project gets the Anthropic-on-Vertex / Gemini-on-Vertex quota
approvals.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = REPO_ROOT / "src" / "trusted_router" / "data" / "openrouter_snapshot.json"

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
HTTP_TIMEOUT = 30.0
MAX_WORKERS = 8

# OpenRouter's per-endpoint `provider_name` → TR provider slug. Anything
# not in this table gets filtered out (TR has no key for it).
PROVIDER_NAME_TO_SLUG: dict[str, str] = {
    "Anthropic": "anthropic",
    "OpenAI": "openai",
    "Google": "gemini",
    "Google AI Studio": "gemini",
    "Cerebras": "cerebras",
    "DeepSeek": "deepseek",
    "Mistral": "mistral",
    "Moonshot AI": "kimi",
    "Moonshot": "kimi",
    "Z.AI": "zai",
    "Z.ai": "zai",
    "ZhipuAI": "zai",
    "Zhipu AI": "zai",
}

# Fields we keep from each model. Anything not in this list is dropped to
# keep the snapshot diff-friendly and avoid pulling in fields whose schema
# OpenRouter changes without notice.
MODEL_FIELDS = (
    "id",
    "name",
    "created",
    "description",
    "context_length",
    "architecture",
    "pricing",
    "top_provider",
    "per_request_limits",
)

ENDPOINT_FIELDS = (
    "name",
    "model_id",
    "model_name",
    "provider_name",
    "tag",
    "context_length",
    "pricing",
    "quantization",
    "max_completion_tokens",
    "max_prompt_tokens",
    "supported_parameters",
    "status",
    "uptime_last_30m",
    "uptime_last_5m",
    "uptime_last_1d",
    "supports_implicit_caching",
)


def fetch_models(client: httpx.Client) -> list[dict[str, Any]]:
    response = client.get(f"{OPENROUTER_BASE}/models")
    response.raise_for_status()
    payload = response.json()
    return list(payload.get("data") or [])


def fetch_endpoints(client: httpx.Client, model_id: str) -> list[dict[str, Any]]:
    response = client.get(f"{OPENROUTER_BASE}/models/{model_id}/endpoints")
    if response.status_code == 404:
        return []
    response.raise_for_status()
    data = response.json().get("data") or {}
    return list(data.get("endpoints") or [])


def filter_endpoints(endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in endpoints:
        provider_name = str(raw.get("provider_name") or "").strip()
        slug = PROVIDER_NAME_TO_SLUG.get(provider_name)
        if slug is None:
            continue
        # OpenRouter labels Vertex-served endpoints with `provider_name="Google"`
        # *and* `tag="google-vertex/..."`. Without the tag check the snapshot
        # would lump those into our `gemini` provider and surface them as
        # routable models, which is wrong — TR doesn't have GCP quota for
        # Anthropic-on-Vertex / Gemini-on-Vertex yet.
        tag = str(raw.get("tag") or "").lower()
        if tag.startswith("google-vertex"):
            continue
        kept = {key: raw.get(key) for key in ENDPOINT_FIELDS if key in raw}
        kept["tr_provider_slug"] = slug
        out.append(kept)
    out.sort(key=lambda e: (e["tr_provider_slug"], str(e.get("provider_name") or "")))
    return out


def slim_model(raw: dict[str, Any], endpoints: list[dict[str, Any]]) -> dict[str, Any]:
    kept = {key: raw.get(key) for key in MODEL_FIELDS if key in raw}
    kept["endpoints"] = endpoints
    return kept


def build_snapshot() -> dict[str, Any]:
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        models = fetch_models(client)
        kept: list[dict[str, Any]] = []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            future_to_model = {
                pool.submit(fetch_endpoints, client, model["id"]): model
                for model in models
                if isinstance(model.get("id"), str)
            }
            for future in as_completed(future_to_model):
                model = future_to_model[future]
                try:
                    endpoints = future.result()
                except httpx.HTTPError as exc:
                    print(f"WARN: {model['id']} endpoints fetch failed: {exc}", file=sys.stderr)
                    continue
                filtered = filter_endpoints(endpoints)
                if not filtered:
                    continue
                kept.append(slim_model(model, filtered))

    kept.sort(key=lambda m: str(m["id"]))
    return {
        "source": "openrouter.ai/api/v1/models + /endpoints",
        "filter": "kept models whose endpoints include one of TR's 9 keyed providers",
        "tr_keyed_providers": sorted(set(PROVIDER_NAME_TO_SLUG.values())),
        "model_count": len(kept),
        "models": kept,
    }


def write_snapshot(snapshot: dict[str, Any]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snapshot, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    SNAPSHOT_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the snapshot would change (CI guard)",
    )
    args = parser.parse_args()

    snapshot = build_snapshot()
    text = json.dumps(snapshot, indent=2, sort_keys=False, ensure_ascii=False) + "\n"

    if args.check:
        if not SNAPSHOT_PATH.exists():
            print(f"ERROR: {SNAPSHOT_PATH} does not exist; run without --check", file=sys.stderr)
            return 1
        existing = SNAPSHOT_PATH.read_text(encoding="utf-8")
        if existing != text:
            print(
                f"ERROR: {SNAPSHOT_PATH.relative_to(REPO_ROOT)} is out of date — "
                f"run scripts/ingest_openrouter_catalog.py and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"snapshot OK: {snapshot['model_count']} models")
        return 0

    write_snapshot(snapshot)
    print(f"wrote {SNAPSHOT_PATH.relative_to(REPO_ROOT)}: {snapshot['model_count']} models")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
