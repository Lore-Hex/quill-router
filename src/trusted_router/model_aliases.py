"""Explicit router-brand and upstream catalog aliases; no fuzzy model matching."""

import json
from collections.abc import Iterable
from typing import Any

_OPENROUTER_MODEL_ID_ALIASES = {
    # Published from Mistral's native catalog before OpenRouter listed it.
    "mistralai/mistral-large-4-0": "mistralai/mistral-large-4",
}


def canonical_openrouter_model_id(model_id: str) -> str:
    """Keep established public IDs when OpenRouter uses a different spelling.

    Apply only to catalog model IDs, never endpoint-native request IDs.
    """
    return _OPENROUTER_MODEL_ID_ALIASES.get(model_id, model_id)


def _openrouter_endpoints_with_native_ids(row: dict[str, Any]) -> list[dict[str, Any]]:
    # Pin the fallback before replacing the row ID, without modifying the feed.
    return [
        {**endpoint, "model_id": endpoint.get("model_id") or row["id"]}
        for endpoint in row.get("endpoints") or []
        if isinstance(endpoint, dict)
    ]


def merge_openrouter_model_rows(raw_models: Iterable[object]) -> list[dict[str, Any]]:
    """Canonicalize feed rows without losing native IDs or provider metadata.

    A canonical row supplies model metadata and wins endpoint conflicts with
    alias rows. Otherwise raw ID and JSON content break ties deterministically.
    Unrelated, non-colliding rows retain their existing endpoint order/content.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in raw_models:
        if isinstance(row, dict) and isinstance(model_id := row.get("id"), str):
            grouped.setdefault(canonical_openrouter_model_id(model_id), []).append(row)

    models: list[dict[str, Any]] = []
    for model_id, rows in sorted(grouped.items()):
        if len(rows) == 1:
            row = rows[0]
            models.append(row if row["id"] == model_id else {
                **row, "id": model_id, "endpoints": _openrouter_endpoints_with_native_ids(row),
            })
            continue
        rows = sorted(rows, key=lambda row: (
            row["id"] != model_id, row["id"], json.dumps(row, sort_keys=True),
        ))
        endpoints: dict[str, dict[str, Any]] = {}
        for row in rows:
            for endpoint in _openrouter_endpoints_with_native_ids(row):
                slug = endpoint.get("tr_provider_slug")
                if isinstance(slug, str):
                    endpoints.setdefault(slug, endpoint)
        models.append({
            **rows[0], "id": model_id,
            "endpoints": [endpoints[slug] for slug in sorted(endpoints)],
        })
    return models


def canonical_router_model_id(model_id: str) -> str:
    publisher, separator, native_id = model_id.partition("/")
    if publisher in {"system1models", "system1models-eu"} and separator:
        return publisher.replace("system1models", "decisionmodelsio") + "/" + native_id
    if model_id.startswith("nyte/"):
        model_id = "trustedrouter/" + model_id.removeprefix("nyte/")
    base, separator, variant = model_id.partition(":")
    if base == "trustedrouter/auto-routing":
        return "trustedrouter/auto" + separator + variant
    return model_id


def router_model_aliases(model_id: str) -> list[str]:
    publisher, separator, native_id = model_id.partition("/")
    if publisher in {"decisionmodelsio", "decisionmodelsio-eu"} and separator:
        return [publisher.replace("decisionmodelsio", "system1models") + "/" + native_id]
    if not model_id.startswith("trustedrouter/"):
        return []
    aliases = ["nyte/" + model_id.removeprefix("trustedrouter/")]
    if model_id == "trustedrouter/auto":
        aliases.extend(("trustedrouter/auto-routing", "nyte/auto-routing"))
    return aliases
