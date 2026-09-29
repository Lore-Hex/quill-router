"""Application data for the approved landscape homepage.

The public catalog and /models view remain the source of prices and privacy.
The marketing fixture contributes copy and editorial ordering, never live values.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from trusted_router.catalog import MODELS

_ROOT = Path(__file__).parent
_PUBLISHERS = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google-ai-studio",
    "x-ai": "grok",
    "deepseek": "deepseek",
    "z-ai": "zai",
    "moonshotai": "kimi",
    "minimax": "minimax",
}


@lru_cache(maxsize=1)
def _content() -> dict[str, Any]:
    return json.loads((_ROOT / "templates/homepage/content.json").read_text())


def homepage_context(api_base_url: str) -> dict[str, Any]:
    # Lazy imports avoid the dashboard renderer's import cycle. Reuse its exact
    # Credits-only pricing/provider aggregation instead of duplicating it here.
    from trusted_router.dashboard import _model_view, _price
    from trusted_router.routes.catalog import _current_catalog_payload

    content = _content()
    catalog = _current_catalog_payload()
    public_ids = {shape["id"] for shape in catalog.shapes}
    icons = {
        publisher: f"/static/homepage/provider-{asset}.png"
        for publisher, asset in _PUBLISHERS.items()
    }
    views: dict[str, dict[str, Any]] = {}
    lists: dict[str, list[dict[str, Any]]] = {}
    for key, selected in content["catalog"]["lists"].items():
        rows = []
        for fixture_id in selected:
            model_id = content["catalog"]["models"][fixture_id]["id"]
            if model_id not in public_ids or model_id not in MODELS:
                continue
            if model_id not in views:
                # The homepage needs no AI-IQ lookup or request analytics.
                views[model_id] = _model_view(MODELS[model_id], test_mode=True)
            view = views[model_id]
            if not view["prepaid"]:
                continue
            if key == "z" and not view["zdr_available"]:
                continue
            if key == "c" and not view["e2e_available"]:
                continue
            labels = []
            if view["zdr_available"]:
                labels.append("ZDR")
            if view["e2e_available"]:
                labels.append("E2EE")
            rows.append({
                "id": model_id,
                "name": view["name"],
                "detail_href": view["detail_href"],
                "icon": icons.get(model_id.split("/")[0], "/static/homepage/mark.svg"),
                "provider_count": view["provider_count"],
                "route_labels": labels,
                "input_price": _price(int(view["prompt_price_sort"]), include_zero=True).replace("/1M", ""),
                "input_from": " to " in str(view["prompt_price"]),
                "output_price": _price(int(view["completion_price_sort"]), include_zero=True).replace("/1M", ""),
                "output_from": " to " in str(view["completion_price"]),
                "context": view["context_length_compact"],
            })
        lists[key] = rows
    migration = dict(content["migration"], base_url=api_base_url)
    migration["agent_prompt"] = migration["agent_prompt"].replace(
        content["migration"]["base_url"], api_base_url,
    )
    assets = _ROOT / "static/homepage"
    digest = hashlib.sha256()
    for asset in sorted(assets.glob("*")):
        if asset.suffix in {".css", ".js"}:
            digest.update(asset.read_bytes())
    return {
        "homepage_version": digest.hexdigest()[:12],
        "homepage_catalog": {
            "total": len(public_ids),
            "lists": lists,
            "checked_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        },
        "homepage_data": {
            "migration": migration,
            "tooltips": content["tooltips"],
            "catalog_total": len(public_ids),
            "publisher_icons": icons,
        },
    }
