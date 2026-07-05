from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from typing import Any, cast

from trusted_router.services.settle_outbox_apply import ApplyOutcome, apply_frozen_settle
from trusted_router.storage import STORE
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
from trusted_router.storage_models import SettleOutboxRow

logger = logging.getLogger(__name__)

# SF7 / §6: the drain fires NONE of the inline post-settle side effects:
# auto-refill, budget-alert emails, metadata broadcast, or provider-error
# benchmark recording. Accepted losses from the §6 addendum: drained
# generations never reach metadata-broadcast destinations, and drained refunds
# record no provider-error benchmark sample.


def spanner_settle_outbox() -> SpannerSettleOutbox:
    """Build the native-table settle outbox from the active Spanner store."""
    database = getattr(STORE, "_database", None)
    param_types = getattr(STORE, "_param_types", None)
    if database is None or param_types is None:
        raise RuntimeError("settle outbox drain requires the Spanner store")
    return SpannerSettleOutbox(database, param_types)


def drain_settle_outbox(limit: int) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    outbox = spanner_settle_outbox()
    rows = outbox.claim(limit=limit)
    outcomes: Counter[str] = Counter()
    recovered_micro = 0

    for row in rows:
        error_note: str | None = None
        try:
            outcome = apply_frozen_settle(row)
        except Exception as exc:  # noqa: BLE001 - generic drain handler; apply classifies known errors.
            outcome = ApplyOutcome.ERROR
            error_note = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "settle outbox apply failed authorization_id=%s intent_kind=%s",
                row.authorization_id,
                row.intent_kind,
            )
        outcomes[outcome] += 1
        try:
            _resolve_row(outbox, row, outcome, error_note=error_note)
        except Exception:  # noqa: BLE001 - keep one bad row from aborting the batch.
            # A Spanner blip during resolve must not abort the batch; unresolved
            # rows stay leased and are reclaimed after lease expiry.
            logger.exception(
                "settle outbox resolve failed authorization_id=%s intent_kind=%s",
                row.authorization_id,
                row.intent_kind,
            )
            outcomes["resolve_error"] += 1
            continue
        if outcome == ApplyOutcome.SETTLED_NOW and row.intent_kind == "settle":
            recovered_micro += int(row.actual_cost_micro)
    purged = outbox.purge_done()
    # §3/§4: free-release only expired settled=false holds whose authorization
    # has no pending/dead outbox row. Running after row resolution means a just-
    # recovered charge is already settled; the claim gate makes that ordering a
    # latency nicety, while the Increment-2 guard is the lost-charge interlock.
    # Limit 200 drains the ~2.6k wiring-time backlog in about an hour of 5-min
    # ticks without a tr_credit_balance write burst; steady state is far lower.
    reaped = cast(Any, STORE).reap_expired_reservations(
        now=dt.datetime.now(dt.UTC),
        limit=200,
    )
    if reaped > 0:
        logger.info("reaped %s expired reservations", reaped)

    return {
        "claimed": len(rows),
        "outcomes": dict(outcomes),
        "recovered_micro": recovered_micro,
        "purged": purged,
        "reaped": reaped,
    }


def _resolve_row(
    outbox: SpannerSettleOutbox,
    row: SettleOutboxRow,
    outcome: str,
    *,
    error_note: str | None,
) -> None:
    lease_owner = row.lease_owner
    if outcome == ApplyOutcome.SETTLED_NOW:
        outbox.mark(row.authorization_id, row.intent_kind, done=True, lease_owner=lease_owner)
        logger.info(
            "recovered settle charge authorization_id=%s actual_cost_micro=%s",
            row.authorization_id,
            row.actual_cost_micro,
        )
        return

    if outcome == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE:
        outbox.mark(row.authorization_id, row.intent_kind, done=True, lease_owner=lease_owner)
        if row.intent_kind == "refund" and row.actual_cost_micro > 0:
            logger.warning(
                "settle outbox review: kept charge beat refund intent authorization_id=%s",
                row.authorization_id,
            )
        return

    if outcome == ApplyOutcome.ALREADY_SETTLED_LEGACY:
        outbox.mark(row.authorization_id, row.intent_kind, done=True, lease_owner=lease_owner)
        if row.intent_kind == "settle" and outbox.get(row.authorization_id, "refund") is not None:
            logger.warning(
                "settle outbox review: legacy settled with sibling refund intent authorization_id=%s",
                row.authorization_id,
            )
        return

    if outcome == ApplyOutcome.ALREADY_RELEASED_FREE:
        if row.intent_kind == "settle":
            error = "already_released_free: settle charge was lost"
            outbox.mark(
                row.authorization_id,
                row.intent_kind,
                done=False,
                error=error,
                lease_owner=lease_owner,
                force_dead=True,
            )
            logger.error(
                "ALERT settle outbox lost charge authorization_id=%s actual_cost_micro=%s",
                row.authorization_id,
                row.actual_cost_micro,
            )
        else:
            outbox.mark(row.authorization_id, row.intent_kind, done=True, lease_owner=lease_owner)
        return

    if outcome == ApplyOutcome.RESERVATION_MISSING:
        outbox.mark(
            row.authorization_id,
            row.intent_kind,
            done=False,
            error="reservation_missing",
            lease_owner=lease_owner,
            force_dead=True,
        )
        logger.error(
            "ALERT settle outbox reservation missing authorization_id=%s reservation_id=%s",
            row.authorization_id,
            row.reservation_id,
        )
        return

    if outcome == ApplyOutcome.INVALID_ROW:
        outbox.mark(
            row.authorization_id,
            row.intent_kind,
            done=False,
            error="invalid_row",
            lease_owner=lease_owner,
            force_dead=True,
        )
        logger.warning(
            "settle outbox invalid frozen row authorization_id=%s intent_kind=%s",
            row.authorization_id,
            row.intent_kind,
        )
        return

    if outcome == ApplyOutcome.PARK_TYPED_UNAVAILABLE:
        outbox.park(
            row.authorization_id,
            row.intent_kind,
            lease_owner=lease_owner,
            note="typed store unavailable",
        )
        logger.warning(
            "settle outbox parked typed row authorization_id=%s intent_kind=%s",
            row.authorization_id,
            row.intent_kind,
        )
        return

    if outcome == ApplyOutcome.ERROR:
        status = outbox.mark(
            row.authorization_id,
            row.intent_kind,
            done=False,
            error=error_note or "apply_frozen_settle error",
            lease_owner=lease_owner,
        )
        if status == "dead":
            logger.error(
                "ALERT settle outbox exhausted retries authorization_id=%s intent_kind=%s",
                row.authorization_id,
                row.intent_kind,
            )
        return

    status = outbox.mark(
        row.authorization_id,
        row.intent_kind,
        done=False,
        error=f"unknown outcome: {outcome}",
        lease_owner=lease_owner,
    )
    if status == "dead":
        logger.error(
            "ALERT settle outbox exhausted retries authorization_id=%s intent_kind=%s",
            row.authorization_id,
            row.intent_kind,
        )
