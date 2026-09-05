from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections import Counter
from typing import Any, cast

from trusted_router.services.settle_outbox_apply import (
    _ACTIVITY_PARK_NOTE,
    ApplyOutcome,
    apply_frozen_settle,
)
from trusted_router.storage import STORE
from trusted_router.storage_errors import is_deterministic_store_error
from trusted_router.storage_gcp_authorize import ReapPassResult
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
from trusted_router.storage_models import SettleOutboxRow, generation_id_for_authorization
from trusted_router.synthetic.alerts import ops_alert

logger = logging.getLogger(__name__)

# Six hours is long enough to ride out a real Bigtable outage on 60-second
# parks, but short enough that a permanently broken row cannot churn the drain
# forever before an operator takes over.
_ACTIVITY_REPAIR_MAX_AGE_SECONDS = 6 * 60 * 60
_REAP_BURST_ALERT_THRESHOLD = 20

# Wall-clock budget for the row loop, below the 300s Cloud Run / Cloud
# Scheduler request deadline so a slow batch returns a partial result instead
# of a 504 that hides which rows were resolved. Rows not reached are reported
# as ``deferred`` and stay leased until the lease lapses.
_DRAIN_BUDGET_SECONDS = 240.0
# The claim lease must cover the whole budget (plus the claim pass itself) so a
# row cannot lose its lease mid-run and be resolved by two workers at once.
_DRAIN_LEASE_SECONDS = 300
# Deterministic Spanner rejections (a UNIQUE-index violation, a constraint or
# schema precondition, a malformed statement) fail identically on every replay
# until the code or data changes. Parking keeps the row pending (a
# GUARD_STATUSES member, so the reaper keeps the reservation frozen), does not
# burn attempts toward dead, and retries after a fix lands without operator
# SQL. Transient errors keep the attempts-counted exponential backoff.
_DETERMINISTIC_PARK_SECONDS = 3600

_monotonic = time.monotonic

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


def drain_settle_outbox(
    limit: int,
    *,
    reap_snapshot_booking_enabled: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    outbox = spanner_settle_outbox()
    rows = outbox.claim(limit=limit, lease_seconds=_DRAIN_LEASE_SECONDS)
    deadline = _monotonic() + _DRAIN_BUDGET_SECONDS
    outcomes: Counter[str] = Counter()
    recovered_micro = 0
    deferred = 0

    for row in rows:
        if _monotonic() >= deadline:
            # Out of budget: leave the row leased and untouched. Its lease
            # outlives this request, so the next tick reclaims it cleanly.
            deferred += 1
            continue
        error_note: str | None = None
        apply_error: Exception | None = None
        try:
            outcome = apply_frozen_settle(row)
        except Exception as exc:  # noqa: BLE001 - generic drain handler; apply classifies known errors.
            outcome = ApplyOutcome.ERROR
            apply_error = exc
            error_note = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "settle outbox apply failed authorization_id=%s intent_kind=%s",
                row.authorization_id,
                row.intent_kind,
            )
        outcomes[outcome] += 1
        try:
            _resolve_row(outbox, row, outcome, error_note=error_note, apply_error=apply_error)
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
    reap_now = dt.datetime.now(dt.UTC)
    rich_reaper = getattr(STORE, "reap_expired_reservations_result", None)
    if callable(rich_reaper):
        reap_result = cast(
            ReapPassResult,
            rich_reaper(
                now=reap_now,
                limit=200,
                snapshot_booking_enabled=reap_snapshot_booking_enabled,
            ),
        )
    else:
        reaped_count = cast(Any, STORE).reap_expired_reservations(
            now=reap_now,
            limit=200,
        )
        reap_result = ReapPassResult(count=int(reaped_count))
    logger.info(
        "gateway.reaped count=%s released_hold_micro=%s started_marker_share=%.6f "
        "snapshot_bookings=%s out_of_cohort_share=%.6f not_eligible=%s guarded=%s "
        "guard_lost=%s errors=%s refunded=%s",
        reap_result.count,
        reap_result.released_hold_micro,
        reap_result.started_marker_share,
        reap_result.snapshot_bookings,
        reap_result.out_of_cohort_share,
        reap_result.not_eligible,
        reap_result.guarded,
        reap_result.guard_lost,
        reap_result.errors,
        reap_result.refunded,
    )
    if reap_result.count >= _REAP_BURST_ALERT_THRESHOLD:
        ops_alert(
            "ALERT gateway reaper burst "
            f"count={reap_result.count} "
            f"released_hold_micro={reap_result.released_hold_micro} "
            f"snapshot_bookings={reap_result.snapshot_bookings}",
            fingerprint=["gateway", "reaped-burst"],
            tags={"reaped_count": str(reap_result.count)},
        )

    if deferred:
        logger.warning(
            "settle outbox drain budget exhausted claimed=%s deferred=%s budget_seconds=%s",
            len(rows),
            deferred,
            _DRAIN_BUDGET_SECONDS,
        )

    return {
        "claimed": len(rows),
        "outcomes": dict(outcomes),
        "recovered_micro": recovered_micro,
        "purged": purged,
        "reaped": reap_result.count,
        "deferred": deferred,
    }


def _resolve_row(
    outbox: SpannerSettleOutbox,
    row: SettleOutboxRow,
    outcome: str,
    *,
    error_note: str | None,
    apply_error: Exception | None = None,
) -> None:
    lease_owner = row.lease_owner
    # Benign done transitions may ignore a lost fence: the winner re-runs the
    # idempotent apply and marks the row done.
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
        candidate_since_text = _activity_since_text(row.last_error)
        if candidate_since_text is not None:
            try:
                candidate_since = dt.datetime.fromisoformat(
                    candidate_since_text.replace("Z", "+00:00")
                )
                if candidate_since.tzinfo is None or candidate_since.utcoffset() is None:
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
                if candidate_since > now:
                    # A bad writer clock must not create a negative-age repair
                    # window that can never expire; start its clock locally.
                    logger.warning(
                        "settle outbox activity repair has future since; clamping "
                        "authorization_id=%s intent_kind=%s since=%r",
                        row.authorization_id,
                        row.intent_kind,
                        candidate_since_text,
                    )
                else:
                    since = candidate_since
                    since_text = candidate_since_text

        # last_error carries the continuous unrepaired-activity clock. The
        # activity note survives PARK_TYPED_UNAVAILABLE below, making this a
        # genuine overall bound instead of a per-outage window. Typed-store
        # outage time deliberately counts: throughout that outage the activity
        # index is genuinely still unrepaired.
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
            status = outbox.mark(
                row.authorization_id,
                row.intent_kind,
                done=False,
                error="activity_repair_expired",
                lease_owner=lease_owner,
                force_dead=True,
            )
            if status != "dead":
                # The status='dead' monitor is the source of truth. A duplicate
                # or contradictory page is worse than none; the worker that
                # actually wins this row will emit the alert.
                logger.warning(
                    "settle outbox activity repair escalation skipped because "
                    "row is no longer claimable by this owner "
                    "authorization_id=%s intent_kind=%s",
                    row.authorization_id,
                    row.intent_kind,
                )
                return
            ops_alert(
                "ALERT settle outbox activity repair expired "
                f"authorization_id={row.authorization_id} "
                f"generation_id={generation_id_for_authorization(row.authorization_id)} "
                f"request_id={_request_id(row)} "
                f"reservation_id={row.reservation_id}; CHARGE IS ALREADY APPLIED and Spanner is "
                "correct; only the per-request Bigtable activity row is missing; "
                "row is now dead with settle_body PRESERVED for repair; fix "
                "Bigtable, then set the row back to pending to let the drain retry",
                fingerprint=["settle-outbox", "activity-repair-expired"],
                tags={"authorization_id": row.authorization_id},
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

    if outcome == ApplyOutcome.REAPED_SNAPSHOT:
        outbox.mark(row.authorization_id, row.intent_kind, done=True, lease_owner=lease_owner)
        telemetry = "settle_lost" if row.intent_kind == "settle" else "refund_lost"
        logger.warning(
            "gateway.%s authorization_id=%s disposition=reaped_snapshot late_actual_cost_micro=%s",
            telemetry,
            row.authorization_id,
            row.actual_cost_micro,
        )
        return

    if outcome == ApplyOutcome.RESOLVED_ZERO_COST_ELSEWHERE:
        outbox.mark(row.authorization_id, row.intent_kind, done=True, lease_owner=lease_owner)
        # Rare $0 race: the money path is unchanged and no alert is warranted.
        # Apply verified the idempotent Bigtable activity write before resolving
        # without creating a Spanner billing Generation.
        logger.warning(
            "settle outbox warning: settle intent found reservation already zero-resolved "
            "authorization_id=%s reservation_id=%s likely reaper race; "
            "activity index verified without a Spanner billing write",
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
            status = outbox.mark(
                row.authorization_id,
                row.intent_kind,
                done=False,
                error=error,
                lease_owner=lease_owner,
                force_dead=True,
            )
            if status != "dead":
                # The winner re-derives the observation and alerts exactly once;
                # a stale duplicate or contradictory page is worse than none.
                logger.warning(
                    "settle outbox resolution skipped because row was no longer "
                    "claimable by this owner authorization_id=%s intent_kind=%s",
                    row.authorization_id,
                    row.intent_kind,
                )
                return
            ops_alert(
                f"ALERT settle outbox lost charge authorization_id={row.authorization_id} "
                f"actual_cost_micro={row.actual_cost_micro}",
                fingerprint=["settle-outbox", "lost-charge"],
                tags={"authorization_id": row.authorization_id},
            )
        else:
            outbox.mark(row.authorization_id, row.intent_kind, done=True, lease_owner=lease_owner)
        return

    if outcome == ApplyOutcome.RESERVATION_MISSING:
        status = outbox.mark(
            row.authorization_id,
            row.intent_kind,
            done=False,
            error="reservation_missing",
            lease_owner=lease_owner,
            force_dead=True,
        )
        if status != "dead":
            # The winner re-derives the observation and alerts exactly once;
            # a stale duplicate or contradictory page is worse than none.
            logger.warning(
                "settle outbox resolution skipped because row was no longer "
                "claimable by this owner authorization_id=%s intent_kind=%s",
                row.authorization_id,
                row.intent_kind,
            )
            return
        ops_alert(
            f"ALERT settle outbox reservation missing authorization_id={row.authorization_id} "
            f"reservation_id={row.reservation_id}",
            fingerprint=["settle-outbox", "reservation-missing"],
            tags={"authorization_id": row.authorization_id},
        )
        return

    if outcome == ApplyOutcome.INVALID_ROW:
        status = outbox.mark(
            row.authorization_id,
            row.intent_kind,
            done=False,
            error="invalid_row",
            lease_owner=lease_owner,
            force_dead=True,
        )
        if status != "dead":
            # The winner re-derives the observation and warns exactly once; a
            # stale duplicate or contradictory warning is worse than none.
            logger.warning(
                "settle outbox resolution skipped because row was no longer "
                "claimable by this owner authorization_id=%s intent_kind=%s",
                row.authorization_id,
                row.intent_kind,
            )
            return
        logger.warning(
            "settle outbox invalid frozen row authorization_id=%s intent_kind=%s",
            row.authorization_id,
            row.intent_kind,
        )
        return

    if outcome == ApplyOutcome.PARK_TYPED_UNAVAILABLE:
        note = "typed store unavailable"
        activity_since_text = _activity_since_text(row.last_error)
        if activity_since_text is not None:
            note = f"{_ACTIVITY_PARK_NOTE} since={activity_since_text}"
        outbox.park(
            row.authorization_id,
            row.intent_kind,
            lease_owner=lease_owner,
            note=note,
        )
        logger.warning(
            "settle outbox parked typed row authorization_id=%s intent_kind=%s",
            row.authorization_id,
            row.intent_kind,
        )
        return

    if outcome == ApplyOutcome.ERROR:
        if apply_error is not None and is_deterministic_store_error(apply_error):
            # Replaying a transaction that Spanner rejects deterministically
            # burns the drain's budget (each replay re-takes and re-orphans
            # the same locks) and walks the row toward dead in 8 ticks. Park
            # it: status stays 'pending' (hold frozen), attempts unchanged.
            outbox.park(
                row.authorization_id,
                row.intent_kind,
                lease_owner=lease_owner,
                retry_after_seconds=_DETERMINISTIC_PARK_SECONDS,
                note=error_note or "apply_frozen_settle error",
            )
            logger.warning(
                "settle outbox parked deterministic apply error authorization_id=%s "
                "intent_kind=%s error=%s",
                row.authorization_id,
                row.intent_kind,
                type(apply_error).__name__,
            )
            return
        status = outbox.mark(
            row.authorization_id,
            row.intent_kind,
            done=False,
            error=error_note or "apply_frozen_settle error",
            lease_owner=lease_owner,
        )
        if status == "dead":
            ops_alert(
                f"ALERT settle outbox exhausted retries authorization_id={row.authorization_id} "
                f"intent_kind={row.intent_kind}",
                fingerprint=["settle-outbox", "exhausted-retries"],
                tags={"authorization_id": row.authorization_id, "intent_kind": row.intent_kind},
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
        ops_alert(
            f"ALERT settle outbox exhausted retries authorization_id={row.authorization_id} "
            f"intent_kind={row.intent_kind}",
            fingerprint=["settle-outbox", "exhausted-retries"],
            tags={"authorization_id": row.authorization_id, "intent_kind": row.intent_kind},
        )


def _activity_since_text(last_error: str | None) -> str | None:
    if not isinstance(last_error, str):
        return None
    prefix = f"{_ACTIVITY_PARK_NOTE} since="
    if not last_error.startswith(prefix):
        return None
    return last_error.removeprefix(prefix)


def _request_id(row: SettleOutboxRow) -> str | None:
    try:
        body = json.loads(row.settle_body) if row.settle_body is not None else None
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(body, dict) or body.get("request_id") is None:
        return None
    return str(body["request_id"])
