"""Home-plane endpoint serving federated key records to peer planes.

This is the one place a standalone plane learns about a user it has never
seen. What it returns — and refuses to return — is the whole security
boundary:

  * NEVER `salt` or `secret_hash`. A peer verifies bearer tokens through
    the attested gateway using the lookup hash; it does not need the raw
    key material, and copying it would place home-issued secrets at rest
    in a second jurisdiction. Absent by construction, not by filtering.

  * NEVER credits or usage counters. Identity federates; money does not.
    A peer seeds a federated key at ZERO balance and requires an explicit
    transfer, which is what keeps the conservation law intact.

  * NEVER a management key. Those can mint keys and move money; a peer
    that could federate one could escalate itself.

  * Its OWN auth token, not the internal gateway token. Anything holding
    the gateway token can already call /internal/gateway/authorize and
    /settle. Reusing it here would mean one leaked secret grants both
    "spend money" and "enumerate the user directory".

The credit-transfer endpoint below shares this module but NOT that token, for
exactly the reason above: the peer token is the one federation secret that must
never move money, so being credited is gated on a separate
`federation_credit_inbound_token`. See trusted_router.credit_transfer for the
conservation invariant the endpoint upholds.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from trusted_router import credit_transfer
from trusted_router.auth import SettingsDep
from trusted_router.credit_transfer import validate_amount, validate_transfer_id
from trusted_router.errors import api_error
from trusted_router.routes.helpers import json_body
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.security import constant_time_equal
from trusted_router.services.credit_transfer import (
    CreditTransferUnavailable,
    cancel_credit_transfer,
    credit_transfer_client_from_settings,
    push_credit_transfer,
    recover_credit_transfers,
)
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


def require_federation_peer(request: Request, settings: Any) -> None:
    """Authenticate a peer plane.

    Distinct from require_internal_gateway on purpose — see the module
    docstring. A plane with no peer token configured serves nobody.
    """
    expected = getattr(settings, "federation_peer_token", "") or ""
    if not expected:
        raise api_error(
            403,
            "Federation is not enabled on this plane",
            ErrorType.FORBIDDEN,
        )
    supplied = request.headers.get("x-trustedrouter-federation-token") or ""
    if not constant_time_equal(supplied, expected):
        raise api_error(401, "Invalid federation peer token", ErrorType.UNAUTHORIZED)


def require_federation_credit_peer(request: Request, settings: Any) -> None:
    """Authenticate a plane that is trying to CREDIT this one.

    A distinct token from require_federation_peer. Sharing that one would hand
    the directory-read secret the power to change a balance, which is the
    precise coupling this module's docstring says must not exist.

    Empty token = this plane refuses every inbound transfer. Defaulting to
    "closed" means a deployment cannot be funded by accident, and a
    misconfigured peer fails loudly at setup instead of quietly moving money.
    """
    expected = getattr(settings, "federation_credit_inbound_token", "") or ""
    if not expected:
        raise api_error(
            403,
            "This plane does not accept federated credit transfers",
            ErrorType.FORBIDDEN,
        )
    supplied = request.headers.get("x-trustedrouter-federation-credit-token") or ""
    if not constant_time_equal(supplied, expected):
        raise api_error(401, "Invalid federation credit token", ErrorType.UNAUTHORIZED)


def register(router: APIRouter) -> None:
    @router.post("/internal/federation/resolve-key")
    async def federation_resolve_key(request: Request, settings: SettingsDep) -> dict[str, Any]:
        require_federation_peer(request, settings)
        body = await json_body(request)
        lookup_hash = str(body.get("api_key_lookup_hash") or "")
        if not lookup_hash:
            raise api_error(400, "api_key_lookup_hash is required", ErrorType.BAD_REQUEST)

        api_key = STORE.get_key_by_lookup_hash(lookup_hash)
        if api_key is None:
            # A genuine "no such key" — the peer caches this briefly so a
            # leaked key in a retry loop cannot become sustained
            # cross-border traffic. Distinct from a 5xx, which the peer
            # must treat as an outage and NOT cache.
            raise api_error(404, "Unknown API key", ErrorType.NOT_FOUND)

        if getattr(api_key, "management", False):
            raise api_error(
                403,
                "Management keys cannot be federated",
                ErrorType.FORBIDDEN,
            )

        workspace = STORE.get_workspace(api_key.workspace_id)
        if workspace is None:
            raise api_error(404, "Workspace is unavailable", ErrorType.NOT_FOUND)

        # An explicit allow-list. A field added to ApiKey later must be
        # opted in here deliberately — the alternative (serialize the
        # object and strip secrets) leaks the next secret-shaped field
        # somebody adds.
        return {
            "data": {
                "lookup_hash": api_key.lookup_hash,
                "key_hash": api_key.hash,
                "workspace_id": api_key.workspace_id,
                "name": getattr(api_key, "name", "") or "",
                "disabled": bool(getattr(api_key, "disabled", False)),
                "expires_at": getattr(api_key, "expires_at", None),
                "limit_microdollars": getattr(api_key, "limit_microdollars", None),
                "limit_daily_microdollars": getattr(api_key, "limit_daily_microdollars", None),
                "limit_weekly_microdollars": getattr(api_key, "limit_weekly_microdollars", None),
                "limit_monthly_microdollars": getattr(api_key, "limit_monthly_microdollars", None),
                "include_byok_in_limit": bool(
                    getattr(api_key, "include_byok_in_limit", True)
                ),
                "budget_alert_only": bool(getattr(api_key, "budget_alert_only", False)),
                "workspace_billing_paused": bool(
                    getattr(workspace, "billing_paused", False)
                ),
                # Lets the peer detect a changed record without diffing
                # every field, and lets a future revocation feed say
                # "anything older than X is stale".
                "revision": getattr(api_key, "updated_at", None)
                or getattr(api_key, "created_at", None),
            }
        }

    @router.post("/internal/federation/apply-usage")
    async def federation_apply_usage(request: Request, settings: SettingsDep) -> dict[str, Any]:
        """HOME side of deferred settlement: book a peer's recorded debt.

        The peer served a federated key's CREDITS traffic while this plane
        was unreachable and now delivers the frozen cost. Insert-once per
        (source_plane, authorization_id); replays get the RECORDED verdict.
        Debit-only: nothing on this path can create spendable credits.

        source_plane comes from WHICH token authenticated — the body never
        carries it, so one peer cannot claim under another's identity, and
        the insert-once key is anchored in possession of a secret rather
        than in text on the wire. (The credit-transfer route treats its
        body-supplied source as AUDIT ONLY for the same reason.)

        Every non-200 answer here carries a STRUCTURED code the peer's
        forwarder classifies on. A peer that receives a bare 404 (a rollback
        past this deploy, a proxy default page) must treat it as an outage —
        which is exactly what its classifier does with anything that lacks
        these codes.
        """
        from trusted_router.config import parse_settlement_inbound_tokens

        token_map = parse_settlement_inbound_tokens(
            getattr(settings, "federation_settlement_inbound_tokens", "") or ""
        )
        if not token_map:
            raise api_error(
                403,
                "This plane does not accept federated settlements",
                ErrorType.FORBIDDEN,
            )
        supplied = request.headers.get("x-trustedrouter-federation-settlement-token") or ""
        source_plane = ""
        for token, plane in token_map.items():
            if constant_time_equal(supplied, token):
                source_plane = plane
                break
        if not source_plane:
            raise api_error(
                401, "Invalid federation settlement token", ErrorType.UNAUTHORIZED
            )

        apply = getattr(STORE, "apply_federated_usage", None)
        if apply is None:
            raise api_error(
                501,
                "This plane's store cannot apply federated settlements",
                ErrorType.ENDPOINT_NOT_SUPPORTED,
            )

        body = await json_body(request)
        authorization_id = str(body.get("authorization_id") or "")
        workspace_id = str(body.get("workspace_id") or "")
        cost = body.get("cost_microdollars")
        if not authorization_id or len(authorization_id) > 128:
            raise api_error(400, "authorization_id is required", ErrorType.BAD_REQUEST)
        if not workspace_id:
            raise api_error(400, "workspace_id is required", ErrorType.BAD_REQUEST)
        if isinstance(cost, bool) or not isinstance(cost, int) or cost <= 0:
            raise api_error(
                400, "cost_microdollars must be a positive integer", ErrorType.BAD_REQUEST
            )

        outcome = await run_in_threadpool(
            lambda: apply(
                source_plane=source_plane,
                authorization_id=authorization_id,
                workspace_id=workspace_id,
                cost_microdollars=cost,
                daily_cap_microdollars=(
                    settings.federation_settlement_workspace_daily_cap_microdollars
                ),
            )
        )
        if outcome in {"applied", "already"}:
            return {"data": {"outcome": outcome, "authorization_id": authorization_id}}
        if outcome == "conflict":
            raise api_error(
                409,
                "This authorization id is recorded against different terms",
                ErrorType.SETTLEMENT_TERMS_CONFLICT,
            )
        if outcome == "workspace_unknown":
            raise api_error(
                404,
                "No such workspace on this plane",
                ErrorType.WORKSPACE_UNKNOWN,
            )
        if outcome == "clamped":
            raise api_error(
                429,
                "Daily federated-settlement cap reached for this workspace",
                ErrorType.SETTLEMENT_CLAMPED,
                headers={"Retry-After": "3600"},
            )
        raise api_error(500, f"Unrecognized outcome {outcome!r}", ErrorType.INTERNAL_ERROR)

    @router.post("/internal/federation/credit-transfer")
    async def federation_credit_transfer(
        request: Request, settings: SettingsDep
    ) -> dict[str, Any]:
        """DESTINATION side: decide one transfer id's fate, exactly once.

        The reply is a VERDICT, not an acknowledgement, and it is the source
        plane's only way to move a transfer out of escrow. Two properties make
        that safe:

          * The verdict is an insert-once row, so a redelivered accept credits
            once and a cancel that arrives after an accept is told "accepted".
          * The returned outcome is what was RECORDED, which may differ from
            what was asked. The caller must apply the answer, not its request.

        A 200 therefore means "this is the settled fate of that id". Every
        other status means "unknown" — see CreditTransferClient.deliver, which
        must not turn any of them into a rejection.
        """
        require_federation_credit_peer(request, settings)
        body = await json_body(request)
        try:
            transfer_id = validate_transfer_id(str(body.get("transfer_id") or ""))
            amount = validate_amount(_positive_int(body.get("amount_microdollars")))
        except ValueError as exc:
            raise api_error(400, str(exc), ErrorType.BAD_REQUEST) from exc
        workspace_id = str(body.get("workspace_id") or "")
        if not workspace_id:
            raise api_error(400, "workspace_id is required", ErrorType.BAD_REQUEST)
        action = str(body.get("action") or "accepted")
        if action not in {"accepted", "rejected"}:
            raise api_error(400, "action must be accepted or rejected", ErrorType.BAD_REQUEST)

        try:
            outcome = await run_in_threadpool(
                STORE.claim_credit_transfer,
                transfer_id=transfer_id,
                workspace_id=workspace_id,
                amount_microdollars=amount,
                # AUDIT ONLY. Free text from the wire; never used to authorize.
                source=str(body.get("source_plane") or ""),
                accept=action == "accepted",
            )
        except credit_transfer.CreditTransferConflict as exc:
            # The id is already recorded against DIFFERENT terms, so the stored
            # verdict does not answer this request. Refusing is the whole point:
            # replying "accepted" here would let a second source plane bank a
            # verdict it never earned, having already debited itself. 409 keeps
            # that plane's value escrowed and recoverable — the source treats
            # every non-200 as "unknown" and never as a rejection.
            raise api_error(409, str(exc), ErrorType.CONFLICT) from exc
        except ValueError as exc:
            # No local balance row: this workspace has not been federated here
            # yet. 409, not 400 — the request is well-formed and will succeed
            # once a key for that workspace has been resolved, and no claim was
            # recorded, so a retry is safe.
            raise api_error(409, str(exc), ErrorType.CONFLICT) from exc
        return {"data": {"transfer_id": transfer_id, "outcome": outcome}}

    @router.post("/internal/federation/credit-transfers")
    async def federation_open_credit_transfer(
        request: Request, settings: SettingsDep
    ) -> dict[str, Any]:
        """SOURCE side: move credits from this plane to the configured peer.

        Guarded by the INTERNAL GATEWAY token rather than a federation one:
        anything holding it can already authorize and settle, i.e. move money,
        so initiating a transfer grants it nothing new. The federation peer
        token deliberately cannot reach here.

        `transfer_id` is caller-supplied and is the idempotency key for the
        whole cross-plane move. A generated one would make every retry a new
        debit.
        """
        require_internal_gateway(request, settings)
        client = credit_transfer_client_from_settings(settings)
        if client is None:
            raise api_error(
                403,
                "This plane has no credit-transfer destination configured",
                ErrorType.FORBIDDEN,
            )
        body = await json_body(request)
        workspace_id = str(body.get("workspace_id") or "")
        if not workspace_id:
            raise api_error(400, "workspace_id is required", ErrorType.BAD_REQUEST)
        try:
            transfer_id = validate_transfer_id(str(body.get("transfer_id") or ""))
            amount = validate_amount(_positive_int(body.get("amount_microdollars")))
        except ValueError as exc:
            raise api_error(400, str(exc), ErrorType.BAD_REQUEST) from exc
        cancel = bool(body.get("cancel", False))

        try:
            if cancel:
                transfer = await run_in_threadpool(
                    cancel_credit_transfer, transfer_id=transfer_id, client=client
                )
            else:
                transfer = await run_in_threadpool(
                    push_credit_transfer,
                    transfer_id=transfer_id,
                    workspace_id=workspace_id,
                    amount_microdollars=amount,
                    client=client,
                )
        except KeyError as exc:
            raise api_error(404, "Unknown transfer id", ErrorType.NOT_FOUND) from exc
        except credit_transfer.CreditTransferConflict as exc:
            # MUST precede the ValueError arm below, which it subclasses.
            # Falling through to that arm reports a reused id as 402
            # "insufficient credits" under a message promising the id is still
            # usable — the opposite of the truth. This id already names a
            # DIFFERENT move whose escrow is debited and live, and an operator
            # who believed that message would retry with a fresh id and take a
            # SECOND debit. 409: pick another id, and go look at this one.
            raise api_error(409, str(exc), ErrorType.CONFLICT) from exc
        except ValueError as exc:
            # The conditional debit refused. Nothing was written, so the same
            # transfer id is still usable after a top-up.
            raise api_error(402, str(exc), ErrorType.INSUFFICIENT_CREDITS) from exc
        except CreditTransferUnavailable as exc:
            # 202, NOT 5xx: the escrow is durable and the value is safe on this
            # plane. Only the destination's verdict is missing, and the
            # recovery pass will collect it. Reporting failure here would
            # invite an operator to "retry" with a new transfer id, which is a
            # second debit.
            return {
                "data": {
                    "transfer_id": transfer_id,
                    "state": credit_transfer.ESCROWED,
                    "value_held_by": _value_held_by(credit_transfer.ESCROWED),
                    "detail": str(exc),
                }
            }
        return {
            "data": {
                "transfer_id": transfer.id,
                # Echoed from the RECORD, not the request. An operator reading
                # a "delivered" reply needs to see which workspace it is
                # delivered for; without this field a reply about somebody
                # else's transfer is indistinguishable from one about theirs.
                "workspace_id": transfer.workspace_id,
                "state": transfer.state,
                "amount_microdollars": transfer.amount_microdollars,
                "value_held_by": _value_held_by(transfer.state),
            }
        }

    @router.post("/internal/federation/credit-transfers/recover")
    async def federation_recover_credit_transfers(
        request: Request, settings: SettingsDep
    ) -> dict[str, Any]:
        """Re-ask the destination about every still-escrowed transfer.

        The crash-recovery path. Safe to run at any time and as often as
        wanted: the destination's verdict is insert-once, so asking twice
        cannot credit twice.
        """
        require_internal_gateway(request, settings)
        client = credit_transfer_client_from_settings(settings)
        if client is None:
            raise api_error(
                403,
                "This plane has no credit-transfer destination configured",
                ErrorType.FORBIDDEN,
            )
        body = await json_body(request)
        try:
            limit = _positive_int(body.get("limit", 100))
        except ValueError as exc:
            raise api_error(400, "limit must be an integer", ErrorType.BAD_REQUEST) from exc
        return {
            "data": await run_in_threadpool(
                recover_credit_transfers, client=client, limit=limit
            )
        }


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("value must be an integer")
    return value


def _value_held_by(state: str) -> str:
    """Say where the value is, in the response, in every state.

    An operator reading a transfer's status should never have to infer this.
    ESCROWED deliberately does NOT claim "source": the debit happened here, but
    the destination may already have accepted and the reply been lost, in which
    case this plane's view is stale. Only the destination's claim row can
    settle it, so the honest answer is "pending its verdict" — and an operator
    told that will run the recovery pass rather than assume the money is back.
    """
    if state == credit_transfer.DELIVERED:
        return "destination"
    if state == credit_transfer.RETURNED:
        return "source"
    return "escrow_pending_destination_verdict"
