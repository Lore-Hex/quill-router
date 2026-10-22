"""Pure A1 health freshness and publisher evidence. No clocks, IO or caches."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_a1_deltas as d
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_a1_numbers import binary64
from trusted_router.async_settle_shadow_v2_schema import (
    Schema,
    array,
    boolean,
    day,
    digest,
    identity,
    integer,
    literal,
    nullable,
    require,
    sequence,
    sha256,
    text,
    versioned,
)

HEALTH = d.extend(p1.HEALTH, {"observed_at_ns": integer, "publisher_heartbeat_ns": integer,
    "backlog_age_at_seconds": binary64}, removed=("observed_at_us", "publisher_heartbeat_us"),
    projection=d.health_projection, shape="health", binary64_paths=(("backlog_age_at_seconds",),))
HEALTH_CLAIM = d.extend(p1.HEALTH_CLAIM, {"claim_at_ns": integer}, removed=("claim_at_us",),
    projection=d.claim_projection, shape="claim")
HEALTH_READ_METADATA = Schema({"committed_at_ns": integer, "read_at_ns": integer}, 128)


def health_usable(body: Any, metadata: Any, read_elapsed_ns: Any, since_reply_ns: Any) -> bool:
    """Exact O/C/R + q/e reader predicate; malformed inputs are contract errors."""
    HEALTH(body)
    HEALTH_READ_METADATA(metadata)
    integer(read_elapsed_ns)
    integer(since_reply_ns)
    boundary = body["slot"] * 2000000000
    observed = body["observed_at_ns"]
    committed, read = metadata["committed_at_ns"], metadata["read_at_ns"]
    # HEALTH has already enforced heartbeat equality. The lossless projection
    # at O neutralizes only P1's superseded age envelope; retained policy is
    # still decided by P1, independently of the exact A1 O/C/R/q/e envelope.
    retained = d.health_projection(body)
    return bool(p1.health_usable(retained, retained["observed_at_us"])
        and boundary <= observed <= boundary + 250000000
        and observed <= committed <= boundary + 750000000
        and committed <= read
        and read_elapsed_ns <= 250000000
        and read_elapsed_ns + since_reply_ns <= 1500000000
        and (read - observed) + read_elapsed_ns + since_reply_ns < 5000000000)


def validate_publication(claim: Any, body: Any, previous: Any = None) -> None:
    """Won-claim and strictly increasing publication fences over supplied evidence."""
    HEALTH_CLAIM(claim)
    HEALTH(body)
    require(claim["owner_boot_id"] == body["publisher_boot_id"] and claim["slot"] == body["slot"], "health_slot_gap")
    require(claim["claim_at_ns"] <= body["observed_at_ns"] <= body["slot"] * 2000000000 + 250000000, "health_slot_gap")
    if previous is not None:
        HEALTH(previous)
        require(previous["slot"] < body["slot"] and previous["observed_at_ns"] < body["observed_at_ns"], "health_slot_gap")


def _basis(v: Any) -> None:
    available = v["source_state"] in ("installed", "unusable")
    require(all((v[k] is not None) == available for k in ("body", "committed_at_ns", "read_at_ns")))
    if available:
        usable = health_usable(v["body"], {k: v[k] for k in HEALTH_READ_METADATA.fields}, v["read_elapsed_ns"], v["since_reply_ns"])
        require(usable == (v["source_state"] == "installed"))


HEALTH_BASIS = Schema({"v": literal(2), "policy_version": literal("shadow-v2"), "contract_revision": literal("a1"),
    "reader_boot": identity, "source_state": literal("installed", "unusable", "empty", "read_failed"),
    "body": nullable(HEALTH), "committed_at_ns": nullable(integer), "read_at_ns": nullable(integer),
    "read_elapsed_ns": integer, "since_reply_ns": integer}, 6144, _basis,
    binary64_paths=(("body", "backlog_age_at_seconds"),))


def _basis_batch(v: Any) -> None:
    hashes = [sha256(b) for b in v["bases"]]
    require(hashes == sorted(set(hashes)))
    require(all(b["reader_boot"] == v["boot"] for b in v["bases"]))
    require((v["previous_sha256"] is None) == (int(v["sequence"]) == 1))


HEALTH_BASIS_BATCH = Schema({"v": literal(2), "policy_version": literal("shadow-v2"), "contract_revision": literal("a1"),
    "boot": identity, "day": day, "sequence": sequence, "previous_sha256": nullable(digest),
    "bases": array(HEALTH_BASIS, 10, 1)}, 65536, _basis_batch,
    binary64_paths=(("bases", "*", "body", "backlog_age_at_seconds"),))
PUBLISHER_OPERATIONS = d.extend(p1.PUBLISHER_OPERATIONS, dict.fromkeys(d.PUBLISHER_ADDITIONS, integer),
    projection=d.publisher_projection, tagged=False)


def validate_publisher_final(value: Any) -> None:
    PUBLISHER_OPERATIONS(value)
    d.apply_deltas("publisher-final", value)


# Closed vocabulary shared with the reply diagnostic; never log arbitrary errors.
TRACE_RULES = frozenset({
    "trace_missed_shape", "trace_cycle_shape", "trace_failed_only_flags",
    "trace_release_signature", "trace_missed_nulls", "trace_missed_rpcs",
    "trace_observed_heartbeat_pair", "trace_claim_before_start",
    "trace_claim_observed_order", "trace_heartbeat_equal", "trace_digest_needs_observation",
    "trace_ack_needs_digest", "trace_lost_nulls", "trace_published_fields",
    "trace_published_cycle_bound", "trace_published_window", "trace_late_flag", "trace_late_cycle",
})


def _trace(v: Any) -> None:
    missed = v["outcome"] == "missed"
    require((v["claim_start_us"] is None) == missed, "trace_missed_shape")
    require((v["cycle_us"] is None) == missed, "trace_cycle_shape")
    require(not (v["unknown_outcome"] or v["late_cycle"]) or v["outcome"] == "failed", "trace_failed_only_flags")
    if "release_reason" in v:
        require(v["outcome"] == "failed" and v["unknown_outcome"]
                and v["cycle_us"] == v["rpc_count"] == v["rpc_elapsed_us"] == 0
                and all(v[k] is None for k in ("claim_at_ns", "observed_at_ns",
                    "publisher_heartbeat_ns", "body_sha256", "ack_at_us")), "trace_release_signature")
    if missed:
        require(all(v[k] is None for k in ("claim_at_ns", "observed_at_ns", "publisher_heartbeat_ns", "body_sha256", "ack_at_us")), "trace_missed_nulls")
        require(v["rpc_count"] == v["rpc_elapsed_us"] == 0, "trace_missed_rpcs")
    require((v["observed_at_ns"] is None) == (v["publisher_heartbeat_ns"] is None), "trace_observed_heartbeat_pair")
    if v["claim_at_ns"] is not None:
        require(v["claim_start_us"] is not None, "trace_claim_before_start")
    if v["observed_at_ns"] is not None:
        # Check the unchanged publication window before heartbeat equality so
        # a single late-observation probe identifies the window it violates.
        if v["outcome"] == "published":
            require(v["observed_at_ns"] <= v["slot"] * 2000000000 + 250000000, "trace_published_window")
        require(v["claim_at_ns"] is not None and v["claim_at_ns"] <= v["observed_at_ns"], "trace_claim_observed_order")
        require(v["observed_at_ns"] == v["publisher_heartbeat_ns"], "trace_heartbeat_equal")
    if v["body_sha256"] is not None:
        require(v["observed_at_ns"] is not None, "trace_digest_needs_observation")
    if v["ack_at_us"] is not None:
        require(v["body_sha256"] is not None and not v["unknown_outcome"], "trace_ack_needs_digest")
    if v["outcome"] in ("lost_claim", "published"):
        # An aborted claim read can lose contention before returning a server
        # timestamp. Validate the timestamp when one was actually received.
        if v["claim_at_ns"] is not None:
            d.apply_deltas("claim", v)
    if v["outcome"] == "lost_claim":
        require(all(v[k] is None for k in ("observed_at_ns", "publisher_heartbeat_ns", "body_sha256", "ack_at_us")), "trace_lost_nulls")
    if v["outcome"] == "published":
        require(all(v[k] is not None for k in ("claim_at_ns", "observed_at_ns", "body_sha256", "ack_at_us", "cycle_us")), "trace_published_fields")
        require(v["cycle_us"] <= 500000, "trace_published_cycle_bound")
    if v["cycle_us"] is not None and v["cycle_us"] > 500000:
        require(v["late_cycle"], "trace_late_flag")
    if v["late_cycle"]:
        require(v["cycle_us"] is not None and v["cycle_us"] > 500000, "trace_late_cycle")


PUBLISHER_SLOT_TRACE = versioned({"contract_revision": literal("a1"), "day": day, "boot": identity,
    "slot": integer, **{k: nullable(integer) for k in "claim_start_us claim_at_ns observed_at_ns publisher_heartbeat_ns ack_at_us cycle_us".split()},
    "body_sha256": nullable(digest), "outcome": literal("missed", "lost_claim", "published", "failed"),
    "unknown_outcome": boolean, "late_cycle": boolean, "rpc_count": integer, "rpc_elapsed_us": integer}, 1024, _trace)
# Historical traces retain their exact bytes. Only released-job records carry
# this optional diagnostic; no reader freshness or publication rule changes.
PUBLISHER_SLOT_TRACE = Schema(
    PUBLISHER_SLOT_TRACE.fields, PUBLISHER_SLOT_TRACE.limit, PUBLISHER_SLOT_TRACE.invariant,
    optional_fields={"release_reason": text(64)})


def _trace_page(v: Any) -> None:
    entries = v["entries"]
    require(v["first_slot"] == entries[0]["slot"] and v["last_slot"] == entries[-1]["slot"])
    require(all(row["day"] == v["day"] and row["boot"] == v["boot"]
                and row["slot"] == v["first_slot"] + i for i, row in enumerate(entries)))


PUBLISHER_TRACE_PAGE = versioned({"contract_revision": literal("a1"), "day": day, "boot": identity,
    "first_slot": integer, "last_slot": integer, "previous_sha256": nullable(digest),
    "entries": array(PUBLISHER_SLOT_TRACE, 60, 1)}, 65536, _trace_page)


def validate_publisher_traces(counter: Any, entries: Any, *, day: Any, boot: Any,
                              first_slot: Any, last_slot: Any) -> None:
    """Reconcile independent scheduled bounds with literal outcomes, never invent traces.

    Claims/scans cannot be reconstructed from the stage timestamps after a lost
    reply; their exact reconciliation requires P3's operation evidence. Their
    inequalities are enforced here, without inventing those missing facts.
    Cycle durations determine their exact histogram (inclusive upper bounds).
    RPC counts determine sample count only: individual RPC durations are absent.
    """
    validate_publisher_final(counter)
    p1.day(day)
    identity(boot)
    nullable(integer)(first_slot)
    nullable(integer)(last_slot)
    array(PUBLISHER_SLOT_TRACE, 43200)(entries)
    if first_slot is None or last_slot is None:
        require(first_slot is last_slot is None and entries == [] and counter["scheduled_slots"] == 0, "publisher_trace_gap")
    else:
        require(last_slot >= first_slot and len(entries) == last_slot - first_slot + 1, "publisher_trace_gap")
        require(all(row["day"] == day and row["boot"] == boot and row["slot"] == first_slot + i for i, row in enumerate(entries)), "publisher_trace_gap")
    totals = {"scheduled_slots": len(entries), "missed_slots": 0, "claim_attempts": 0,
              "lost_claims": 0, "publications": 0, "failures": 0, "unknown_outcomes": 0, "late_cycles": 0}
    outcomes = {"missed": "missed_slots", "lost_claim": "lost_claims", "published": "publications", "failed": "failures"}
    for row in entries:
        totals[outcomes[row["outcome"]]] += 1
        totals["claim_attempts"] += row["outcome"] != "missed"
        totals["unknown_outcomes"] += row["unknown_outcome"]
        totals["late_cycles"] += row["late_cycle"]
    require(all(counter[k] == value for k, value in totals.items()), "publisher_accounting_gap")
    cycle_counts = [0] * len(counter["cycle_us"]["counts"])
    for row in entries:
        if row["cycle_us"] is not None:
            cycle_counts[sum(row["cycle_us"] > edge for edge in counter["cycle_us"]["bounds_us"])] += 1
    require(counter["cycle_us"]["counts"] == cycle_counts, "publisher_accounting_gap")
    require(sum(counter["rpc_us"]["counts"]) == sum(row["rpc_count"] for row in entries), "publisher_accounting_gap")
    observed = sum(row["observed_at_ns"] is not None for row in entries)
    require(observed <= counter["scans"] <= counter["claims"] <= counter["claim_attempts"] - counter["lost_claims"], "publisher_accounting_gap")


def validate_basis_trace(basis: Any, trace: Any) -> None:
    HEALTH_BASIS(basis)
    PUBLISHER_SLOT_TRACE(trace)
    body = basis["body"]
    require(body is not None, "publisher_trace_gap")
    require(trace["boot"] == body["publisher_boot_id"] and trace["slot"] == body["slot"]
            and trace["observed_at_ns"] == body["observed_at_ns"]
            and trace["publisher_heartbeat_ns"] == body["publisher_heartbeat_ns"]
            and trace["body_sha256"] == sha256(body), "publisher_trace_gap")
