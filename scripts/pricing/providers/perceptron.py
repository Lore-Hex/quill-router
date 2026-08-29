"""Perceptron Inc catalog contract.

Perceptron's visual-model catalog is authenticated but intentionally not part
of hourly price publication: it does not expose exact billable prices. Keeping
the contract here prevents the similarly named perceptron.cloud service from
being wired by mistake.
"""

from __future__ import annotations

import os
from typing import Any

from scripts.pricing.base import fetch_json

SLUG = "perceptron"
BASE_URL = "https://api.perceptron.inc"
URL = f"{BASE_URL}/v1/models"
EXPECTED_MODEL_IDS = frozenset(
    {
        "isaac-0.1",
        "isaac-0.2-1b",
        "isaac-0.2-2b-preview",
        "perceptron-mk1",
    }
)


def _model_ids(payload: object) -> frozenset[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("perceptron: model catalog has no data list")
    return frozenset(
        row["id"]
        for row in payload["data"]
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    )


def fetch_catalog() -> list[dict[str, Any]]:
    api_key = os.environ.get("PERCEPTRON_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("perceptron: PERCEPTRON_API_KEY is required")
    payload = fetch_json(
        URL,
        extra_headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "TrustedRouter/1.0",
        },
    )
    model_ids = _model_ids(payload)
    if not EXPECTED_MODEL_IDS <= model_ids:
        missing = ", ".join(sorted(EXPECTED_MODEL_IDS - model_ids))
        raise RuntimeError(f"perceptron: expected catalog models missing: {missing}")
    return [row for row in payload["data"] if isinstance(row, dict)]


def fetch() -> None:
    fetch_catalog()
    raise RuntimeError(
        "perceptron: authenticated catalog has no exact billable prices; "
        "specialized visual routes remain dark"
    )
