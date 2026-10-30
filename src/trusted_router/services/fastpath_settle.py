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
import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any

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
    # REAPED_SNAPSHOT when a reap of the stream's snapshot won: the records
    # are the snapshot's, which the store builds from the row
    # (docs/design/fast-admission-streams.md section 6.3); None for the
    # terminal's own outcome.
    outcome: str | None = None


REAPED_SNAPSHOT = "reaped_snapshot"


class Undecided(Exception):
    """The front door has not decided the terminal: `recorded` (durable in
    the drain log, the lease's order still to decide), `failed`, `invalid`,
    a settle or a refund that won over the other kind, a closed lease's
    settle answered to a refund (which has no settle body to record), or no
    front door. The caller keeps the intent pending and asks again later."""


def _reaped(authorization: GatewayAuthorization, *, kind: str, cost: int, charge: int) -> Decision:
    """Decision 70: the stream's snapshot won, so a later terminal charges
    nothing more. The lease booked the reap; the records are the snapshot's,
    at the lease's cost, outcome reaped_snapshot, as today's reaper writes
    them and a later settle answers already_settled with them."""
    logger.warning(
        "fastpath pilot terminal after a reap authorization_id=%s kind=%s cost=%d priced=%d",
        authorization.id,
        kind,
        cost,
        charge,
    )
    return Decision(success=True, charge=int(cost), generation=None, outcome=REAPED_SNAPSHOT)


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
        if terminal.kind == "reap":
            return _reaped(authorization, kind=kind, cost=int(terminal.charge), charge=charge)
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
        # outcome. A reap's records are the snapshot's, whichever terminal
        # asks; a settle's booking needs the settle's body, so only a
        # settle writes it.
        if terminal.outcome == REAPED_SNAPSHOT and terminal.cost_known:
            return _reaped(authorization, kind=kind, cost=int(terminal.cost), charge=charge)
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


# What reconcile_reaped did with a leased stream's expired row.
RECONCILED_REAP = "reaped_snapshot"
RECONCILED_REFUND = "refunded"
RECONCILED_SETTLED_WITHOUT_INTENT = "settled_without_intent"
RECONCILED_PENDING = "pending"
RECONCILED_SKIPPED = "skipped"

# Past the hold's end of life by this much, a disposition still pending is
# told: the owner releases or reaps every hold by its end of life plus its
# grace (a minute), and the auditor drains a stopped owner's lease within
# its overdue allowance (ten minutes).
LEASED_REAP_OVERDUE = dt.timedelta(minutes=15)


def _end_of_life(authorization: GatewayAuthorization) -> dt.datetime | None:
    value = authorization.fastpath_end_of_life
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def reconcile_reaped(
    client: frontdoor.FrontDoorClient | None,
    store: Any,
    authorization_id: str,
    *,
    now: dt.datetime,
    grace_seconds: int,
) -> str:
    """A leased stream's row the reaper found expired with a heartbeat on it
    (docs/design/fast-admission-streams.md section 6.4): its hold is the
    lease's, which reaps it at the snapshot the owner holds, so Python never
    decides it and never sends a refund for it (in a draining lease a refund
    could come before the auditor's reap in the drain log and charge
    nothing). It reads the lease's disposition by the envelope and writes
    the records the lease decided, booking nothing:

    - reaped_snapshot with its cost: the snapshot's records at that cost;
    - refunded or released: refunded;
    - settled: a settle the lease booked with no Python intent left to
      write it (the reaper's scan passes over a row with a pending intent),
      told as fastpath.settled_without_intent and left;
    - anything else, a front door that does not answer, answers an error, or
      does not serve the read: pending, never refunded. The row's expiry is
      pushed out by the grace, so the next pass asks again, and a pending
      disposition past the hold's end of life plus LEASED_REAP_OVERDUE is
      told as fastpath.leased_reap_overdue.
    """
    authorization = store.get_gateway_authorization(authorization_id)
    if authorization is None or authorization.settled or authorization.fastpath_envelope is None:
        return RECONCILED_SKIPPED
    answer: frontdoor.Disposition | None = None
    if client is not None:
        try:
            answer = client.disposition(envelope=str(authorization.fastpath_envelope))
        except frontdoor.FrontDoorUnavailable as exc:
            logger.warning("fastpath leased reap disposition unanswered authorization_id=%s why=%s", authorization.id, exc)
    if answer is not None and answer.status == frontdoor.SETTLED:
        if answer.outcome == REAPED_SNAPSHOT and answer.cost_known:
            store.typed_finalize_leased_result(
                authorization.id,
                success=True,
                actual_microdollars=int(answer.cost),
                selected_usage_type=UsageType.CREDITS,
                generation=None,
                authorization_snapshot=authorization,
                outcome=REAPED_SNAPSHOT,
            )
            logger.info(
                "fastpath leased reap recorded authorization_id=%s cost=%d", authorization.id, int(answer.cost),
            )
            return RECONCILED_REAP
        if answer.outcome in REFUNDED_OUTCOMES:
            store.typed_finalize_leased_result(
                authorization.id,
                success=False,
                actual_microdollars=0,
                selected_usage_type=UsageType.CREDITS,
                generation=None,
                authorization_snapshot=authorization,
            )
            return RECONCILED_REFUND
        if answer.outcome == "settled":
            logger.error(
                "fastpath.settled_without_intent authorization_id=%s cost=%d cost_known=%s",
                authorization.id,
                int(answer.cost),
                answer.cost_known,
            )
            _push_expiry(store, authorization, now=now, grace_seconds=grace_seconds)
            return RECONCILED_SETTLED_WITHOUT_INTENT
    if answer is not None and answer.status == frontdoor.INVALID:
        logger.error("fastpath leased reap envelope refused authorization_id=%s", authorization.id)
    _push_expiry(store, authorization, now=now, grace_seconds=grace_seconds)
    end = _end_of_life(authorization)
    if end is not None and now > end + LEASED_REAP_OVERDUE:
        logger.error(
            "fastpath.leased_reap_overdue authorization_id=%s end_of_life=%s status=%s",
            authorization.id,
            authorization.fastpath_end_of_life,
            answer.status if answer is not None else "unanswered",
        )
    return RECONCILED_PENDING


def _push_expiry(store: Any, authorization: GatewayAuthorization, *, now: dt.datetime, grace_seconds: int) -> None:
    """The row's reservation expires again a grace from now, so the reaper's
    next pass asks the lease again and its scan is not filled by one row."""
    reservation_id = authorization.credit_reservation_id
    if reservation_id:
        store.extend_reservation_expiry(str(reservation_id), now + dt.timedelta(seconds=int(grace_seconds)))


def reconcile_reaped_leases(
    client: frontdoor.FrontDoorClient | None,
    store: Any,
    authorization_ids: tuple[str, ...] | list[str],
    *,
    now: dt.datetime,
    grace_seconds: int,
) -> dict[str, int]:
    """reconcile_reaped for each of a reaper pass's leased streams, one's
    failure that one's error alone, and the counts by outcome."""
    counts: dict[str, int] = {}
    for authorization_id in authorization_ids:
        try:
            outcome = reconcile_reaped(client, store, authorization_id, now=now, grace_seconds=grace_seconds)
        except Exception:  # noqa: BLE001 - one row's failure must not end the pass
            logger.exception("fastpath leased reap reconcile failed authorization_id=%s", authorization_id)
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts
