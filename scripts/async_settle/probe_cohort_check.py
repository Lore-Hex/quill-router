#!/usr/bin/env python3
"""Read ten-minute cohort metadata through the bounded, indexed shadow day reader."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from trusted_router.async_settle_shadow_evidence import COUNTER, SAMPLE
from trusted_router.config import Settings
from trusted_router.storage import create_store
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore
from trusted_router.storage_models import generation_id_for_authorization
from trusted_router.storage_operational_analytics import analytics_surrogate
from trusted_router.synthetic.shadow_probe_contract import SHAPES


def check(evidence: Any, workspace: str, now: dt.datetime, generation_ids: list[str] | None = None) -> dict[str, Any]:
    cutoff = now - dt.timedelta(minutes=10)
    fingerprint = analytics_surrogate("workspace", workspace)
    compared: Counter[tuple[str, str, bool, str]] = Counter()
    counters = []
    identities: dict[str, list[str]] = {}
    cohort_errors = []
    if generation_ids is not None and (not generation_ids or len(generation_ids) > 10000
            or len(set(generation_ids)) != len(generation_ids)
            or any(re.fullmatch(r"gen-[0-9a-f]{32}", value) is None for value in generation_ids)):
        raise ValueError("invalid denominator")
    for day in sorted({cutoff.date().isoformat(), now.date().isoformat()}):
        for _, raw in evidence.day(SAMPLE, day):
            row = json.loads(raw)
            if (row["workspace_fingerprint"] == fingerprint
                    and int(cutoff.timestamp() * 1e6) <= row["observed_at_us"] <= int(now.timestamp() * 1e6)):
                compared[row["adapter"], row["route_type"], row["streamed"], row["classification"]] += 1
                identity = generation_id_for_authorization(row["authorization_id"])
                identities.setdefault(identity, []).append(row["classification"])
                if row["classification"] != "exact" or row.get("eligibility", {}).get("exclusion"):
                    cohort_errors.append(identity + ": non-exact or excluded")
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
    errors = [] if generation_ids is not None else ["no denominator"]
    for identity in generation_ids or []:
        found = identities.get(identity, [])
        if found != ["exact"]:
            errors.append(identity + (": missing" if not found else ": non-exact or duplicate"))
    errors.extend(cohort_errors)
    # Writer counters cannot attribute losses to a workspace. Veto ambiguous
    # losses conservatively, never label these diagnostics cohort-specific.
    if any(row.get("exclusions") or row.get("drops") or row.get("rejections")
           or row.get("comparison_dropped") or row.get("dimension_overflow")
           or row.get("counter_overflow") for row in counters):
        errors.append("writer/day loss or exclusion; cohort attribution unavailable")
    return dict(window_minutes=10, errors=errors, reconciled=len(generation_ids or []),
                buckets=buckets, writer_day_counters=counters,
                counter_scope="all shadow traffic; cumulative writer/day, not workspace-attributable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--generation-ids", type=Path)
    args = parser.parse_args(argv)
    try:
        store = create_store(Settings(), initialize_schema=False)
        result = check(EvidenceStore(store._database), args.workspace, dt.datetime.now(dt.UTC),
                       args.generation_ids.read_text().splitlines() if args.generation_ids else None)
    except Exception:
        print("Shadow cohort evidence unavailable")
        return 2
    print(json.dumps(result, sort_keys=True))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
