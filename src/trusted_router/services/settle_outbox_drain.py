from __future__ import annotations

import datetime as dt
import json
import logging
from collections import Counter
from typing import Any, cast

from trusted_router.services.settle_outbox_apply import ApplyOutcome, apply_frozen_settle
from trusted_router.storage import STORE
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
from trusted_router.storage_models import SettleOutboxRow, generation_id_for_authorization

logger = logging.getLogger(__name__)

# Six hours is long enough to ride out a real Bigtable outage on 60-second
# parks, but short enough that a permanently broken row cannot churn the drain
# forever before an operator takes over.
_ACTIVITY_REPAIR_MAX_AGE_SECONDS = 6 * 60 * 60
_ACTIVITY_PARK_NOTE = "bigtable activity index pending"

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
    # Operationally, INFO logs ship to Axiom via the scoped `trusted_router`
    # package logger (`TR_AXIOM_LOG_LEVEL`, default INFO), but Cloud Logging
    # still only has request logs. The durable health signals are request-log
    # tick latency plus the response's reaped/outcomes fields. See
    # docs/runbook.md#settle-outbox.
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

    if outcome == ApplyOutcome.ACTIVITY_PENDING:
        now = dt.datetime.now(dt.UTC)
        since = now
        since_text = now.isoformat().replace("+00:00", "Z")
        if isinstance(row.last_error, str) and row.last_error.startswith(
            _ACTIVITY_PARK_NOTE
        ):
            _note, since_marker, candidate_since_text = row.last_error.partition(
                "since="
            )
        else:
            since_marker = ""
            candidate_since_text = ""
        if since_marker:
            try:
                candidate_since = dt.datetime.fromisoformat(
                    candidate_since_text.replace("Z", "+00:00")
                )
                if (
                    candidate_since.tzinfo is None
                    or candidate_since.utcoffset() is None
                ):
                    raise ValueError("activity repair since must include a timezone")
            except (OverflowError, ValueError):
                # A malformed timestamp is a separate data bug. Start the
                # window now instead of risking a premature terminal transition.
                logger.warning(
                    "settle outbox activity repair has malformed since; parking "
                    "authorization_id=%s intent_kind=%s since=%r",
                    row.authorization_id,
                    row.intent_kind,
                    candidate_since_text,
                )
            else:
                since = candidate_since
                since_text = candidate_since_text

        # last_error is the activity-failure clock. PARK_TYPED_UNAVAILABLE
        # deliberately overwrites it and restarts this window: typed-store
        # outage time is not activity-failure time and must not consume this
        # repair budget.
        age_seconds = (now - since).total_seconds()
        if age_seconds > _ACTIVITY_REPAIR_MAX_AGE_SECONDS:
            # Dead preserves settle_body, the only typed activity-repair
            # evidence; done destroys it and makes the missing activity
            # permanently unrecoverable. Dead is the existing human-review
            # terminal, monitored by the status='dead' count and documented in
            # the runbook, and stops a full apply every 60 seconds. Keeping
            # terminal_at NULL is deliberate: the reservation and gateway
            # authorization are repair evidence, so retention stays pinned only
            # until an operator responds. Freezing the hold costs nothing
            # because the reservation is already settled. After fixing
            # Bigtable, the operator can set this row back to pending with
            # next_attempt_at in the past for due() to reclaim it.
            outbox.mark(
                row.authorization_id,
                row.intent_kind,
                done=False,
                error="activity_repair_expired",
                lease_owner=lease_owner,
                force_dead=True,
            )
            logger.error(
                "ALERT settle outbox activity repair expired "
                "authorization_id=%s generation_id=%s request_id=%s "
                "reservation_id=%s; CHARGE IS ALREADY APPLIED and Spanner is "
                "correct; only the per-request Bigtable activity row is missing; "
                "row is now dead with settle_body PRESERVED for repair; fix "
                "Bigtable, then set the row back to pending to let the drain retry",
                row.authorization_id,
                generation_id_for_authorization(row.authorization_id),
                _request_id(row),
                row.reservation_id,
            )
            return
        outbox.park(
            row.authorization_id,
            row.intent_kind,
            lease_owner=lease_owner,
            retry_after_seconds=60,
            note=f"{_ACTIVITY_PARK_NOTE} since={since_text}",
        )
        logger.warning(
            "settle outbox activity repair pending authorization_id=%s",
            row.authorization_id,
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

    if outcome == ApplyOutcome.RESOLVED_ZERO_COST_ELSEWHERE:
        outbox.mark(row.authorization_id, row.intent_kind, done=True, lease_owner=lease_owner)
        # Rare $0 race: money is correct and no alert is warranted, but this row
        # did not write a Generation. reconcile_generation_activity can repair
        # per-request records if needed; we intentionally avoid a bypass write
        # primitive for this edge case.
        logger.warning(
            "settle outbox warning: settle intent found reservation already zero-resolved "
            "authorization_id=%s reservation_id=%s likely reaper race; "
            "no generation record was written by this row",
            row.authorization_id,
            row.reservation_id,
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


def _request_id(row: SettleOutboxRow) -> str | None:
    try:
        body = json.loads(row.settle_body) if row.settle_body is not None else None
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(body, dict) or body.get("request_id") is None:
        return None
    return str(body["request_id"])
