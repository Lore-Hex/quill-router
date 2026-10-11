"""Versioned, content-free evidence schemas; no persistence or lifecycle effects."""
from __future__ import annotations

import itertools
from typing import Any

from trusted_router.async_settle_shadow_v2_contract import (
    ADAPTER,
    AUTHORITY,
    CANDIDATE_SET,
    COHORT_EXCLUSIONS,
    ORIGIN,
    RAW_USAGE,
    REQUESTED_FACTS,
    ROUTE,
    validate_identity,
)
from trusted_router.async_settle_shadow_v2_schema import (
    COORDINATES,
    LOCATOR,
    Schema,
    array,
    ascii_text,
    boolean,
    bounded_string,
    day,
    day_at,
    decode,
    digest,
    identity,
    integer,
    literal,
    mapping,
    name,
    native_identity,
    nullable,
    require,
    revision,
    sequence,
    sha256,
    signed_integer,
    versioned,
)
from trusted_router.detached_jws import canonical

PAGE_BYTES = 65536
EVENT_BYTES = 2048
PAGE_EVENTS = 128
MAX_PAGES = 4
MAX_DAY_BUCKETS = 3
TRANSACTION_ROWS = 6
TRANSACTION_BODY_BYTES = 331776
MANIFEST_BYTES = 262144
WRITERS = 4096
REVISIONS_PER_ROLE = 64
GAPS = 128
TERMINAL_CAP = 100000
EVENT_CAP = 400000
RESERVATION_BLOCK = 100
LIVE_EXPORT_KEYS = 10000
KINDS = tuple("async_settle_shadow_" + suffix + "_v2" for suffix in (
    "sample", "certificate", "receipts", "counter", "closure", "control", "health", "health_control"))
REASONS = literal(*sorted(COHORT_EXCLUSIONS))
LEGACY_REASONS = frozenset("""untyped non_credits settlement_authority unsupported_route service_tier app_markup custom_markup receipt_fee request_fee custom_model user_model tool_cost search_cost image_cost video_cost partner liberty native_batch fusion polyphemus private_tier_basis unsupported_adapter unknown_parameters replay header_absent snapshot_unavailable usage_missing usage_estimated malformed_usage arithmetic_overflow booking_pending booking_unknown rebuild_unavailable snapshot_reconstruction_failed catalog_change go_failure configuration_conflict missing_envelope snapshot_size winner_polarity legacy_oracle_unavailable counter_overflow header_duplicate header_size base64 json_encoding json_duplicate json_shape integer proof_signature proof_expired hash identity raw_usage rate_limit queue_full daily_cap store_unavailable evidence_size worker_error""".split())
SAMPLE_REASON = literal(*sorted(LEGACY_REASONS))
# New policy diagnostics are explicit additions, never arbitrary retained reasons.
COUNTER_REASON = literal(*sorted(LEGACY_REASONS | frozenset("""original_receipt_locator_gap
original_receipt_identity_gap original_receipt_digest_gap original_receipt_backing_gap
original_receipt_missing candidate_receipt_size receipt_locator_size exclusion_conflict
missing_signer missing_context transport_failure local_work_overflow evaluator_failed
ambiguous_fallback evidence_schema_gap evidence_identity_gap""".split())))
BINDING_FIXTURE_SHA256 = "68568699b000210413a20916ffa1f56093ab0223bb6a65d935ae453a9d96a28a"
IDENTITY_FIELDS = {"authorization_id": identity, "generation_id": name,
    "workspace_id": native_identity, "key_id": native_identity, "invocation_nonce": identity,
    "reservation_id": native_identity, "billing_authority": AUTHORITY, "journal_region": name,
    "epoch": integer, "settle_origin": ORIGIN}
COMMITTED_IDENTITIES = ("workspace_id", "key_id", "invocation_nonce", "reservation_id")
COMMITTED_FIELDS = {k: v for k, v in IDENTITY_FIELDS.items() if k not in COMMITTED_IDENTITIES}
COMMITTED_FIELDS.update({k + "_sha256": digest for k in COMMITTED_IDENTITIES})
AUTHORIZATION_IDENTITY = Schema(COMMITTED_FIELDS, 1024, validate_identity)
DIMENSIONS = Schema({"adapter": nullable(ADAPTER), "route_type": nullable(ROUTE), "streamed": nullable(boolean)}, 256)
DECISION_REFERENCE = Schema({"reason": nullable(REASONS), "facts_sha256": nullable(digest),
                             "candidate_set_sha256": nullable(digest)}, 256)
PREDICTION = Schema({"prediction": literal("yes", "no", "unknown"), "reason": name,
    "health_source": literal("shadow_observation"), "health_slot": nullable(integer),
    "prediction_basis": literal("counterfactual_backlog_v2"), "basis_sha256": digest}, 512)
BOOKING = Schema({"outcome": literal("settled", "refunded", "pending", "unknown", "not_attempted"),
    "reference": nullable(digest), "winner": nullable(literal("settle", "refund"))}, 512)
RECEIPT_REFERENCE = Schema({"day": day, "boot": identity, "event_seq": sequence}, 128)


def _event(v: Any) -> None:
    require(v["authorization_day"] <= day_at(v["event_at_us"]))
    require(v["receipt_at_us"] >= v["event_at_us"])
    if v["phase"] == "authorize":
        require(v["receipt_locator"] is not None and v["original_receipt"] is None
                and v["certificate_sha256"] is None and v["payload_sha256"] is None)
        loc = v["receipt_locator"]
        require(loc["event_seq"] == v["seq"] and loc["day"] == day_at(v["event_at_us"]))
        require(v["prediction"] is None and v["partition"] == "C")
    else:
        require(v["receipt_locator"] is None)
    if v["partition"] == "B":
        require(v["proof_kind"] in ("certificate", "observed_binding") and v["eligibility"] == "excluded")
        if v["proof_kind"] == "certificate":
            require(v["original_receipt"] is not None and v["certificate_sha256"] is not None)
        else:
            require(v["payload_sha256"] is not None)
        require(v["comparison"] in (None, "unevaluable"))
    if v["partition"] == "A":
        require(v["eligibility"] == "eligible" and v["proof_kind"] == "compared"
                and v["comparison"] in ("exact", "explained-by-catalog-change")
                and v["payload_sha256"] is not None)
    if v["partition"] != "C":
        require(v["booking"]["outcome"] in ("settled", "refunded") and v["booking"]["reference"] is not None)
    if v["phase"] in ("settle", "refund"):
        require((v["prediction"] is not None) == (v["eligibility"] == "eligible"))
        if v["partition"] in ("A", "B"):
            require((v["phase"], v["booking"]["outcome"], v["booking"]["winner"]) in
                    (("settle", "settled", "settle"), ("refund", "refunded", "refund")))
    else:
        # Completion resolves the original attempt; it is not another prediction.
        require(v["prediction"] is None)
    require(v["authorization_id"] == v["identity"]["authorization_id"])
    require((v["phase"] == "completion") == (v["completion_of"] is not None))


TERMINAL_EVENT = versioned({"seq": sequence, "event_at_us": integer, "receipt_at_us": integer,
    "phase": literal("authorize", "replay", "settle", "refund", "completion"),
    "authorization_day": day, "authorization_id": identity, "identity": AUTHORIZATION_IDENTITY,
    "router_revision": revision, "dimensions": DIMENSIONS, "decision": DECISION_REFERENCE,
    "facts": nullable(REQUESTED_FACTS), "candidates": nullable(CANDIDATE_SET),
    "receipt_locator": nullable(COORDINATES), "original_receipt": nullable(LOCATOR),
    "certificate_sha256": nullable(digest), "payload_sha256": nullable(digest),
    "proof_kind": literal("none", "compared", "certificate", "observed_binding"),
    "partition": literal("A", "B", "C"), "eligibility": literal("eligible", "excluded", "unknown"),
    "comparison": nullable(literal("exact", "explained-by-catalog-change", "mismatch", "unevaluable")),
    "booking": BOOKING, "prediction": nullable(PREDICTION),
    "retry_original": nullable(RECEIPT_REFERENCE), "completion_of": nullable(sequence),
    "diagnostic": nullable(Schema({"reason": name, "required_bytes": integer, "candidate_count": integer}, 256)),
}, EVENT_BYTES, _event)


AUTHORIZE_EVENT = versioned({k: check for k, check in TERMINAL_EVENT.fields.items()
    if k not in {"v", "policy_version", "original_receipt", "certificate_sha256", "payload_sha256",
                 "proof_kind", "comparison", "booking", "prediction", "retry_original", "completion_of"}}, EVENT_BYTES,
    lambda v: require(v["phase"] == "authorize" and v["partition"] == "C"
        and v["receipt_locator"] is not None and v["seq"] == v["receipt_locator"]["event_seq"]
        and v["authorization_day"] == v["receipt_locator"]["day"] == day_at(v["event_at_us"])
        and v["receipt_at_us"] >= v["event_at_us"]
        and v["authorization_id"] == v["identity"]["authorization_id"]))


class EventSchema:
    limit = EVENT_BYTES

    def __call__(self, value: Any) -> None:
        require(type(value) is dict)
        (AUTHORIZE_EVENT if value.get("phase") == "authorize" else TERMINAL_EVENT)(value)

    def parse(self, raw: bytes) -> dict[str, Any]:
        value = decode(raw, self.limit)
        self(value)
        assert isinstance(value, dict)
        return value

    def serialize(self, value: dict[str, Any]) -> bytes:
        self(value)
        return canonical(value)


EVENT = EventSchema()


def _page(v: Any) -> None:
    events = v["events"]
    require(events[0]["seq"] == v["first_seq"] and events[-1]["seq"] == v["last_seq"])
    require(int(v["last_seq"]) - int(v["first_seq"]) + 1 == len(events))
    require(all(int(event["seq"]) == int(v["first_seq"]) + index for index, event in enumerate(events)))
    for event in events:
        require(day_at(event["event_at_us"]) == v["day"])
        if event["receipt_locator"] is not None:
            require(event["receipt_locator"] == {"day": v["day"], "boot": v["boot"],
                "first_seq": v["first_seq"], "event_seq": event["seq"]})


def validate_completion(original: dict[str, Any], completion: dict[str, Any]) -> None:
    """Reconcile a supplied original terminal, including cross-page references.

    P3/P5 must supply the exact same-writer original; local shape alone is not
    resolution. The original prediction and observation timestamp never move.
    """
    EVENT(original)
    EVENT(completion)
    require(original["phase"] in ("settle", "refund") and completion["phase"] == "completion")
    require(completion["completion_of"] == original["seq"] < completion["seq"])
    require(completion["event_at_us"] >= original["event_at_us"])
    for key in ("authorization_day", "authorization_id", "identity", "dimensions", "eligibility",
                "proof_kind", "original_receipt", "certificate_sha256", "payload_sha256", "decision"):
        require(completion[key] == original[key])
    if original["booking"]["winner"] is not None:
        require(completion["booking"]["winner"] == original["booking"]["winner"])
    if completion["partition"] in ("A", "B"):
        require((original["phase"], completion["booking"]["outcome"], completion["booking"]["winner"]) in
                (("settle", "settled", "settle"), ("refund", "refunded", "refund")))
    if original["comparison"] == "mismatch":
        require(completion["partition"] == "C" and completion["comparison"] == "mismatch")


PAGE = versioned({"boot": identity, "day": day, "first_seq": sequence, "last_seq": sequence,
    "previous_page_sha256": nullable(digest), "events": array(EVENT, PAGE_EVENTS, 1)}, PAGE_BYTES, _page)
CERTIFICATE_PROJECTION = versioned({**COMMITTED_FIELDS, "iss": name, "aud": literal("router-shadow-exclusion"),
    "iat": integer, "exp": integer, "requested_route": name, "requested_streamed": boolean,
    "reason": REASONS, "facts_sha256": digest, "candidate_set_sha256": nullable(digest),
    "router_revision": revision, "original_receipt": LOCATOR, "async_eligible": literal(False)}, 4096, validate_identity)
CERTIFICATE_RECORD = versioned({"claims": CERTIFICATE_PROJECTION, "certificate_sha256": digest,
    "issuer_keyring_sha256": digest, "verified_at_us": integer,
    "verification_result": literal("verified"), "authorization_day": day}, 4096,
    lambda v: require(v["claims"]["iat"] <= v["verified_at_us"] // 1000000 < v["claims"]["exp"]
                      and v["claims"]["exp"] - v["claims"]["iat"] == 172800
                      and v["authorization_day"] == v["claims"]["original_receipt"]["day"]))


def _health(v: Any) -> None:
    require(v["observed_at_us"] <= v["publisher_heartbeat_us"])
    require(v["p50_age_us"] <= v["p95_age_us"] <= v["oldest_unresolved_age_us"])
    require(v["dead_count"] <= v["backlog_count"] == v["sample_count"])
    if v["sample_count"] == 0:
        require(all(v[k] == 0 for k in ("frozen_micro", "p50_age_us", "p95_age_us", "oldest_unresolved_age_us")))


HEALTH = versioned({"authority": literal("local"), "source": literal("shadow_observation"),
    "publisher_boot_id": identity, "publisher_region": name, "router_revision": revision,
    "complete": boolean, **{k: integer for k in "slot observed_at_us publisher_heartbeat_us sample_count backlog_count frozen_micro p50_age_us p95_age_us oldest_unresolved_age_us dead_count".split()}}, 4096, _health)
HEALTH_CLAIM = versioned({"authority": literal("local"), "slot": integer,
    "claim_at_us": integer, "owner_boot_id": identity}, 1024)


def health_usable(v: dict[str, Any], now_us: int) -> bool:
    HEALTH(v)
    integer(now_us)
    return (v["complete"] and v["sample_count"] <= 10000
            and v["observed_at_us"] <= v["publisher_heartbeat_us"] <= now_us
            and now_us - v["observed_at_us"] < 5000000)


CHECKPOINT_FIELDS = "checkpoint_attempts checkpoint_failures checkpoint_recoveries unknown_commits unreconciled_commits irrecoverable_losses".split()
COUNTS = "authorize_attempts authorize_fresh authorize_replay settle_attempts refund_attempts A B C proof_compared proof_certificate proof_observed requested_eligible requested_excluded requested_unknown".split()


def _partition(v: Any) -> None:
    require(v["authorize_fresh"] + v["authorize_replay"] == v["authorize_attempts"])
    require(v["A"] + v["B"] + v["C"] == v["settle_attempts"] + v["refund_attempts"])
    require(v["eligible_demand"] is None or v["A"] <= v["eligible_demand"] <= v["A"] + v["C"])


def _bucket(v: Any) -> None:
    _partition(v)
    if v["phase"] == "authorize":
        require(v["settle_attempts"] == v["refund_attempts"] == 0)
    else:
        require(v["authorize_attempts"] == 0)
        require(v["refund_attempts"] == 0 if v["phase"] == "settle" else v["settle_attempts"] == 0)


BUCKET = Schema({"phase": literal("authorize", "settle", "refund"), "dimensions": DIMENSIONS,
    **{k: integer for k in COUNTS}, "eligible_demand": nullable(integer)}, 2048, _bucket)
HISTOGRAM = Schema({"bounds_us": array(integer, 8, 8), "counts": array(integer, 9, 9)}, 512,
    lambda v: require(v["bounds_us"] == [100, 500, 1000, 2000, 5000, 10000, 50000, 200000]))
PUBLISHER_OPERATIONS = Schema({**{k: integer for k in "claim_attempts claims scans publications failures missed_slots".split()},
    "cycle_us": HISTOGRAM, "rpc_us": HISTOGRAM}, 2048)


def _counter(v: Any) -> None:
    _partition(v)
    require(v["checkpoint_seq"] >= 1)
    require(sum(len(v[k]) for k in ("exclusions", "rejections", "drops")) <= 128)
    for field in ("exclusions", "rejections", "drops", "terminal_counts"):
        keys = ("phase", "adapter", "route_type", "streamed") + (() if field == "terminal_counts" else ("reason",))
        require(len({canonical([row[k] for k in keys]) for row in v[field]}) == len(v[field]))
    require(not sum(b["mismatch"] for b in v["counts"]) or v["last_mismatch_at_us"] is not None)
    require(v["durable_event_hwm"] <= v["event_seq"])
    require(v["checkpoint_recoveries"] <= v["checkpoint_failures"] <= v["checkpoint_attempts"])
    require(v["unreconciled_commits"] <= v["unknown_commits"])
    require(len({canonical((b["phase"], b["dimensions"])) for b in v["buckets"]}) == len(v["buckets"]))
    for k in COUNTS:
        require(sum(b[k] for b in v["buckets"]) == v[k])
    for k in ("authorize_attempts", "authorize_fresh", "authorize_replay", "settle_attempts", "refund_attempts"):
        require(sum(b[k] for b in v["counts"]) == v[k])
    demands = [b["eligible_demand"] for b in v["buckets"]]
    require(v["eligible_demand"] is None if None in demands else v["eligible_demand"] == sum(demands))
    require(v["started_at_us"] <= v["flushed_at_us"])
    # Retained local v1 equations, independent of the v2 A/B/C bucket equations.
    for b in v["counts"]:
        require(b["observed_attempts"] == b["settle_attempts"] + b["refund_attempts"]
            == b["observed_eligible"] + b["observed_ineligible"] + b["observed_unknown"])
        require(b["authorize_attempts"] == b["authorize_fresh"] + b["authorize_replay"])
        require(b["evaluable"] == b["exact"] + b["explained"])
        require(b["envelope_present"] <= b["observed_attempts"] and b["header_absent"] <= b["authorize_attempts"])
    outcomes = sum(b[k] for b in v["counts"] for k in ("exact", "explained", "mismatch", "requires_review", "unevaluable"))
    require(outcomes <= v["comparison_attempts"] and v["samples_inserted"] <= v["comparison_attempts"])


LEGACY_COUNT_FIELDS = "authorize_attempts authorize_fresh authorize_replay header_absent requested_eligible snapshot_sent settle_attempts refund_attempts envelope_present observed_attempts observed_eligible observed_ineligible observed_unknown evaluable exact explained mismatch requires_review unevaluable".split()
LEGACY_PHASE_FIELDS = "envelope_present observed_attempts observed_eligible observed_ineligible observed_unknown evaluable exact explained mismatch requires_review unevaluable comparison_attempts comparison_dropped samples_inserted duplicate_samples conflicting_samples booking_pending booking_unknown".split()
COUNTER_DIMENSIONS = Schema({"adapter": literal("openai", "anthropic", "other", "unknown"),
    "route_type": literal("chat.completions", "responses", "other", "unknown"), "streamed": nullable(boolean)}, 256)
LEGACY_COUNT = Schema({**COUNTER_DIMENSIONS.fields, **{k: integer for k in LEGACY_COUNT_FIELDS}}, 2048)
LEGACY_PHASE_COUNT = Schema({**COUNTER_DIMENSIONS.fields, "phase": literal("settle", "refund"),
    **{k: integer for k in LEGACY_PHASE_FIELDS}}, 2048)
COUNT_DIAGNOSTIC = Schema({**COUNTER_DIMENSIONS.fields, "phase": literal("authorize", "settle", "refund", "worker"),
    "reason": COUNTER_REASON, "count": integer}, 512)
OBSERVER = Schema({k: integer for k in "workspace_reads health_reads read_failures missed_ticks late_installs max_consecutive_failures degraded_seconds prediction_yes prediction_no prediction_unknown".split()}, 2048)
FIXED_DIMENSIONS = tuple(itertools.product(("openai", "anthropic", "other", "unknown"),
    ("chat.completions", "responses", "other", "unknown"), (False, True, None)))


def _fixed_counts(value: Any) -> None:
    array(LEGACY_COUNT, 48, 48)(value)
    require([(v["adapter"], v["route_type"], v["streamed"]) for v in value] == list(FIXED_DIMENSIONS))


COUNTER = versioned({"day": day, "boot": identity, "router_revision": revision, "region": bounded_string(128),
    **{k: integer for k in COUNTS + CHECKPOINT_FIELDS}, "eligible_demand": nullable(integer),
    "event_seq": integer, "durable_event_hwm": integer, "checkpoint_seq": integer,
    "receipt_count": integer, "receipt_chain_sha256": nullable(digest), "loss_count": integer,
    "reset_count": integer, "diagnostics": mapping(name, integer, 128),
    "started_at_us": integer, "flushed_at_us": integer, "closed": boolean,
    "counts": _fixed_counts, "terminal_counts": array(LEGACY_PHASE_COUNT, 96),
    "exclusions": array(COUNT_DIAGNOSTIC, 128), "rejections": array(COUNT_DIAGNOSTIC, 128), "drops": array(COUNT_DIAGNOSTIC, 128),
    "dimension_overflow": integer, "counter_overflow": boolean,
    **{k: integer for k in "comparison_attempts comparison_dropped samples_inserted duplicate_samples conflicting_samples booking_pending booking_unknown".split()},
    "first_evidence_at_us": nullable(integer), "last_mismatch_at_us": nullable(integer), "first_gap_at_us": nullable(integer),
    "authorize_shadow_hist": array(integer, 9, 9), "evidence_write_hist": array(integer, 9, 9), "admission_observer": OBSERVER,
    "buckets": array(BUCKET, 144), "publisher": PUBLISHER_OPERATIONS}, 65536, _counter, retained_strings=True)
CLOSURE = versioned({"day": day, "boot": identity, "router_revision": revision, "region": name,
    "reason": literal("utc_boundary", "shutdown", "opt_out"), "interval_end_us": integer,
    "final_event_seq": integer, "final_checkpoint_seq": integer, "durable_event_hwm": integer,
    "receipt_chain_sha256": digest, "counter_sha256": digest, "retained_work": literal(0),
    "pending_comparisons": literal(0), "unresolved_commits": literal(0), "loss_count": integer,
    "observer_fold_through_us": integer, "previous_day_closure_sha256": nullable(digest)}, 4096,
    lambda v: require(v["durable_event_hwm"] == v["final_event_seq"]
                      and v["observer_fold_through_us"] >= v["interval_end_us"]))
SEALED_CLOSURE = versioned({"closure_sha256": digest, "physical_commit_ack_at_us": integer}, 4096)

# Stored diagnostic usage deliberately does not require successful normalization.
STORED_USAGE = Schema({k: integer for k in "uncached_input_tokens total_prompt_tokens output_tokens cache_read_tokens cache_creation_tokens reasoning_tokens".split()}, 1024)

# Preserve every v1 sample field, including arithmetic and nullable diagnostics.
SAMPLE_FIELDS: dict[str, Any] = {"authorization_id": identity, "authorization_day": day,
    "observed_at_us": integer, "authorize_at_us": integer, "booking_observed_at_us": nullable(integer),
    "workspace_fingerprint": nullable(digest), "adapter": literal("openai", "anthropic", "other", "unknown"),
    "route_type": literal("chat.completions", "responses", "other", "unknown"), "streamed": nullable(boolean),
    "model_id": nullable(name), "endpoint_id": nullable(name),
    **{k: nullable(digest) for k in ("snapshot_hash", "payload_hash", "rebuilt_snapshot_hash")},
    "raw_usage": nullable(RAW_USAGE), **{k: nullable(STORED_USAGE) for k in ("python_usage", "go_usage", "legacy_usage")},
    **{k: nullable(integer) for k in "frozen_micro python_micro go_micro booked_micro rebuilt_micro legacy_frozen_micro".split()},
    **{k: nullable(signed_integer) for k in "python_minus_go booked_minus_frozen rebuilt_minus_frozen booked_minus_rebuilt".split()},
    "classification": literal("hash", "identity", "normalization", "evaluator_disagreement", "requires_review", "unevaluable", "exact", "explained-by-catalog-change"),
    "reason_codes": array(SAMPLE_REASON, 8, unique=True),
    "eligibility": Schema({"requested": boolean, "observed": nullable(boolean), "exclusion": nullable(SAMPLE_REASON)}, 256),
    "booking": Schema({"attempted_kind": literal("settle", "refund"), "outcome": literal("settled", "refunded", "pending", "unknown"),
        "source": literal("finalized_authorization", "none"), "price_source": literal("catalog_at_authorize_time", "stage_d_document", "unknown")}, 512),
    "admission": Schema({"prediction": literal("yes", "no", "unknown"), "reason": literal("eligible", "ineligible_tier", "cap_exceeded", "drain_unhealthy", "cache_missing", "cache_stale", "cache_busy", "invalid_data"),
        **{k: nullable(integer) for k in "tier pending_micro cap_micro workspace_age_us health_age_us health_p95_us".split()}}, 1024),
    "timing": Schema({**{k: nullable(integer) for k in "authorize_shadow_us handoff_prepare_us router_settle_us booking_confirm_us comparator_us".split()}, "evidence_write_us": literal(None)}, 512),
    "deployment": Schema({"region": ascii_text(128, 1), "instance": ascii_text(128, 1), "router_revision": revision,
        "go_revision": nullable(revision), "python_evaluator": literal("billing-v1"), "go_evaluator": literal("billing-v1")}, 8192, retained_strings=True),
    "provenance": Schema({"binding_verified": boolean, "raw_matches_body": boolean,
        "rebuild_matches_booking_view": boolean, "snapshot_transport": literal("full", "hash_only", "unknown"),
        "s0_reconstruction": literal("not_needed", "verified", "failed", "not_attempted"),
        "legacy_oracle": literal("stage_d_candidate_v1", "unavailable"), "fixture_sha256": literal(BINDING_FIXTURE_SHA256)}, 512),
    "proof_kind": literal("compared", "observed_binding"), "original_terminal_receipt": RECEIPT_REFERENCE,
    "health_source": literal("shadow_observation"), "health_slot": nullable(integer), "prediction_basis": literal("counterfactual_backlog_v2"),
}


def _sample(v: Any) -> None:
    require(day_at(v["authorize_at_us"]) == v["authorization_day"])
    require(v["reason_codes"] == sorted(v["reason_codes"]))
    if "winner_polarity" in v["reason_codes"]:
        require(v["booked_minus_frozen"] is None and v["booked_minus_rebuilt"] is None
                and v["classification"] not in ("exact", "explained-by-catalog-change"))
    if v["eligibility"]["observed"] is False:
        require(v["admission"]["prediction"] != "yes")
    if v["proof_kind"] == "observed_binding":
        require(v["eligibility"]["observed"] is False and v["eligibility"]["exclusion"] is not None
                and v["provenance"]["binding_verified"] and v["admission"]["prediction"] != "yes")
        # Retained admission is diagnostic only: only eligible terminal EVENT
        # predictions enter Y/N/U or seed the clock, never observed exclusions.
    for delta, left, right in (("python_minus_go", "python_micro", "go_micro"),
        ("booked_minus_frozen", "booked_micro", "frozen_micro"),
        ("rebuilt_minus_frozen", "rebuilt_micro", "frozen_micro"),
        ("booked_minus_rebuilt", "booked_micro", "rebuilt_micro")):
        if v[delta] is not None:
            require(v[left] is not None and v[right] is not None and v[delta] == v[left] - v[right])
    if v["classification"] in ("exact", "explained-by-catalog-change"):
        require(v["proof_kind"] == "compared" and v["provenance"]["binding_verified"]
                and v["provenance"]["raw_matches_body"])
        require(v["python_micro"] is not None and v["booked_micro"] is not None
                and v["python_micro"] == v["go_micro"] == v["legacy_frozen_micro"]
                and v["python_usage"] is not None and v["python_usage"] == v["go_usage"] == v["legacy_usage"])
        require((v["booking"]["attempted_kind"], v["booking"]["outcome"]) in (("settle", "settled"), ("refund", "refunded")))
        if v["classification"] == "exact":
            require(v["booked_micro"] == v["python_micro"])
        else:
            require(v["booked_micro"] == v["rebuilt_micro"] != v["python_micro"]
                    and v["snapshot_hash"] != v["rebuilt_snapshot_hash"]
                    and v["provenance"]["rebuild_matches_booking_view"] and v["booking"]["attempted_kind"] == "settle")


SAMPLE = versioned(SAMPLE_FIELDS, 8192, _sample, retained_strings=True)
INTERVAL = Schema({"start_us": integer, "end_us": integer}, 128,
    lambda v: require(v["start_us"] <= v["end_us"]))
FLAG_INTERVAL = Schema({"interval": INTERVAL, "admission": literal(False), "protection": literal(False),
    "negotiation": literal(False), "fast_drain": literal(False)}, 256)
ROSTER = mapping(identity, Schema({"revision": revision, "region": name, "lifetime": INTERVAL}, 256), WRITERS)
GAP_INTERVAL = Schema({"interval": INTERVAL, "reason": name}, 256)
MANIFEST = versioned({"day": day, "config_sha256": digest, "fixture_sha256": digest,
    "router_writers": ROSTER, "enclave_writers": ROSTER,
    "revisions": Schema({"router": array(revision, REVISIONS_PER_ROLE, unique=True), "enclave": array(revision, REVISIONS_PER_ROLE, unique=True)}, 8192),
    **{k: digest for k in "counter_root closure_root receipts_root prerequisite_ledger_root independent_inventory_sha256".split()},
    "first_evidence_at_us": nullable(integer), "proof_manifest_sha256": nullable(digest),
    "disabled_flag_intervals": array(FLAG_INTERVAL, GAPS), "complete": boolean,
    "gaps": array(GAP_INTERVAL, GAPS)}, MANIFEST_BYTES,
    lambda v: require(len(v["router_writers"]) + len(v["enclave_writers"]) <= WRITERS
                      and (not v["complete"] or not v["gaps"])))
CAP = versioned({"day": day, "terminal_reserved": integer, "event_reserved": integer}, 4096,
    lambda v: require(v["terminal_reserved"] <= TERMINAL_CAP and v["event_reserved"] <= EVENT_CAP
        and v["terminal_reserved"] % RESERVATION_BLOCK == v["event_reserved"] % RESERVATION_BLOCK == 0))
RETENTION = versioned({"retired_through": day, "kinds": array(literal(*KINDS), len(KINDS), len(KINDS), unique=True)}, 4096)
ARTIFACT = Schema({"sha256": digest, "completed_at_us": integer}, 256)
PREREQUISITE_LEDGER = versioned({"role": literal("router", "enclave"), "boot": identity,
    "revision": revision, "region": name, "policy_sha256": digest, "fixture_sha256": digest,
    "config_sha256": digest, "boot_at_us": integer, "collection_interval": INTERVAL,
    "traffic_ready_at_us": integer, **{k: ARTIFACT for k in ("transport", "size", "regional_load", "boot_transport", "observer_freshness", "marker_keys")},
    "previous_ledger_sha256": nullable(digest)}, MANIFEST_BYTES,
    lambda v: require(v["boot_at_us"] <= v["traffic_ready_at_us"]
        and all(v[k]["completed_at_us"] <= v["traffic_ready_at_us"] for k in ("transport", "size", "regional_load", "boot_transport", "observer_freshness", "marker_keys"))))
PROOF_MANIFEST = versioned({"source_shas": Schema({"router": revision, "enclave": revision}, 128),
    **{k: digest for k in "evaluator_fixture_sha256 binding_fixture_sha256 wire_fixture_sha256 ledger_root shutdown_inventory_sha256 cohort_seed_policy_sha256 pre_budget_sha256 post_budget_sha256 publisher_slot_trace_sha256 external_proofs_sha256 resolution_dag_sha256".split()},
    "rosters_by_day": mapping(day, Schema({"router": ROSTER, "enclave": ROSTER}, MANIFEST_BYTES), 32),
    "instance_regions_by_day": mapping(day, mapping(identity, name, WRITERS), 32),
    "attachments": array(Schema({"sha256": digest, "bytes": integer}, 128,
        lambda v: require(v["bytes"] <= MANIFEST_BYTES)), LIVE_EXPORT_KEYS, unique=True),
    "explicit_keys": array(name, LIVE_EXPORT_KEYS, unique=True), "complete": boolean}, MANIFEST_BYTES)
GATE_OUTPUT = versioned({"gate": literal("transport", "fleet-budgets-pre", "fleet-budgets", "shadow-serving", "pre-flip", "post-flip"),
    "verdict": literal("PASS", "BLOCKED"), "completed_at_us": integer, "proof_manifest_sha256": digest,
    "prerequisite_ledger_root": digest, "gaps": array(name, GAPS), "clock_start_us": nullable(integer),
    "elapsed_seconds": integer, "coverage_sha256": digest}, MANIFEST_BYTES,
    lambda v: require(v["verdict"] != "PASS" or not v["gaps"]))


def validate_row(kind: str, key: str, body: bytes) -> dict[str, Any]:
    schemas = dict(zip(KINDS, (SAMPLE, CERTIFICATE_RECORD, PAGE, COUNTER, CLOSURE, MANIFEST, HEALTH, HEALTH_CLAIM), strict=True))
    require(kind in schemas)
    schema = schemas[kind]
    if kind == KINDS[5]:
        schema = RETENTION if key == "retention-v2" else CAP if key.endswith("/cap-v2") else MANIFEST
    value = schema.parse(body)
    if schema in (SAMPLE, CERTIFICATE_RECORD):
        auth = value["authorization_id"] if schema is SAMPLE else value["claims"]["authorization_id"]
        expected = value["authorization_day"] + "/" + auth
    elif schema is PAGE:
        expected = "/".join(value[k] for k in ("day", "boot", "first_seq"))
    elif schema in (COUNTER, CLOSURE):
        expected = value["day"] + "/" + value["boot"]
    elif schema in (MANIFEST, CAP):
        expected = value["day"] + ("/cap-v2" if schema is CAP else "/manifest-v2")
    else:
        expected = "retention-v2" if schema is RETENTION else "local/fleet" if schema is HEALTH else "local/publish"
    require(key == expected, "evidence_identity_gap")
    return value


def transaction_bodies(pages: list[bytes], counter: bytes, closure: bytes | None) -> int:
    require(len(pages) <= MAX_PAGES, "page_count")
    parsed = [PAGE.parse(p) for p in pages]
    require(len({p["day"] for p in parsed}) <= MAX_DAY_BUCKETS, "day_buckets")
    require(len({(p["day"], p["boot"], p["first_seq"]) for p in parsed}) == len(parsed), "page_identity")
    counter_value = COUNTER.parse(counter)
    require(all(p["boot"] == counter_value["boot"] for p in parsed), "page_identity")
    if closure is not None:
        closure_value = CLOSURE.parse(closure)
        require((closure_value["boot"], closure_value["day"]) == (counter_value["boot"], counter_value["day"]), "closure_identity")
    total = sum(map(len, pages)) + len(counter) + (len(closure) if closure else 0)
    require(total <= TRANSACTION_BODY_BYTES, "transaction_size")
    return total


def committed_identity(claims: dict[str, Any]) -> dict[str, Any]:
    return {k: claims[k] for k in IDENTITY_FIELDS if k not in COMMITTED_IDENTITIES} | {
        k + "_sha256": sha256(claims[k]) for k in COMMITTED_IDENTITIES}
