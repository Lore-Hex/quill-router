"""Spend-lease settlement rules shared by inline and durable repair paths."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trusted_router.app_markup_billing import (
    APP_MARKUP_APP_ID_SETTLE_FIELD,
    APP_MARKUP_OWNER_SETTLE_FIELD,
    APP_MARKUP_PAYOUT_SETTLE_FIELD,
    app_markup_microdollars_from_charge,
    app_markup_owner_share_microdollars,
)
from trusted_router.custom_model_billing import (
    USER_MODEL_ID_SETTLE_FIELD,
    USER_MODEL_OWNER_SETTLE_FIELD,
    USER_MODEL_PAYOUT_SETTLE_FIELD,
    owner_share_microdollars,
)
from trusted_router.custom_model_markup_billing import (
    CUSTOM_MODEL_MARKUP_CHARGE_SETTLE_FIELD,
    CUSTOM_MODEL_MARKUP_ID_SETTLE_FIELD,
    CUSTOM_MODEL_MARKUP_OWNER_SETTLE_FIELD,
    CUSTOM_MODEL_MARKUP_PAYOUT_SETTLE_FIELD,
    collected_custom_model_markup_microdollars,
    custom_model_markup_owner_share_microdollars,
)
from trusted_router.partner_billing import PARTNER_OPERATOR_COST_SETTLE_FIELD
from trusted_router.spend_lease_state import (
    AuthorizationDurability,
    AuthorizationObservation,
    FinalizationOutcome,
    MonetaryMismatchProof,
    SpendLeaseMonetaryMismatch,
)
from trusted_router.storage_models import GatewayAuthorization

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpendLeaseRepairAmounts:
    """The complete frozen monetary authority after applying the omitted clamp."""

    actual_cost_micro: int
    app_markup_micro: int
    operator_cost_micro: int | None
    user_model_payout_micro: int | None
    app_markup_payout_micro: int | None
    custom_model_markup_micro: int
    custom_model_markup_payout_micro: int | None
    settle_body: dict[str, Any]


def complete_typed_binding(typed_columns: Mapping[str, Any]) -> bool:
    """Whether all three typed facts that imply spend-lease settlement exist."""

    return all(
        typed_columns.get(column) is not None
        for column in (
            "spend_lease_id",
            "spend_lease_gen",
            "spend_lease_allocated_micro",
        )
    )


def derive_spend_lease_settlement(
    payload: Mapping[str, Any] | None,
    typed_columns: Mapping[str, Any],
    merged: dict[str, Any],
) -> None:
    """Make the typed binding tuple authoritative for the settlement class."""

    if not complete_typed_binding(typed_columns):
        return
    payload_settlement = payload.get("settlement") if payload is not None else None
    if payload_settlement not in (None, "spend_lease"):
        log.error(
            "spend_lease.settlement_kind_mismatch",
            extra={
                "spend_lease_id": typed_columns.get("spend_lease_id"),
                "spend_lease_gen": typed_columns.get("spend_lease_gen"),
                "payload_settlement": payload_settlement,
                "derived_settlement": "spend_lease",
            },
        )
    merged["settlement"] = "spend_lease"


def clamp_spend_lease_charge(
    authorization: GatewayAuthorization,
    actual_cost_micro: int,
) -> int:
    """Apply decision 50's customer-charge cap to a spend-lease settlement."""

    if authorization.settlement != "spend_lease":
        return actual_cost_micro
    allocated = authorization.spend_lease_allocated_micro
    if allocated is None:
        cap = int(authorization.estimated_microdollars)
        log.error(
            "spend_lease.settle_binding_facts_missing",
            extra={
                "authorization_id": authorization.id,
                "spend_lease_id": authorization.spend_lease_id,
                "spend_lease_gen": authorization.spend_lease_gen,
                "estimated_microdollars": authorization.estimated_microdollars,
                "actual_microdollars": actual_cost_micro,
            },
        )
    else:
        cap = min(int(allocated), int(authorization.estimated_microdollars))
    if actual_cost_micro <= cap:
        return actual_cost_micro
    log.warning(
        "billing.spend_lease_settle_capped_to_allocation",
        extra={
            "authorization_id": authorization.id,
            "spend_lease_id": authorization.spend_lease_id,
            "spend_lease_gen": authorization.spend_lease_gen,
            "spend_lease_allocated_micro": allocated,
            "actual_microdollars": actual_cost_micro,
            "overrun_microdollars": actual_cost_micro - cap,
        },
    )
    return cap


def derive_spend_lease_repair_amounts(
    authorization: GatewayAuthorization,
    actual_cost_micro: int,
    settle_body: Mapping[str, Any],
) -> SpendLeaseRepairAmounts:
    """Clamp and deterministically rewrite every charge-derived frozen amount."""

    charge = clamp_spend_lease_charge(authorization, actual_cost_micro)
    body = dict(settle_body)
    app_markup = (
        app_markup_microdollars_from_charge(charge, authorization.app_markup_basis_points)
        if authorization.app_markup_basis_points > 0
        else 0
    )
    app_payout: int | None = None
    if authorization.app_markup_basis_points > 0:
        app_payout = app_markup_owner_share_microdollars(app_markup)
        body[APP_MARKUP_PAYOUT_SETTLE_FIELD] = app_payout
        body[APP_MARKUP_OWNER_SETTLE_FIELD] = authorization.app_owner_user_id
        body[APP_MARKUP_APP_ID_SETTLE_FIELD] = authorization.app_id

    additional_cost = body.get("additional_cost_microdollars", 0)
    if isinstance(additional_cost, bool) or not isinstance(additional_cost, int):
        additional_cost = 0
    custom_markup = collected_custom_model_markup_microdollars(
        charge,
        authorization.custom_model_markup_basis_points,
        app_markup_basis_points=authorization.app_markup_basis_points,
        additional_cost_microdollars=additional_cost,
    )
    custom_payout: int | None = None
    if authorization.custom_model_markup_basis_points > 0:
        custom_payout = custom_model_markup_owner_share_microdollars(custom_markup)
        body[CUSTOM_MODEL_MARKUP_CHARGE_SETTLE_FIELD] = custom_markup
        body[CUSTOM_MODEL_MARKUP_PAYOUT_SETTLE_FIELD] = custom_payout
        body[CUSTOM_MODEL_MARKUP_OWNER_SETTLE_FIELD] = (
            authorization.custom_model_owner_user_id
        )
        body[CUSTOM_MODEL_MARKUP_ID_SETTLE_FIELD] = authorization.custom_model_id

    user_payout: int | None = None
    operator_cost: int | None = None
    if authorization.user_provided_model_id is not None:
        user_payout = owner_share_microdollars(max(0, charge - app_markup))
        operator_cost = user_payout
        body[USER_MODEL_PAYOUT_SETTLE_FIELD] = user_payout
        body[USER_MODEL_OWNER_SETTLE_FIELD] = authorization.user_model_owner_user_id
        body[USER_MODEL_ID_SETTLE_FIELD] = authorization.user_provided_model_id
        body[PARTNER_OPERATOR_COST_SETTLE_FIELD] = operator_cost

    return SpendLeaseRepairAmounts(
        actual_cost_micro=charge,
        app_markup_micro=app_markup,
        operator_cost_micro=operator_cost,
        user_model_payout_micro=user_payout,
        app_markup_payout_micro=app_payout,
        custom_model_markup_micro=custom_markup,
        custom_model_markup_payout_micro=custom_payout,
        settle_body=body,
    )


def mirror_finalized_spend_lease_best_effort(store: Any, authorization: GatewayAuthorization) -> None:
    """Mirror a committed winning finalize; ledger failures never change settle."""

    if authorization.settlement != "spend_lease" or not authorization.settled:
        return
    ledger = getattr(store, "_spend_lease_ledger", None)
    try:
        if ledger is None:
            raise RuntimeError("spend lease ledger is unavailable")
        lease_id = str(authorization.spend_lease_id or "")
        region = str(authorization.region or "")
        gen = authorization.spend_lease_gen
        allocated_micro = authorization.spend_lease_allocated_micro
        if not lease_id or not region or gen is None or allocated_micro is None:
            raise RuntimeError("spend lease binding facts are incomplete")
        local = ledger.get(lease_id, region=region)
        if local is None:
            raise RuntimeError("spend lease row is unavailable")
        allocation = next(
            item for item in local.allocations if item.authorization_id == authorization.id
        )
        outcome = (
            None
            if authorization.finalization_outcome is None
            else FinalizationOutcome(authorization.finalization_outcome)
        )
        observation = AuthorizationObservation(
            idempotency_scope=allocation.idempotency_scope,
            authorization_id=allocation.authorization_id,
            request_fingerprint=allocation.request_fingerprint,
            lease_id=lease_id,
            gen=int(gen),
            allocated_micro=int(allocated_micro),
            key_hash=authorization.key_hash,
            workspace_id=authorization.workspace_id,
            durability=AuthorizationDurability.TERMINAL,
            finalization_outcome=outcome,
            finalized_cost_microdollars=(
                authorization.finalized_cost_microdollars
                if outcome == FinalizationOutcome.SETTLED
                else None
            ),
        )
        try:
            ledger.mirror(lease_id, region=region, observation=observation)
        except SpendLeaseMonetaryMismatch as exc:
            ledger.quarantine(
                lease_id,
                region=region,
                idempotency_scope=allocation.idempotency_scope,
                proof=MonetaryMismatchProof(
                    exc.finalized_cost_microdollars,
                    exc.allocated_micro,
                ),
            )
    except Exception:
        log.error(
            "spend_lease.eager_mirror_failed",
            extra={
                "authorization_id": authorization.id,
                "spend_lease_id": authorization.spend_lease_id,
                "spend_lease_gen": authorization.spend_lease_gen,
            },
            exc_info=True,
        )
