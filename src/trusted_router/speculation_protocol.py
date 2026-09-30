"""Pure speculation_protocol v1 wire contract; no dispatch or billing authority.

All trust and current binding inputs come from the caller, never the token.
See docs/speculation-protocol-v1.md for the frozen cross-language contract.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REAL_TYP = "speculation-eligibility+jws"
SHADOW_TYP = "speculation-eligibility-shadow+jws"
DESCRIPTOR_TYP = "speculation-descriptor+jws"
MAX_INT = (1 << 63) - 1
MARGIN = 2


class ProtocolError(ValueError):
    """Stable refusal code shared with the Go consumer."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ProtocolError(reason)


def _integer(value: Any) -> None:
    _require(type(value) is int and 0 <= value <= MAX_INT, "integer")


def _string(value: Any) -> None:
    _require(isinstance(value, str) and bool(value) and all(
        0x20 <= ord(c) <= 0x7E and c not in '\\"<>&' for c in value
    ), "string")


def _hash(value: Any) -> None:
    _require(isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None,
             "hash")


def _object(value: Any, strings: str = "", integers: str = "", nested: str = "") -> None:
    _require(isinstance(value, dict) and set(value) == set(
        (strings + " " + integers + " " + nested).split()), "fields")
    for field in strings.split():
        _string(value[field])
    for field in integers.split():
        _integer(value[field])


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ProtocolError("integer")


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError("json") from exc


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    _require(bool(value) and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None, "base64")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ProtocolError("base64") from exc
    _require(_b64encode(raw) == value, "base64")
    return raw


@dataclass(frozen=True)
class TrustedKey:
    kid: str
    purpose: str
    public_key_b64url: str
    iss: str = ""
    aud: str = ""
    environment: str = ""
    plane: str = ""


@dataclass(frozen=True)
class VerifiedGrant:
    compact: str
    payload: bytes
    shadow: bool
    start_deadline: int

    @property
    def claims(self) -> dict[str, Any]:
        # A fresh copy keeps callers from mutating verified authority in place.
        return json.loads(self.payload)


@dataclass(frozen=True)
class VerifiedDescriptor:
    compact: str
    payload: bytes

    @property
    def claims(self) -> dict[str, Any]:
        return json.loads(self.payload)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _verify(token: str, keys: Sequence[TrustedKey], typ: str,
            purpose: str) -> tuple[dict[str, Any], TrustedKey, bytes]:
    _require(isinstance(token, str) and len(token) <= 65536 and token.count(".") == 2,
             "compact")
    h, p, s = token.split(".")
    header = _json(_b64decode(h))
    _object(header, "alg kid typ")
    _require(header["alg"] == "EdDSA", "algorithm")
    _require(header["typ"] == typ, "type")
    matches = [key for key in keys if key.kid == header["kid"]]
    _require(len(matches) == 1, "key")
    key = matches[0]
    _require(key.purpose == purpose, "purpose")
    payload = _b64decode(p)
    signature = _b64decode(s)
    try:
        Ed25519PublicKey.from_public_bytes(_b64decode(key.public_key_b64url)).verify(
            signature, (h + "." + p).encode("ascii"))
    except (ValueError, InvalidSignature) as exc:
        raise ProtocolError("signature") from exc
    claims = _json(payload)
    _require(isinstance(claims, dict), "fields")
    return claims, key, payload


GRANT_STRINGS = ("iss aud environment plane workspace_id key_id lookup_digest boot_id "
                 "stable_slot_id region grant_id")
GRANT_INTEGERS = ("v generation workspace_epoch key_epoch image_policy_version tier "
                  "paid_headroom_micro iat exp start_before key_expires_at trust_fresh_until "
                  "per_request_ceiling_micro")
ROUTE_STRINGS = ("endpoint_id provider upstream_model region routing_policy_hash catalog_hash "
                 "privacy input_bound_method")
ROUTE_INTEGERS = ("adapter_capability_version input_bound output_limit input_rate_micro_per_m "
                  "output_rate_micro_per_m maximum_request_fees_micro price_expires_at")
BINDINGS = ("workspace_id key_id lookup_digest boot_id stable_slot_id region generation "
            "workspace_epoch key_epoch image_policy_version").split()


def _check_canonical(claims: dict[str, Any], payload: bytes) -> None:
    # Call only after exact schema/type/charset validation. Canonical encoding
    # is defined on the v1 domain, never on language-specific float/string forms.
    _require(payload == _canonical(claims), "canonical_payload")


def _grant_schema(claims: dict[str, Any]) -> None:
    _object(claims, GRANT_STRINGS, GRANT_INTEGERS, "history route permits")
    _object(claims["route"], ROUTE_STRINGS, ROUTE_INTEGERS, "stage_d")
    _require(type(claims["route"]["stage_d"]) is bool, "stage_d")
    _object(claims["history"], integers="clean_since count last_success_at sequence window_start")
    _require(isinstance(claims["permits"], list) and bool(claims["permits"]), "permits")
    for permit in claims["permits"]:
        _object(permit, integers="ordinal b_micro")


def cost_ceiling(input_bound: int, input_rate_micro_per_m: int, output_limit: int,
                 output_rate_micro_per_m: int, maximum_request_fees_micro: int) -> int:
    """Design §3: separately ceil each token component, then add mandatory fees.

    Reject overflow at every intermediate operation, matching signed int64 Go.
    """
    for value in (input_bound, input_rate_micro_per_m, output_limit,
                  output_rate_micro_per_m, maximum_request_fees_micro):
        _integer(value)
    total = maximum_request_fees_micro
    for count, rate in ((input_bound, input_rate_micro_per_m),
                        (output_limit, output_rate_micro_per_m)):
        product = count * rate
        _require(product <= MAX_INT, "overflow")
        rounded = product // 1_000_000 + int(product % 1_000_000 != 0)
        total += rounded
        _require(total <= MAX_INT, "overflow")
    return total


def workspace_allowance(tier_ceiling_micro: int, paid_headroom_micro: int) -> int:
    _integer(tier_ceiling_micro)
    _integer(paid_headroom_micro)
    return min(tier_ceiling_micro // 100, paid_headroom_micro // 10, 1_000_000)


def verify_grant(token: str, keys: Sequence[TrustedKey], context: Mapping[str, Any],
                 now: int, *, shadow: bool = False) -> VerifiedGrant:
    """Verify eligibility at physical-start time against current trusted context.

    Context contains all BINDINGS, the entire approved route, and a trusted
    tier_ceiling_micro. It must come from current authenticated local state.
    """
    _integer(now)
    claims, key, payload = _verify(token, keys, SHADOW_TYP if shadow else REAL_TYP,
                                   "shadow-grant" if shadow else "grant")
    _grant_schema(claims)
    _check_canonical(claims, payload)
    _require(claims["v"] == 1, "version")
    for field in ("iss", "aud", "environment", "plane"):
        _require(claims[field] == getattr(key, field), "identity")
    _hash(claims["lookup_digest"])
    for field in BINDINGS:
        _require(field in context and type(context[field]) is type(claims[field])
                 and claims[field] == context[field], "binding")
    route = claims["route"]
    for field in ("routing_policy_hash", "catalog_hash"):
        _hash(route[field])
    _require(type(route["stage_d"]) is bool and route["stage_d"], "stage_d")
    _require(route == context.get("route"), "route")
    _require(route["region"] == claims["region"], "route")
    _require(0 < route["input_bound"] <= 8192 and 0 < route["output_limit"] <= 512,
             "token_bound")
    _require(route["adapter_capability_version"] > 0, "adapter")
    _require(claims["tier"] in (2, 3), "tier")
    _require(claims["paid_headroom_micro"] >= 5_000_000, "paid_headroom")
    history = claims["history"]
    issued = claims["iat"]
    _require(history["count"] >= 20 and history["sequence"] >= history["count"], "history_count")
    _require(issued - 600 <= history["window_start"] <= history["last_success_at"] <= issued
             and history["last_success_at"] >= issued - 30
             and history["clean_since"] <= issued - 900, "history_time")
    _require(0 < claims["exp"] - issued <= 30 and issued < claims["start_before"], "lifetime")
    deadline = min(claims["start_before"], claims["exp"] - MARGIN,
                   claims["key_expires_at"] - MARGIN, route["price_expires_at"] - MARGIN,
                   claims["trust_fresh_until"] - MARGIN)
    _require(issued <= now < deadline, "start_window")
    ceiling = claims["per_request_ceiling_micro"]
    _require(0 < ceiling <= 10_000, "ceiling")
    b_micro = cost_ceiling(route["input_bound"], route["input_rate_micro_per_m"],
                           route["output_limit"], route["output_rate_micro_per_m"],
                           route["maximum_request_fees_micro"])
    _require(0 < b_micro <= ceiling, "cost")
    permits = claims["permits"]
    ordinals: set[int] = set()
    for permit in permits:
        _require(permit["ordinal"] not in ordinals, "ordinal")
        ordinals.add(permit["ordinal"])
        _require(b_micro <= permit["b_micro"] <= ceiling, "permit_cost")
    _require("tier_ceiling_micro" in context, "tier_ceiling")
    allowance = workspace_allowance(context["tier_ceiling_micro"], claims["paid_headroom_micro"])
    _require(sum(p["b_micro"] for p in permits) <= allowance, "allowance")
    return VerifiedGrant(token, payload, shadow, deadline)


DESCRIPTOR_STRINGS = ("grant_id grant_sha256 execution_id invocation_nonce request_sha256 "
                      "routing_policy_hash endpoint_id workspace_id key_id boot_id")
DESCRIPTOR_INTEGERS = "v ordinal b_micro workspace_epoch key_epoch"


def verify_descriptor(token: str, keys: Sequence[TrustedKey], grant: VerifiedGrant,
                      request_bytes: bytes, execution_id: str,
                      invocation_nonce: str) -> VerifiedDescriptor:
    _require(not grant.shadow, "dry_run_cannot_dispatch")
    claims, key, payload = _verify(token, keys, DESCRIPTOR_TYP, "descriptor")
    _object(claims, DESCRIPTOR_STRINGS, DESCRIPTOR_INTEGERS)
    _check_canonical(claims, payload)
    _require(claims["v"] == 1, "version")
    for field in ("grant_sha256", "request_sha256", "routing_policy_hash"):
        _hash(claims[field])
    g = grant.claims
    _require(key.kid == g["boot_id"], "descriptor_boot")
    for field in ("grant_id", "workspace_id", "key_id", "boot_id", "workspace_epoch", "key_epoch"):
        _require(claims[field] == g[field], "descriptor_binding")
    _require(claims["grant_sha256"] == sha256(grant.compact.encode("ascii")), "grant_hash")
    _require(claims["request_sha256"] == sha256(request_bytes), "request_hash")
    _require(claims["execution_id"] == execution_id and claims["invocation_nonce"] == invocation_nonce,
             "invocation")
    for field in ("endpoint_id", "routing_policy_hash"):
        _require(claims[field] == g["route"][field], "descriptor_route")
    _require(any(p["ordinal"] == claims["ordinal"] and p["b_micro"] == claims["b_micro"]
                 for p in g["permits"]), "descriptor_permit")
    return VerifiedDescriptor(token, payload)


def verify_acceptance(response: Mapping[str, Any], descriptor: VerifiedDescriptor,
                      authorization: Mapping[str, Any]) -> str:
    """Caller authenticates response AND supplies durable ordinary authorization.

    Missing marker means ordinary skew handling; present invalid marker is an
    error. Normal fields must equal that authorization, including route/billing.
    This result does not open the Stage D output gate.
    """
    _require(response.get("authorization") == authorization, "authorization")
    d = descriptor.claims
    # Even an unmarked success must carry a valid ordinary invocation claim.
    for field in ("invocation_nonce", "workspace_id", "key_id"):
        _require(authorization.get(field) == d[field], "authorization")
    _string(authorization.get("authorization_id"))
    _require(authorization.get("billing_mode") == "ordinary", "authorization")
    if "speculation_accepted" not in response:
        return "ordinary"
    marker = response["speculation_accepted"]
    _object(marker, "descriptor_sha256 invocation_nonce authorization_id endpoint_id routing_policy_hash", "v")
    _require(marker["v"] == 1, "version")
    _hash(marker["descriptor_sha256"])
    _hash(marker["routing_policy_hash"])
    d = descriptor.claims
    _require(marker["descriptor_sha256"] == sha256(descriptor.compact.encode("ascii")), "descriptor_hash")
    for field in ("invocation_nonce", "endpoint_id", "routing_policy_hash"):
        _require(marker[field] == d[field], "marker_binding")
    for field in ("authorization_id", "invocation_nonce", "endpoint_id", "routing_policy_hash",
                  "workspace_id", "key_id"):
        expected = marker.get(field, d.get(field))
        _require(authorization.get(field) == expected, "authorization")
    _require(authorization.get("billing_mode") == "ordinary" and
             authorization.get("stage_d") is True, "authorization")
    return "accepted"


def renewal_verdict(previous: VerifiedGrant, candidate: VerifiedGrant) -> str:
    """Pure ordering check; never restores permits or clears any deny latch."""
    if previous.compact == candidate.compact:
        return "replay"
    old, new = previous.claims, candidate.claims
    _require(previous.shadow == candidate.shadow, "renewal")
    for field in ("workspace_id", "key_id", "lookup_digest", "boot_id", "stable_slot_id",
                  "iss", "aud", "environment", "plane", "region"):
        _require(old[field] == new[field], "renewal")
    _require(new["grant_id"] != old["grant_id"] and new["generation"] > old["generation"]
             and new["iat"] >= old["iat"] and new["workspace_epoch"] >= old["workspace_epoch"]
             and new["key_epoch"] >= old["key_epoch"]
             and new["history"]["sequence"] >= old["history"]["sequence"], "renewal")
    return "renewed"


def descriptor_replay(previous: VerifiedDescriptor, candidate: VerifiedDescriptor) -> str:
    _require(previous.compact == candidate.compact, "replay_conflict")
    return "replay"


WORKSPACE_REASONS = frozenset({"credit_exhausted", "billing_denied", "trust_ineligible",
    "trust_demoted", "abuse_latched", "payment_failed", "trust_reconciliation_stale",
    "workspace_paused", "billing_paused"})
KEY_REASONS = frozenset({"key_revoked", "key_disabled", "key_expired", "key_invalid",
    "key_limit_exceeded", "key_window_limit_exceeded", "key_strict_limit_exceeded",
    "key_spend_limit_imposed"})


def classify_verdict(*, source: str, status: int, reason: str, workspace_id: str | None,
                     key_id: str | None, rate_scope: str | None) -> dict[str, Any]:
    """Only normalized authenticated authorize denials belong in this taxonomy."""
    _require(source == "authenticated_router", "verdict_source")
    _integer(status)
    _require(400 <= status <= 599, "verdict_status")
    scope = "none"
    if reason in KEY_REASONS and workspace_id and key_id:
        scope = "key"
    elif workspace_id and (reason in WORKSPACE_REASONS or status == 402):
        scope = "workspace"
    elif status == 429 and workspace_id:
        scope = "key" if rate_scope == "key" and key_id else "workspace"
    breaker = "key_boot" if scope == "none" and (status >= 500 or reason in {
        "authorize_timeout", "transport_error", "infrastructure_error"}) else "none"
    return {"discard_this_execution": True, "durable_scope": scope,
            "local_infrastructure_breaker": breaker,
            "commit_required_with_real_rights": scope != "none",
            "commit_required_shadow_only": False,
            "storage_failure_status_with_real_rights": 503 if scope != "none" else status}
