from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import Principal, SettingsDep, principal_from_request
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.money import microdollars_to_decimal
from trusted_router.schemas import CreditTransferRequest
from trusted_router.storage import STORE
from trusted_router.storage_models import CreditMovement, User, Workspace, workspace_billing_paused
from trusted_router.types import ErrorType


def require_credit_transfer_management(
    request: Request,
    settings: SettingsDep,
) -> Principal:
    """Require first-party authority; OAuth delegation never moves money."""
    principal = principal_from_request(request, settings)
    # app_id is the credential type marker. Do not reduce this to a scope
    # check: an old or malformed delegated key with empty scopes is still an
    # app credential and must remain unable to move the user's money.
    if principal.api_key is not None and principal.api_key.app_id:
        raise api_error(
            403,
            "A delegated key cannot transfer credits",
            ErrorType.FORBIDDEN,
        )
    if not principal.is_management:
        raise api_error(
            403,
            "Only a management key or active console session can transfer credits",
            ErrorType.FORBIDDEN,
        )
    return principal


CreditTransferPrincipal = Annotated[
    Principal, Depends(require_credit_transfer_management)
]


def register_credit_transfer_routes(router: APIRouter) -> None:
    @router.post("/credits/transfers")
    async def create_credit_transfer(
        body: CreditTransferRequest,
        principal: CreditTransferPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        movement, duplicate = await run_in_threadpool(
            execute_credit_transfer,
            principal,
            body,
            settings,
        )
        return JSONResponse(
            {"data": transfer_shape(movement)},
            status_code=200 if duplicate else 201,
        )

    @router.get("/credits/transfers")
    async def list_credit_transfers(
        principal: CreditTransferPrincipal,
    ) -> dict[str, list[dict[str, Any]]]:
        if not STORE.supports_user_credit_transfers():
            raise api_error(
                501,
                "This deployment cannot transfer credits",
                ErrorType.ENDPOINT_NOT_SUPPORTED,
            )
        movements = await run_in_threadpool(
            STORE.list_credit_movements,
            principal.workspace.id,
            kinds=["user_transfer_out", "user_transfer_in"],
            limit=100,
        )
        return {"data": transfer_shapes(movements)}


def execute_credit_transfer(
    principal: Principal,
    body: CreditTransferRequest,
    settings: Settings,
) -> tuple[CreditMovement, bool]:
    sender = _principal_user(principal)
    if principal.workspace.owner_user_id != sender.id:
        raise api_error(403, "Only the workspace owner can transfer credits", ErrorType.FORBIDDEN)
    _require_active_verified(sender, role="Sender")
    _require_active_workspace(principal.workspace, role="Sender")
    if not STORE.supports_user_credit_transfers():
        raise api_error(
            501,
            "This deployment cannot transfer credits",
            ErrorType.ENDPOINT_NOT_SUPPORTED,
        )

    # Evaluate eligibility even when the username lookup misses.
    recipient = STORE.find_user_by_username(
        body.recipient_username, fallback_user_id=sender.id
    )
    eligible = _eligible_recipient(recipient)
    if not eligible or recipient is None:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    if recipient.id == sender.id:
        raise api_error(400, "Credits cannot be sent to yourself", ErrorType.BAD_REQUEST)
    _require_active_verified(recipient, role="Recipient")
    recipient_workspace = _owned_credit_workspace(recipient)
    if principal.workspace.id == recipient_workspace.id:
        raise api_error(400, "Credits cannot be sent to the same workspace", ErrorType.BAD_REQUEST)
    owner = STORE.get_user(recipient_workspace.owner_user_id)
    owner_eligible = _eligible_recipient(owner)
    if not owner_eligible or owner is None or owner.id != recipient.id:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    _require_active_workspace(recipient_workspace, role="Recipient")

    transfer_id = _transfer_id(sender.id, principal.workspace.id, body.idempotency_key)
    outcome, movement = STORE.transfer_workspace_credits(
        principal.workspace.id,
        recipient_workspace.id,
        body.amount_microdollars,
        transfer_id,
        daily_cap_microdollars=settings.user_credit_transfer_daily_cap_microdollars,
    )
    if outcome == "paused":
        raise api_error(403, "Account is unavailable", ErrorType.FORBIDDEN)
    if outcome == "insufficient":
        raise api_error(
            402,
            "Insufficient available credits",
            ErrorType.INSUFFICIENT_CREDITS,
        )
    if outcome == "daily_limit":
        raise api_error(429, "Daily credit transfer limit exceeded", ErrorType.RATE_LIMITED)
    if movement is None:
        raise RuntimeError("credit transfer completed without its ledger movement")
    if outcome == "duplicate" and (
        abs(movement.amount_microdollars) != body.amount_microdollars
        or movement.counterparty_account_id != recipient_workspace.id
    ):
        raise api_error(409, "Idempotency key does not match transfer", ErrorType.BAD_REQUEST)
    return movement, outcome == "duplicate"


def transfer_shapes(movements: list[CreditMovement]) -> list[dict[str, Any]]:
    ids = list({wid for m in movements for wid in (m.account_id, m.counterparty_account_id) if wid})
    usernames = STORE.workspace_owner_usernames(ids)
    return [transfer_shape(m, usernames) for m in movements]


def transfer_shape(
    movement: CreditMovement, usernames: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    if usernames is None:
        usernames = STORE.workspace_owner_usernames(
            [movement.account_id, str(movement.counterparty_account_id or "")]
        )
    outgoing = movement.kind == "user_transfer_out"
    sender_workspace_id = (
        movement.account_id if outgoing else str(movement.counterparty_account_id or "")
    )
    recipient_workspace_id = (
        str(movement.counterparty_account_id or "") if outgoing else movement.account_id
    )
    amount = abs(movement.amount_microdollars)
    return {
        "id": movement.movement_id,
        "sender_username": usernames.get(sender_workspace_id),
        "recipient_username": usernames.get(recipient_workspace_id),
        "amount": microdollars_to_decimal(amount),
        "amount_microdollars": amount,
        "direction": "sent" if outgoing else "received",
        "created_at": movement.created_at,
    }


def _principal_user(principal: Principal) -> User:
    if principal.user is not None:
        return principal.user
    if principal.api_key is not None and principal.api_key.creator_user_id:
        user = STORE.get_user(principal.api_key.creator_user_id)
        if user is not None:
            return user
    raise api_error(403, "A user-owned credential is required", ErrorType.FORBIDDEN)


def _eligible_recipient(user: User | None) -> bool:
    candidate = user if user is not None else User(id="", email="")
    active = not (candidate.suspended or candidate.disabled or candidate.identity_status in {"suspended", "disabled"})
    verified = candidate.identity_verified
    return active and verified


def _require_active_verified(user: User, *, role: str) -> None:
    if user.suspended or user.disabled or user.identity_status in {"suspended", "disabled"}:
        raise api_error(403, f"{role} account is unavailable", ErrorType.FORBIDDEN)
    if not user.identity_verified:
        raise api_error(
            403,
            f"{role} identity verification is required",
            ErrorType.VERIFICATION_REQUIRED,
        )


def _require_active_workspace(workspace: Workspace, *, role: str) -> None:
    if workspace_billing_paused(workspace):
        raise api_error(403, "Account is unavailable", ErrorType.FORBIDDEN)
    if workspace.deleted or workspace.federated_home:
        raise api_error(403, f"{role} account is unavailable", ErrorType.FORBIDDEN)


def _owned_credit_workspace(user: User) -> Workspace:
    owned = sorted(
        (
            workspace
            for workspace in STORE.list_workspaces_for_user(user.id)
            if workspace.owner_user_id == user.id and not workspace.federated_home
        ),
        key=lambda workspace: (workspace.created_at, workspace.id),
    )
    if not owned:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return owned[0]


def _workspace_username(workspace_id: str) -> str | None:
    workspace = STORE.get_workspace(workspace_id)
    if workspace is None:
        return None
    user = STORE.get_user(workspace.owner_user_id)
    return user.username if user is not None else None


def _transfer_id(sender_user_id: str, workspace_id: str, key: str | None) -> str:
    if key is None:
        return f"user_transfer:{uuid.uuid4().hex}"
    digest = hashlib.sha256(
        f"{sender_user_id}\0{workspace_id}\0{key}".encode()
    ).hexdigest()
    return f"user_transfer:{digest}"


__all__ = [
    "CreditTransferPrincipal",
    "execute_credit_transfer",
    "register_credit_transfer_routes",
    "transfer_shape",
]
