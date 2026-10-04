"""Isolated catalog reader, also runnable against a historical source tree.

Pin clocks BEFORE importing the registry: its construction applies retirements.
Only this offline process changes clock functions; production never does.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any


def snapshot(at: datetime, freshness_at: datetime) -> dict[str, Any]:
    from trusted_router import catalog_data, provider_lifecycle

    provider_lifecycle._utc_now = lambda: at
    catalog_data._utc_now = lambda: freshness_at
    from trusted_router import catalog

    rows = []
    for model in catalog.MODELS.values():
        row = catalog.model_to_openrouter_shape(model)
        tr = row["trustedrouter"]
        if tr.get("internal_only"):
            continue
        # Before PR #1512, ask that revision's routing predicate, never copy
        # its rule or apply today's stricter privacy policy retroactively.
        if not hasattr(catalog, "model_capabilities") and model.id not in catalog.META_MODEL_IDS and not getattr(model, "hidden_public_metadata", False):
            confidential = {
                ep.id: catalog.endpoint_meets_privacy_requirement(ep, catalog.PRIVACY_TIER_CONFIDENTIAL)
                for ep in catalog.endpoints_for_model(model.id)
            }
            tr["capabilities"] = {"confidential": any(confidential.values())}
            for endpoint in tr.get("endpoints", []):
                endpoint["capabilities"] = {"confidential": confidential.get(endpoint["id"], False)}
        schedules = {}
        for ep in catalog.endpoints_for_model(model.id):
            schedule = provider_lifecycle.provider_pricing_schedule(ep.provider, model.id, at=at)
            price = provider_lifecycle.provider_price_microdollars(ep.provider, model.id, at=at)
            if schedule is None and price is None:
                continue
            schedule = dict(schedule or {})
            period = schedule.pop("current_period", None)
            if price is not None:
                rates = asdict(price)
                multiplier = schedule.get("peak_multiplier", 1) if period == "peak" else 1
                schedule["off_peak_microdollars_per_million_tokens"] = {
                    key: value // multiplier if value is not None else None
                    for key, value in rates.items()
                }
            schedules[ep.provider] = schedule
        tr["pricing_schedules"] = schedules
        rows.append(row)
    # Dates are read from the lifecycle module itself, including pricing
    # cutovers that have no retirement. No duplicate list of dates to maintain.
    dates = {r.effective_at for r in provider_lifecycle._RETIREMENTS}
    dates.update(value for name, value in vars(provider_lifecycle).items()
                 if name.endswith("_AT") and isinstance(value, datetime))
    cutovers = sorted(value.astimezone(UTC).isoformat().replace("+00:00", "Z")
                      for value in dates if value > at)
    retirements = [dict(provider=r.provider, models=sorted(r.model_ids),
                        effective_at=r.effective_at.astimezone(UTC).isoformat().replace("+00:00", "Z"))
                   for r in provider_lifecycle._RETIREMENTS if r.effective_at > at]
    return {"rows": rows, "cutovers": cutovers, "retirements": retirements}


if __name__ == "__main__":
    at = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
    freshness_at = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
    print(json.dumps(snapshot(at, freshness_at)))
