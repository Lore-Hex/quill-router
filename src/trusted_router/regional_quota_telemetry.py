"""Regional admission evidence. Bit positions are an append-only wire contract."""

from __future__ import annotations

from typing import Any

REGIONAL_PREDICATE_REASONS = (
    "stage_c", "disabled", "issuance_disabled", "cohort", "backend", "estimate",
    "route_type", "candidate_not_credits", "exact_global", "key_lifetime",
    "key_daily", "key_weekly", "key_monthly", "custom_model", "user_model",
    "partner_mode", "additional_cost", "native_batch", "app_markup", "receipt_fee",
)


def regional_predicate_reason(
    *,
    stage_c: bool,
    enabled: bool,
    issuance_enabled: bool,
    in_cohort: bool,
    backend_available: bool,
    estimate: int,
    route_type: str | None,
    all_candidates_credits: bool,
    any_exact_global: bool,
    key_lifetime: int | None,
    key_daily: int | None,
    key_weekly: int | None,
    key_monthly: int | None,
    custom_model: Any | None,
    user_model: Any | None,
    partner_mode: Any | None,
    additional_cost: int,
    native_batch: bool,
    app_markup: int,
    receipt_fee: int,
) -> tuple[str | None, int]:
    """Return the first failure and ALL failures in original conjunction order.

    A null reason with mask zero means eligible; a null mask in an event means
    authorization exited before evaluating the predicate.
    """
    failures = (
        stage_c,
        not enabled,
        not issuance_enabled,
        not in_cohort,
        not backend_available,
        estimate <= 0,
        route_type not in {None, "chat.completions", "responses"},
        not all_candidates_credits,
        any_exact_global,
        key_lifetime is not None,
        key_daily is not None,
        key_weekly is not None,
        key_monthly is not None,
        custom_model is not None,
        user_model is not None,
        partner_mode is not None,
        additional_cost != 0,
        native_batch,
        app_markup != 0,
        receipt_fee != 0,
    )
    mask = sum(1 << index for index, failed in enumerate(failures) if failed)
    reason = next(
        (reason for reason, failed in zip(REGIONAL_PREDICATE_REASONS, failures, strict=True)
         if failed),
        None,
    )
    return reason, mask

