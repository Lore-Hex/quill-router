"""Existing creator earnings remain accessible after model retirement."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter

from trusted_router.auth import ManagementPrincipal, Principal
from trusted_router.errors import api_error
from trusted_router.money import format_money_display
from trusted_router.schemas import EarningsTransferRequest
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


def register_earnings_routes(router: APIRouter) -> None:
    @router.get("/earnings")
    @router.get("/user-models/earnings", include_in_schema=False)
    def get_user_model_earnings(
        principal: ManagementPrincipal,
        before: str | None = None,
    ) -> dict[str, Any]:
        if before is not None:
            try:
                cutoff = datetime.fromisoformat(before.replace("Z", "+00:00"))
                if cutoff.tzinfo is None:
                    cutoff = cutoff.replace(tzinfo=UTC)
                before = cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z")
            except (ValueError, OverflowError) as exc:
                raise api_error(
                    400, "before must be an ISO 8601 timestamp", ErrorType.BAD_REQUEST
                ) from exc
        return {"data": _earnings_data(_owner_user_id(principal), before=before)}

    @router.post("/earnings/transfer")
    @router.post("/user-models/earnings/transfer", include_in_schema=False)
    def transfer_user_model_earnings(
        body: EarningsTransferRequest,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        owner_user_id = _owner_user_id(principal)
        if body.amount_microdollars <= 0:
            raise api_error(
                400,
                "Transfer amount must be positive",
                ErrorType.BAD_REQUEST,
            )
        workspace = next(
            (
                candidate
                for candidate in STORE.list_workspaces_for_user(owner_user_id)
                if candidate.id == body.workspace_id
            ),
            None,
        )
        if workspace is None:
            # Membership and existence deliberately collapse to the same answer.
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        if workspace.federated_home:
            raise api_error(
                400,
                "Earnings cannot be transferred to a federated workspace",
                ErrorType.BAD_REQUEST,
            )
        event_suffix = body.idempotency_key or str(uuid.uuid4())
        outcome = STORE.transfer_earnings_to_workspace(
            owner_user_id,
            workspace.id,
            body.amount_microdollars,
            f"earnings_transfer:{owner_user_id}:{event_suffix}",
        )
        if outcome == "insufficient":
            available = STORE.earnings_summary(owner_user_id)["available"]
            raise api_error(
                402,
                "Insufficient earnings",
                ErrorType.INSUFFICIENT_CREDITS,
                extra={"available": available},
            )
        summary = _earnings_summary_shape(STORE.earnings_summary(owner_user_id))
        return {
            "data": {
                "deduplicated": outcome == "duplicate",
                "summary": summary,
            }
        }


def _owner_user_id(principal: Principal) -> str:
    if principal.user is not None:
        return principal.user.id
    if principal.api_key is not None and principal.api_key.creator_user_id:
        return principal.api_key.creator_user_id
    raise api_error(
        403,
        "A user-owned management session or key is required",
        ErrorType.FORBIDDEN,
    )


def _earnings_data(user_id: str, *, before: str | None = None) -> dict[str, Any]:
    since = (datetime.now(UTC) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    by_model = STORE.custom_model_earnings_by_model(user_id, since=since)
    recent = STORE.list_credit_movements(
        f"user:{user_id}",
        limit=50,
        before=before,
    )
    return {
        # GET response only; transfer POSTs keep the strong default.
        "summary": _earnings_summary_shape(STORE.earnings_summary(user_id, allow_stale=True)),
        "by_model_30d": [
            {"model_id": model_id, "earned_microdollars": amount}
            for model_id, amount in sorted(
                by_model.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ],
        "recent": [
            {
                "account_id": movement.account_id,
                "movement_id": movement.movement_id,
                "kind": movement.kind,
                "amount_microdollars": movement.amount_microdollars,
                "amount_display": format_money_display(movement.amount_microdollars),
                "counterparty_account_id": movement.counterparty_account_id,
                "custom_model_id": movement.custom_model_id,
                "authorization_id": movement.authorization_id,
                "created_at": movement.created_at,
            }
            for movement in recent
        ],
    }


def _earnings_summary_shape(summary: dict[str, int]) -> dict[str, int | str]:
    return {
        "total_earned": summary["total_earned"],
        "total_earned_display": format_money_display(summary["total_earned"]),
        "total_transferred": summary["total_transferred"],
        "total_transferred_display": format_money_display(summary["total_transferred"]),
        "available": summary["available"],
        "available_display": format_money_display(summary["available"]),
    }
