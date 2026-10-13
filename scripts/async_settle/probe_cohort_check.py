#!/usr/bin/env python3
"""Read ten-minute cohort metadata through the bounded, indexed shadow day reader."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from typing import Any

from trusted_router.async_settle_shadow_evidence import COUNTER, SAMPLE
from trusted_router.config import Settings
from trusted_router.storage import create_store
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore
from trusted_router.storage_operational_analytics import analytics_surrogate
from trusted_router.synthetic.shadow_probe_contract import SHAPES


def check(evidence: Any, workspace: str, now: dt.datetime) -> dict[str, Any]:
    cutoff = now - dt.timedelta(minutes=10)
    fingerprint = analytics_surrogate("workspace", workspace)
    compared: Counter[tuple[str, str, bool, str]] = Counter()
    counters = []
    for day in sorted({cutoff.date().isoformat(), now.date().isoformat()}):
        for _, raw in evidence.day(SAMPLE, day):
            row = json.loads(raw)
            if (row["workspace_fingerprint"] == fingerprint
                    and int(cutoff.timestamp() * 1e6) <= row["observed_at_us"] <= int(now.timestamp() * 1e6)):
                compared[row["adapter"], row["route_type"], row["streamed"], row["classification"]] += 1
        for _, raw in evidence.day(COUNTER, day):
            row = json.loads(raw)
            if row["flushed_at_us"] < int(cutoff.timestamp() * 1e6):
                continue
            # Counter schema has no workspace dimension. Never mislabel these
            # cumulative writer/day diagnostics as ten-minute cohort deltas.
            counters.append({name: row.get(name) for name in (
                "instance", "region", "router_revision", "flushed_at_us", "counts",
                "terminal_counts", "samples_inserted", "comparison_attempts",
                "comparison_dropped", "exclusions", "drops", "rejections",
                "dimension_overflow", "counter_overflow")})
    buckets = []
    for shape in SHAPES:
        route = "chat.completions" if shape.route == "/chat/completions" else "responses"
        counts = {c: n for (a, r, s, c), n in compared.items()
                  if (a, r, s) == (shape.provider, route, shape.stream)}
        buckets.append(dict(adapter=shape.provider, route_type=route, streamed=shape.stream,
                            compared=sum(counts.values()), classifications=counts))
    return dict(window_minutes=10, buckets=buckets, writer_day_counters=counters,
                counter_scope="all shadow traffic; cumulative writer/day, not workspace-attributable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)
    try:
        store = create_store(Settings(), initialize_schema=False)
        result = check(EvidenceStore(store._database), args.workspace, dt.datetime.now(dt.UTC))
    except Exception:
        print("Shadow cohort evidence unavailable")
        return 2
    print(json.dumps(result, sort_keys=True))
    # Missing/inexact buckets cannot pass a smoke comparison check. Diagnostics
    # remain visible for operator comparison across two reads (daily cumulative).
    return 0 if all(b["compared"] and set(b["classifications"]) == {"exact"}
                    for b in result["buckets"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
