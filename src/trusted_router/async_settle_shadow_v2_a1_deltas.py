"""Executable A1 differences from frozen P1; retained rules delegate to P1."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from trusted_router.async_settle_shadow_v2_deltas import Delta
from trusted_router.async_settle_shadow_v2_schema import Check, Schema, literal, require

PUBLISHER_ADDITIONS = frozenset("scheduled_slots lost_claims unknown_outcomes late_cycles".split())
DAY_ROOTS = frozenset("close_result_root publisher_trace_root health_basis_root".split())
PROOF_ROOTS = DAY_ROOTS | {"handoff_accounting_root", "authorization_obligation_root"}
CLOSE_KIND = "async_settle_shadow_close_result_v2"


def without(value: dict[str, Any], *fields: str) -> dict[str, Any]:
    return {k: v for k, v in value.items() if k not in fields}


def health_projection(v: dict[str, Any]) -> dict[str, Any]:
    # P1 compares these integers only for ordering/range, not their unit.
    return {**without(v, "contract_revision", "observed_at_ns", "publisher_heartbeat_ns", "backlog_age_at_seconds"),
            "observed_at_us": v["observed_at_ns"], "publisher_heartbeat_us": v["publisher_heartbeat_ns"]}


def claim_projection(v: dict[str, Any]) -> dict[str, Any]:
    return {**without(v, "contract_revision", "claim_at_ns"), "claim_at_us": v["claim_at_ns"]}


def publisher_projection(v: dict[str, Any]) -> dict[str, Any]:
    return without(v, *PUBLISHER_ADDITIONS)


def counter_projection(v: dict[str, Any]) -> dict[str, Any]:
    return {**without(v, "contract_revision"), "publisher": publisher_projection(v["publisher"])}


def conservation(v: Any) -> bool:
    return (v["scheduled_slots"] != v["missed_slots"] + v["claim_attempts"]
            or v["claim_attempts"] != v["lost_claims"] + v["publications"] + v["failures"]
            or v["unknown_outcomes"] > v["failures"] or v["late_cycles"] > v["failures"]
            or not v["scans"] <= v["claims"] <= v["claim_attempts"])


def binary64_wall(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        number = float(value)
    except OverflowError:
        return False
    return math.isfinite(number) and number >= 0 and number == value


DELTAS = (
    Delta("a1-binary64-wall", "health-number", "v2-only-broaden", "§3.1 / §3.2", binary64_wall),
    Delta("a1-heartbeat-snapshot", "health", "strengthen", "§3.1", lambda v: v["observed_at_ns"] != v["publisher_heartbeat_ns"]),
    Delta("a1-server-slot", "claim", "strengthen", "§3.1", lambda v: not v["slot"] * 2000000000 <= v["claim_at_ns"] <= v["slot"] * 2000000000 + 250000000),
    Delta("a1-publisher-conservation", "publisher-final", "strengthen", "§3.1.2", conservation),
    Delta("a1-cross-day-sequence", "completion", "v2-only-broaden", "§6.0.1", lambda v: v["original_day"] < v["completion_day"]),
    Delta("a1-close-kind", "retention", "v2-only-broaden", "§7.4", lambda v: v == CLOSE_KIND),
)


def apply_deltas(shape: str, value: Any) -> None:
    for delta in DELTAS:
        if delta.shape == shape and delta.direction != "v2-only-broaden":
            require(not delta.predicate(value), delta.name)


def broadening(name: str, value: Any) -> bool:
    return next(d for d in DELTAS if d.name == name).predicate(value)


def extend(base: Schema, fields: Mapping[str, Check], *, removed: tuple[str, ...] = (),
           projection: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
           shape: str = "", tagged: bool = True,
           invariant: Check | None = None,
           binary64_paths: tuple[tuple[str, ...], ...] = ()) -> Schema:
    additions = {"contract_revision": literal("a1"), **fields} if tagged else dict(fields)

    def validate(value: Any) -> None:
        projected = (projection(value) if projection is not None
                     else without(value, *additions))
        base(projected)
        apply_deltas(shape, value)
        if invariant is not None:
            invariant(value)
    return Schema({**{k: v for k, v in base.fields.items() if k not in removed}, **additions},
                  base.limit, validate, base.retained_strings, binary64_paths)
