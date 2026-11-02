"""Bounded, read-only route attribution report. No Spanner reads or content."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from typing import Any

from trusted_router.model_aliases import canonical_router_model_id
from trusted_router.operational_analytics import OperationalAnalyticsClient

METRICS = (
    "generations", "input_tokens", "output_tokens", "cached_input_tokens",
    "customer_charge_microdollars", "latency_samples", "latency_total_ms",
    "ttft_samples", "ttft_total_ms", "estimated_usage_generations",
)


def summarize(rows: list[dict[str, Any]], *, cloud: str) -> dict[str, Any]:
    """Group equivalent router spellings while retaining the stored aliases."""
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    totals = dict.fromkeys(METRICS, 0)
    unknown = 0
    for row in rows:
        raw = str(row["requested_model"] or "")
        key = (
            str(row["day"]), canonical_router_model_id(raw),
            str(row["served_model"]), str(row["provider"]), str(row["usage_type"]),
        )
        group = groups.setdefault(key, {
            "day": key[0], "requested_route": key[1] or None,
            "served_model": key[2], "provider": key[3], "usage_type": key[4],
            "requested_aliases": [], **dict.fromkeys(METRICS, 0),
        })
        if raw and raw not in group["requested_aliases"]:
            group["requested_aliases"].append(raw)
        for name in METRICS:
            value = int(row[name])
            group[name] += value
            totals[name] += value
        if not raw:
            unknown += int(row["generations"])
    for group in groups.values():
        group["requested_aliases"].sort()
    return {
        "cloud": cloud,
        "scope": "Successful non-synthetic settled generations in this cloud's database",
        "limitations": [
            "Not unique end-user requests: orchestration can produce multiple generations.",
            "Does not count rejected requests, failed attempts, or all-provider failures.",
            "Charges are customer charges, not provider revenue or margin.",
            "Observed volume is not a routing eligibility or capacity forecast.",
            "Missing cloud reports are unknown, not zero; do not sum mirrored databases.",
        ],
        "attribution_coverage": {
            "known_generations": totals["generations"] - unknown,
            "unknown_generations": unknown,
            "known_fraction": (
                (totals["generations"] - unknown) / totals["generations"]
                if totals["generations"] else None
            ),
        },
        "totals": totals,
        "routes": [groups[key] for key in sorted(groups)],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", choices=("gcp", "aws", "azure"), required=True)
    parser.add_argument("--start", type=dt.datetime.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.datetime.fromisoformat, required=True)
    args = parser.parse_args()
    client = OperationalAnalyticsClient(
        base_url=os.environ["CH_URL"], user=os.environ["CH_USER"],
        password=os.environ["CH_PASSWORD"], database=os.environ["CH_DATABASE"],
    )
    report = summarize(client.routing_traffic(start_at=args.start, end_at=args.end), cloud=args.cloud)
    report["window"] = {
        "start_inclusive": args.start.astimezone(dt.UTC).isoformat(),
        "end_exclusive": args.end.astimezone(dt.UTC).isoformat(),
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
