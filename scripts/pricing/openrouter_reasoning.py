"""Capture OpenRouter's per-model reasoning metadata for the catalog.

OpenRouter's public `GET /api/v1/models` carries, on every reasoning model, a
`reasoning` object (`mandatory`, `default_enabled`, `supported_efforts`,
`default_effort`): which effort levels the model itself accepts. TrustedRouter
serves the same model ids, so `trusted_router.reasoning_metadata` republishes it,
narrowed to what TrustedRouter's gateway honors on each route.

Writes src/trusted_router/data/openrouter_reasoning.json with only those four
fields per model id, sorted, so a refresh diff shows exactly what changed. A
failed or malformed fetch keeps the existing file: stale metadata is still the
model's real effort set, and an empty file would drop every model's levels.

    uv run python -m scripts.pricing.openrouter_reasoning
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

SOURCE = "https://openrouter.ai/api/v1/models"
DATA_PATH = Path(__file__).resolve().parents[2] / "src" / "trusted_router" / "data" / "openrouter_reasoning.json"
EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
# OpenRouter lists ~340 reasoning models; far fewer means a broken or partial response.
MIN_MODELS = 100


def parse(document: object) -> dict[str, dict[str, Any]]:
    """id -> reasoning metadata, for rows whose metadata is well-formed. Raises on a bad document."""
    rows = document.get("data") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ValueError("OpenRouter model list has no data array")
    models: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        reasoning = row.get("reasoning")
        if not isinstance(reasoning, dict):
            continue
        efforts = reasoning.get("supported_efforts", [])
        if not isinstance(efforts, list) or not all(isinstance(e, str) and e in EFFORTS for e in efforts):
            continue
        default_effort = reasoning.get("default_effort")
        models[row["id"]] = {
            "mandatory": reasoning.get("mandatory") is True,
            "default_enabled": reasoning.get("default_enabled") is True,
            "supported_efforts": efforts,
            "default_effort": default_effort if default_effort in EFFORTS else None,
        }
    return models


def render(models: dict[str, dict[str, Any]]) -> str:
    document = {
        "source": SOURCE,
        "about": "OpenRouter's per-model reasoning metadata; see scripts/pricing/openrouter_reasoning.py.",
        "models": {model_id: models[model_id] for model_id in sorted(models)},
    }
    return json.dumps(document, indent=2) + "\n"


def main() -> int:
    try:
        response = httpx.get(SOURCE, timeout=30.0)
        response.raise_for_status()
        models = parse(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        print(f"openrouter_reasoning: keeping existing file ({exc})", file=sys.stderr)
        return 0
    if len(models) < MIN_MODELS:
        print(f"openrouter_reasoning: only {len(models)} models; keeping existing file", file=sys.stderr)
        return 0
    DATA_PATH.write_text(render(models), encoding="utf-8")
    print(f"openrouter_reasoning: {len(models)} models")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
