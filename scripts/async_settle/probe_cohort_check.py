#!/usr/bin/env python3
"""Pre-activation diagnostic using the existing bounded ten-minute evidence reader.

Use a complete v1 --generation-ids-out pass file after settlement. Buffered
requests use identity; streams use count_in_isolated_window, with membership
not proven. Certification belongs to the gates package using router evidence.
A simultaneous missing and extra sample of the same shape inside the pass
cannot be told apart by this tool. --require-stream-membership fails closed
when stream membership is not proven. ID-only files are uncertified diagnostics
and always exit non-zero. Select a window covering the pass, clock skew and
both guard intervals; wait at least 32 seconds after the last response.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from trusted_router.async_settle_shadow_evidence import COUNTER, SAMPLE
from trusted_router.config import Settings
from trusted_router.enclave_regions import ENCLAVE_REGIONS
from trusted_router.storage import create_store
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore
from trusted_router.storage_models import generation_id_for_authorization
from trusted_router.storage_operational_analytics import analytics_surrogate
from trusted_router.synthetic.shadow_probe import DENOMINATOR_PREFIX
from trusted_router.synthetic.shadow_probe_contract import SHAPES

# Probe and router clocks may differ by at most two seconds. Require an
# additional quiet thirty seconds on each side, within the existing window.
PASS_CLOCK_SKEW_US = 2_000_000
PASS_GUARD_INTERVAL_US = 30_000_000
STREAM_MEMBERSHIP_LIMITATION = (
    "A simultaneous missing and extra sample of the same shape inside the pass "
    "cannot be told apart by this tool."
)
UNCERTIFIED_INPUT = "uncertified diagnostic: complete v1 pass trailer required"


# Counter policy table: action, fields, reason. Tests require exact schema
# coverage, including nested count fields; new diagnostics require a decision.
COUNTER_POLICY = (
    ("report", "v instance region router_revision policy_version started_at_us flushed_at_us sequence closed", "Writer identity, version and interval; not cohort attribution."),
    ("buckets", "counts terminal_counts", "Inspect every mismatch, conflict and non-exact diagnostic below."),
    ("veto", "exclusions drops rejections comparison_dropped dimension_overflow counter_overflow conflicting_samples booking_pending booking_unknown", "Cumulative ambiguity, loss or conflict vetoes even if another workspace caused it."),
    ("window", "last_mismatch_at_us first_gap_at_us", "A mismatch or coverage gap in the requested window vetoes."),
    ("ignore", "comparison_attempts samples_inserted duplicate_samples first_evidence_at_us", "Volume and identical retries do not contradict exact per-ID reconciliation."),
    ("ignore", "authorize_shadow_hist evidence_write_hist admission_observer", "Latency and admission health belong to fleet certification, not this smoke comparison."),
)
BUCKET_POLICY = (
    ("veto", "mismatch requires_review unevaluable explained observed_unknown observed_ineligible comparison_dropped conflicting_samples booking_pending booking_unknown", "Non-exact, conflicted or incomplete observations veto conservatively."),
    ("ignore", "authorize_attempts authorize_fresh authorize_replay header_absent requested_eligible snapshot_sent settle_attempts refund_attempts envelope_present observed_attempts observed_eligible evaluable exact comparison_attempts samples_inserted duplicate_samples", "Traffic and exact/identical retry totals; reconcile retained identities separately."),
)


def counter_veto(row: dict[str, Any], cutoff_us: int, now_us: int) -> bool:
    for action, fields, _reason in COUNTER_POLICY:
        for field in fields.split():
            value = row.get(field)
            if action == "veto" and value:
                return True
            if action == "window" and value is not None and cutoff_us <= value <= now_us:
                return True
            if action == "buckets":
                for bucket in value or []:
                    if any(bucket.get(name) for mode, names, _ in BUCKET_POLICY
                           if mode == "veto" for name in names.split()):
                        return True
    return False


def parse_denominator(lines: list[str]) -> tuple[list[str], dict[str, Any] | None]:
    summary = None
    ids = lines
    if lines and lines[-1].startswith(DENOMINATOR_PREFIX):
        summary = json.loads(lines[-1][len(DENOMINATOR_PREFIX):])
        ids = lines[:-1]
        fields = {"requests_sent", "buffered_with_id", "streams_sent", "streams_without_id", "failed"}
        if (not isinstance(summary, dict)
                or set(summary) != fields | {"by_shape", "stream_generation_ids", "first_request_sent_at_us", "last_response_finished_at_us"}
                or any(type(summary[name]) is not int or not 0 <= summary[name] <= 10000 for name in fields)):
            raise ValueError("invalid denominator: pass counts")
        shapes = summary["by_shape"]
        if not isinstance(shapes, dict) or set(shapes) != {shape.name for shape in SHAPES}:
            raise ValueError("invalid denominator: shapes")
        for counts in shapes.values():
            if (not isinstance(counts, dict) or set(counts) != {"sent", "with_id"}
                    or any(type(n) is not int or not 0 <= n <= 10000 for n in counts.values())
                    or counts["with_id"] > counts["sent"]):
                raise ValueError("invalid denominator: shape counts")
        stream_ids = summary["stream_generation_ids"]
        streams_sent = sum(shapes[s.name]["sent"] for s in SHAPES if s.stream)
        streams_with_id = sum(shapes[s.name]["with_id"] for s in SHAPES if s.stream)
        buffered_with_id = sum(shapes[s.name]["with_id"] for s in SHAPES if not s.stream)
        if (not isinstance(stream_ids, list)
                or summary["requests_sent"] != sum(c["sent"] for c in shapes.values())
                or not summary["requests_sent"]
                or summary["failed"] > summary["requests_sent"]
                or summary["buffered_with_id"] != buffered_with_id or buffered_with_id != len(ids)
                or summary["streams_sent"] != streams_sent
                or summary["streams_without_id"] != streams_sent - streams_with_id
                or len(stream_ids) != streams_with_id):
            raise ValueError("invalid denominator: totals do not add up")
        sent_counts = {counts["sent"] for counts in shapes.values()}
        region_count = len(ENCLAVE_REGIONS)  # Same inventory used by probe.targets.
        sent = shapes[SHAPES[0].name]["sent"]
        if len(sent_counts) != 1 or sent <= 0 or sent % region_count:
            raise ValueError("invalid denominator: incomplete fixed pass grid")
        TypeAdapter(Settings.model_fields["async_settle_shadow_probe_repetitions"].rebuild_annotation()).validate_python(
            sent // region_count, strict=True)
        start, end = summary["first_request_sent_at_us"], summary["last_response_finished_at_us"]
        if type(start) is not int or type(end) is not int or not 0 < start <= end:
            raise ValueError("invalid denominator: pass boundaries")
    elif any(line.startswith("#") for line in lines):
        raise ValueError("invalid denominator: trailer version or position")
    all_ids = ids + (summary["stream_generation_ids"] if summary is not None else [])
    if (len(all_ids) > 10000
            or any(not isinstance(value, str) or re.fullmatch(r"gen-[0-9a-f]{32}", value) is None for value in all_ids)
            or len(set(all_ids)) != len(all_ids)):
        raise ValueError("invalid denominator: identities")
    return ids, summary


def check(evidence: Any, workspace: str, now: dt.datetime, generation_ids: list[str] | None = None,
          *, require_stream_membership: bool = False) -> dict[str, Any]:
    cutoff = now - dt.timedelta(minutes=10)
    fingerprint = analytics_surrogate("workspace", workspace)
    compared: Counter[tuple[str, str, bool, str]] = Counter()
    counters = []
    identities: dict[str, list[str]] = {}
    cohort_errors = []
    streamed_identities: Counter[str] = Counter()
    summary = None
    if generation_ids is not None:
        generation_ids, summary = parse_denominator(generation_ids)
    if summary is None and generation_ids is not None:
        return dict(window_minutes=10, errors=[UNCERTIFIED_INPUT if generation_ids is not None else "no denominator"],
                    reconciled=0, buckets=[], writer_day_counters=[],
                    buffered_level="identity", stream_level="count_in_isolated_window",
                    stream_membership="not_proven", stream_membership_limitation=STREAM_MEMBERSHIP_LIMITATION)
    pass_start = pass_end = 0
    if summary is not None:
        pass_start = summary["first_request_sent_at_us"] - PASS_CLOCK_SKEW_US
        pass_end = summary["last_response_finished_at_us"] + PASS_CLOCK_SKEW_US
        if (pass_start - PASS_GUARD_INTERVAL_US < int(cutoff.timestamp() * 1e6)
                or pass_end + PASS_GUARD_INTERVAL_US > int(now.timestamp() * 1e6)):
            raise ValueError("invalid denominator: pass and guard intervals must fit the ten-minute window")
    for day in sorted({cutoff.date().isoformat(), now.date().isoformat()}):
        for _, raw in evidence.day(SAMPLE, day):
            row = json.loads(raw)
            if (row["workspace_fingerprint"] == fingerprint
                    and int(cutoff.timestamp() * 1e6) <= row["observed_at_us"] <= int(now.timestamp() * 1e6)):
                identity = generation_id_for_authorization(row["authorization_id"])
                authorized = row.get("authorize_at_us")
                if summary is not None and (type(authorized) is not int or not pass_start <= authorized <= pass_end
                                            or authorized > row["observed_at_us"]):
                    in_guard = (type(authorized) is int
                                and not pass_start <= authorized <= pass_end
                                and pass_start - PASS_GUARD_INTERVAL_US <= authorized <= pass_end + PASS_GUARD_INTERVAL_US)
                    cohort_errors.append(identity + (": traffic in pass guard interval" if in_guard
                                                     else ": authorization outside pass boundaries or missing"))
                    continue
                compared[row["adapter"], row["route_type"], row["streamed"], row["classification"]] += 1
                if row["streamed"] is True:
                    streamed_identities[identity] += 1
                elif row["streamed"] is not False:
                    cohort_errors.append(identity + ": invalid streamed dimension")
                identities.setdefault(identity, []).append(row["classification"])
                if row["classification"] != "exact" or row.get("eligibility", {}).get("exclusion"):
                    cohort_errors.append(identity + ": non-exact or excluded")
        for _, raw in evidence.day(COUNTER, day):
            row = json.loads(raw)
            if row["flushed_at_us"] < int(cutoff.timestamp() * 1e6):
                continue
            # Counter schema has no workspace dimension. Never mislabel these
            # cumulative writer/day diagnostics as ten-minute cohort deltas.
            counters.append({name: row.get(name) for action, fields, _ in COUNTER_POLICY
                             if action != "ignore" for name in fields.split()})
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
        if identity in streamed_identities:
            errors.append(identity + ": buffered identity marked streamed")
        if found != ["exact"]:
            errors.append(identity + (": missing" if not found else ": non-exact or duplicate"))
    if generation_ids is not None and summary is not None:
        expected_streams = summary["streams_sent"]
        expected_total = summary["requests_sent"]
        buffered_ids = set(generation_ids)
        unmatched = {identity: found for identity, found in identities.items() if identity not in buffered_ids}
        total = sum(len(found) for found in identities.values())
        stream_count = sum(streamed_identities[identity] for identity in unmatched)
        for identity in unmatched:
            if not streamed_identities[identity]:
                errors.append(identity + ": unmatched sample is not streamed")
        for shape, bucket in zip(SHAPES, buckets, strict=True):
            expected = summary["by_shape"][shape.name]["sent"]
            if bucket["compared"] != expected:
                errors.append(f"{shape.name} sample count: expected {expected}, found {bucket['compared']}")
        if total != expected_total:
            errors.append(f"workspace sample count: expected {expected_total}, found {total}")
        if stream_count != expected_streams:
            errors.append(f"unmatched stream count: expected {expected_streams}, found {stream_count}; IDs: {sorted(unmatched)}")
        for identity, found in identities.items():
            if len(found) != 1:
                errors.append(identity + ": duplicate sample")
        if summary is not None:
            if summary["failed"]:
                errors.append(f"probe pass failed: {summary['failed']} requests")
            if len(generation_ids) + expected_streams != expected_total:
                errors.append("buffered requests missing IDs")
            for identity in summary["stream_generation_ids"]:
                if identity not in unmatched:
                    errors.append(identity + ": optional stream ID missing")
    if require_stream_membership:
        errors.append("stream membership not_proven: " + STREAM_MEMBERSHIP_LIMITATION)
    errors.extend(cohort_errors)
    # Writer counters cannot attribute losses to a workspace. Veto ambiguous
    # losses conservatively, never label these diagnostics cohort-specific.
    if any(counter_veto(row, int(cutoff.timestamp() * 1e6), int(now.timestamp() * 1e6))
           for row in counters):
        errors.append("writer/day loss, exclusion or conflict; cohort attribution unavailable")
    return dict(window_minutes=10, errors=errors, reconciled=len(generation_ids or []),
                buffered_level="identity", stream_level="count_in_isolated_window",
                stream_membership="not_proven", stream_membership_limitation=STREAM_MEMBERSHIP_LIMITATION,
                buckets=buckets, writer_day_counters=counters,
                counter_scope="all shadow traffic; cumulative writer/day, not workspace-attributable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--generation-ids", type=Path)
    parser.add_argument("--require-stream-membership", action="store_true",
                        help="fail when stream membership is not proven by identity")
    args = parser.parse_args(argv)
    try:
        store = create_store(Settings(), initialize_schema=False)
        result = check(EvidenceStore(store._database), args.workspace, dt.datetime.now(dt.UTC),
                       args.generation_ids.read_text().splitlines() if args.generation_ids else None,
                       require_stream_membership=args.require_stream_membership)
    except ValueError:
        print("Shadow cohort diagnostic invalid: complete v1 denominator and covered pass boundaries required")
        return 2
    except Exception:
        print("Shadow cohort evidence unavailable")
        return 2
    print(json.dumps(result, sort_keys=True))
    print("Buffered requests: identity. Streams: count_in_isolated_window; membership not_proven.")
    print(STREAM_MEMBERSHIP_LIMITATION)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
