"""Compared-only receipt construction over the pinned contracts.

All inputs are detached observations supplied by the caller. This module does
not discover facts, look up authorizations, read a catalog or issue exclusions.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from trusted_router.async_settle_shadow_binding import verify_binding
from trusted_router.async_settle_shadow_compare import Comparison, Context, compare
from trusted_router.async_settle_shadow_evidence import retry_classification
from trusted_router.async_settle_shadow_journal import Reservation
from trusted_router.async_settle_shadow_v2_a1_reconciliation import resolve_event
from trusted_router.async_settle_shadow_v2_contract import (
    CANDIDATE_BYTES,
    COHORT_EXCLUSIONS,
    LOCAL_BYTES,
    Decision,
    RequestedFacts,
    decide,
    fit_decision,
    parse_header,
)
from trusted_router.async_settle_shadow_v2_evidence import (
    BUCKET,
    COUNTS,
    EVENT,
    PREDICTION,
    SAMPLE,
)
from trusted_router.async_settle_shadow_v2_projection import sample_projection, wire_projection
from trusted_router.async_settle_shadow_v2_schema import POLICY, day_at, decode, require
from trusted_router.detached_jws import TrustedKey, b64encode, canonical


@dataclass(frozen=True)
class PreparedAuthorization:
    decision: Decision
    event: dict[str, Any]

    @property
    def body(self) -> bytes:
        """Standalone evidence consumers validate here; requests use Port.finalize."""
        return EVENT.serialize(self.event)


# The authorize envelope is constructed here with these four literal values.
# Size only its variable payload; JSON object keys, separators and fixed values
# never need a request-time walk. P3 still validates/encodes the complete EVENT.
_AUTHORIZE_FIXED = dict(v=2, policy_version=POLICY, phase="authorize", partition="C")
_AUTHORIZE_VARIABLE = (
    "seq", "event_at_us", "receipt_at_us", "authorization_day", "authorization_id",
    "identity", "router_revision", "dimensions", "decision", "facts", "candidates",
    "receipt_locator", "eligibility", "diagnostic",
)
_AUTHORIZE_FRAMING = (
    len(canonical({**_AUTHORIZE_FIXED, **dict.fromkeys(_AUTHORIZE_VARIABLE)}))
    - len(canonical([None] * len(_AUTHORIZE_VARIABLE)))
)
_ESCAPED_JSON = r'[\x00-\x1f"\\]|[^\x00-\x7f]'


def event_size(value: Any) -> int:
    """Exact size of one variable part, with no validation or Python tree walk."""
    if isinstance(value, str) and re.search(_ESCAPED_JSON, value) is None:
        return len(value) + 2
    return len(canonical(value))


def authorization_size(event: dict[str, Any]) -> int:
    """Fixed envelope framing plus one native encoding of variable values only.

    This is a size calculation, not a schema boundary. Escapes, UTF-8, nulls,
    integer widths, candidate counts and diagnostic backing are counted exactly.
    The fixed members are literal values at the sole construction site below.
    """
    return _AUTHORIZE_FRAMING + event_size([event[k] for k in _AUTHORIZE_VARIABLE])


def decision_reference(decision: Decision) -> dict[str, Any]:
    # Diagnostic reasons belong in diagnostic, not the closed exclusion enum.
    return dict(reason=decision.reason if decision.reason in COHORT_EXCLUSIONS else None,
                facts_sha256=decision.facts_sha256,
                candidate_set_sha256=decision.candidate_set_sha256)


def mapped_decision(f: dict[str, Any], reason: str | None, projection: list[dict[str, Any]] | None,
                    complete: bool, supported: bool) -> Decision:
    """Contract decision algebra over handler-owned values, validated by EVENT.

    This compared-only projection cannot issue an exclusion. The contract's
    facts/candidate validators still run, once, inside P3's native EVENT walk.
    The standalone input path below retains decide's external-input boundary.
    """
    unresolved = Decision("unknown", "missing_context", False, None, None)
    if reason is not None and reason not in COHORT_EXCLUSIONS:
        return Decision("unknown", reason, False, None, None)
    fixed = {
        "untyped": f["settle_origin"] != "typed", "non_credits": f["usage_class"] != "Credits",
        "settlement_authority": f["billing_authority"] != "local",
        "unsupported_route": f["route"] not in ("chat.completions", "responses"),
        "service_tier": f["service_tier"] not in (None, "default"),
        "app_markup": f["app_markup"] > 0, "custom_markup": f["custom_markup"] > 0,
        "receipt_fee": f["receipt_requested"] or f["receipt_fee"] > 0,
        "partner": f["commercial_program"] == "partner", **f["features"],
    }
    fixed["unsupported_adapter"] = fixed["partner"]
    def digest(value: Any) -> str:
        return hashlib.sha256(canonical(value)).hexdigest()
    if reason is not None and fixed.get(reason, False):
        return Decision("excluded", reason, False, digest({"facts": f, "reason": reason}), None)
    if projection is None or not complete:
        return unresolved
    require(len({c["endpoint_id"] for c in projection}) == len(projection), "missing_context")
    if reason in ("partner", "unsupported_adapter"):
        qualifies = all(c["adapter"] == "other" for c in projection)
    elif reason in ("tool_cost", "search_cost", "image_cost", "video_cost", "request_fee", "private_tier_basis"):
        qualifies = all(reason in c["features"] for c in projection)
    else:
        qualifies = False
    if qualifies:
        return Decision("excluded", reason, True, digest({"facts": f, "reason": reason}), digest(projection))
    if reason == "unknown_parameters" or any(fixed.values()):
        return unresolved
    if any(c["adapter"] in ("openai", "anthropic") for c in projection) and supported:
        return Decision("eligible", None, False, None, None)
    return unresolved


def prepare_authorization(reservation: Reservation, *, identity: dict[str, Any],
                          revision: str, dimensions: dict[str, Any],
                          facts: RequestedFacts | None, candidates: bytes | None,
                          complete_candidates: bool, complete_supported_context: bool,
                          reason: str | None, server_facts: dict[str, Any] | None = None,
                          server_candidates: list[dict[str, Any]] | None = None,
                          native_request: bool = False) -> PreparedAuthorization:
    """Fit the complete event before handing its immutable bytes to issuance.

    P4a preserves a qualifying reason but does not certify an exclusion. Missing
    facts and unsupported representations remain unknown; no defaults enter
    RequestedFacts. An oversize event is replaced by the contract diagnostic.
    """
    decision = Decision("unknown", reason if reason in {"header_absent", "replay"} else "missing_context", False, None, None)
    projection = None
    if server_facts is not None:
        decision = mapped_decision(server_facts, reason, server_candidates,
                                   complete_candidates, complete_supported_context)
        projection = server_candidates if complete_candidates else None
    elif facts is not None:
        try:
            decision = decide(facts, reason, candidates, complete_candidates=complete_candidates,
                              complete_supported_context=complete_supported_context)
            if candidates is not None and complete_candidates:
                # decide owns candidate validation and its input bound.
                projection = decode(candidates, CANDIDATE_BYTES)
        except (ValueError, TypeError, KeyError):
            decision = Decision("unknown", "missing_context", False, None, None)
            projection = None
    event = dict(v=2, policy_version=POLICY, seq=f"{reservation.event_seq:020}",
        event_at_us=reservation.observed_us, receipt_at_us=reservation.observed_us,
        phase="authorize", authorization_day=reservation.day,
        authorization_id=identity["authorization_id"], identity=identity,
        router_revision=revision, dimensions=dimensions, decision=decision_reference(decision),
        facts=server_facts if server_facts is not None else facts.object() if facts is not None else None, candidates=projection,
        receipt_locator=reservation.coordinates, partition="C",
        eligibility="eligible" if decision.eligibility == "eligible" else "unknown",
        diagnostic=(None if decision.eligibility == "eligible" else
                    dict(reason=decision.reason or "missing_context", required_bytes=0, candidate_count=0)))
    required = authorization_size(event)
    count = len(projection) if projection is not None else 0
    fitted = (Decision("unknown", "candidate_receipt_size" if decision.candidate_dependent else "receipt_locator_size",
                       False, None, None, required, count) if required > EVENT.limit else decision)
    if not native_request and server_facts is None:
        fitted = fit_decision(decision, event)
    if decision.candidate_dependent and not projection:
        fitted = Decision("unknown", "original_receipt_backing_gap", False, None, None)
    if fitted != decision:
        event.update(facts=None, candidates=None, decision=decision_reference(fitted), eligibility="unknown")
    if fitted.eligibility != "eligible":
        event["diagnostic"] = dict(reason=fitted.reason or "missing_context",
            required_bytes=fitted.required_bytes or 0, candidate_count=fitted.candidate_count or 0)
    # The diagnostic itself must fit; failure is a lost slot, never truncation.
    return PreparedAuthorization(fitted, event)


def unresolved_authorization(prepared: PreparedAuthorization, reason: str) -> bytes:
    event = dict(prepared.event)
    event.update(eligibility="unknown", facts=None, candidates=None,
                 diagnostic=dict(reason=reason, required_bytes=0, candidate_count=0))
    return EVENT.serialize(event)


def replay_event(reservation: Reservation, *, original_page: dict[str, Any],
                 original_locator: dict[str, Any], revision: str) -> bytes:
    original = resolve_event(original_page, original_locator)
    return replay_decision(reservation, original=original, original_locator=original_locator, revision=revision)


def replay_decision(reservation: Reservation, *, original: dict[str, Any],
                    original_locator: dict[str, Any], revision: str) -> bytes:
    """Link an immutable finalized decision retained by this issuing worker."""
    EVENT(original)
    return EVENT.serialize(replay_object(reservation, original=original,
        original_locator=original_locator, revision=revision))


def replay_object(reservation: Reservation, *, original: dict[str, Any],
                  original_locator: dict[str, Any], revision: str) -> dict[str, Any]:
    """Retained cache bytes were validated by their original finalize."""
    require(original["phase"] == "authorize", "authorization_link_gap")
    require(original["event_at_us"] <= reservation.observed_us, "authorization_link_gap")
    event = {**original, "seq": f"{reservation.event_seq:020}",
        "phase": "replay", "event_at_us": reservation.observed_us,
        "receipt_at_us": reservation.observed_us, "router_revision": revision,
        "receipt_locator": None, "facts": None, "candidates": None,
        "original_receipt": None, "certificate_sha256": None, "payload_sha256": None,
        "proof_kind": "none", "comparison": None, "prediction": None,
        "booking": dict(outcome="not_attempted", reference=None, winner=None),
        "retry_original": {k: original_locator[k] for k in ("day", "boot", "event_seq")},
        "completion_of": None}
    return event


def observed_binding_payload_reference() -> NoReturn:
    """The retained observed-exclusion envelope has no terminal payload hash.

    EVENT requires that hash for B/observed_binding. A digest recipe for this
    envelope is not in the pinned contract; inventing one would change retries.
    """
    raise ValueError("original_receipt_backing_gap")


@dataclass(frozen=True)
class VerifiedTerminal:
    partition: str
    eligibility: str
    proof_kind: str
    reason: str | None
    comparison: Comparison | None = None
    authorization_day: str | None = None
    authorization_at_us: int | None = None


def verify_terminal(headers: tuple[str, ...], context: Context, keys: Sequence[TrustedKey],
                    *, local_bytes: int) -> VerifiedTerminal:
    """Ruling 2: the unchanged signed binding proves the authorize decision."""
    unknown = VerifiedTerminal("C", "unknown", "none", "header_absent")
    if not headers:
        return unknown
    try:
        value = parse_header(headers)
        if value["variant"] == "excluded":
            return VerifiedTerminal("C", "unknown", "none", "exclusion_proof_gap")
        require(type(local_bytes) is int and 0 <= local_bytes <= LOCAL_BYTES, "snapshot_size")
        compared = compare((b64encode(canonical(wire_projection(value))),), context, keys)
        issued_us = (verify_binding(value["billing_shadow_binding"], keys, context.received_at).iat*1000000
                     if compared.binding_verified else None)
        proof_day = day_at(issued_us) if issued_us is not None else None
        eligible = "eligible" if compared.binding_verified and compared.verified_streamed is not None else "unknown"
        if compared.observed_eligible is False:
            # The retained comparator establishes exclusion only after binding,
            # snapshot, stream and raw/body agreement; identity failures remain unknown.
            eligible = ("excluded" if compared.binding_verified and compared.raw_matches_body
                        and not compared.reasons.intersection({"identity", "usage_estimated"}) else "unknown")
        unknown = VerifiedTerminal("C", eligible, "compared" if compared.payload_hash else "none",
                                   "eligible_coverage_gap", compared, proof_day, issued_us)
        if not (compared.binding_verified and compared.raw_matches_body):
            return unknown
        if not (context.booking.confirmed and context.booking.amount is not None
                and context.booking.kind == context.attempted_kind):
            return VerifiedTerminal("C", eligible, unknown.proof_kind, "booking_unknown", compared, proof_day, issued_us)
        if compared.observed_eligible is False:
            observed_binding_payload_reference()
        if (compared.payload_hash is not None and compared.observed_eligible is True
                and compared.classification in ("exact", "explained-by-catalog-change")):
            return VerifiedTerminal("A", "eligible", "compared", None, compared, proof_day, issued_us)
        return unknown
    except (ValueError, KeyError, TypeError) as error:
        return VerifiedTerminal("C", unknown.eligibility, unknown.proof_kind,
                                str(error) if isinstance(error, ValueError) else "missing_context",
                                unknown.comparison, unknown.authorization_day, unknown.authorization_at_us)


def terminal_receipt(reservation: Reservation, *, original: dict[str, Any], revision: str,
                     phase: str, dimensions: dict[str, Any], result: VerifiedTerminal,
                     booking: dict[str, Any], prediction: dict[str, Any] | None,
                     retry_original: dict[str, Any] | None = None) -> bytes:
    """Construct one attempt from its resolved authorize facts and worker result.

    Predictions are captured by P3 at receipt time, before any booking/sample
    persistence. Booking references are supplied by the booking evidence owner.
    """
    EVENT(original)
    require(original["phase"] == "authorize", "authorization_link_gap")
    require(phase in ("settle", "refund"), "evidence_schema_gap")
    compared = result.comparison
    classification = None
    if compared is not None:
        classification = (compared.classification if compared.classification in
            ("exact", "explained-by-catalog-change", "unevaluable") else "mismatch")
    event = dict(v=2, policy_version=POLICY, seq=f"{reservation.event_seq:020}",
        event_at_us=reservation.observed_us, receipt_at_us=reservation.observed_us,
        phase=phase, authorization_day=original["authorization_day"],
        authorization_id=original["authorization_id"], identity=original["identity"],
        router_revision=revision, dimensions=dimensions, decision=original["decision"],
        facts=None, candidates=None, receipt_locator=None, original_receipt=None,
        certificate_sha256=None, payload_sha256=compared.payload_hash if compared else None,
        proof_kind=result.proof_kind, partition=result.partition, eligibility=result.eligibility,
        comparison=classification, booking=booking, prediction=prediction,
        retry_original=retry_original, completion_of=None,
        diagnostic=(dict(reason=result.reason, required_bytes=0, candidate_count=0)
                    if result.reason is not None else None))
    return EVENT.serialize(event)


def receipt_bucket(event: dict[str, Any]) -> dict[str, Any]:
    """One attempt delta; completion events never create a second denominator."""
    EVENT(event)
    phase = event["phase"]
    require(phase != "completion", "retry_original_gap")
    authorize = phase in ("authorize", "replay")
    bucket = dict(phase="authorize" if authorize else phase, dimensions=event["dimensions"],
                  **dict.fromkeys(COUNTS, 0), eligible_demand=0)
    if authorize:
        bucket["authorize_attempts"] = 1
        bucket["authorize_fresh" if phase == "authorize" else "authorize_replay"] = 1
        bucket["requested_" + event["eligibility"]] = 1
    else:
        bucket[phase + "_attempts"] = 1
        bucket[event["partition"]] = 1
        bucket["eligible_demand"] = (None if event["eligibility"] == "unknown"
                                     else int(event["eligibility"] == "eligible"))
        proof = {"compared": "proof_compared", "observed_binding": "proof_observed"}.get(event["proof_kind"])
        if proof is not None:
            bucket[proof] = 1
    BUCKET(bucket)
    return bucket


@dataclass(frozen=True)
class Demand:
    eligible: int | None
    yes: int
    no: int
    unknown: int
    gaps: tuple[str, ...]


def receipt_demand(events: Sequence[dict[str, Any]]) -> Demand:
    """Eligible C failures still demand a prediction; unknown is never zero.

    Input is one reconciled writer/day/phase/dimension bucket, with one final
    interpretation per attempt. Caller owns page coverage and completion joins.
    """
    eligible: int | None = 0
    counts = dict(yes=0, no=0, unknown=0)
    gaps: set[str] = set()
    for event in events:
        EVENT(event)
        if event["phase"] not in ("settle", "refund"):
            continue
        if event["partition"] == "C":
            gaps.add("terminal_coverage_gap")
        if event["eligibility"] == "unknown":
            eligible = None
            gaps.add("eligible_coverage_gap")
        elif event["eligibility"] == "eligible":
            if eligible is not None:
                eligible += 1
            PREDICTION(event["prediction"])
            counts[event["prediction"]["prediction"]] += 1
    if eligible is not None:
        if sum(counts.values()) != eligible or eligible > 0 and counts["yes"] + counts["no"] == 0:
            gaps.add("prediction_demand_gap")
        if 100 * counts["unknown"] > 2 * eligible:
            gaps.add("prediction_unknown_gap")
    return Demand(eligible, counts["yes"], counts["no"], counts["unknown"], tuple(sorted(gaps)))


def sample_retry(original: dict[str, Any], retry: dict[str, Any]) -> str:
    """Reuse v1 logical retry rules; never replace the first durable sample."""
    SAMPLE(original)
    SAMPLE(retry)
    require((original["authorization_day"], original["authorization_id"])
            == (retry["authorization_day"], retry["authorization_id"]), "evidence_identity_gap")
    return retry_classification(sample_projection(original), sample_projection(retry))
