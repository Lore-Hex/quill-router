"""Lossless retained projections: named drops, fixed tags, new diagnostic rows only."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_retained as retained
from trusted_router.async_settle_shadow_v2_deltas import apply_deltas, broadening
from trusted_router.async_settle_shadow_v2_schema import Schema

SAMPLE_ADDITIONS = frozenset("proof_kind original_terminal_receipt health_source health_slot prediction_basis".split())
COUNTER_ADDITIONS = frozenset("""day boot authorize_attempts authorize_fresh authorize_replay
settle_attempts refund_attempts A B C proof_compared proof_certificate proof_observed
requested_eligible requested_excluded requested_unknown eligible_demand event_seq durable_event_hwm
checkpoint_seq receipt_count receipt_chain_sha256 loss_count reset_count diagnostics buckets publisher
checkpoint_attempts checkpoint_failures checkpoint_recoveries unknown_commits unreconciled_commits irrecoverable_losses""".split())


def sample_projection(obj: dict[str, Any]) -> dict[str, Any]:
    return {**{k: v for k, v in obj.items() if k not in SAMPLE_ADDITIONS},
            "v": 1, "policy_version": "shadow-v1"}


def counter_projection(obj: dict[str, Any]) -> dict[str, Any]:
    projected = {**{k: v for k, v in obj.items() if k not in COUNTER_ADDITIONS},
                 "v": 1, "policy_version": "shadow-v1"}
    for field in ("exclusions", "rejections", "drops"):
        rows = projected.get(field)
        if isinstance(rows, list):
            projected[field] = [row for row in rows if not isinstance(row, dict)
                                or not broadening("v2-counter-reasons", row)]
    return projected


def wire_projection(obj: dict[str, Any]) -> dict[str, Any]:
    return {**{k: v for k, v in obj.items() if k not in ("variant", "policy_version")}, "v": 1}


def binding_projection(obj: dict[str, Any]) -> dict[str, Any]:
    # Binding claims are unchanged, including their purpose-specific context.
    return obj


def observed_projection(obj: dict[str, Any]) -> dict[str, Any]:
    return obj


def usage_projection(obj: dict[str, Any]) -> dict[str, Any]:
    return obj


def delegate_sample(obj: dict[str, Any]) -> None:
    retained.validate_sample(sample_projection(obj), obj["authorization_day"] + "/" + obj["authorization_id"])


def delegate_counter(obj: dict[str, Any]) -> None:
    retained.validate_counter(obj["day"] + "/" + obj["instance"], counter_projection(obj))


class RetainedSchema(Schema):
    """Retained validation precedes new fields, invariants, and executable deltas."""

    def __call__(self, value: Any) -> None:
        shape = "sample" if "authorization_day" in self.fields else "counter"
        try:
            (delegate_sample if shape == "sample" else delegate_counter)(value)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
            raise ValueError("evidence_schema_gap") from exc
        apply_deltas("retained-object", value)
        super().__call__(value)
        apply_deltas(shape, value)


def validate_binding_claims(obj: dict[str, Any], now: int) -> retained.ShadowClaims:
    return retained.validate_claims(binding_projection(obj), now)
