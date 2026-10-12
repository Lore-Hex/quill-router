"""Executable shadow-v2 deltas. Predicates mean rejection, or vocabulary membership.

Object-domain predicates reject before any delegation; shape strengthen/size
predicates reject after retained delegation. Broaden predicates
identify only new vocabularies; they never excuse a failed retained constraint.
The exclusion certificate is a new object, not a relaxed price binding.

Parity reference: sample/counter use the standalone v1 object validators;
wire uses the complete legacy decoder-plus-validator on raw bytes (with only
v2 wrapper fields removed and the version restored, preserving retained tokens).
The §4.2/§7 round-1 conservative transport resolution requires canonical
ASCII-escaped JSON string tokens, including keys, on every v2 retained transport.
This is stricter than v1 wire decoding. Retained object strings additionally
exclude surrogate code points, unlike the standalone v1 counter validator.
Escaped controls and Unicode scalar values remain permitted in retained fields
where their v1 validator permits them; new fields keep their own strict schemas.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from trusted_router import billing_snapshot as billing
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.detached_jws import canonical

NEW_COUNTER_REASONS = frozenset("""original_receipt_locator_gap
original_receipt_identity_gap original_receipt_digest_gap original_receipt_backing_gap
original_receipt_missing candidate_receipt_size receipt_locator_size exclusion_conflict
missing_signer missing_context transport_failure local_work_overflow evaluator_failed
ambiguous_fallback evidence_schema_gap evidence_identity_gap""".split())


def native_json_domain(value: Any, depth: int = 0, *, binary64_paths: tuple[tuple[str, ...], ...] = ()) -> bool:
    """Reject non-exact JSON-native types before encoding can normalize them.

    V1 native validators may accept dict/list/str/int subclasses, tuples and
    integral floats (see the differential grid); v2 deliberately does not.
    This includes OrderedDict/defaultdict, str-subclass keys and IntEnum members.
    Bool and int are separate exact domain members; field validators still
    decide which applies.
    Sixteen container levels match both decoders. Cycles necessarily exceed
    that bound, so they produce the same fixed diagnostic without recursion
    exhaustion. Shared acyclic children are allowed; nothing is normalized.
    """
    kind = type(value)
    if kind is dict:
        return depth >= 16 or any(type(k) is not str or native_json_domain(v, depth + 1, binary64_paths=tuple(p[1:] for p in binary64_paths if p and p[0] == k) if binary64_paths else ())
                                  for k, v in value.items())
    if kind is list:
        return depth >= 16 or any(native_json_domain(v, depth + 1, binary64_paths=tuple(p[1:] for p in binary64_paths if p and p[0] == "*") if binary64_paths else ()) for v in value)
    if kind is float and () in binary64_paths:
        return not math.isfinite(value)
    return not (kind is str or kind is int or kind is bool or value is None)


def transport_spelling(raw: bytes) -> bool:
    """Reject noncanonical string tokens, not whitespace or object-key ordering.

    Malformed JSON is still owned by the retained decoder; token decoding here
    can also raise ValueError, which is a rejection, never an acceptance escape.
    """
    return any(token.group() != canonical(json.loads(token.group()))
               for token in re.finditer(rb'"(?:[^"\\]|\\.)*"', raw))


def scalar_string(value: Any) -> bool:
    """The shared retained decoder's sole extra object-string restriction."""
    if isinstance(value, str):
        return re.search(r"[\ud800-\udfff]", value) is not None
    if isinstance(value, dict):
        return any(scalar_string(k) or scalar_string(v) for k, v in value.items())
    if isinstance(value, list):
        return any(scalar_string(v) for v in value)
    return False


def clean(v: Any) -> bool:
    return v["classification"] in ("exact", "explained-by-catalog-change")


def unnormalized(v: Any) -> bool:
    if not clean(v):
        return False
    try:
        for field in ("python_usage", "go_usage", "legacy_usage"):
            billing.NormalizedUsage.model_validate(v[field])
    except ValueError:
        return True
    return False


def strict_dimension(v: Any) -> bool:
    return any(type(row["streamed"]) not in (bool, type(None))
               for field in ("counts", "terminal_counts", "exclusions", "rejections", "drops")
               for row in v[field])


@dataclass(frozen=True)
class Delta:
    name: str
    shape: str
    direction: Literal["strengthen", "v2-only-broaden", "size"]
    design: str
    predicate: Callable[..., bool]


DELTAS = (
    Delta("native-json-domain", "every v2 object", "strengthen", "§8 G1 (no coercions) / §7 (exact schemas)", native_json_domain),
    Delta("transport-spelling", "retained-transport", "strengthen", "§4.2 / §7 (round-1 lexical resolution)", transport_spelling),
    Delta("scalar-string", "retained-object", "strengthen", "§4.2 / §7 (Unicode scalar strings)", scalar_string),
    Delta("clean-requires-eligible", "sample", "strengthen", "§2", lambda v: clean(v) and v["eligibility"]["observed"] is not True),
    # Subsumes the former clean-usage (null-only) exception.
    Delta("clean-requires-normalized-usage", "sample", "strengthen", "§2", unnormalized),
    Delta("clean-winner-conflict", "sample", "strengthen", "§2 / §8 G9", lambda v: clean(v) and "winner_polarity" in v["reason_codes"]),
    Delta("excluded-admission", "sample", "strengthen", "§2 / §5", lambda v: v["eligibility"]["observed"] is False and v["admission"]["prediction"] == "yes"),
    Delta("strict-dimension", "counter", "strengthen", "§7 / §8 G1", strict_dimension),
    Delta("strict-diagnostic-array", "counter", "strengthen", "§7 / §8 G1", lambda v: any(type(v[k]) is not list for k in ("exclusions", "rejections", "drops"))),
    Delta("v2-counter-reasons", "counter-row", "v2-only-broaden", "§4 / §7", lambda v: isinstance(v.get("reason"), str) and v["reason"] in NEW_COUNTER_REASONS),
    Delta("wire-wrapper-size", "wire", "size", "§4.2", lambda v: len(canonical(v)) > 8192),
    Delta("sample-wrapper-size", "sample", "size", "§7", lambda v: len(canonical(v)) > 8192),
    Delta("counter-wrapper-size", "counter", "size", "§7", lambda v: len(canonical(v)) > 65536),
    Delta("certificate-issuer-bound", "certificate", "strengthen", "§4.1", lambda v: len(v["iss"]) > 128),
    Delta("certificate-origin", "certificate", "v2-only-broaden", "§4.1", lambda v: v["settle_origin"] == "legacy"),
    Delta("certificate-authority", "certificate", "v2-only-broaden", "§4.1", lambda v: v["billing_authority"] in ("spend_lease", "regional_lease", "federated", "deferred_home")),
)


def broadening(name: str, value: Any) -> bool:
    return next(d for d in DELTAS if d.name == name).predicate(value)


def apply_deltas(shape: str, value: Any, *, binary64_paths: tuple[tuple[str, ...], ...] = ()) -> None:
    for delta in DELTAS:
        if delta.shape == shape and delta.direction != "v2-only-broaden":
            rejected = (delta.predicate(value, binary64_paths=binary64_paths)
                        if binary64_paths and delta.name == "native-json-domain" else delta.predicate(value))
            require(not rejected, delta.name)
