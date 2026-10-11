"""Separate-purpose exclusion signatures and pure original-receipt verification."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import TypeAdapter, create_model

from trusted_router import async_settle_shadow_v2_retained as retained
from trusted_router.async_settle_shadow_v2_contract import (
    COHORT_EXCLUSIONS,
    RequestedFacts,
    decide,
    fit_decision,
    validate_identity,
)
from trusted_router.async_settle_shadow_v2_deltas import apply_deltas, broadening
from trusted_router.async_settle_shadow_v2_evidence import (
    CERTIFICATE_RECORD,
    COMMITTED_IDENTITIES,
    EVENT,
    IDENTITY_FIELDS,
    PAGE,
    committed_identity,
)
from trusted_router.async_settle_shadow_v2_schema import (
    LOCATOR,
    Schema,
    boolean,
    canonical,
    day_at,
    decode,
    digest,
    integer,
    literal,
    name,
    nullable,
    receipt_key,
    require,
    revision,
    sequence,
    sha256,
    text,
    versioned,
)
from trusted_router.detached_jws import TrustedKey, b64encode, verify

PURPOSE = "async-settle-shadow-exclusion"
TYP = "tr-async-settle-shadow-exclusion-v2"
AUDIENCE = "router-shadow-exclusion"
LIFETIME = 172800
CERTIFICATE_BYTES = 2048
HEADER = Schema({"alg": literal("EdDSA"), "kid": text(64), "typ": literal(TYP)}, 132)
COMMON_FIELDS = frozenset((*IDENTITY_FIELDS, "iss", "iat", "exp", "async_eligible")) - {"settle_origin", "billing_authority"}
# Reuse the copied v1 field definitions independently for EVERY actual origin
# and authority. No repaired/substituted workspace or other identity is valid.
CommonClaims = create_model("CommonClaims", __base__=retained.Frozen,
    **{k: (retained.ShadowClaims.model_fields[k].annotation,
           retained.ShadowClaims.model_fields[k]) for k in COMMON_FIELDS})  # type: ignore[call-overload]


def validate_common(claims: dict[str, Any]) -> None:
    CommonClaims.model_validate({k: claims[k] for k in COMMON_FIELDS})
    TypeAdapter(retained.TicketClaims.model_fields["async_eligible"].rebuild_annotation()).validate_python(claims["async_eligible"])
    validate_identity(claims)
    apply_deltas("certificate", claims)
    require(claims["settle_origin"] == "typed" or broadening("certificate-origin", claims))
    require(claims["billing_authority"] == "local" or broadening("certificate-authority", claims))


CLAIMS = versioned({**dict.fromkeys(COMMON_FIELDS, lambda value: None),
    "settle_origin": lambda value: None, "billing_authority": lambda value: None,
    "aud": literal(AUDIENCE), "requested_route": name, "requested_streamed": boolean,
    "reason": literal(*sorted(COHORT_EXCLUSIONS)), "facts_sha256": digest,
    "candidate_set_sha256": nullable(digest), "router_revision": revision,
    "original_receipt": LOCATOR}, 1401, validate_common)


def validate_claims(claims: dict[str, Any], now: int) -> None:
    try:
        LOCATOR(claims.get("original_receipt"))
    except ValueError as exc:
        raise ValueError("original_receipt_locator_gap") from exc
    CLAIMS(claims)
    integer(now)
    validate_identity(claims)
    require(claims["exp"] - claims["iat"] == LIFETIME and claims["iat"] <= now < claims["exp"], "proof_expired")
    # Reservation fixes the original day, even when signing completes after midnight.
    receipt_key(claims["original_receipt"])


def verify_signature(token: str, keys: Sequence[TrustedKey], expected: Mapping[str, Any],
                     now: int) -> dict[str, Any]:
    require(type(token) is str and token.isascii() and len(token) <= CERTIFICATE_BYTES, "receipt_locator_size")
    claims, key, payload = verify(token, keys, TYP, PURPOSE)
    HEADER({"alg": "EdDSA", "kid": key.kid, "typ": TYP})
    require(payload == canonical(claims) and key.iss == claims.get("iss")
            and key.aud == AUDIENCE, "proof_signature")
    validate_claims(claims, now)
    # Complete, server-owned identity context is mandatory; no partial binding API.
    require(set(expected) == set(IDENTITY_FIELDS), "identity")
    for field in IDENTITY_FIELDS:
        require(canonical(expected[field]) == canonical(claims[field]), "identity")
    return claims


def certificate_record(claims: dict[str, Any], token: str, keyring_sha256: str,
                       verified_at_us: int) -> dict[str, Any]:
    projection = {k: v for k, v in claims.items() if k not in COMMITTED_IDENTITIES}
    projection.update({k + "_sha256": sha256(claims[k]) for k in COMMITTED_IDENTITIES})
    record = {"v": 2, "policy_version": "shadow-v2", "claims": projection,
        "certificate_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "issuer_keyring_sha256": keyring_sha256, "verified_at_us": verified_at_us,
        "verification_result": "verified", "authorization_day": claims["original_receipt"]["day"]}
    CERTIFICATE_RECORD(record)
    return record


@dataclass(frozen=True)
class ReceiptVerification:
    partition: str
    reason: str | None
    event_bytes: bytes | None = None


def verify_original_receipt(claims: dict[str, Any], *, returned_key: tuple[str, str],
                            page: bytes | None) -> ReceiptVerification:
    """Validate the one supplied complete-PK result; no IO, catalog or fallback."""
    def gap(reason: str) -> ReceiptVerification:
        return ReceiptVerification("C", reason)

    try:
        locator = claims["original_receipt"]
        key = receipt_key(locator)
    except (ValueError, KeyError, TypeError):
        return gap("original_receipt_locator_gap")
    if returned_key != key:
        return gap("original_receipt_identity_gap")
    if page is None:
        return gap("original_receipt_missing")
    try:
        value = decode(page, PAGE.limit)
        require(type(value) is dict and set(value) == set(PAGE.fields))
        if any(value[k] != locator[k] for k in ("day", "boot", "first_seq")):
            return gap("original_receipt_identity_gap")
        require(type(value["events"]) is list and 1 <= len(value["events"]) <= 128)
        sequences = []
        for event in value["events"]:
            sequence(event["seq"])
            sequences.append(int(event["seq"]))
            require(type(event.get("identity")) is dict)
            if event.get("authorization_id") != event["identity"].get("authorization_id"):
                return gap("original_receipt_identity_gap")
            if event.get("receipt_locator") is not None and event["receipt_locator"] != {
                "day": value["day"], "boot": value["boot"], "first_seq": value["first_seq"], "event_seq": event["seq"]
            }:
                return gap("original_receipt_identity_gap")
        sequence(value["first_seq"])
        sequence(value["last_seq"])
        if (sequences[0] != int(value["first_seq"]) or sequences[-1] != int(value["last_seq"])
                or any(b != a + 1 for a, b in zip(sequences, sequences[1:], strict=False))):
            return gap("original_receipt_identity_gap")
        value = PAGE.parse(page)
    except (ValueError, KeyError, TypeError, OverflowError):
        return gap("original_receipt_backing_gap")
    if any(value[k] != locator[k] for k in ("day", "boot", "first_seq")):
        return gap("original_receipt_identity_gap")
    matches = [e for e in value["events"] if e["seq"] == locator["event_seq"]]
    if len(matches) != 1:
        return gap("original_receipt_identity_gap")
    return _verify_event(claims, matches[0])


def _verify_event(claims: dict[str, Any], event: dict[str, Any]) -> ReceiptVerification:
    locator = claims["original_receipt"]

    def gap(reason: str) -> ReceiptVerification:
        return ReceiptVerification("C", reason)

    if (event["phase"] != "authorize" or event["receipt_locator"] != {k: v for k, v in locator.items() if k != "event_sha256"}
            or event["authorization_day"] != locator["day"]
            or day_at(event["event_at_us"]) != locator["day"]
            or event["authorization_id"] != claims["authorization_id"]
            or event["identity"] != committed_identity(claims)
            or event["router_revision"] != claims["router_revision"]):
        return gap("original_receipt_identity_gap")
    if sha256(event) != locator["event_sha256"]:
        return gap("original_receipt_digest_gap")
    facts, candidates = event["facts"], event["candidates"]
    if facts is None or (claims["candidate_set_sha256"] is not None and candidates is None):
        return gap("original_receipt_backing_gap")
    if (facts["route"] != claims["requested_route"] or facts["streamed"] is not claims["requested_streamed"]
            or facts["settle_origin"] != claims["settle_origin"]
            or facts["billing_authority"] != claims["billing_authority"]):
        return gap("original_receipt_identity_gap")
    try:
        decision = decide(RequestedFacts.from_object(facts), claims["reason"],
                          canonical(candidates) if candidates is not None else None,
                          complete_candidates=candidates is not None)
    except ValueError:
        return gap("original_receipt_backing_gap")
    if decision.eligibility != "excluded":
        return gap("exclusion_conflict")
    reference = {k: claims[k] for k in ("reason", "facts_sha256", "candidate_set_sha256")}
    if (event["decision"] != reference or decision.facts_sha256 != claims["facts_sha256"]
            or decision.candidate_set_sha256 != claims["candidate_set_sha256"]):
        return gap("original_receipt_digest_gap")
    return ReceiptVerification("B", None, canonical(event))


def verify_certificate(token: str, keys: Sequence[TrustedKey], expected: Mapping[str, Any],
                       now: int, *, returned_key: tuple[str, str], page: bytes | None,
                       attempt: dict[str, Any], server_attempt: dict[str, Any],
                       booking: str) -> ReceiptVerification:
    from trusted_router.async_settle_shadow_v2_contract import ATTEMPT, SERVER_OBSERVATION

    claims = verify_signature(token, keys, expected, now)
    ATTEMPT(attempt)
    SERVER_OBSERVATION(server_attempt)
    if (attempt["authorization_id"] != claims["authorization_id"]
            or any(server_attempt[k] != attempt[k] for k in ("authorization_id", "terminal_kind"))):
        return ReceiptVerification("C", "identity")
    for field in ("selected_endpoint", "adapter", "route_type", "streamed"):
        actual = server_attempt[field]
        if actual is not None and canonical(actual) != canonical(attempt[field]):
            return ReceiptVerification("C", "exclusion_conflict")
    if attempt["route_type"] != claims["requested_route"] or attempt["streamed"] is not claims["requested_streamed"]:
        return ReceiptVerification("C", "exclusion_conflict")
    backing = verify_original_receipt(claims, returned_key=returned_key, page=page)
    if backing.partition != "B":
        return backing
    if claims["candidate_set_sha256"] is not None:
        assert backing.event_bytes is not None
        event = EVENT.parse(backing.event_bytes)
        candidates = event["candidates"]
        if not any(c["endpoint_id"] == server_attempt["selected_endpoint"] and c["adapter"] == server_attempt["adapter"] for c in candidates):
            return ReceiptVerification("C", "exclusion_conflict")
    if booking != {"settle": "settled", "refund": "refunded"}[attempt["terminal_kind"]]:
        return ReceiptVerification("C", "booking_unknown")
    return backing


@dataclass(frozen=True)
class Issuance:
    certificate: str | None
    reason: str | None
    required_bytes: int | None = None
    candidate_count: int | None = None


class ExclusionSigner:
    def __init__(self, private: Ed25519PrivateKey, trusted: TrustedKey) -> None:
        HEADER({"alg": "EdDSA", "kid": trusted.kid, "typ": TYP})
        name(trusted.iss)
        require(trusted.purpose == PURPOSE and trusted.aud == AUDIENCE
                and b64encode(private.public_key().public_bytes_raw()) == trusted.public_key_b64url,
                "proof_signature")
        self.private, self.trusted = private, trusted

    def sign(self, claims: dict[str, Any], now: int) -> str:
        """Low-level signing primitive; issuance must also pass complete event fit."""
        validate_claims(claims, now)
        require(claims["iss"] == self.trusted.iss, "proof_signature")
        header = {"alg": "EdDSA", "kid": self.trusted.kid, "typ": TYP}
        material = b64encode(canonical(header)) + "." + b64encode(canonical(claims))
        token = material + "." + b64encode(self.private.sign(material.encode("ascii")))
        require(len(token) <= CERTIFICATE_BYTES, "receipt_locator_size")
        return token

    def issue(self, claims: dict[str, Any], event: dict[str, Any], now: int,
              *, fresh: bool, complete_candidates: bool) -> Issuance:
        if not fresh:
            return Issuance(None, "replay")
        try:
            facts = RequestedFacts.from_object(event["facts"])
            projection = event["candidates"]
            decision = decide(facts, claims["reason"], canonical(projection) if projection is not None else None,
                              complete_candidates=complete_candidates)
            fitted = fit_decision(decision, event)
            if fitted.eligibility != "excluded":
                return Issuance(None, fitted.reason, fitted.required_bytes, fitted.candidate_count)
            EVENT(event)
            locator = {**event["receipt_locator"], "event_sha256": sha256(event)}
            require(claims["original_receipt"] == locator, "original_receipt_digest_gap")
            require(event["decision"] == {"reason": decision.reason, "facts_sha256": decision.facts_sha256,
                    "candidate_set_sha256": decision.candidate_set_sha256}, "original_receipt_digest_gap")
            token = self.sign(claims, now)
            # Charge maximal valid verification timestamp and fixed-size digests.
            certificate_record(claims, token, "0" * 64, claims["exp"] * 1000000 - 1)
            # Reuse full backing verification before exposing the signed bytes.
            checked = _verify_event(claims, event)
            require(checked.partition == "B", checked.reason or "original_receipt_backing_gap")
            return Issuance(token, None)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            return Issuance(None, "receipt_locator_size" if str(exc) == "evidence_size" else str(exc) if isinstance(exc, ValueError) else "missing_context")
