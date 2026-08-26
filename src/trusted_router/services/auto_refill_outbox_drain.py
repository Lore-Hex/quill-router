"""Control-owned drain for auto-refill requests attached to settle outbox rows."""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any

from trusted_router.config import Settings
from trusted_router.services.auto_refill import (
    maybe_charge_after_settle,
    settlement_auto_refill_idempotency_key,
)
from trusted_router.services.settle_outbox_drain import spanner_settle_outbox
from trusted_router.storage import STORE
from trusted_router.storage_errors import is_transient_store_error
from trusted_router.synthetic.alerts import ops_alert

logger = logging.getLogger(__name__)

STALE_AFTER_SECONDS = 5 * 60
RETRY_AFTER_SECONDS = 5 * 60
TRANSIENT_FAILURE_ALERT_THRESHOLD = 3
TRANSIENT_FAILURE_REMINDER_INTERVAL = 20


@dataclass
class AutoRefillDrainPass:
    """Debounce retryable store failures without hiding a sustained outage."""

    consecutive_transient_failures: int = 0

    def run(self, settings: Settings, *, limit: int = 100) -> dict[str, Any] | None:
        try:
            result = drain_auto_refill_outbox(settings, limit=limit)
        except Exception as exc:
            if not is_transient_store_error(exc):
                raise
            self.consecutive_transient_failures += 1
            failures = self.consecutive_transient_failures
            if failures == TRANSIENT_FAILURE_ALERT_THRESHOLD or (
                failures > TRANSIENT_FAILURE_ALERT_THRESHOLD
                and failures % TRANSIENT_FAILURE_REMINDER_INTERVAL == 0
            ):
                logger.exception(
                    "auto-refill outbox storage unavailable repeatedly "
                    "consecutive_failures=%d",
                    failures,
                )
            else:
                logger.warning(
                    "auto-refill outbox transient storage failure; scheduled retry "
                    "consecutive_failures=%d error_type=%s",
                    failures,
                    type(exc).__name__,
                )
            return None

        if self.consecutive_transient_failures:
            logger.info(
                "auto-refill outbox recovered after %d transient failures",
                self.consecutive_transient_failures,
            )
            self.consecutive_transient_failures = 0
        return result


def drain_auto_refill_outbox(settings: Settings, *, limit: int = 100) -> dict[str, Any]:
    """Claim refill work and perform Stripe calls only on the control surface."""
    if settings.service_surface not in {"control", "combined"}:
        raise RuntimeError("auto-refill outbox drain is control-owned")
    limit = max(1, min(int(limit), 500))
    outbox = spanner_settle_outbox()
    oldest, depth = outbox.auto_refill_pending_freshness()
    stale_age_seconds = _age_seconds(oldest)
    if stale_age_seconds is not None and stale_age_seconds >= STALE_AFTER_SECONDS:
        ops_alert(
            "ALERT auto-refill outbox stale "
            f"oldest_age_seconds={int(stale_age_seconds)} depth={depth}; "
            "customers below threshold may not have been charged",
            fingerprint=["auto-refill-outbox", "stale"],
            tags={"queue_depth": str(depth)},
        )

    rows = outbox.claim_auto_refills(limit=limit)
    outcomes: Counter[str] = Counter()
    for row in rows:
        authorization = STORE.get_gateway_authorization(row.authorization_id)
        if authorization is None or not authorization.settled:
            outbox.resolve_auto_refill(
                row.authorization_id,
                lease_owner=str(row.lease_owner),
                done=False,
                error="settlement_not_finalized",
                retry_after_seconds=60,
                count_attempt=False,
            )
            outcomes["settlement_pending"] += 1
            continue
        try:
            outcome = maybe_charge_after_settle(
                row.workspace_id,
                settings=settings,
                idempotency_key=settlement_auto_refill_idempotency_key(row.authorization_id),
            )
        except Exception as exc:  # noqa: BLE001 - queue row must survive any unexpected bug.
            logger.exception(
                "auto-refill outbox apply failed authorization_id=%s",
                row.authorization_id,
            )
            status = outbox.resolve_auto_refill(
                row.authorization_id,
                lease_owner=str(row.lease_owner),
                done=False,
                error=f"{type(exc).__name__}: {exc}",
                retry_after_seconds=RETRY_AFTER_SECONDS,
            )
            outcomes["error"] += 1
            if status == "dead":
                _alert_dead(row.authorization_id, row.workspace_id)
            continue

        outcomes[outcome.reason] += 1
        status = outbox.resolve_auto_refill(
            row.authorization_id,
            lease_owner=str(row.lease_owner),
            done=not outcome.retryable,
            error=outcome.reason if outcome.retryable else None,
            retry_after_seconds=RETRY_AFTER_SECONDS,
            count_attempt=outcome.retryable,
        )
        if status == "dead":
            _alert_dead(row.authorization_id, row.workspace_id)

    return {
        "claimed": len(rows),
        "outcomes": dict(outcomes),
        "queue_depth": depth,
        "oldest_age_seconds": (
            int(stale_age_seconds) if stale_age_seconds is not None else None
        ),
    }


def _age_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return max(0.0, (dt.datetime.now(dt.UTC) - parsed).total_seconds())


def _alert_dead(authorization_id: str, workspace_id: str) -> None:
    ops_alert(
        "ALERT auto-refill outbox exhausted retries "
        f"authorization_id={authorization_id} workspace_id={workspace_id}",
        fingerprint=["auto-refill-outbox", "dead"],
        tags={"authorization_id": authorization_id, "workspace_id": workspace_id},
    )
