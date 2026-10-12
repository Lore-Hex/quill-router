"""Dormant shadow-v2 cohort and terminal transport contract; no live dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trusted_router import async_settle_shadow_v2_retained as retained
from trusted_router import billing_snapshot as billing
from trusted_router.async_settle_shadow_v2_deltas import apply_deltas
from trusted_router.async_settle_shadow_v2_projection import (
    observed_projection,
    usage_projection,
    wire_projection,
)
from trusted_router.async_settle_shadow_v2_schema import (
    POLICY,
    Schema,
    array,
    ascii_text,
    boolean,
    decode,
    identity,
    integer,
    literal,
    name,
    nullable,
    require,
    revision,
    sha256,
    text,
    versioned,
)
from trusted_router.async_settle_shadow_wire import KEYS as RETAINED_WIRE_FIELDS
from trusted_router.async_settle_shadow_wire import parse_header as retained_header
from trusted_router.async_settle_shadow_wire import snapshot_from_object
from trusted_router.detached_jws import b64decode, b64encode, canonical

FIXTURE_MANIFEST_SHA256 = "dc364e152a770b2904c41f13b3af54625da4fcb7652571129e7481e195b1d9e7"
RECEIPT_BYTES = 2048
CANDIDATE_BYTES = 65536
CANDIDATES = 128
HEADER_BYTES = 12288
WIRE_BYTES = 8192
INLINE_BYTES = 6144
LOCAL_BYTES = 65536
EXCLUDED_BYTES = 4096
EXCLUDED_HEADER_BYTES = 5462
COHORT_EXCLUSIONS = frozenset("""untyped non_credits settlement_authority unsupported_route
service_tier app_markup custom_markup receipt_fee custom_model user_model native_batch fusion
polyphemus liberty tool_cost search_cost image_cost video_cost request_fee private_tier_basis
partner unsupported_adapter unknown_parameters""".split())
NEVER_EXCLUSIONS = frozenset("""snapshot_unavailable missing_envelope snapshot_size missing_signer
missing_context transport_failure local_work_overflow usage_missing usage_estimated malformed_usage
arithmetic_overflow evaluator_failed booking_unknown ambiguous_fallback replay header_absent""".split())
AUTHORITY = literal("local", "spend_lease", "regional_lease", "federated", "deferred_home")
ORIGIN = literal("typed", "legacy")
ADAPTER = literal("openai", "anthropic", "other")
ROUTE = literal("chat.completions", "responses", "other")
FEATURES = tuple("custom_model user_model native_batch fusion polyphemus liberty".split())
DEPENDENT = tuple("tool_cost search_cost image_cost video_cost request_fee private_tier_basis".split())
FEATURE_FLAGS = Schema({k: boolean for k in FEATURES + DEPENDENT}, 1024)
REQUESTED_FACTS = Schema({
    "settle_origin": ORIGIN, "usage_class": literal("Credits", "BYOK", "Invoice"),
    "billing_authority": AUTHORITY, "route": name, "streamed": boolean,
    "service_tier": nullable(literal("default", "priority", "auto", "flex")),
    "app_markup": integer, "custom_markup": integer, "receipt_requested": boolean,
    "receipt_fee": integer, "commercial_program": literal("ordinary", "partner"),
    "features": FEATURE_FLAGS,
    # No approved unknown-feature enumeration exists in v2; fail closed.
    "unknown_features": array(name, 0),
}, 2048)
CANDIDATE = Schema({"endpoint_id": name, "adapter": ADAPTER, "features": array(literal(*(FEATURES + DEPENDENT)), len(FEATURES + DEPENDENT), unique=True)}, 2048)
CANDIDATE_SET = array(CANDIDATE, CANDIDATES, 1)


def generation_id(authorization_id: str) -> str:
    identity(authorization_id)
    return retained.generation_id_for_authorization(authorization_id)


def validate_identity(value: Any) -> None:
    """Shared by raw signed claims and content-free committed projections."""
    require(value["epoch"] > 0 and value["generation_id"] == generation_id(value["authorization_id"]), "identity")


@dataclass(frozen=True)
class RequestedFacts:
    """Owned immutable bytes, with no mutable caller references or missing defaults."""
    raw: bytes

    def __post_init__(self) -> None:
        require(self.raw == REQUESTED_FACTS.serialize(REQUESTED_FACTS.parse(self.raw)))

    @classmethod
    def from_object(cls, value: dict[str, Any]) -> RequestedFacts:
        return cls(REQUESTED_FACTS.serialize(value))

    def object(self) -> dict[str, Any]:
        return REQUESTED_FACTS.parse(self.raw)


@dataclass(frozen=True)
class Decision:
    eligibility: str
    reason: str | None
    candidate_dependent: bool
    facts_sha256: str | None
    candidate_set_sha256: str | None
    required_bytes: int | None = None
    candidate_count: int | None = None


def decide(facts: RequestedFacts, reason: str | None, candidates: bytes | None,
           *, complete_candidates: bool, fresh: bool = True,
           complete_supported_context: bool = True) -> Decision:
    """Select a proof obligation; exclusion issuance additionally requires event fit.

    The supplied candidate order is the complete authorization fallback order, not
    a live catalog view. Unknown context, omitted candidates and unsupported enums
    never create an exclusion. A caller cannot use this result as a certificate.
    """
    f = facts.object()
    unresolved = Decision("unknown", "missing_context", False, None, None)
    if not fresh:
        return Decision("unknown", "replay", False, None, None)
    if reason is not None and reason not in COHORT_EXCLUSIONS:
        return Decision("unknown", reason, False, None, None)
    fixed = {
        "untyped": f["settle_origin"] != "typed",
        "non_credits": f["usage_class"] != "Credits",
        "settlement_authority": f["billing_authority"] != "local",
        "unsupported_route": f["route"] not in ("chat.completions", "responses"),
        "service_tier": f["service_tier"] not in (None, "default"),
        "app_markup": f["app_markup"] > 0, "custom_markup": f["custom_markup"] > 0,
        "receipt_fee": f["receipt_requested"] or f["receipt_fee"] > 0,
        "partner": f["commercial_program"] == "partner",
        **f["features"],
    }
    # A fixed commercial program applies regardless of the selected adapter.
    fixed["unsupported_adapter"] = fixed["partner"]
    if reason is not None and fixed.get(reason, False):
        return Decision("excluded", reason, False, sha256({"facts": f, "reason": reason}), None)
    if candidates is None or not complete_candidates:
        return unresolved
    projection = decode(candidates, CANDIDATE_BYTES)
    CANDIDATE_SET(projection)
    require(candidates == canonical(projection))
    require(len({c["endpoint_id"] for c in projection}) == len(projection))
    if reason in ("partner", "unsupported_adapter"):
        qualifies = all(c["adapter"] == "other" for c in projection)
    elif reason in DEPENDENT:
        qualifies = all(reason in c["features"] for c in projection)
    else:
        qualifies = False
    if qualifies:
        return Decision("excluded", reason, True, sha256({"facts": f, "reason": reason}), sha256(projection))
    if reason == "unknown_parameters":
        return unresolved
    if any(fixed.values()):
        return unresolved  # Requested reason did not prove the nonordinary facts.
    if any(c["adapter"] in ("openai", "anthropic") for c in projection) and complete_supported_context:
        return Decision("eligible", None, False, None, None)
    return unresolved


def fit_decision(decision: Decision, complete_event: dict[str, Any]) -> Decision:
    """Check the WHOLE event, including coordinates and all factual backing."""
    required = len(canonical(complete_event))
    candidates = complete_event.get("candidates")
    count = len(candidates) if isinstance(candidates, list) else 0
    if required > RECEIPT_BYTES:
        return Decision("unknown", "candidate_receipt_size" if decision.candidate_dependent
                        else "receipt_locator_size", False, None, None, required, count)
    if decision.candidate_dependent and not candidates:
        return Decision("unknown", "original_receipt_backing_gap", False, None, None)
    return decision


ATTEMPT = Schema({"authorization_id": identity, "terminal_kind": literal("settle", "refund"),
                  "selected_endpoint": nullable(name), "adapter": nullable(ADAPTER),
                  "route_type": name, "streamed": boolean}, 1024,
                 lambda v: require((v["selected_endpoint"] is None) == (v["adapter"] is None)
                                   and (v["terminal_kind"] == "refund" or v["adapter"] is not None)))
# Null means unobserved, including legacy refund placeholders. Callers must
# report their provenance honestly; the verifier never copies wire facts here.
SERVER_OBSERVATION = Schema({"authorization_id": identity, "terminal_kind": literal("settle", "refund"),
    "selected_endpoint": nullable(name), "adapter": nullable(ADAPTER),
    "route_type": nullable(name), "streamed": nullable(boolean)}, 1024)
EXCLUDED = versioned({"variant": literal("excluded"), "certificate": text(2048, r"[A-Za-z0-9_.-]+"),
                      "attempt": ATTEMPT, "go_revision": revision}, EXCLUDED_BYTES)
def _retained_schema(model: type[billing.Frozen], limit: int, validator: Any = None) -> Schema:
    # Exact keys come from the retained DTO; its validator owns the value domain.
    def validate(value: Any) -> None:
        if validator is None:
            model.model_validate(value)
        else:
            validator(value)
    return Schema(dict.fromkeys(model.model_fields, lambda value: None), limit, validate)


NORMAL_USAGE = _retained_schema(billing.NormalizedUsage, 1024,
    lambda v: billing.NormalizedUsage.model_validate(usage_projection(v)))
SNAPSHOT = _retained_schema(billing.BillingSnapshot, INLINE_BYTES, snapshot_from_object)


def observed(value: Any) -> None:
    billing.Eligibility.model_validate(observed_projection(value))


def wire_raw_usage(value: Any) -> None:
    billing.RawUsage.model_validate(usage_projection(value))


def _compared(v: Any) -> None:
    # This module is pure transitively. Keep the retained DTO/refund special
    # case in its original validator instead of maintaining a second parser.
    try:
        retained_header((b64encode(canonical(wire_projection(v))),))
    except ValueError as exc:
        raise ValueError("evidence_schema_gap") from exc
    apply_deltas("wire", v)


class ComparedSchema(Schema):
    def _validate(self, value: Any) -> None:
        try:
            _compared(value)
        except (KeyError, TypeError, AttributeError, OverflowError) as exc:
            raise ValueError("evidence_schema_gap") from exc
        super()._validate(value)


# Only the new discriminator is typed here. The pure retained parser owns all
# retained fields and cross-field rules, including optional snapshot semantics.
COMPARED_FIELDS = {"variant": literal("compared"),
                   **dict.fromkeys(RETAINED_WIRE_FIELDS - {"v", "billing_snapshot"}, lambda value: None)}
COMPARED_FULL = ComparedSchema({"v": literal(2), "policy_version": literal(POLICY),
    **COMPARED_FIELDS, "billing_snapshot": lambda value: None}, WIRE_BYTES, retained_strings=True)
COMPARED_HASH_ONLY = ComparedSchema({"v": literal(2), "policy_version": literal(POLICY),
    **COMPARED_FIELDS}, WIRE_BYTES, retained_strings=True)


def parse_terminal(raw: bytes) -> dict[str, Any]:
    value = decode(raw, WIRE_BYTES, retained_strings=True)
    require(type(value) is dict)
    if value.get("variant") == "excluded":
        return EXCLUDED.parse(raw)
    schema = COMPARED_FULL if "billing_snapshot" in value else COMPARED_HASH_ONLY
    return schema.parse(raw)


def parse_header(headers: tuple[str, ...]) -> dict[str, Any]:
    require(len(headers) == 1, "header_duplicate")
    header = headers[0]
    require(type(header) is str and header.isascii() and len(header) <= HEADER_BYTES, "header_size")
    raw = b64decode(header)
    result = parse_terminal(raw)
    if result["variant"] == "excluded":
        require(len(header) <= EXCLUDED_HEADER_BYTES, "header_size")
    return result


CAPABILITY_HEADERS = Schema({"X-TR-Settlement-Shadow-Policy": literal(POLICY)}, 128)
AUTHORIZE_EXCLUDED = Schema({"billing_shadow_policy": literal(POLICY),
    "billing_shadow_exclusion": text(2048, r"[A-Za-z0-9_.-]+")}, 4096)
AUTHORIZE_COMPARED = Schema({"billing_shadow_policy": literal(POLICY),
    "billing_shadow_binding": ascii_text(2048),
    "billing_snapshot": Schema(SNAPSHOT.fields, LOCAL_BYTES, SNAPSHOT.invariant)}, LOCAL_BYTES, retained_strings=True)
