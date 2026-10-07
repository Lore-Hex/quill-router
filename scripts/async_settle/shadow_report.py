"""Conservative day-prefix shadow report. Logs are never an evidence source."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from trusted_router.async_settle_shadow_binding import FIXTURE_SHA256
from trusted_router.async_settle_shadow_evidence import (
    COHORT_EXCLUSIONS,
    CONTROL,
    COUNT_FIELDS,
    COUNTER,
    DIMENSIONS,
    SAMPLE,
    validate_manifest,
    validate_sample,
)
from trusted_router.async_settle_shadow_wire import bounded_json
from trusted_router.detached_jws import canonical

EXIT_CRITERIA = {
    "seven_days_admission_off": "604800 continuous seconds, complete roster and flag coverage",
    "denominators_and_diagnostics": "reconciled dimensional counters; known predictions and ages",
    "zero_unexplained_all_evaluable": "no mismatch, gap, drop, unknown, or retry conflict",
    "shared_fixtures": "both repositories' 759-case and literal build artifacts",
    "frozen_pricing_crash_mutations": "F1/F2b/F2c differential, crash and mutation artifacts",
    "d2_d3_capacity_slo": "independent regional handoff, revocation, drain and load evidence",
    "policy_rollback_status": "reviewed exposure/exclusion policy and rollback/status evidence",
    "rare_cases": "catalog/tier/cache/retry/refund/zero fixture and observed coverage matrix",
}
EXTERNAL_GATES = ("deployment_inventory", "authorization_transport_measurements", "maximum_header_hops",
                  "fleet_load_budget", "publisher_poll_freshness", "lifecycle_cpu", "shared_fixtures",
                  "frozen_pricing_crash_mutations", "d2_d3_capacity_slo", "policy_rollback_status", "rare_cases")
MANIFEST_FIELDS = set("v day instance_boot_ids router_revisions go_revisions configuration_sha256 admission_disabled_from_us admission_disabled_until_us first_evidence_at_us completeness gap_intervals proof_manifest_sha256".split())
COUNTER_FIELDS = set("v instance region router_revision policy_version started_at_us flushed_at_us sequence closed counts exclusions rejections drops dimension_overflow counter_overflow comparison_attempts comparison_dropped samples_inserted duplicate_samples conflicting_samples booking_pending booking_unknown first_evidence_at_us last_mismatch_at_us first_gap_at_us authorize_shadow_hist evidence_write_hist admission_observer".split())


def percentiles(values: list[int | None]) -> dict[str, Any]:
    known = sorted(v for v in values if v is not None)
    return {"count": len(known), "null_count": len(values)-len(known), **{
        name: known[math.ceil(len(known)*q)-1] if known else None
        for name, q in (("p50", .5), ("p95", .95), ("p99", .99))}}


def delta_bin(value: int) -> str:
    if value == 0:
        return "0"
    n = abs(value)
    return ("+" if value > 0 else "-") + ("1" if n == 1 else "2-10" if n <= 10 else
             "11-100" if n <= 100 else "101-1000" if n <= 1000 else ">1000")


def _uint(value: Any) -> bool:
    return type(value) is int and 0 <= value < 1 << 63


def validate_counter(identity: str, body: dict[str, Any], *, partitions: bool = True) -> None:
    import uuid
    if set(body) != COUNTER_FIELDS or len(canonical(body)) > 65536:
        raise ValueError("counter schema")
    day, boot = identity.split("/")
    dt.date.fromisoformat(day)
    if str(uuid.UUID(boot)) != boot or body["instance"] != boot or body["v"] != 1 or type(body["v"]) is not int:
        raise ValueError("counter identity")
    if body["policy_version"] != "shadow-v1" or type(body["closed"]) is not bool:
        raise ValueError("counter policy")
    if not all(_uint(body[k]) for k in ("started_at_us", "flushed_at_us", "sequence", "dimension_overflow", "comparison_attempts", "comparison_dropped", "samples_inserted", "duplicate_samples", "conflicting_samples", "booking_pending", "booking_unknown")):
        raise ValueError("counter integer")
    if body["flushed_at_us"] < body["started_at_us"] or body["sequence"] < 1:
        raise ValueError("counter interval")
    if type(body["counter_overflow"]) is not bool:
        raise ValueError("overflow flag")
    for key in ("first_evidence_at_us", "last_mismatch_at_us", "first_gap_at_us"):
        if body[key] is not None and not _uint(body[key]):
            raise ValueError("counter timestamp")
    if not isinstance(body["router_revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", body["router_revision"]):
        raise ValueError("counter revision")
    if not isinstance(body["region"], str) or not 1 <= len(body["region"]) <= 128:
        raise ValueError("counter region")
    observer_fields = set("workspace_reads health_reads read_failures missed_ticks prediction_yes prediction_no prediction_unknown".split())
    if set(body["admission_observer"]) != observer_fields or not all(_uint(n) for n in body["admission_observer"].values()):
        raise ValueError("observer schema")
    buckets = body["counts"]
    if not isinstance(buckets, list) or len(buckets) != len(DIMENSIONS):
        raise ValueError("counter buckets")
    found = []
    for bucket in buckets:
        if set(bucket) != {"adapter", "route_type", "streamed", *COUNT_FIELDS} or not all(_uint(bucket[k]) for k in COUNT_FIELDS):
            raise ValueError("counter bucket schema")
        found.append((bucket["adapter"], bucket["route_type"], bucket["streamed"]))
        if partitions and (bucket["observed_attempts"] != bucket["settle_attempts"] + bucket["refund_attempts"]
                or bucket["observed_attempts"] != sum(bucket[k] for k in ("observed_eligible", "observed_ineligible", "observed_unknown"))
                or bucket["authorize_attempts"] != bucket["authorize_fresh"] + bucket["authorize_replay"]
                or bucket["evaluable"] != bucket["exact"] + bucket["explained"]
                or bucket["envelope_present"] > bucket["observed_attempts"]
                or bucket["header_absent"] > bucket["authorize_attempts"]):
            raise ValueError("counter partition")
    if tuple(found) != DIMENSIONS:
        raise ValueError("counter dimensions")
    outcomes = sum(bucket[k] for bucket in buckets for k in ("exact", "explained", "mismatch", "requires_review", "unevaluable"))
    if partitions and (outcomes > body["comparison_attempts"] or body["samples_inserted"] > body["comparison_attempts"]):
        raise ValueError("counter comparison partition")
    if sum(bucket["mismatch"] for bucket in buckets) and body["last_mismatch_at_us"] is None:
        raise ValueError("missing mismatch timestamp")
    if sum(len(body[k]) for k in ("exclusions", "rejections", "drops")) > 128:
        raise ValueError("counter dimensions overflow")
    from trusted_router.async_settle_shadow_evidence import REASONS
    for name in ("exclusions", "rejections", "drops"):
        seen = set()
        for row in body[name]:
            if (set(row) != {"phase", "adapter", "route_type", "streamed", "reason", "count"}
                    or row["phase"] not in {"authorize", "settle", "refund", "worker"}
                    or row["reason"] not in REASONS or not _uint(row["count"])
                    or (row["adapter"], row["route_type"], row["streamed"]) not in DIMENSIONS):
                raise ValueError("reason schema")
            key = tuple(row[k] for k in ("phase", "adapter", "route_type", "streamed", "reason"))
            if key in seen:
                raise ValueError("duplicate reason")
            seen.add(key)
    for key in ("authorize_shadow_hist", "evidence_write_hist"):
        if len(body[key]) != 9 or not all(_uint(n) for n in body[key]):
            raise ValueError("histogram")


def positive_sample(row: dict[str, Any]) -> bool:
    p = row["provenance"]
    admission = row["admission"]
    booking = row["booking"]
    return bool(row["classification"] in {"exact", "explained-by-catalog-change"}
        and row["eligibility"]["requested"] is True and row["eligibility"]["observed"] is True
        and p["binding_verified"] is True and p["raw_matches_body"] is True
        and p["s0_reconstruction"] in {"not_needed", "verified"}
        and all(row[k] is not None for k in ("python_micro", "go_micro", "booked_micro", "snapshot_hash", "payload_hash", "python_usage", "go_usage", "legacy_usage"))
        and row["python_micro"] == row["go_micro"]
        and row["python_usage"] == row["go_usage"] == row["legacy_usage"]
        and booking["source"] == "finalized_authorization"
        and (booking["attempted_kind"], booking["outcome"]) in {("settle", "settled"), ("refund", "refunded")}
        and (row["classification"] == "exact" and row["python_micro"] == row["booked_micro"]
             or row["classification"] == "explained-by-catalog-change" and row["legacy_frozen_micro"] == row["python_micro"]
             and row["booked_micro"] == row["rebuilt_micro"] and p["rebuild_matches_booking_view"] is True)
        and admission["prediction"] in {"yes", "no"}
        and all(_uint(admission[k]) and admission[k] < 5_000_000 for k in ("workspace_age_us", "health_age_us")))


def known_exclusion(row: dict[str, Any]) -> bool:
    """Verified out-of-cohort facts are denominators, never positive clock seeds."""
    return bool(row["classification"] == "unevaluable" and row["eligibility"]["observed"] is False
        and row["provenance"]["binding_verified"] is True and row["provenance"]["raw_matches_body"] is True
        and row["provenance"]["s0_reconstruction"] in {"not_needed", "verified"}
        and row["snapshot_hash"] is not None and row["reason_codes"] and set(row["reason_codes"]) <= COHORT_EXCLUSIONS
        and row["booking"]["source"] == "finalized_authorization" and row["booked_micro"] is not None
        and row["admission"]["prediction"] in {"yes", "no"}
        and all(_uint(row["admission"][k]) and row["admission"][k] < 5_000_000 for k in ("workspace_age_us", "health_age_us")))


def report(rows: list[dict[str, Any]], days: list[str], proof: dict[str, Any]) -> dict[str, Any]:
    requested = sorted(set(days))
    if not requested:
        raise ValueError("days required")
    for day in requested:
        if dt.date.fromisoformat(day).isoformat() != day:
            raise ValueError("day")
    samples, counters, manifests = [], {}, {}
    gaps: list[str] = []
    gap_ends: list[int | None] = []
    def gap(reason: str, day: str | None = None) -> None:
        gaps.append(reason)
        gap_ends.append(None if day is None else int(dt.datetime.combine(
            dt.date.fromisoformat(day)+dt.timedelta(days=1), dt.time(), dt.UTC).timestamp()*1e6))
    resets: list[dict[str, Any]] = []
    identities = set()
    for row in rows:
        if set(row) != {"kind", "id", "body"} or (row["kind"], row["id"]) in identities:
            raise ValueError("duplicate or invalid evidence")
        identities.add((row["kind"], row["id"]))
        kind, identity, body = row["kind"], row["id"], row["body"]
        if kind == SAMPLE:
            validate_sample(body, identity)
            samples.append(body)
        elif kind == COUNTER:
            validate_counter(identity, body, partitions=False)
            counters[identity] = body
        elif kind == CONTROL and identity.endswith("/manifest-v1"):
            validate_manifest(body, identity)
            if set(body) != MANIFEST_FIELDS or len(canonical(body)) > 262144 or body["day"] + "/manifest-v1" != identity:
                raise ValueError("manifest schema")
            if len(body["instance_boot_ids"]) > 4096 or sorted(set(body["instance_boot_ids"])) != body["instance_boot_ids"] or len(body["gap_intervals"]) > 128:
                raise ValueError("manifest bounds")
            manifests[body["day"]] = body
        elif kind == CONTROL and identity.endswith("/cap-v1"):
            if (set(body) != {"v", "limit", "reserved", "updated_at_us"} or type(body["v"]) is not int or body["v"] != 1
                    or body["limit"] != 100000 or not _uint(body["reserved"]) or body["reserved"] > 100000
                    or not _uint(body["updated_at_us"])):
                raise ValueError("invalid cap row")
        else:
            raise ValueError("unknown evidence kind")
    proof_hash = hashlib.sha256(canonical(proof)).hexdigest()
    gates = {name: isinstance(proof.get(name), str) and re.fullmatch(r"[0-9a-f]{64}", proof[name]) is not None for name in EXTERNAL_GATES}
    if proof.get("fixture_sha256") != FIXTURE_SHA256 or not all(gates.values()):
        gap("external_proofs_missing")
    inventory = proof.get("instance_boot_ids_by_day", {})
    intervals: list[tuple[int, int]] = []
    for day in requested:
        manifest = manifests.get(day)
        if manifest is None:
            gap(day+":missing_manifest", day)
            continue
        actual = sorted(identity.split("/")[1] for identity in counters if identity.startswith(day+"/"))
        if (actual != manifest["instance_boot_ids"] or actual != inventory.get(day) or not actual
                or manifest["proof_manifest_sha256"] != proof_hash):
            gap(day+":roster_unknown", day)
        if manifest["completeness"] != "complete" or manifest["gap_intervals"]:
            gap(day+":manifest_gap", day)
        start, end = manifest["admission_disabled_from_us"], manifest["admission_disabled_until_us"]
        if not _uint(start) or not _uint(end) or end <= start:
            gap(day+":flag_interval_unknown", day)
            continue
        covered = []
        day_start = int(dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), dt.UTC).timestamp()*1e6)
        day_end = day_start + 86400_000000
        # A midnight flush can close yesterday's row after today's row starts.
        # Retain that real overlap for rostered writers, never infer continuity
        # merely from the recurrence of the same boot ID on another day.
        for counter in counters.values():
            if counter["instance"] not in actual or counter["router_revision"] not in manifest["router_revisions"]:
                continue
            left = max(day_start, start, counter["started_at_us"])
            right = min(day_end, end, counter["flushed_at_us"])
            if left < right:
                covered.append((left, right))
        for identity, counter in counters.items():
            if not identity.startswith(day+"/"):
                continue
            persistence = sum(counter[k] for k in (
                "samples_inserted", "duplicate_samples", "conflicting_samples", "comparison_dropped"))
            if persistence != counter["comparison_attempts"]:
                gap(identity+":persistence_count_gap", day)
            if counter["router_revision"] not in manifest["router_revisions"]:
                gap(identity+":revision_unknown", day)
            eligible = sum(bucket["observed_eligible"] for bucket in counter["counts"])
            outcomes_total = sum(bucket[k] for bucket in counter["counts"]
                for k in ("exact", "explained", "mismatch", "requires_review", "unevaluable"))
            if outcomes_total > counter["comparison_attempts"]:
                gap(identity+":comparison_outcome_gap", day)
            if counter["comparison_attempts"] > eligible:
                gap(identity+":comparison_observation_gap", day)
            # Reconcile each writer and dimension in both directions. Neither
            # authorize exclusions nor another bucket can account for a terminal.
            # Diagnostic failures independently block coverage below.
            for bucket in counter["counts"]:
                observed = bucket["observed_attempts"]
                if (observed != sum(bucket[k] for k in ("observed_eligible", "observed_ineligible", "observed_unknown"))
                        or observed != bucket["settle_attempts"] + bucket["refund_attempts"]
                        or bucket["authorize_attempts"] != bucket["authorize_fresh"] + bucket["authorize_replay"]
                        or bucket["evaluable"] != bucket["exact"] + bucket["explained"]
                        or bucket["envelope_present"] > observed
                        or bucket["header_absent"] > bucket["authorize_attempts"]):
                    gap(identity+":observed_partition_gap", day)
                exclusions = sum(row["count"] for row in counter["exclusions"]
                    if row["phase"] in {"settle", "refund"} and row["reason"] in COHORT_EXCLUSIONS
                    and all(row[k] == bucket[k] for k in ("adapter", "route_type", "streamed")))
                if exclusions != bucket["observed_ineligible"]:
                    gap(identity+":ineligible_coverage_gap", day)
                outcomes = sum(bucket[k] for k in ("exact", "explained", "mismatch", "requires_review", "unevaluable"))
                if outcomes > observed:
                    gap(identity+":outcome_observation_gap", day)
                failures = sum(row["count"] for group in ("exclusions", "rejections", "drops")
                    for row in counter[group] if row["phase"] != "authorize"
                    and row["reason"] not in COHORT_EXCLUSIONS
                    and all(row[k] == bucket[k] for k in ("adapter", "route_type", "streamed")))
                accounted = max(0, outcomes - bucket["observed_ineligible"]) + failures
                if bucket["observed_eligible"] != accounted + counter["booking_pending"] + counter["booking_unknown"]:
                    gap(identity+":eligible_coverage_gap", day)
            if (not counter["closed"] or counter["first_gap_at_us"] is not None or counter["counter_overflow"]
                    or counter["dimension_overflow"] or counter["comparison_dropped"] or counter["booking_pending"] or counter["booking_unknown"]
                    or any(counter["admission_observer"][key] for key in ("prediction_unknown", "read_failures", "missed_ticks"))
                    or any(row["count"] for key in ("drops", "rejections") for row in counter[key])
                    or any(row["observed_unknown"] for row in counter["counts"])):
                gap(identity+":counter_gap", day)
            if counter["last_mismatch_at_us"] is not None or counter["conflicting_samples"]:
                resets.append(dict(at_us=counter["last_mismatch_at_us"], revision=counter["router_revision"], reason="counter_mismatch"))
        # Only the union of actual writer lifetimes inside the flag interval
        # covers time. A boot ID in tomorrow's roster cannot bridge a shutdown.
        merged: list[tuple[int, int]] = []
        for left, right in sorted(covered):
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], right))
            else:
                merged.append((left, right))
        if not merged or merged[0][0] > max(day_start, start) or merged[-1][1] < min(day_end, end) or len(merged) > 1:
            gap(day+":writer_coverage_gap", day)
        intervals.extend(merged)
    intervals.sort()
    for previous, current in zip(intervals, intervals[1:], strict=False):
        if previous[1] < current[0]:
            gap("interval_gap", dt.datetime.fromtimestamp((current[0]-1)/1e6, dt.UTC).date().isoformat())
    low = int(dt.datetime.combine(dt.date.fromisoformat(requested[0]), dt.time(), dt.UTC).timestamp()*1e6)
    high = int(dt.datetime.combine(dt.date.fromisoformat(requested[-1])+dt.timedelta(days=1), dt.time(), dt.UTC).timestamp()*1e6)
    samples = sorted((row for row in samples if low <= row["observed_at_us"] < high), key=lambda row: row["observed_at_us"])
    inserted: Counter[tuple[str, str]] = Counter()
    for row in samples:
        observation_day = dt.datetime.fromtimestamp(row["observed_at_us"]/1e6, dt.UTC).date().isoformat()
        manifest = manifests.get(observation_day)
        deployment = row["deployment"]
        inserted[observation_day, deployment["instance"]] += 1
        writer = counters.get(observation_day + "/" + deployment["instance"])
        if (writer is None or not writer["started_at_us"] <= row["observed_at_us"] <= writer["flushed_at_us"]):
            gap(row["authorization_id"]+":writer_interval_gap", observation_day)
        if (manifest is None or deployment["instance"] not in manifest["instance_boot_ids"]
                or deployment["router_revision"] not in manifest["router_revisions"]
                or deployment["go_revision"] not in manifest["go_revisions"]):
            gap(row["authorization_id"]+":deployment_unknown", observation_day)
        if row["classification"] in {"hash", "identity", "normalization", "evaluator_disagreement"}:
            resets.append(dict(at_us=row["observed_at_us"], revision=row["deployment"]["router_revision"], reason=row["classification"]))
        elif not positive_sample(row) and not known_exclusion(row):
            gap(row["authorization_id"]+":sample_gap", observation_day)
    for identity, counter in counters.items():
        day, boot = identity.split("/")
        if day in requested and counter["samples_inserted"] != inserted[day, boot]:
            gap(identity+":sample_count_gap", day)
    # A closed, fully covered later day can restore coverage after an earlier
    # gap. Correctness resets additionally need an explicit reviewed resolution
    # in the proof manifest; a restart or mere revision change is insufficient.
    restored_after = max((end for end in gap_ends if end is not None), default=low)
    unresolved = any(end is None for end in gap_ends)
    resolutions = proof.get("resolved_mismatches", [])
    if not isinstance(resolutions, list) or len(resolutions) > 128:
        raise ValueError("reset resolutions")
    for resolution in resolutions:
        if (not isinstance(resolution, dict) or set(resolution) != {"at_us", "revision", "fixed_revision", "serving_since_us", "artifact_sha256"}
                or not _uint(resolution["at_us"]) or not _uint(resolution["serving_since_us"])
                or resolution["serving_since_us"] <= resolution["at_us"]
                or any(not isinstance(resolution[k], str) or re.fullmatch(r"[0-9a-f]{40}", resolution[k]) is None for k in ("revision", "fixed_revision"))
                or resolution["fixed_revision"] == resolution["revision"]
                or not isinstance(resolution["artifact_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", resolution["artifact_sha256"]) is None):
            raise ValueError("reset resolution schema")
    for reset in resets:
        resolved = next((item for item in resolutions if (item["at_us"], item["revision"]) == (reset["at_us"], reset["revision"])), None)
        if resolved is None:
            unresolved = True
            continue
        fixed_day = dt.datetime.fromtimestamp(resolved["serving_since_us"]/1e6, dt.UTC).date().isoformat()
        manifest = manifests.get(fixed_day)
        if manifest is None or manifest["router_revisions"] != [resolved["fixed_revision"]]:
            unresolved = True
            continue
        reset["resolved_at_us"] = resolved["serving_since_us"]
        reset["fixed_revision"] = resolved["fixed_revision"]
        restored_after = max(restored_after, resolved["serving_since_us"])
    candidates = [row for row in samples if positive_sample(row) and row["observed_at_us"] >= restored_after]
    start = candidates[0]["observed_at_us"] if candidates and not unresolved else None
    # Measure the connected covered interval containing the positive seed.
    # Overlap counts once; an uncovered interval can never accrue clean time.
    end = start
    if start is not None:
        for left, right in intervals:
            if left <= end < right:
                end = min(right, high)
    continuous = (end-start)/1e6 if start is not None and end is not None else 0
    completeness = not unresolved and restored_after < high
    metrics = {"classification": dict(Counter(row["classification"] for row in samples)),
               "prediction": dict(Counter(row["admission"]["prediction"] for row in samples)),
               "signed_delta_histogram": dict(Counter(delta_bin(row["booked_minus_frozen"]) for row in samples if row["booked_minus_frozen"] is not None)),
               "delta_null_count": sum(row["booked_minus_frozen"] is None for row in samples)}
    denominators = [dict(adapter=a, route_type=r, streamed=s, **dict.fromkeys(COUNT_FIELDS, 0)) for a, r, s in DIMENSIONS]
    for identity, counter in counters.items():
        if identity.split("/")[0] in requested:
            for total, bucket in zip(denominators, counter["counts"], strict=True):
                for field in COUNT_FIELDS:
                    total[field] += bucket[field]
    predictions = {kind: sum(counter["admission_observer"]["prediction_"+kind] for identity, counter in counters.items()
                             if identity.split("/")[0] in requested) for kind in ("yes", "no", "unknown")}
    prediction_total = sum(predictions.values())
    metrics["prediction_attempts"] = predictions
    metrics["prediction_known_fraction"] = (predictions["yes"]+predictions["no"])/prediction_total if prediction_total else None
    region_timing = {}
    for region in sorted({row["deployment"]["region"] for row in samples}):
        subset = [row for row in samples if row["deployment"]["region"] == region]
        region_timing[region] = {name: percentiles([row[section][name] for row in subset])
            for section, names in (("timing", ("authorize_shadow_us", "handoff_prepare_us", "router_settle_us", "booking_confirm_us", "comparator_us", "evidence_write_us")),
                                   ("admission", ("workspace_age_us", "health_age_us"))) for name in names}
    status = {name: "PASS" if gates.get(name, False) else "BLOCKED" for name in EXIT_CRITERIA}
    status["seven_days_admission_off"] = "PASS" if continuous >= 604800 and completeness else "BLOCKED"
    status["denominators_and_diagnostics"] = "PASS" if completeness and candidates else "BLOCKED"
    status["zero_unexplained_all_evaluable"] = "PASS" if completeness and candidates and not unresolved else "BLOCKED"
    usage = {section: {field: percentiles([row[section][field] if row[section] is not None else None for row in samples])
                       for field in fields.split()} for section, fields in (
        ("raw_usage", "input_tokens output_tokens cache_read_tokens cache_creation_tokens reasoning_tokens"),
        *((name, "uncached_input_tokens total_prompt_tokens output_tokens cache_read_tokens cache_creation_tokens reasoning_tokens")
          for name in ("python_usage", "go_usage", "legacy_usage")))}
    revisions = dict(router=sorted({body["router_revision"] for identity, body in counters.items() if identity.split("/")[0] in requested}),
                     go=sorted({revision for day, body in manifests.items() if day in requested for revision in body["go_revisions"]}))
    return dict(status="PASS" if all(v == "PASS" for v in status.values()) else "BLOCKED",
        exit_criteria=[dict(criterion=k, rule=v, status=status[k]) for k, v in EXIT_CRITERIA.items()],
        completeness="complete" if completeness else "unknown", gaps=sorted(set(gaps)), resets=resets,
        clean_window_start_us=start, continuous_seconds=continuous, distinct_samples=len(samples),
        scope="successful opted-in authorizations; opted-in settle/refund attempts",
        counters=list(counters.values()), denominators=denominators,
        histogram_upper_bounds_us=[100, 500, 1000, 2000, 5000, 10000, 50000, 200000, None],
        metrics=metrics, regional_percentiles=region_timing, usage=usage, source_revisions=revisions,
        fixture_sha256=FIXTURE_SHA256, proof_sha256=proof_hash,
        report_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", action="append", required=True)
    parser.add_argument("--proof-manifest", type=Path, required=True)
    parser.add_argument("--evidence-json", type=Path, help="Offline exported rows (including synthetic tests)")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--after-id", default="")
    parser.add_argument("--publish-manifest", type=Path,
                        help="Persist an explicitly reviewed day manifest; never derives a roster from writers")
    args = parser.parse_args()
    proof = bounded_json(args.proof_manifest.read_bytes(), 262144, signed=True)
    if args.evidence_json:
        raw = args.evidence_json.read_bytes()
        rows = bounded_json(raw, len(raw), signed=True)
    else:
        from trusted_router.storage import STORE, typed_billing_store
        from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore
        store = EvidenceStore(typed_billing_store(STORE)._database)
        if args.publish_manifest:
            body = bounded_json(args.publish_manifest.read_bytes(), 262144)
            if (body["day"] not in args.day or body["instance_boot_ids"] != proof.get("instance_boot_ids_by_day", {}).get(body["day"])
                    or body["proof_manifest_sha256"] != hashlib.sha256(canonical(proof)).hexdigest()):
                raise ValueError("manifest must match explicit reviewed inventory and proof")
            store.publish_manifest(body, time.monotonic()+.2)
        days = set(args.day)
        for day in args.day:
            days.update((dt.date.fromisoformat(day)-dt.timedelta(days=n)).isoformat() for n in (1, 2))
        rows = []
        for kind in (SAMPLE, COUNTER, CONTROL):
            for day in sorted(days if kind == SAMPLE else args.day):
                if args.cleanup:
                    print(json.dumps(dict(kind=kind, day=day, after_id=store.cleanup(kind, day, args.after_id))))
                    continue
                limit = 8192 if kind == SAMPLE else 65536 if kind == COUNTER else 262144
                rows.extend(dict(kind=kind, id=identity, body=bounded_json(body.encode(), limit, signed=True)) for identity, body in store.day(kind, day))
        if args.cleanup:
            return
    output = report(rows, args.day, proof)
    print(json.dumps(output, indent=2))
    raise SystemExit(0 if output["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
