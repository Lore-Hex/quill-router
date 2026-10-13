"""Compared-only receipt construction over the pinned contracts.

All inputs are detached observations supplied by the caller. This module does
not discover facts, look up authorizations, read a catalog or issue exclusions.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from trusted_router.async_settle_shadow_compare import Comparison, Context, compare
from trusted_router.async_settle_shadow_evidence import retry_classification
from trusted_router.async_settle_shadow_journal import Reservation
from trusted_router.async_settle_shadow_v2_a1_reconciliation import resolve_event
from trusted_router.async_settle_shadow_v2_contract import (
    CANDIDATE_BYTES,
    CANDIDATE_SET,
    COHORT_EXCLUSIONS,
    LOCAL_BYTES,
    Decision,
    RequestedFacts,
    decide,
    fit_decision,
    generation_id,
    parse_header,
)
from trusted_router.async_settle_shadow_v2_evidence import (
    BUCKET,
    COUNTS,
    EVENT,
    PREDICTION,
    SAMPLE,
    committed_identity,
)
from trusted_router.async_settle_shadow_v2_projection import sample_projection, wire_projection
from trusted_router.async_settle_shadow_v2_schema import POLICY, decode, require
from trusted_router.detached_jws import TrustedKey, b64encode, canonical


@dataclass(frozen=True)
class PreparedAuthorization:
    decision: Decision
    body: bytes


def decision_reference(decision: Decision) -> dict[str, Any]:
    # Diagnostic reasons belong in diagnostic, not the closed exclusion enum.
    return dict(reason=decision.reason if decision.reason in COHORT_EXCLUSIONS else None,
                facts_sha256=decision.facts_sha256,
                candidate_set_sha256=decision.candidate_set_sha256)


def prepare_authorization(reservation: Reservation, *, identity: dict[str, Any],
                          revision: str, dimensions: dict[str, Any],
                          facts: RequestedFacts | None, candidates: bytes | None,
                          complete_candidates: bool, complete_supported_context: bool,
                          reason: str | None) -> PreparedAuthorization:
    """Fit the complete event before handing its immutable bytes to issuance.

    P4a preserves a qualifying reason but does not certify an exclusion. Missing
    facts and unsupported representations remain unknown; no defaults enter
    RequestedFacts. An oversize event is replaced by the contract diagnostic.
    """
    decision = Decision("unknown", "missing_context", False, None, None)
    projection = None
    if facts is not None:
        try:
            decision = decide(facts, reason, candidates, complete_candidates=complete_candidates,
                              complete_supported_context=complete_supported_context)
            if candidates is not None and complete_candidates:
                # decide owns candidate validation and its input bound.
                projection = decode(candidates, CANDIDATE_BYTES)
                CANDIDATE_SET(projection)
        except (ValueError, TypeError, KeyError):
            decision = Decision("unknown", "missing_context", False, None, None)
            projection = None
    event = dict(v=2, policy_version=POLICY, seq=f"{reservation.event_seq:020}",
        event_at_us=reservation.observed_us, receipt_at_us=reservation.observed_us,
        phase="authorize", authorization_day=reservation.day,
        authorization_id=identity["authorization_id"], identity=identity,
        router_revision=revision, dimensions=dimensions, decision=decision_reference(decision),
        facts=facts.object() if facts is not None else None, candidates=projection,
        receipt_locator=reservation.coordinates, partition="C",
        eligibility="eligible" if decision.eligibility == "eligible" else "unknown",
        diagnostic=(None if decision.eligibility == "eligible" else
                    dict(reason=decision.reason or "missing_context", required_bytes=0, candidate_count=0)))
    fitted = fit_decision(decision, event)
    if fitted != decision:
        event.update(facts=None, candidates=None, decision=decision_reference(fitted), eligibility="unknown")
    if fitted.eligibility != "eligible":
        event["diagnostic"] = dict(reason=fitted.reason or "missing_context",
            required_bytes=fitted.required_bytes or 0, candidate_count=fitted.candidate_count or 0)
    # The diagnostic itself must fit; failure is a lost slot, never truncation.
    return PreparedAuthorization(fitted, EVENT.serialize(event))


def unresolved_authorization(prepared: PreparedAuthorization, reason: str) -> bytes:
    event = EVENT.parse(prepared.body)
    event.update(eligibility="unknown", facts=None, candidates=None,
                 diagnostic=dict(reason=reason, required_bytes=0, candidate_count=0))
    return EVENT.serialize(event)


def replay_event(reservation: Reservation, *, original_page: dict[str, Any],
                 original_locator: dict[str, Any], revision: str) -> bytes:
    original = resolve_event(original_page, original_locator)
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
    return EVENT.serialize(event)


def compared_authorization_locator() -> NoReturn:
    """Missing contract seam: resolve a compared binding to its original page.

    The unchanged binding has no receipt coordinate. No complete-key discovery
    function or bounded cross-boot index is supplied by P1-A1/P3. A caller with
    no independently resolved original page must keep the attempt unresolved.
    """
    raise ValueError("authorization_link_gap")


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


def verify_terminal(headers: tuple[str, ...], context: Context, keys: Sequence[TrustedKey],
                    *, local_bytes: int, original_page: dict[str, Any] | None,
                    original_locator: dict[str, Any] | None) -> VerifiedTerminal:
    """Worker-only verification; v1 owns binding, usage, arithmetic and booking.

    Supplied pages must already be independently established durable by the
    shell. Parsing a page here is deliberately not a storage acknowledgement.
    """
    unknown = VerifiedTerminal("C", "unknown", "none", "missing_context")
    original = None
    link_gap = None
    try:
        original = eligible_original(context, original_page, original_locator)
        unknown = VerifiedTerminal("C", "eligible", "none", "eligible_coverage_gap")
    except (ValueError, KeyError, TypeError) as error:
        link_gap = str(error) if isinstance(error, ValueError) else "missing_context"
    try:
        value = parse_header(headers)
        if value["variant"] == "excluded":
            return VerifiedTerminal("C", unknown.eligibility, "none", "exclusion_proof_gap")
        # Excluded never copies pricing. Compared cannot evade the local cap.
        require(type(local_bytes) is int and 0 <= local_bytes <= LOCAL_BYTES, "snapshot_size")
        compared = compare((b64encode(canonical(wire_projection(value))),), context, keys)
        if link_gap is not None:
            return VerifiedTerminal("C", "unknown", "compared", link_gap, compared)
        assert original is not None
        unknown = VerifiedTerminal("C", "eligible", "compared", "eligible_coverage_gap", compared)
        if not (compared.binding_verified and compared.raw_matches_body):
            return unknown
        require(compared.verified_streamed is original["facts"]["streamed"], "authorization_link_gap")
        require(context.body.route_type == original["facts"]["route"], "authorization_link_gap")
        if not (context.booking.confirmed and context.booking.amount is not None
                and context.booking.kind == context.attempted_kind):
            return VerifiedTerminal("C", "eligible", "compared", "booking_unknown", compared)
        if compared.observed_eligible is False and compared.classification == "unevaluable":
            if compared.payload_hash is None:
                observed_binding_payload_reference()
            return VerifiedTerminal("B", "excluded", "observed_binding", None, compared)
        if compared.payload_hash is None:
            return unknown
        if compared.observed_eligible is True and compared.classification in ("exact", "explained-by-catalog-change"):
            return VerifiedTerminal("A", "eligible", "compared", None, compared)
        return unknown
    except (ValueError, KeyError, TypeError) as error:
        return VerifiedTerminal("C", unknown.eligibility, unknown.proof_kind,
                                str(error) if isinstance(error, ValueError) else "missing_context",
                                unknown.comparison)


def eligible_original(context: Context, page: dict[str, Any] | None,
                      locator: dict[str, Any] | None) -> dict[str, Any]:
    if page is None or locator is None:
        compared_authorization_locator()
    original = resolve_event(page, locator)
    auth = context.authorization
    identity = committed_identity(dict(authorization_id=auth.id,
        generation_id=generation_id(auth.id), workspace_id=auth.workspace_id,
        key_id=auth.key_hash, invocation_nonce=auth.invocation_nonce,
        reservation_id=auth.credit_reservation_id, billing_authority=auth.settlement,
        journal_region=context.journal_region if context.journal_region is not None else context.region,
        epoch=context.epoch, settle_origin="typed"))
    require(original["phase"] == "authorize" and original["identity"] == identity,
            "authorization_link_gap")
    require(original["eligibility"] == "eligible" and original["facts"] is not None,
            "authorization_link_gap")
    require(original["event_at_us"] // 1000000 <= context.received_at, "authorization_link_gap")
    candidates = original["candidates"]
    decision = decide(RequestedFacts.from_object(original["facts"]),
        original["decision"]["reason"], canonical(candidates) if candidates is not None else None,
        complete_candidates=candidates is not None,
        complete_supported_context=candidates is not None
            and all(c["adapter"] in ("openai", "anthropic") for c in candidates))
    require(decision.eligibility == "eligible" and decision_reference(decision) == original["decision"],
            "authorization_link_gap")
    # Actual endpoint membership is checked independently of mutable catalog data.
    if context.selected_endpoint is not None:
        require(any(c["endpoint_id"] == context.selected_endpoint for c in candidates), "authorization_link_gap")
    return original


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
