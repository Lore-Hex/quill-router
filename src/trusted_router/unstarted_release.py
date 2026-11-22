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

The rule judges the row as it is stored, not as it decodes: the decoder
fills a missing field with a default, and a default is not evidence. Every
field the rule reads must be there, of the type authorize writes and with a
value the rule knows; the only absences it accepts are the ones the
serializer makes on purpose (`OMITTED_WHEN_NONE`). Every doubt keeps the hold.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from trusted_router.stage_d import PRICING_DOCUMENT_VERSION

log = logging.getLogger("trusted_router.unstarted_release")

#: The rule's name in logs and metrics, beside the reaper's own, "expired".
RULE = "unstarted_stream"

MODE_OFF = "off"
MODE_LOG = "log"
MODE_ON = "on"
MODES = (MODE_OFF, MODE_LOG, MODE_ON)

#: The deadline's default and its bounds, in seconds from the reservation's
#: commit. It is not a bound on a live stream's wait for its first
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
#: authorize wrote, which is an application clock's reading plus this.
RESERVATION_TTL_SECONDS = 2 * 60 * 60
#: The rule's own comparisons use the database's clock alone: the
#: reservation's commit timestamp, the database's time at the scan, and the
#: commit timestamp of the boot's registration. An observer has no commit
#: timestamp to hand: it reads a first heartbeat's age from two application
#: clocks (the authorize's and its own), which are assumed to agree within
#: this many seconds, and calls the heartbeat late that much before the
#: deadline, so it reports one too many rather than one too few.
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

#: The fields the serializer leaves out when they are None
#: (storage_gcp_codec.json_body drops a None field whose default is None).
#: For these, and only these, the rule reads an absent member as None.
OMITTED_WHEN_NONE = (
    "video_pricing_snapshot",
    "started_at",
    "heartbeat_at",
    "heartbeat_hash",
    "delivered_usage",
    "selected_endpoint_id",
)

#: The contradiction line's counter. The line starts "metric=<this> ", as
#: the drain's release counters do, so the settle drain worker's output
#: boundary keeps it (settle_drain_observability._COUNTED_METRICS) and one
#: log filter matches the line wherever it is written.
CONTRADICTION_METRIC = "unstarted_release_contradiction_total"
LATE_FIRST_HEARTBEAT = "late_first_heartbeat"
CHARGE_WITHOUT_HEARTBEAT = "charge_without_heartbeat"
#: An observation that did not complete writes this counter instead, so a
#: window with no contradiction line can be told from a window in which the
#: evidence was not being gathered: an observer that could not finish
#: (where=heartbeat, where=settle), or a pass that did not examine every row
#: it was meant to (where=pass).
OBSERVATION_FAILED_METRIC = "unstarted_release_observation_failed_total"
#: A first heartbeat this share of the deadline after its authorization, and
#: not yet late, is written too (INFO, no counter): the tail of the wait
#: for a first heartbeat, which the deadline has to stay clear of.
SLOW_FIRST_HEARTBEAT_SHARE = 3

_MISSING: Any = object()


def class_reason(row: Mapping[str, Any]) -> str | None:
    """Why the stored row is not a stream the heartbeat contract can apply
    to, by the facts authorize froze on it, or None when it is one.

    Stage D's reason must be ``ok`` (a stream on chat.completions or
    responses, every candidate in credits, standard pricing, no priority
    tier, the typed backend, an accepted boot); any other reason is another
    class. An ``ok`` row must then be exactly as authorize writes one: local
    settlement, credits, not a batch, no video pricing, a boot binding, a
    pricing snapshot of the known shape, and the token bounds a heartbeat is
    checked against. A member that is missing, of another type or of another
    value is malformed, and the hold is kept.
    """
    if row.get("stage_d_reason", _MISSING) != "ok":
        return OTHER_CLASS
    usage_type = row.get("usage_type", _MISSING)
    boot_kid = row.get("stage_d_boot_kid", _MISSING)
    if (
        row.get("settlement", _MISSING) != "local"
        or type(usage_type) is not str
        or usage_type != "Credits"
        or row.get("native_batch_eligible", _MISSING) is not False
        or row.get("video_pricing_snapshot") is not None
        or type(boot_kid) is not str
        or not boot_kid
        or not _whole_number(row.get("stage_d_prompt_tokens", _MISSING))
        or not _whole_number(row.get("stage_d_max_output_tokens", _MISSING))
        or not _whole_number(row.get("estimated_microdollars", _MISSING))
        or not pricing_snapshot_valid(row.get("pricing_snapshot", _MISSING))
    ):
        return MALFORMED
    return None


def start_reason(row: Mapping[str, Any]) -> str | None:
    """Why the stored row is not one no heartbeat has reached, or None when
    it is: the sequence is the zero authorize writes for a row with a
    snapshot, and nothing else a heartbeat writes is there. A sequence above
    zero, or any of those written, is a started stream; a sequence that is
    missing or not a whole number is malformed."""
    seq = row.get("heartbeat_seq", _MISSING)
    written = (
        row.get("started_at") is not None
        or row.get("heartbeat_at") is not None
        or row.get("heartbeat_hash") is not None
        or row.get("delivered_usage") is not None
        or row.get("selected_endpoint_id") is not None
    )
    if not _whole_number(seq):
        return STARTED if written else MALFORMED
    if seq > 0 or written:
        return STARTED
    return None


def pricing_snapshot_valid(snapshot: Any) -> bool:
    """Whether the text is a Stage D pricing document as authorize freezes
    one (stage_d.endpoint_pricing_document): version 1 as a number, kind
    ``endpoint``, and at least one candidate, each with an endpoint, a price
    history version, the four rates, its tiers, a request fee and a
    rounding rule, of the types the document writes."""
    if type(snapshot) is not str:
        return False
    try:
        document = json.loads(snapshot)
    except ValueError:
        return False
    if (
        type(document) is not dict
        or type(document.get("v")) is not int
        or document["v"] != PRICING_DOCUMENT_VERSION
        or document.get("kind") != "endpoint"
    ):
        return False
    candidates = document.get("candidates")
    if type(candidates) is not list or not candidates:
        return False
    return all(_candidate_valid(candidate) for candidate in candidates)


def _candidate_valid(candidate: Any) -> bool:
    if type(candidate) is not dict:
        return False
    endpoint_id = candidate.get("endpoint_id")
    rounding = candidate.get("rounding")
    tiers = candidate.get("tiers")
    if (
        type(endpoint_id) is not str
        or not endpoint_id
        or type(rounding) is not str
        or not rounding
        or not _whole_number(candidate.get("price_history_version"))
        or not _whole_number(candidate.get("request_fee_micro"))
        or not _rates_valid(candidate.get("rates"))
        or type(tiers) is not list
    ):
        return False
    for tier in tiers:
        if type(tier) is not dict or not _rates_valid(tier.get("rates")):
            return False
        bound = tier.get("max_prompt_tokens", _MISSING)
        if bound is not None and not _whole_number(bound):
            return False
    return True


def _rates_valid(rates: Any) -> bool:
    return type(rates) is dict and all(
        _whole_number(rates.get(name))
        for name in (
            "input_micro_per_million",
            "output_micro_per_million",
            "cached_input_micro_per_million",
            "cache_creation_micro_per_million",
        )
    )


def ineligibility(
    row: Mapping[str, Any] | None,
    *,
    reservation_id: str,
    created_at: Any,
    read_at: Any,
    after_seconds: int,
    declared_since: Callable[[str], Any],
) -> str | None:
    """Why the rule leaves this row alone, or None when it releases it.

    ``row`` is the authorization as stored (the reader's ``stored``: its
    payload's members, its typed columns over them, its ``settled``
    column), never the decoded object. ``created_at`` is the reservation's
    commit timestamp and ``read_at`` the database's own time when the pass
    read it: the age is the database's, with no application clock in it.
    ``declared_since`` reads, by a boot's key id, the commit time at which
    its unbroken run of registrations declaring usage heartbeats began; the
    row is of the class only if that is before the reservation's commit, by
    the same clock. That the boot declares them now would not say so: a key
    can register again with another declaration.

    Every doubt keeps the hold. The boot is read last, so only a row that
    passed every other test costs that read.
    """
    if not row:
        return NOT_TYPED
    if row.get("settled", _MISSING) is not False:
        return TERMINAL
    if row.get("credit_reservation_id", _MISSING) != reservation_id:
        return BINDING
    reason = class_reason(row) or start_reason(row)
    if reason is not None:
        return reason
    created = _instant(created_at)
    read = _instant(read_at)
    if created is None or read is None or (read - created).total_seconds() < int(after_seconds):
        return YOUNG
    started = _instant(declared_since(str(row["stage_d_boot_kid"])))
    if started is None or not started < created:
        return BOOT_UNDECLARED
    return None


def decoded_view(authorization: Any) -> dict[str, Any]:
    """The decoded authorization as the mapping the rule reads, for the
    observers, which have no stored row to hand. Its defaults are filled, so
    it can make a row look more like the class than its stored form does:
    an observer then reports one too many, never one too few. The rule's
    own decision never uses it."""
    row = dict(vars(authorization))
    usage_type = row.get("usage_type")
    row["usage_type"] = str(getattr(usage_type, "value", usage_type))
    return row


def age_seconds(authorization: Any, now: datetime) -> float | None:
    """Seconds since the row's creation by application clocks, or None when
    it cannot be read. For the observers' lines only."""
    created = _instant(getattr(authorization, "created_at", _MISSING))
    if created is None:
        return None
    return (_utc(now) - created).total_seconds()


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

    Late (at the deadline less the clock allowance, since this age is read
    from application clocks) it is a contradiction: the rule would have
    released the row by now. In log mode the heartbeat was accepted and the
    stream runs; with the rule on it was refused and the request ended with
    nothing delivered. From a third of the deadline on it is written
    without the counter, so the tail of the wait is seen before it reaches
    the deadline. ``authorization`` is the row as read before the
    heartbeat's transaction; a replay of a first heartbeat finds a started
    row and writes nothing."""
    if mode == MODE_OFF or int(seq) != 1:
        return
    try:
        # The cheap tests first: nearly every first heartbeat is seconds old
        # and stops at its age, before the snapshot is parsed.
        row = decoded_view(authorization)
        if start_reason(row) is not None:
            return
        age = age_seconds(authorization, now or datetime.now(UTC))
        if age is None or age * SLOW_FIRST_HEARTBEAT_SHARE < int(after_seconds):
            return
        if class_reason(row) is not None or declared_since(str(row["stage_d_boot_kid"])) is None:
            return
        if age < int(after_seconds) - CLOCK_SKEW_ALLOWANCE_SECONDS:
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
        observation_failed("heartbeat", exc)


def observe_settle(
    authorization: Any,
    *,
    charging: bool,
    mode: str,
    declared_since: Callable[[str], Any],
    now: datetime | None = None,
) -> None:
    """A charging settle that arrived for a row of the heartbeated class
    with no heartbeat on it: the fact the rule rests on did not hold for
    this request, at any age. ``charging`` is whether the settle carries a
    charge above zero (a settle of nothing loses nothing and is not one of
    these). ``authorization`` is the row as the settle path read it; a
    replay of a settled stream has its heartbeats on it and is not one
    either."""
    if mode == MODE_OFF or not charging:
        return
    try:
        # The cheap test first: a stream that heartbeated, which is every
        # ordinary stream's settle, stops here, before the snapshot is parsed.
        row = decoded_view(authorization)
        if start_reason(row) is not None or class_reason(row) is not None:
            return
        if declared_since(str(row["stage_d_boot_kid"])) is None:
            return
        age = age_seconds(authorization, now or datetime.now(UTC))
        log.error(
            "metric=%s value=1 kind=%s settled=%s age_seconds=%d mode=%s",
            CONTRADICTION_METRIC,
            CHARGE_WITHOUT_HEARTBEAT,
            str(bool(row.get("settled"))).lower(),
            int(age) if age is not None else -1,
            mode,
            extra={"metric": CONTRADICTION_METRIC, "value": 1},
        )
    except Exception as exc:  # noqa: BLE001 - evidence must never fail a settle
        observation_failed("settle", exc)


def observation_failed(where: str, exc: BaseException) -> None:
    """An observer that could not finish: counted, with the error's class
    and no text of it, so the gate for turning the rule on can require that
    the observers worked (docs/runbook.md). Never raises."""
    try:
        log.warning(
            "metric=%s value=1 where=%s error_class=%s",
            OBSERVATION_FAILED_METRIC,
            where,
            type(exc).__name__,
            extra={"metric": OBSERVATION_FAILED_METRIC, "value": 1},
        )
    except Exception:  # noqa: BLE001, S110 - a log handler must not fail a heartbeat or a settle
        pass


def observe_pass(*, mode: str, errors: int, truncated: bool, unguarded: bool, failed: bool = False) -> None:
    """A pass of the rule that did not examine every row it was meant to:
    a candidate it could not read or decide (``errors``), a scan that
    filled its limit (``truncated``: more rows crossed the deadline in the
    scan's window than one pass examines), a pass that could not ask the
    settle outbox (``unguarded``), or one that failed outright. In log mode
    the pass is itself an observation, and what it did not examine it did
    not report, so it is counted with the observers' own failures. A pass
    that examined everything writes nothing here. Never raises."""
    if not (failed or errors or truncated or unguarded):
        return
    try:
        log.warning(
            "metric=%s value=1 where=pass mode=%s errors=%d truncated=%s unguarded=%s failed=%s",
            OBSERVATION_FAILED_METRIC,
            mode,
            int(errors),
            str(bool(truncated)).lower(),
            str(bool(unguarded)).lower(),
            str(bool(failed)).lower(),
            extra={"metric": OBSERVATION_FAILED_METRIC, "value": 1},
        )
    except Exception:  # noqa: BLE001, S110 - a log handler must not fail the drain
        pass


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
