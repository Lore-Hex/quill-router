"""Fail-closed Wafer route policy loaded from the generated provider manifest."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

WAFER_MANIFEST_PATH = (
    Path(__file__).resolve().parent / "data" / "provider_models" / "wafer.json"
)


@lru_cache(maxsize=1)
def _wafer_zdr_index() -> dict[str, bool]:
    """Index canonical and native IDs only when Wafer publishes a boolean."""

    try:
        payload: Any = json.loads(WAFER_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}

    index: dict[str, bool] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        supported = row.get("zdr_supported")
        if not isinstance(supported, bool):
            continue
        for key in (row.get("id"), row.get("upstream_id")):
            if isinstance(key, str) and key:
                index[key] = supported
    return index


def wafer_zdr_support(model_or_upstream_id: str) -> bool | None:
    """Return Wafer's published model-level ZDR support, or unknown."""

    return _wafer_zdr_index().get(model_or_upstream_id)
