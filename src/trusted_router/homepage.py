"""Application data for the approved landscape homepage.

The public catalog and /models view remain the source of prices and privacy.
The marketing fixture contributes copy and editorial ordering, never live values.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from trusted_router.catalog import MODELS

logger = logging.getLogger(__name__)
_model_count_failure_logged = False


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


def publisher_icons() -> dict[str, str]:
    """Lab icon per model publisher, shared by the homepage and the site header search."""
    return {
        publisher: f"/static/homepage/provider-{asset}.png"
        for publisher, asset in _PUBLISHERS.items()
    }


def live_model_count() -> int:
    """Public catalog size for the header search label; reads the cached catalog payload.

    Every public page renders this, so a catalog that cannot be built must not
    take the page down: the label falls back to "Search models" on 0.
    """
    global _model_count_failure_logged
    from trusted_router.routes.catalog import _current_catalog_payload

    try:
        count = len(_current_catalog_payload().shapes)
    except Exception:  # noqa: BLE001 - the header label is not worth a 500 on /status
        # Every public page calls this, so log the traceback once per outage, not per request.
        if not _model_count_failure_logged:
            logger.exception("header model count unavailable")
            _model_count_failure_logged = True
        return 0
    _model_count_failure_logged = False
    return count


def homepage_context(api_base_url: str) -> dict[str, Any]:
    # Lazy imports avoid the dashboard renderer's import cycle. Reuse its exact
    # Credits-only pricing/provider aggregation instead of duplicating it here.
    from trusted_router.dashboard import _model_view, _price
    from trusted_router.routes.catalog import _current_catalog_payload

    content = _content()
    catalog = _current_catalog_payload()
    public_ids = {shape["id"] for shape in catalog.shapes}
    icons = publisher_icons()
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
        "homepage_pricing": _pricing_comparison(public_ids),
        "homepage_catalog": {
            "total": len(public_ids),
            "lists": lists,
            "checked_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        },
        "homepage_data": {
            "migration": migration,
            "tooltips": content["tooltips"],
            "catalog_total": len(public_ids),
        },
    }


def _pricing_comparison(public_ids: set[str]) -> dict[str, Any] | None:
    """Compare the approved example's current Credits routes, as /models does."""
    from trusted_router.dashboard import _credits_endpoints, _price, endpoints_for_model

    model_id = "z-ai/glm-5.3-flash"
    if model_id not in public_ids or model_id not in MODELS:
        return None
    endpoints = _credits_endpoints(endpoints_for_model(model_id))
    prices = [endpoint.prompt_price_microdollars_per_million_tokens for endpoint in endpoints]
    if not prices or any(price < 0 for price in prices):
        return None
    low, high = min(prices), max(prices)
    ratio = Decimal(high) / Decimal(low) if low > 0 and high > low else None
    return {
        "name": MODELS[model_id].name,
        "href": f"/models/{model_id}",
        "low": _price(low, include_zero=True).replace("/1M", ""),
        "high": _price(high, include_zero=True).replace("/1M", ""),
        "has_range": high > low,
        "ratio": f"{ratio:.1f}" if ratio is not None and ratio >= Decimal("1.05") else None,
    }
