"""A leased authorization's terminal, as the settle route and the drain
forward it to the fast path's front door (docs/design/fast-admission-pilot.md).

The route makes the terminal durable first (today's settle-outbox intent),
then asks the front door; the drain asks again later for an intent the front
door had not decided. Both read the answer the same way: the records say
what the fast path decided, never what the route asked.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
from dataclasses import dataclass

from trusted_router.services import fastpath_frontdoor as frontdoor
from trusted_router.storage_models import GatewayAuthorization, Generation
from trusted_router.types import UsageType

logger = logging.getLogger("trusted_router.fastpath")

SETTLED_OUTCOMES = frozenset({"settled", "reaped_snapshot"})
REFUNDED_OUTCOMES = frozenset({"refunded", "released"})


@dataclass(frozen=True)
class Decision:
    """What the fast path decided for the terminal: a booking of `charge`
    (success) or none, and the generation to record, carrying that charge."""

    success: bool
    charge: int
    generation: Generation | None


class Undecided(Exception):
    """The front door has not decided the terminal: `recorded` (durable in
    the drain log, the lease's order still to decide), `failed`, `invalid`,
    a winner of another kind, a booking answered to a refund (which has no
    settle body to record), or no front door. The caller keeps the intent
    pending and asks again later."""


def charge_for(*, success: bool, actual_cost: int, selected_usage_type: UsageType | str) -> int:
    """What the lease is told: the credit charge, or nothing for a BYOK
    terminal, which books no credit usage on today's path either."""
    credit = UsageType.coerce(selected_usage_type) == UsageType.CREDITS
    return int(actual_cost) if success and credit else 0


def full_record(authorization: GatewayAuthorization, *, generation: Generation | None, charge: int) -> bytes:
    """The full record the front door publishes for the auditor's pending
    work: the boot binding under "boot", as the pending worker reads it."""
    boot_kid = authorization.stage_d_boot_kid or ""
    if not boot_kid:
        logger.error("fastpath pilot terminal without a boot binding authorization_id=%s", authorization.id)
    return json.dumps(
        {
            "boot": base64.b64encode(boot_kid.encode("utf-8")).decode("ascii"),
            "kind": "settle",
            "authorization_id": authorization.id,
            "workspace_id": authorization.workspace_id,
            "generation_id": generation.id if generation is not None else None,
            "model": authorization.model_id,
            "charge": charge,
            "estimate": int(authorization.estimated_microdollars),
        },
        separators=(",", ":"),
    ).encode("utf-8")


def forward_terminal(
    client: frontdoor.FrontDoorClient | None,
    authorization: GatewayAuthorization,
    *,
    success: bool,
    charge: int,
    generation: Generation | None,
) -> Decision:
    """The terminal sent to the front door and its answer read as a decision.
    Raises Undecided when the fast path has not decided it."""
    if client is None:
        raise Undecided("no front door")
    envelope = str(authorization.fastpath_envelope)
    kind = "settle" if success else "refund"
    money = {"charge": charge, "estimate": int(authorization.estimated_microdollars)}
    try:
        if success:
            terminal = client.settle(
                envelope=envelope, charge=charge, full=full_record(authorization, generation=generation, charge=charge),
                money=money,
            )
        else:
            terminal = client.refund(envelope=envelope, money=money)
    except frontdoor.FrontDoorUnavailable as exc:
        raise Undecided(f"{kind}: no front door answered") from exc
    logger.info(
        "fastpath pilot terminal %s authorization_id=%s kind=%s won=%s charge=%d outcome=%s",
        terminal.status,
        authorization.id,
        kind,
        terminal.kind,
        terminal.charge,
        terminal.outcome,
    )
    if terminal.status == frontdoor.WON:
        if terminal.kind != kind:
            # Another terminal of this authorization won at the owner; its
            # own intent writes the records.
            raise Undecided(f"{kind}: lost to {terminal.kind}")
        booked_success, booked = success, int(terminal.charge)
        if booked != charge:
            logger.warning(
                "fastpath pilot winner's charge differs authorization_id=%s won=%d priced=%d",
                authorization.id,
                booked,
                charge,
            )
    elif terminal.status == frontdoor.SETTLED:
        # The lease closed before this terminal: the disposition is the
        # outcome. A booking's records need the settle's body, so only a
        # settle writes them.
        if terminal.outcome in SETTLED_OUTCOMES and terminal.cost_known:
            if not success:
                raise Undecided(f"{kind}: answered with a booking")
            booked_success, booked = True, int(terminal.cost)
        elif terminal.outcome in REFUNDED_OUTCOMES:
            booked_success, booked = False, 0
        else:
            raise Undecided(f"{kind}: closed lease with no outcome")
        logger.warning(
            "fastpath pilot terminal after the lease closed authorization_id=%s outcome=%s cost=%d priced=%d",
            authorization.id,
            terminal.outcome,
            booked,
            charge,
        )
    else:
        raise Undecided(f"{kind}: {terminal.status}")
    recorded = generation if booked_success else None
    if recorded is not None and recorded.total_cost_microdollars != booked:
        recorded = dataclasses.replace(recorded, total_cost_microdollars=booked)
    return Decision(success=booked_success, charge=booked, generation=recorded)
