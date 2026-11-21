"""The early release of a hold whose stream never started.

docs/design/fast-admission-streams.md section 13. A gateway that authorizes
a request and then exits without a settle or a refund leaves the hold until
the reaper's expiry, two hours on. For one class of request the control plane
can tell sooner that the request can no longer be charged: a stream the
gateway heartbeats. Its first heartbeat is sent before the response head, and
a refused first heartbeat ends the request with nothing delivered and no
settle, so a row of that class with no heartbeat on it has delivered nothing,
and once the row is terminal it never will.

This module is the rule and the lines that report on it, with no storage in
it: the reaper's transaction applies `ineligibility` under its strong reads
(storage_gcp_authorize), and the heartbeat and settle paths call the
observers, which write the evidence the rule is judged by.

Every test here keeps the hold on doubt. A field the rule reads must be
there, of the type authorize writes and with a value the rule knows; a row
that is not exactly a heartbeated stream as authorize leaves it is not of
the class.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from trusted_router.stage_d import parse_pricing_snapshot

log = logging.getLogger("trusted_router.unstarted_release")

#: The rule's name in logs and metrics, beside the reaper's own, "expired".
RULE = "unstarted_stream"

MODE_OFF = "off"
MODE_LOG = "log"
MODE_ON = "on"
MODES = (MODE_OFF, MODE_LOG, MODE_ON)

#: The deadline's default and its bounds, in seconds from the authorization's
#: creation. It is not a bound on a live stream's wait for its first
#: heartbeat: the gateway has none (20 s for each candidate before the last,
#: 300 s for the last, over a candidate list of any length). A stream whose
#: first heartbeat comes after the deadline is ended by the rule with
#: nothing delivered and nothing charged; the deadline only decides how
#: rare that is (design section 13.3). The ceiling keeps the rule ahead of
#: the reaper's two hours.
DEFAULT_AFTER_SECONDS = 900
MIN_AFTER_SECONDS = 600
MAX_AFTER_SECONDS = 6900
#: A request reservation's life (user_model_rules.GATEWAY_RESERVATION_TTL_SECONDS;
#: a test holds the two equal). The pass finds its candidates by the expiry
#: authorize wrote, which is the creation plus this.
RESERVATION_TTL_SECONDS = 2 * 60 * 60
#: The clocks the rule compares are control-plane instances' own: the
#: authorize's (the row's creation), the boot registration's (the
#: declaration's start) and the drain's (now). They are assumed to agree
#: within this many seconds, and every comparison is made that much later
#: than it reads: a row is old enough only this long after the deadline, and
#: a declaration counts only if it started this long before the row.
CLOCK_SKEW_ALLOWANCE_SECONDS = 60

# Why a row is left alone. Each is a count in the pass's line.
NOT_TYPED = "not_typed"
TERMINAL = "terminal"
BINDING = "binding"
OTHER_CLASS = "other_class"
MALFORMED = "malformed"
STARTED = "started"
YOUNG = "young"
BOOT_UNDECLARED = "boot_undeclared"
REASONS = (NOT_TYPED, TERMINAL, BINDING, OTHER_CLASS, MALFORMED, STARTED, YOUNG, BOOT_UNDECLARED)

#: The contradiction line's counter. The line starts "metric=<this> ", as
#: the drain's release counters do, so the settle drain worker's output
#: boundary keeps it (settle_drain_observability._COUNTED_METRICS) and one
#: log filter matches the line wherever it is written.
CONTRADICTION_METRIC = "unstarted_release_contradiction_total"
LATE_FIRST_HEARTBEAT = "late_first_heartbeat"
CHARGE_WITHOUT_HEARTBEAT = "charge_without_heartbeat"
#: A first heartbeat this share of the deadline after its authorization, and
#: not yet past it, is written too (INFO, no counter): the tail of the wait
#: for a first heartbeat, which the deadline has to stay clear of.
SLOW_FIRST_HEARTBEAT_SHARE = 3

_MISSING = object()


def _field(authorization: Any, name: str) -> Any:
    return getattr(authorization, name, _MISSING)


def class_reason(authorization: Any) -> str | None:
    """Why the row is not a stream the heartbeat contract can apply to, by
    the facts authorize froze on it, or None when it is one.

    Stage D's reason must be ``ok`` (a stream on chat.completions or
    responses, every candidate in credits, standard pricing, no priority
    tier, the typed backend, an accepted boot); any other reason is another
    class. An ``ok`` row must then be exactly as authorize writes one: local
    settlement, credits, no batch and no video pricing, a boot binding, a
    pricing snapshot that parses with candidates, and the token bounds a
    heartbeat is checked against. Anything missing, of another type or of
    another value is malformed, and the hold is kept.
    """
    if _field(authorization, "stage_d_reason") != "ok":
        return OTHER_CLASS
    usage_type = _field(authorization, "usage_type")
    boot_kid = _field(authorization, "stage_d_boot_kid")
    snapshot = _field(authorization, "pricing_snapshot")
    if (
        _field(authorization, "settlement") != "local"
        or str(getattr(usage_type, "value", usage_type)) != "Credits"
        or _field(authorization, "native_batch_eligible") is not False
        or _field(authorization, "video_pricing_snapshot") is not None
        or not isinstance(boot_kid, str)
        or not boot_kid
        or not isinstance(snapshot, str)
        or not _whole_number(_field(authorization, "stage_d_prompt_tokens"))
        or not _whole_number(_field(authorization, "stage_d_max_output_tokens"))
        or not _whole_number(_field(authorization, "estimated_microdollars"))
    ):
        return MALFORMED
    try:
        parse_pricing_snapshot(snapshot)
    except Exception:  # noqa: BLE001 - whatever the parser cannot read is not a snapshot
        return MALFORMED
    return None


def start_reason(authorization: Any) -> str | None:
    """Why the row is not one no heartbeat has reached, or None when it is:
    the sequence is the zero authorize writes for a row with a snapshot, and
    nothing else a heartbeat writes is there. A sequence above zero, or any
    of those written, is a started stream; a sequence that is missing or not
    a whole number is malformed."""
    seq = _field(authorization, "heartbeat_seq")
    written = any(
        _field(authorization, name) is not None
        for name in ("started_at", "heartbeat_at", "heartbeat_hash", "delivered_usage", "selected_endpoint_id")
    )
    if not _whole_number(seq):
        return STARTED if written else MALFORMED
    if seq > 0 or written:
        return STARTED
    return None


def heartbeating_stream(authorization: Any) -> bool:
    return class_reason(authorization) is None


def never_started(authorization: Any) -> bool:
    return start_reason(authorization) is None


def age_seconds(authorization: Any, now: datetime) -> float | None:
    """Seconds since the row's creation, or None when it cannot be read."""
    created = _instant(_field(authorization, "created_at"))
    if created is None:
        return None
    return (_utc(now) - created).total_seconds()


def declared_before(authorization: Any, declared_since: Callable[[str], Any]) -> bool:
    """Whether the row's boot was declaring usage heartbeats when the row
    was made: the boot's record says it has declared them at every
    registration since a time that is at least the skew allowance before
    the row's creation (GatewayBoot.usage_heartbeat_declared_since).

    The boot's latest declaration alone would not say so: a key can
    register again with another declaration. A start time before the row
    does, and it can be read at any later moment: a registration that
    withdraws the declaration clears the time, and one that restores it
    starts from then, which is after the row."""
    created = _instant(_field(authorization, "created_at"))
    boot_kid = _field(authorization, "stage_d_boot_kid")
    if created is None or not isinstance(boot_kid, str) or not boot_kid:
        return False
    since = _instant(declared_since(boot_kid))
    if since is None:
        return False
    return since + timedelta(seconds=CLOCK_SKEW_ALLOWANCE_SECONDS) <= created


def ineligibility(
    authorization: Any | None,
    *,
    reservation_id: str,
    now: datetime,
    after_seconds: int,
    declared_since: Callable[[str], Any],
) -> str | None:
    """Why the rule leaves this row alone, or None when it releases it.

    Every doubt keeps the hold: a row that cannot be read, a field that is
    not as authorize writes it, a creation time that cannot be parsed, and a
    boot whose declaration at the row's creation cannot be shown are each a
    reason. The boot is read last, so only a row that passed every other
    test costs that read.
    """
    if authorization is None:
        return NOT_TYPED
    if _field(authorization, "settled") is not False:
        return TERMINAL
    if _field(authorization, "credit_reservation_id") != reservation_id:
        return BINDING
    reason = class_reason(authorization) or start_reason(authorization)
    if reason is not None:
        return reason
    age = age_seconds(authorization, now)
    if age is None or age < int(after_seconds) + CLOCK_SKEW_ALLOWANCE_SECONDS:
        return YOUNG
    if not declared_before(authorization, declared_since):
        return BOOT_UNDECLARED
    return None


def observe_first_heartbeat(
    authorization: Any,
    *,
    seq: int,
    accepted: bool,
    mode: str,
    after_seconds: int,
    declared_since: Callable[[str], Any],
    now: datetime | None = None,
) -> None:
    """A first heartbeat's age, where the deadline has to care about it.

    Past the deadline it is a contradiction: the rule would have released
    the row by now. In log mode the heartbeat was accepted and the stream
    runs; with the rule on it was refused and the request ended with nothing
    delivered. From a third of the deadline on it is written without the
    counter, so the tail of the wait is seen before it reaches the deadline.
    ``authorization`` is the row as read before the heartbeat's transaction;
    a replay of a first heartbeat finds a started row and writes nothing."""
    if mode == MODE_OFF or int(seq) != 1:
        return
    try:
        if not heartbeating_stream(authorization) or not never_started(authorization):
            return
        age = age_seconds(authorization, now or datetime.now(UTC))
        if age is None or age * SLOW_FIRST_HEARTBEAT_SHARE < int(after_seconds):
            return
        if not declared_before(authorization, declared_since):
            return
        if age < int(after_seconds):
            log.info(
                "unstarted_release.slow_first_heartbeat age_seconds=%d after_seconds=%d mode=%s",
                int(age),
                int(after_seconds),
                mode,
            )
            return
        log.error(
            "metric=%s value=1 kind=%s accepted=%s age_seconds=%d after_seconds=%d mode=%s",
            CONTRADICTION_METRIC,
            LATE_FIRST_HEARTBEAT,
            str(bool(accepted)).lower(),
            int(age),
            int(after_seconds),
            mode,
            extra={"metric": CONTRADICTION_METRIC, "value": 1},
        )
    except Exception as exc:  # noqa: BLE001 - evidence must never fail a heartbeat
        log.warning("unstarted release heartbeat observation failed error_class=%s", type(exc).__name__)


def observe_settle(
    authorization: Any,
    *,
    success: bool,
    mode: str,
    declared_since: Callable[[str], Any],
    now: datetime | None = None,
) -> None:
    """A charging settle that arrived for a row of the heartbeated class
    with no heartbeat on it: the fact the rule rests on did not hold for
    this request, at any age. ``authorization`` is the row as the settle
    path read it; a replay of a settled stream has its heartbeats on it and
    is not one of these."""
    if mode == MODE_OFF or not success:
        return
    try:
        if not heartbeating_stream(authorization) or not never_started(authorization):
            return
        if not declared_before(authorization, declared_since):
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
    except Exception as exc:  # noqa: BLE001 - evidence must never fail a settle
        log.warning("unstarted release settle observation failed error_class=%s", type(exc).__name__)


def _whole_number(value: Any) -> bool:
    return type(value) is int and value >= 0


def _instant(value: Any) -> datetime | None:
    """An ISO-8601 string or a datetime as a UTC instant; None for anything
    else, or a string that does not parse."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return _utc(value)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
