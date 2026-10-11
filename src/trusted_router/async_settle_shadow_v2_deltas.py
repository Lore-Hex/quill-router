"""Executable shadow-v2 deltas. Predicates mean rejection, or vocabulary membership.

Strengthen/size predicates reject after retained delegation. Broaden predicates
identify only new vocabularies; they never excuse a failed retained constraint.
The exclusion certificate is a new object, not a relaxed price binding.
"""
from __future__ import annotations

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
    predicate: Callable[[Any], bool]


DELTAS = (
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


def apply_deltas(shape: str, value: Any) -> None:
    for delta in DELTAS:
        if delta.shape == shape and delta.direction != "v2-only-broaden":
            require(not delta.predicate(value), delta.name)
