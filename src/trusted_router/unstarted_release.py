"""The early release of a hold whose stream never started.

docs/design/fast-admission-streams.md section 13. A gateway that authorizes
a request and then exits without a settle or a refund leaves the hold until
the reaper's expiry, two hours on. For one class of request the control plane
can tell much sooner that the request can no longer be charged: a stream the
gateway heartbeats. Its first heartbeat is sent before the response head, and
a refused first heartbeat ends the request with nothing delivered and no
settle, so a row of that class with no heartbeat on it has delivered nothing,
and once the row is terminal it never will.

This module is the rule and the lines that report on it, with no storage in
it: the reaper's transaction applies `ineligibility` under its strong reads
(storage_gcp_authorize), and the heartbeat and settle routes call the two
observers, which write the evidence the rule is judged by.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("trusted_router.unstarted_release")

#: The rule's name in logs and metrics, beside the reaper's own, "expired".
RULE = "unstarted_stream"

MODE_OFF = "off"
MODE_LOG = "log"
MODE_ON = "on"
MODES = (MODE_OFF, MODE_LOG, MODE_ON)

#: The deadline's default and its bounds, in seconds from the authorization's
#: creation. The gateway waits at most 300 s for the first byte of a
#: stream's last candidate and 20 s for each candidate before it, then sends
#: the first heartbeat within 5 s; an authorize is retried inside 28 s. Ten
#: minutes is the floor: under it a stream whose only candidate took the
#: whole 300 s after a retried authorize would be left a margin of less than
#: four minutes. The ceiling keeps the rule ahead of the reaper's two hours.
DEFAULT_AFTER_SECONDS = 900
MIN_AFTER_SECONDS = 600
MAX_AFTER_SECONDS = 6900
#: A request reservation's life (user_model_rules.GATEWAY_RESERVATION_TTL_SECONDS;
#: a test holds the two equal). The pass finds its candidates by the expiry
#: authorize wrote, which is the creation plus this.
RESERVATION_TTL_SECONDS = 2 * 60 * 60

# Why a row is left alone. Each is a count in the pass's line.
NOT_TYPED = "not_typed"
TERMINAL = "terminal"
BINDING = "binding"
OTHER_CLASS = "other_class"
STARTED = "started"
YOUNG = "young"
BOOT_UNDECLARED = "boot_undeclared"
REASONS = (NOT_TYPED, TERMINAL, BINDING, OTHER_CLASS, STARTED, YOUNG, BOOT_UNDECLARED)

#: The contradiction line's counter. The line starts "metric=<this> ", as
#: the drain's release counters do, so the settle drain worker's output
#: boundary keeps it (settle_drain_observability._COUNTED_METRICS) and one
#: log filter matches the line wherever it is written.
CONTRADICTION_METRIC = "unstarted_release_contradiction_total"
LATE_FIRST_HEARTBEAT = "late_first_heartbeat"
CHARGE_WITHOUT_HEARTBEAT = "charge_without_heartbeat"


def heartbeating_stream(authorization: Any) -> bool:
    """Whether the row is of the class the gateway heartbeats, by the facts
    frozen on it at authorize: Stage D's reason is ``ok`` (a stream on
    chat.completions or responses, every candidate in credits, standard
    pricing, no priority tier, the typed backend, an accepted boot), with
    the pricing snapshot and the boot binding a heartbeat needs. Whether
    that boot heartbeats is the caller's read (``heartbeat_declared``)."""
    return bool(
        getattr(authorization, "settlement", "local") == "local"
        and getattr(authorization, "stage_d_reason", None) == "ok"
        and getattr(authorization, "pricing_snapshot", None) is not None
        and getattr(authorization, "stage_d_boot_kid", None)
        and not getattr(authorization, "native_batch_eligible", False)
        and getattr(authorization, "video_pricing_snapshot", None) is None
    )


def never_started(authorization: Any) -> bool:
    """No heartbeat reached the row: none of the four things only a
    heartbeat writes is on it."""
    return bool(
        getattr(authorization, "started_at", None) is None
        and int(getattr(authorization, "heartbeat_seq", None) or 0) == 0
        and getattr(authorization, "heartbeat_at", None) is None
        and getattr(authorization, "delivered_usage", None) is None
    )


def age_seconds(authorization: Any, now: datetime) -> float | None:
    """Seconds since the row's creation, or None when it cannot be read."""
    created = getattr(authorization, "created_at", None)
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(created, datetime):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (_utc(now) - created.astimezone(UTC)).total_seconds()


def ineligibility(
    authorization: Any | None,
    *,
    reservation_id: str,
    now: datetime,
    after_seconds: int,
    heartbeat_declared: Callable[[str], bool],
) -> str | None:
    """Why the rule leaves this row alone, or None when it releases it.

    Every doubt keeps the hold: a row that cannot be read, a creation time
    that cannot be parsed and a boot whose declaration cannot be read are
    each a reason. The boot is read last, so only a row that passed every
    other test costs that read.
    """
    if authorization is None:
        return NOT_TYPED
    if getattr(authorization, "settled", False):
        return TERMINAL
    if getattr(authorization, "credit_reservation_id", None) != reservation_id:
        return BINDING
    if not heartbeating_stream(authorization):
        return OTHER_CLASS
    if not never_started(authorization):
        return STARTED
    age = age_seconds(authorization, now)
    if age is None or age < int(after_seconds):
        return YOUNG
    if not heartbeat_declared(str(authorization.stage_d_boot_kid)):
        return BOOT_UNDECLARED
    return None


def observe_first_heartbeat(
    authorization: Any,
    *,
    seq: int,
    accepted: bool,
    mode: str,
    after_seconds: int,
    heartbeat_declared: Callable[[str], bool],
    now: datetime | None = None,
) -> None:
    """A first heartbeat that arrived for a row the rule would have released
    by now: the deadline was too short for this request. In log mode the
    heartbeat was accepted and the stream runs; with the rule on it was
    refused and the request ended with nothing delivered. ``authorization``
    is the row as read before the heartbeat's transaction."""
    if mode == MODE_OFF or int(seq) != 1:
        return
    try:
        if not heartbeating_stream(authorization) or not never_started(authorization):
            return
        age = age_seconds(authorization, now or datetime.now(UTC))
        if age is None or age < int(after_seconds):
            return
        if not heartbeat_declared(str(authorization.stage_d_boot_kid)):
            return
        log.error(
            "metric=%s value=1 kind=%s accepted=%s age_seconds=%d mode=%s",
            CONTRADICTION_METRIC,
            LATE_FIRST_HEARTBEAT,
            str(bool(accepted)).lower(),
            int(age),
            mode,
            extra={"metric": CONTRADICTION_METRIC, "value": 1},
        )
    except Exception:  # noqa: BLE001 - evidence must never fail a heartbeat
        log.warning("unstarted release heartbeat observation failed", exc_info=True)


def observe_settle(
    authorization: Any,
    *,
    success: bool,
    mode: str,
    heartbeat_declared: Callable[[str], bool],
    now: datetime | None = None,
) -> None:
    """A charging settle that arrived for a row of the heartbeated class
    with no heartbeat on it: the fact the rule rests on did not hold for
    this request, at any age. ``authorization`` is the row as the settle
    route read it; a replay of a settled stream has its heartbeats on it and
    is not one of these."""
    if mode == MODE_OFF or not success:
        return
    try:
        if not heartbeating_stream(authorization) or not never_started(authorization):
            return
        if not heartbeat_declared(str(authorization.stage_d_boot_kid)):
            return
        age = age_seconds(authorization, now or datetime.now(UTC))
        log.error(
            "metric=%s value=1 kind=%s settled=%s age_seconds=%d mode=%s",
            CONTRADICTION_METRIC,
            CHARGE_WITHOUT_HEARTBEAT,
            str(bool(getattr(authorization, "settled", False))).lower(),
            int(age) if age is not None else -1,
            mode,
            extra={"metric": CONTRADICTION_METRIC, "value": 1},
        )
    except Exception:  # noqa: BLE001 - evidence must never fail a settle
        log.warning("unstarted release settle observation failed", exc_info=True)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
