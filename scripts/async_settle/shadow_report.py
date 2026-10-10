"""Conservative day-prefix shadow report. Logs are never an evidence source."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import time
from bisect import bisect_right
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
    PHASE_FIELDS,
    SAMPLE,
    RetryIdentity,
    original_can_back_retry,
    retry_classification,
    retry_identity,
    validate_manifest,
    validate_sample,
)
from trusted_router.async_settle_shadow_wire import bounded_json
from trusted_router.detached_jws import canonical

EXIT_CRITERIA = {
    "seven_days_admission_off": "604800 continuous seconds, complete roster and flag coverage",
    "denominators_and_diagnostics": "reconciled dimensional counters; known predictions and ages",
    "zero_unexplained_all_evaluable": "no mismatch, gap, drop, comparator unknown, or retry conflict; bounded admission unknowns",
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
COUNTER_FIELDS = set("v instance region router_revision policy_version started_at_us flushed_at_us sequence closed counts terminal_counts exclusions rejections drops dimension_overflow counter_overflow comparison_attempts comparison_dropped samples_inserted duplicate_samples conflicting_samples booking_pending booking_unknown first_evidence_at_us last_mismatch_at_us first_gap_at_us authorize_shadow_hist evidence_write_hist admission_observer".split())


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
    observer_fields = set("workspace_reads health_reads read_failures missed_ticks late_installs max_consecutive_failures degraded_seconds prediction_yes prediction_no prediction_unknown".split())
    observer = body["admission_observer"]
    if set(observer) != observer_fields or not all(_uint(n) for n in observer.values()):
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
    seen_phases = set()
    if not isinstance(body["terminal_counts"], list) or len(body["terminal_counts"]) > 2 * len(DIMENSIONS):
        raise ValueError("terminal counter bounds")
    for row in body["terminal_counts"]:
        if (set(row) != {"phase", "adapter", "route_type", "streamed", *PHASE_FIELDS}
                or row["phase"] not in {"settle", "refund"}
                or (row["adapter"], row["route_type"], row["streamed"]) not in DIMENSIONS
                or not all(_uint(row[k]) for k in PHASE_FIELDS)):
            raise ValueError("terminal counter schema")
        key = (row["phase"], row["adapter"], row["route_type"], row["streamed"])
        if key in seen_phases:
            raise ValueError("duplicate terminal counter")
        seen_phases.add(key)
    for key in ("authorize_shadow_hist", "evidence_write_hist"):
        if len(body[key]) != 9 or not all(_uint(n) for n in body[key]):
            raise ValueError("histogram")


def observer_budget(body: dict[str, Any]) -> dict[str, Any]:
    """Per-counter bounds, shared by fleet and shadow coverage gates."""
    obs = body["admission_observer"]
    reads = obs["health_reads"] + obs["workspace_reads"]
    failures = sum(obs[k] for k in ("read_failures", "missed_ticks", "late_installs"))
    known = obs["prediction_yes"] + obs["prediction_no"]
    predictions = known + obs["prediction_unknown"]
    duration = (body["flushed_at_us"] - body["started_at_us"]) / 1e6
    limit = max(3, reads / 100)
    ok = (duration > 0 and failures * 100 <= max(300, reads) and obs["max_consecutive_failures"] <= 2
          and known > 0 and obs["prediction_unknown"] * 100 <= predictions * 2
          and obs["degraded_seconds"] * 100_000_000 <= body["flushed_at_us"] - body["started_at_us"])
    return dict(status="PASS" if ok else "BLOCKED", reads=reads, failures=failures,
                failure_limit=limit, failure_ratio=failures / reads if reads else None,
                max_consecutive_failures=obs["max_consecutive_failures"],
                known_predictions=known, predictions=predictions,
                unknown_predictions=obs["prediction_unknown"],
                unknown_ratio=obs["prediction_unknown"] / predictions if predictions else None,
                degraded_seconds=obs["degraded_seconds"], covered_seconds=duration,
                degraded_ratio=obs["degraded_seconds"] / duration if duration > 0 else None)


def admission_evaluable(row: dict[str, Any], *, allow_unknown: bool = False) -> bool:
    admission = row["admission"]
    return bool(allow_unknown and admission["prediction"] == "unknown"
                or admission["prediction"] in {"yes", "no"}
                and all(_uint(admission[k]) and admission[k] < 5_000_000
                        for k in ("workspace_age_us", "health_age_us")))


def positive_sample(row: dict[str, Any], *, allow_unknown: bool = False) -> bool:
    p = row["provenance"]
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
        and admission_evaluable(row, allow_unknown=allow_unknown))


def known_exclusion(row: dict[str, Any], *, allow_unknown: bool = False) -> bool:
    """Verified out-of-cohort facts are denominators, never positive clock seeds."""
    return bool(row["classification"] == "unevaluable" and row["eligibility"]["observed"] is False
        and row["provenance"]["binding_verified"] is True and row["provenance"]["raw_matches_body"] is True
        and row["provenance"]["s0_reconstruction"] in {"not_needed", "verified"}
        and row["snapshot_hash"] is not None and row["reason_codes"] and set(row["reason_codes"]) <= COHORT_EXCLUSIONS
        and row["booking"]["source"] == "finalized_authorization" and row["booked_micro"] is not None
        and admission_evaluable(row, allow_unknown=allow_unknown))


def collect_rollbacks(resets: list[dict[str, Any]], serving: list[tuple[int, int, str]],
                      resolutions: list[dict[str, Any]], manifests: dict[str, Any]) -> None:
    """Invalidate fixes fleet-wide at the first incompatible serving instant.

    SHAs are opaque. Reviewed A->B resolutions define a partial revision order;
    only B and its reviewed successors establish retention of B's fix. Unknown
    branches, predecessors and A itself cannot accrue post-fix observation.
    """
    successors: dict[str, set[str]] = {}
    for item in resolutions:
        successors.setdefault(item["revision"], set()).add(item["fixed_revision"])
    def descendants(revision: str) -> set[str]:
        found: set[str] = set()
        pending = list(successors.get(revision, ()))
        while pending:
            current = pending.pop()
            if current not in found:
                found.add(current)
                pending.extend(successors.get(current, ()))
        return found
    if any(revision in descendants(revision) for revision in successors):
        raise ValueError("cyclic reset revision order")
    # Every reviewed resolution is defect history, even if its raw mismatch
    # was omitted from this export. Keep row-derived reasons when available.
    known = {(reset["at_us"], reset["revision"]) for reset in resets}
    for item in resolutions:
        key = (item["at_us"], item["revision"])
        if key not in known:
            resets.append(dict(at_us=key[0], revision=key[1], reason="reviewed_resolution"))
            known.add(key)
    processed = set()
    # Appended rollback resets may themselves have a later reviewed resolution.
    for reset in resets:
        key = (reset["at_us"], reset["revision"])
        if key in processed:
            continue
        processed.add(key)
        resolved = next((item for item in resolutions if (item["at_us"], item["revision"]) == key), None)
        if resolved is None:
            continue
        since = resolved["serving_since_us"]
        fixed_day = dt.datetime.fromtimestamp(since/1e6, dt.UTC).date().isoformat()
        manifest = manifests.get(fixed_day)
        if manifest is None or manifest["router_revisions"] != [resolved["fixed_revision"]]:
            continue
        safe = {resolved["fixed_revision"], *descendants(resolved["fixed_revision"])}
        rollback = min(((max(left, since), revision) for left, right, revision in serving
                        if right > since and revision not in safe), default=None)
        if rollback is not None:
            at_us, revision = rollback
            reset["invalidated_at_us"] = at_us
            resets.append(dict(at_us=at_us, revision=revision, reason="revision_rollback"))


def report(rows: list[dict[str, Any]], days: list[str], proof: dict[str, Any],
           *, not_before_us: int = 0) -> dict[str, Any]:
    if not _uint(not_before_us):
        raise ValueError("clock prerequisite timestamp")
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
    serving: list[tuple[int, int, str]] = []
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
    resolution_keys = [(item["at_us"], item["revision"]) for item in resolutions]
    if len(set(resolution_keys)) != len(resolution_keys):
        raise ValueError("duplicate reset resolution")
    identities = set()
    for row in rows:
        if set(row) != {"kind", "id", "body"} or (row["kind"], row["id"]) in identities:
            raise ValueError("duplicate or invalid evidence")
        identities.add((row["kind"], row["id"]))
        kind, identity, body = row["kind"], row["id"], row["body"]
        if kind == SAMPLE:
            validate_sample(body, identity)
            samples.append(body)
            serving.append((body["observed_at_us"], body["observed_at_us"] + 1, body["deployment"]["router_revision"]))
            # Correctness history survives requested-day/diagnostic filtering.
            if body["classification"] in {"hash", "identity", "normalization", "evaluator_disagreement"}:
                resets.append(dict(at_us=body["observed_at_us"], revision=body["deployment"]["router_revision"], reason=body["classification"]))
        elif kind == COUNTER:
            validate_counter(identity, body, partitions=False)
            counters[identity] = body
            serving.append((body["started_at_us"], body["flushed_at_us"], body["router_revision"]))
            if body["last_mismatch_at_us"] is not None or body["conflicting_samples"]:
                resets.append(dict(at_us=body["last_mismatch_at_us"], revision=body["router_revision"], reason="counter_mismatch"))
        elif kind == CONTROL and identity.endswith("/manifest-v1"):
            validate_manifest(body, identity)
            if set(body) != MANIFEST_FIELDS or len(canonical(body)) > 262144 or body["day"] + "/manifest-v1" != identity:
                raise ValueError("manifest schema")
            if len(body["instance_boot_ids"]) > 4096 or sorted(set(body["instance_boot_ids"])) != body["instance_boot_ids"] or len(body["gap_intervals"]) > 128:
                raise ValueError("manifest bounds")
            manifests[body["day"]] = body
            # The daily roster has no per-revision activation time: conservatively
            # treat every listed revision as serving throughout that UTC day.
            day_start = int(dt.datetime.combine(dt.date.fromisoformat(body["day"]), dt.time(), dt.UTC).timestamp()*1e6)
            serving.extend((day_start, day_start + 86400_000000, revision) for revision in body["router_revisions"])
        elif kind == CONTROL and identity.endswith("/cap-v1"):
            if (set(body) != {"v", "limit", "reserved", "updated_at_us"} or type(body["v"]) is not int or body["v"] != 1
                    or body["limit"] != 100000 or not _uint(body["reserved"]) or body["reserved"] > 100000
                    or not _uint(body["updated_at_us"])):
                raise ValueError("invalid cap row")
        else:
            raise ValueError("unknown evidence kind")
    # Finish correctness/reset collection over ALL supplied evidence, before any
    # requested-day filtering. A rollback is itself a new correctness reset;
    # restarting B alone cannot resolve it without another reviewed artifact.
    supported = {(reset["at_us"], reset["revision"]) for reset in resets}
    collect_rollbacks(resets, serving, resolutions, manifests)
    # Derived rollbacks are independent support; the resolution's own synthetic
    # reset is not. Missing support is an unbounded gap, never a clean lookback.
    supported.update((reset["at_us"], reset["revision"]) for reset in resets
                     if reset["reason"] == "revision_rollback")
    for item in resolutions:
        if (item["at_us"], item["revision"]) not in supported:
            gap(f"resolution:{item['at_us']}:{item['revision']}:missing_support")
    # Retry counters have no authorization IDs or payload hashes. At minimum,
    # each phase needs a retained, verified original that the adapter could
    # duplicate. A different non-null retry hash could conflict with that same
    # original. Null/unverified originals prove neither possibility; their
    # retries must be counted as conflicts (which reset correctness) or block.
    # This is an existence check, not reconstruction of discarded retry hashes.
    # Index ALL supplied rows (including lookback days), independently of the
    # original writer and requested-window metrics. One original can support
    # many retries; an opposite-kind row can only yield winner_polarity.
    originals: dict[RetryIdentity, list[int]] = {}
    for row in samples:
        if retry_classification(row, row) != "duplicate":
            continue
        key = retry_identity(row)
        originals.setdefault(key, []).append(row["observed_at_us"])
    for times in originals.values():
        times.sort()
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
            verified = sum(bucket["observed_eligible"] + bucket["observed_ineligible"]
                for bucket in counter["counts"])
            outcomes_total = sum(bucket[k] for bucket in counter["counts"]
                for k in ("exact", "explained", "mismatch", "requires_review", "unevaluable"))
            if outcomes_total != counter["comparison_attempts"]:
                gap(identity+":comparison_outcome_gap", day)
            if counter["comparison_attempts"] > verified:
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
                        or bucket["envelope_present"] != observed
                        or bucket["header_absent"] > bucket["authorize_attempts"]):
                    gap(identity+":observed_partition_gap", day)
                authorize_exclusions = sum(row["count"] for row in counter["exclusions"]
                    if row["phase"] == "authorize"
                    and all(row[k] == bucket[k] for k in ("adapter", "route_type", "streamed")))
                if (bucket["authorize_attempts"] != bucket["snapshot_sent"] + authorize_exclusions
                        or bucket["snapshot_sent"] != bucket["requested_eligible"]
                        or bucket["requested_eligible"] > bucket["authorize_fresh"]):
                    gap(identity+":authorize_coverage_gap", day)
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
                # Clean coverage is a closed partition, not a budget into which
                # unrelated failures or writer-wide booking counts can be put.
                # Exclusions are verified unevaluable comparisons (a subset of
                # outcomes), never an alternative to running the comparator.
                if exclusions > bucket["unevaluable"]:
                    gap(identity+":exclusion_outcome_gap", day)
                accounted = outcomes - exclusions
                if bucket["observed_eligible"] != accounted:
                    gap(identity+":eligible_coverage_gap", day)
                if observed != outcomes or failures:
                    gap(identity+":terminal_coverage_gap", day)
                if (bucket["unevaluable"] != exclusions or bucket["requires_review"]
                        or bucket["mismatch"]):
                    gap(identity+":nonclean_outcome_gap", day)
            # Independent phase partitions must reproduce the aggregate counters.
            # Never infer a terminal's phase from a reason or from another writer.
            for bucket in counter["counts"]:
                phases = [row for row in counter["terminal_counts"]
                    if all(row[k] == bucket[k] for k in ("adapter", "route_type", "streamed"))]
                for field in set(PHASE_FIELDS) & set(COUNT_FIELDS):
                    if sum(row[field] for row in phases) != bucket[field]:
                        gap(identity+":phase_aggregate_gap", day)
                for phase in ("settle", "refund"):
                    terminal = next((row for row in phases if row["phase"] == phase), dict.fromkeys(PHASE_FIELDS, 0))
                    attempts = bucket[phase+"_attempts"]
                    reasons = {group: [row for row in counter[group] if row["phase"] == phase
                        and all(row[k] == bucket[k] for k in ("adapter", "route_type", "streamed"))]
                        for group in ("exclusions", "rejections", "drops")}
                    excluded = sum(row["count"] for row in reasons["exclusions"] if row["reason"] in COHORT_EXCLUSIONS)
                    outcomes = sum(terminal[k] for k in ("exact", "explained", "mismatch", "requires_review", "unevaluable"))
                    if (terminal["observed_attempts"] != attempts
                            or sum(terminal[k] for k in ("observed_eligible", "observed_ineligible", "observed_unknown")) != attempts
                            or terminal["envelope_present"] != attempts
                            or terminal["evaluable"] != terminal["exact"] + terminal["explained"]
                            or excluded != terminal["observed_ineligible"]
                            or excluded != terminal["unevaluable"]
                            or outcomes - excluded != terminal["observed_eligible"]
                            or outcomes != attempts or outcomes != terminal["comparison_attempts"]
                            or sum(terminal[k] for k in ("samples_inserted", "duplicate_samples", "conflicting_samples", "comparison_dropped")) != terminal["comparison_attempts"]
                            or any(row["count"] for group in ("rejections", "drops") for row in reasons[group])
                            or any(row["count"] for row in reasons["exclusions"] if row["reason"] not in COHORT_EXCLUSIONS)):
                        gap(identity+":"+phase+":phase_coverage_gap", day)
            for field in set(PHASE_FIELDS) - set(COUNT_FIELDS):
                if sum(row[field] for row in counter["terminal_counts"]) != counter[field]:
                    gap(identity+":phase_writer_gap", day)
            if (not counter["closed"] or counter["first_gap_at_us"] is not None or counter["counter_overflow"]
                    or counter["dimension_overflow"] or counter["comparison_dropped"] or counter["booking_pending"] or counter["booking_unknown"]
                    or observer_budget(counter)["status"] != "PASS"
                    or any(row["count"] for key in ("drops", "rejections") for row in counter[key])
                    or any(row["observed_unknown"] for row in counter["counts"])
                    or any(row["count"] for row in counter["exclusions"] if row["phase"] == "worker")):
                gap(identity+":counter_gap", day)
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
    unknown_samples: Counter[str] = Counter()
    inserted: Counter[tuple[str, str]] = Counter()
    sample_counts: Counter[tuple[str, str, str, bool | None, str]] = Counter()
    phase_samples: Counter[tuple[Any, ...]] = Counter()
    for row in samples:
        observation_day = dt.datetime.fromtimestamp(row["observed_at_us"]/1e6, dt.UTC).date().isoformat()
        manifest = manifests.get(observation_day)
        deployment = row["deployment"]
        inserted[observation_day, deployment["instance"]] += 1
        writer_id = observation_day + "/" + deployment["instance"]
        writer = counters.get(writer_id)
        category = {"explained-by-catalog-change": "explained", "hash": "mismatch", "identity": "mismatch",
                    "normalization": "mismatch", "evaluator_disagreement": "mismatch"}.get(row["classification"], row["classification"])
        if row["admission"]["prediction"] == "unknown":
            unknown_samples[writer_id] += 1
        eligibility = {True: "observed_eligible", False: "observed_ineligible", None: "observed_unknown"}[row["eligibility"]["observed"]]
        for field in (category, eligibility, row["booking"]["attempted_kind"] + "_attempts"):
            sample_counts[writer_id, row["adapter"], row["route_type"], row["streamed"], field] += 1
        phase_key = (writer_id, row["adapter"], row["route_type"], row["streamed"], row["booking"]["attempted_kind"])
        for field in (category, eligibility, "samples_inserted"):
            phase_samples[(*phase_key, field)] += 1
        if known_exclusion(row, allow_unknown=True):
            for reason in row["reason_codes"]:
                phase_samples[(*phase_key, "exclusion:"+reason)] += 1
        if (writer is None or not writer["started_at_us"] <= row["observed_at_us"] <= writer["flushed_at_us"]):
            gap(row["authorization_id"]+":writer_interval_gap", observation_day)
        if (manifest is None or deployment["instance"] not in manifest["instance_boot_ids"]
                or deployment["router_revision"] not in manifest["router_revisions"]
                or deployment["go_revision"] not in manifest["go_revisions"]):
            gap(row["authorization_id"]+":deployment_unknown", observation_day)
        if (row["classification"] not in {"hash", "identity", "normalization", "evaluator_disagreement"}
                and not positive_sample(row, allow_unknown=True) and not known_exclusion(row, allow_unknown=True)):
            gap(row["authorization_id"]+":sample_gap", observation_day)
    for identity, counter in counters.items():
        day, boot = identity.split("/")
        if day in requested and unknown_samples[identity] > counter["admission_observer"]["prediction_unknown"]:
            gap(identity+":prediction_count_gap", day)
        if day in requested and counter["samples_inserted"] != inserted[day, boot]:
            gap(identity+":sample_count_gap", day)
        if day in requested:
            for bucket in counter["counts"]:
                for field in (*("exact", "explained", "mismatch", "requires_review", "unevaluable"),
                              "observed_eligible", "observed_ineligible", "observed_unknown", "settle_attempts", "refund_attempts"):
                    durable = sample_counts[identity, bucket["adapter"], bucket["route_type"], bucket["streamed"], field]
                    if durable > bucket[field]:
                        gap(identity+":sample_classification_gap", day)
            for terminal in counter["terminal_counts"]:
                phase = terminal["phase"]
                key = (identity, terminal["adapter"], terminal["route_type"], terminal["streamed"], phase)
                if terminal["samples_inserted"] != phase_samples[(*key, "samples_inserted")]:
                    gap(identity+":"+phase+":sample_phase_gap", day)
                if terminal["duplicate_samples"] or terminal["conflicting_samples"]:
                    oldest = (dt.date.fromisoformat(day) - dt.timedelta(days=30)).isoformat()
                    has_original = False
                    for original, times in originals.items():
                        if not oldest <= original.authorization_day <= day:
                            continue
                        position = bisect_right(times, counter["flushed_at_us"])
                        retry = RetryIdentity(phase, counter["region"], original.authorization_day,
                            terminal["adapter"], terminal["route_type"], terminal["streamed"])
                        if position and original_can_back_retry(original, times[position - 1], retry,
                                counter["started_at_us"], counter["flushed_at_us"]):
                            has_original = True
                            break
                    if not has_original:
                        gap(identity+":"+phase+":retry_original_gap", day)
            for key, durable in phase_samples.items():
                writer_id, adapter, route, streamed, phase, field = key
                if writer_id != identity:
                    continue
                def matching(row: dict[str, Any], dims: tuple[Any, ...] = (adapter, route, streamed, phase)) -> bool:
                    return (row["adapter"], row["route_type"], row["streamed"], row["phase"]) == dims
                if field.startswith("exclusion:"):
                    counted = sum(row["count"] for row in counter["exclusions"]
                        if matching(row) and row["reason"] == field.split(":", 1)[1])
                else:
                    counted = sum(row[field] for row in counter["terminal_counts"] if matching(row))
                if durable > counted or field == "samples_inserted" and durable != counted:
                    gap(identity+":"+phase+":sample_phase_gap", day)
    # A closed, fully covered later day can restore coverage after an earlier
    # gap. Correctness resets additionally need an explicit reviewed resolution
    # in the proof manifest; a restart or mere revision change is insufficient.
    restored_after = max((end for end in gap_ends if end is not None), default=low)
    unresolved = any(end is None for end in gap_ends)
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
    candidates = [row for row in samples if positive_sample(row) and row["observed_at_us"] >= max(restored_after, not_before_us)]
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
    metrics["observer_budgets"] = {identity: observer_budget(counter) for identity, counter in counters.items()
                                   if identity.split("/")[0] in requested}
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
