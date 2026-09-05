"""The ``analytics`` section of ``/status.json``: is this cloud's drain alive?

Why this exists as a *published* field rather than a check against the node
-------------------------------------------------------------------------

On the AWS-EU cloud the analytics pipeline is

    settle -> tr_operational_analytics_outbox (DSQL) -> drain -> ClickHouse

and every stage after the first is invisible from outside the VPC.  The Paris
ClickHouse node listens only on its private address and its security group
admits only the VPC CIDR, so nothing on the public internet -- a GitHub Actions
runner included -- can ask it ``SELECT max(created_at) FROM
activity_generations``.  A freshness check that cannot reach the thing it
checks is not a check.

The signal that *is* reachable is the publisher's delivery state. For durable
outboxes, rows are deleted only
after ClickHouse has accepted them (``SELECT -> insert -> DELETE``, in that
order, gated on every configured target succeeding), so the age of the OLDEST
undelivered row is an end-to-end statement about the whole pipeline:

* drain healthy      -> oldest row is seconds old
* drain dead/failing -> oldest row ages without bound, because nothing deletes

That is exactly ``drain_lag_seconds``, the number the drain itself computes and
logs every sweep, and the number its ``backlog_alarm`` fires on.  Publishing it
means an outside observer can see the drain stop even when the drain is not
running to say so -- which is the failure that actually happened: between
2026-08-02 and 2026-08-17 the drain was never installed at all, the outbox grew
to 465,119 rows, and nothing anywhere reported it, because the alarm is emitted
BY the process that was missing.

The direct sink is deliberately different: its bounded buffer drops oldest
rows, so its oldest retained row can stay young during an outage. Direct-mode
readings therefore add time since the last successful delivery plus cumulative
drop and flush-failure counters. An empty direct buffer no longer impersonates
the healthiest state after the flusher has died.

The control plane is the natural publisher because it is already the only
public thing that holds a DSQL connection, and the query is an index seek on
``tr_operational_analytics_outbox_enqueued_at_idx`` (``ORDER BY enqueued_at
LIMIT 1``), not a scan.  Note the asymmetry deliberately: ``outbox_depth`` is
optional, because ``count(*)`` over a large backlog is the expensive question
and the lag already answers the important one.

Spanner is the other asymmetry, and it is deliberate too
------------------------------------------------------

On the Spanner backend the control plane does NOT read the outbox at all.  Its
outbox is keyed ``(shard, commit_ts)`` and the drain deletes every delivered
row, so the per-shard ``ORDER BY commit_ts LIMIT 1`` head read steps over
seven days of deleted-row versions on every execution: measured at 823-957 ms
of Spanner CPU per call in production (2026-09-05), from every Cloud Run
instance, every minute.  Instead the VM poller
(:mod:`clickhouse.ingest_operational_outbox`) publishes a HEARTBEAT after each
pass -- the oldest live ``commit_ts`` it observed, floored by its own
committed-delete watermark, plus when it looked -- as one ``tr_entities`` row
(:data:`OUTBOX_HEARTBEAT_KIND`, :data:`OUTBOX_HEARTBEAT_ID`), and the control
plane reads that row with one point read.  A heartbeat that is absent or older
than the plane's maximum age publishes :data:`REASON_POLLER_STALE`: the plane
never falls back to scanning the outbox, because a lag it has not observed
must not be published as 0.  Postgres/DSQL keeps its index seek and the direct
sink its in-memory snapshot; both are cheap and unchanged.  The poller cannot
import this module (its systemd unit has no ``PYTHONPATH``), so it carries its
own copy of the row and field names; a test pins the two copies equal.

Nothing here does IO.  The storage backend answers with an
:class:`OutboxFreshness` and the publisher calls
:func:`analytics_status_from_reading`;
:mod:`clickhouse.check_fleet_analytics_freshness` reads the published result
back, for every cloud, with no credentials at all.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass
from typing import Any

#: Key this section occupies in the ``/status.json`` ``data`` object.
ANALYTICS_STATUS_KEY = "analytics"

#: Field names, exported so the publisher and the external checker cannot
#: drift apart silently -- a checker reading a key nobody writes any more
#: reports "healthy" forever, which is the same class of bug as the outage
#: this section exists to expose.
AVAILABLE_FIELD = "available"
REASON_FIELD = "reason"
BACKEND_FIELD = "backend"
DRAIN_LAG_FIELD = "drain_lag_seconds"
OUTBOX_DEPTH_FIELD = "outbox_depth"
SECONDS_SINCE_LAST_DELIVERY_FIELD = "seconds_since_last_delivery"
DROPPED_TOTAL_FIELD = "dropped_total"
FLUSH_FAILURES_FIELD = "flush_failures"
GENERATED_AT_FIELD = "generated_at"

#: Every key the published section may contain, in both of its forms.  Pinned
#: by a test, because a public contract that grows by accident is one nobody
#: can safely narrow later: an added key is a promise to whoever started
#: reading it.
PUBLISHED_AVAILABLE_FIELDS: frozenset[str] = frozenset(
    {
        AVAILABLE_FIELD,
        BACKEND_FIELD,
        DRAIN_LAG_FIELD,
        OUTBOX_DEPTH_FIELD,
        SECONDS_SINCE_LAST_DELIVERY_FIELD,
        DROPPED_TOTAL_FIELD,
        FLUSH_FAILURES_FIELD,
        GENERATED_AT_FIELD,
    }
)
PUBLISHED_UNAVAILABLE_FIELDS: frozenset[str] = frozenset({AVAILABLE_FIELD, REASON_FIELD})

#: Mirrors ``clickhouse.ingest_operational_outbox_postgres.DEFAULT_MAX_LAG_SECONDS``.
#: The drain logs ``backlog_alarm`` at this age; a checker that tolerated more
#: would be quieter than the process it is watching.
DEFAULT_MAX_DRAIN_LAG_SECONDS = 3600.0

#: ``reason`` values for the unavailable form.
REASON_NO_DATA = "no_data"
REASON_UNREACHABLE = "unreachable"
#: The deployment has no outbox wired at all -- ``operational_analytics_outbox_enabled``
#: is off, or the backend has no outbox to read.  Deliberately NOT collapsed into
#: an empty outbox: "nothing is being enqueued" and "everything enqueued has been
#: delivered" publish the same 0 rows and mean opposite things.
REASON_NOT_CONFIGURED = "not_configured"
#: Spanner only.  The control plane reads the drain's published heartbeat rather
#: than the outbox (see the module docstring), and the heartbeat is missing or
#: older than the plane tolerates.  Fail-closed on purpose: the plane has no
#: recent observation of the outbox head, and "I have not looked" must not
#: publish as the healthiest lag there is.  A poller that has stopped, is
#: crash-looping, or can no longer write is the expected cause.
REASON_POLLER_STALE = "poller_stale"

#: The ONLY reasons that may appear on the public page.  This is a privacy
#: boundary, not tidiness, and it is a clamp rather than an assertion because
#: it protects an UNCREDENTIALED page from a value produced far away from it.
#:
#: Both Postgres and Spanner backends build ``str(exc)[:500]`` on the line above
#: their ``OutboxFreshness.unavailable(...)`` call, for the log.  Passing that
#: string one field to the right -- a plausible, tidy-looking edit -- would put
#: DSQL/Spanner error text on ``/status.json``: hostnames, VPC addresses, IAM
#: role names, SQLSTATEs, sometimes a fragment of the statement.  The fleet
#: checker then formats the same value into a PUBLIC GitHub issue body.  So the
#: narrowing happens HERE, at the last function before the dict, where no
#: backend can route around it.  Same contract, and the same reason, as
#: :func:`trusted_router.client_reliability.client_observed_status_section`,
#: which narrows every value it publishes.
PUBLISHABLE_REASONS: frozenset[str] = frozenset(
    {REASON_NO_DATA, REASON_UNREACHABLE, REASON_NOT_CONFIGURED, REASON_POLLER_STALE}
)

#: ``backend`` values.  The column the lag comes from differs per backend --
#: Spanner stamps ``commit_ts``, Postgres/DSQL defaults ``enqueued_at`` -- so
#: publishing which one answered is what lets an operator read the right table.
BACKEND_SPANNER = "spanner"
BACKEND_POSTGRES = "postgres"
BACKEND_MEMORY = "memory"
BACKEND_DIRECT = "direct"
#: A backend name the publisher does not recognise.  Narrowed for the same
#: reason as :data:`PUBLISHABLE_REASONS` above -- every value on the public page
#: comes from a closed vocabulary -- and it is not silently dropped, because the
#: fleet checker matches this field against the cloud's expected backend and a
#: missing field would read as a plane that answered correctly.
BACKEND_UNKNOWN = "unknown"

PUBLISHABLE_BACKENDS: frozenset[str] = frozenset(
    {BACKEND_SPANNER, BACKEND_POSTGRES, BACKEND_MEMORY, BACKEND_DIRECT}
)


#: The Spanner poller's heartbeat row.  A generic ``tr_entities`` entity, so it
#: needs no DDL: ``(kind, id)`` is the primary key and ``body`` the JSON below.
#: MIRRORED by literal in ``clickhouse.ingest_operational_outbox`` (which cannot
#: import this module on the VM); ``tests/test_outbox_freshness_heartbeat.py``
#: pins the two copies equal.
OUTBOX_HEARTBEAT_KIND = "operational_outbox_heartbeat"
OUTBOX_HEARTBEAT_ID = "spanner-poller"
OUTBOX_HEARTBEAT_SCHEMA_VERSION = 1
HEARTBEAT_SCHEMA_VERSION_FIELD = "schema_version"
HEARTBEAT_OLDEST_LIVE_COMMIT_TS_FIELD = "oldest_live_commit_ts"
HEARTBEAT_OBSERVED_AT_FIELD = "observed_at"
HEARTBEAT_LAST_DELIVERY_AT_FIELD = "last_delivery_at"
HEARTBEAT_FETCHED_FIELD = "fetched"
HEARTBEAT_POLLER_FIELD = "poller"


@dataclass(frozen=True)
class OutboxHeartbeat:
    """What the Spanner poller last said about the outbox head.

    ``oldest_live_commit_ts`` is the oldest undelivered row the poller could
    see after its pass (``None`` when it saw none -- the drained state, lag 0),
    ``observed_at`` when it looked, and ``last_delivery_at`` when it last
    committed a delete after ClickHouse accepted a batch (``None`` until the
    process has delivered once).  ``fetched`` and ``poller`` are diagnostic.
    """

    oldest_live_commit_ts: dt.datetime | None
    observed_at: dt.datetime
    last_delivery_at: dt.datetime | None = None
    fetched: int = 0
    poller: str = ""

    @classmethod
    def parse(cls, body: str) -> OutboxHeartbeat:
        """Decode a stored heartbeat body; ``ValueError`` on anything unusable.

        Strict on purpose: a row this module cannot read is a row it must not
        publish from, and the caller turns the error into ``poller_stale``.
        """
        try:
            data = json.loads(body)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"heartbeat body is not JSON: {exc}") from None
        if not isinstance(data, dict):
            raise ValueError("heartbeat body is not a JSON object")
        if data.get(HEARTBEAT_SCHEMA_VERSION_FIELD) != OUTBOX_HEARTBEAT_SCHEMA_VERSION:
            raise ValueError("heartbeat schema_version is not supported")
        observed_at = _heartbeat_time(data.get(HEARTBEAT_OBSERVED_AT_FIELD))
        if observed_at is None:
            raise ValueError("heartbeat observed_at is missing")
        fetched = data.get(HEARTBEAT_FETCHED_FIELD, 0)
        if isinstance(fetched, bool) or not isinstance(fetched, int):
            raise ValueError("heartbeat fetched is not an integer")
        poller = data.get(HEARTBEAT_POLLER_FIELD, "")
        if not isinstance(poller, str):
            raise ValueError("heartbeat poller is not a string")
        return cls(
            oldest_live_commit_ts=_heartbeat_time(data.get(HEARTBEAT_OLDEST_LIVE_COMMIT_TS_FIELD)),
            observed_at=observed_at,
            last_delivery_at=_heartbeat_time(data.get(HEARTBEAT_LAST_DELIVERY_AT_FIELD)),
            fetched=fetched,
            poller=poller,
        )


def _heartbeat_time(value: object) -> dt.datetime | None:
    """``None`` for an absent (JSON ``null``) timestamp; ``ValueError`` otherwise."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("heartbeat timestamp is not a string")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("heartbeat timestamp is not ISO 8601") from None
    return _utc(parsed)


@dataclass(frozen=True)
class OutboxFreshness:
    """One delivery backend's externally publishable freshness answer.

    Carrying availability in the reading rather than returning a bare
    ``datetime | None`` is the whole point.  ``None`` already means "the outbox
    is empty", which is the healthiest durable-outbox state there is; a backend
    that could not look, or has no outbox to look at, must not be able to
    express itself with the same value. Direct mode supplements that ambiguous
    bounded-buffer head with delivery age and cumulative counters. Every
    backend answers with one of these, so a backend that cannot answer is
    *loud* rather than absent.
    """

    backend: str
    oldest_enqueued_at: dt.datetime | None = None
    outbox_depth: int | None = None
    seconds_since_last_delivery: float | None = None
    dropped_total: int | None = None
    flush_failures: int | None = None
    available: bool = True
    reason: str | None = None

    @classmethod
    def unavailable(cls, backend: str, reason: str) -> OutboxFreshness:
        return cls(backend=backend, available=False, reason=reason)


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def _iso(value: dt.datetime) -> str:
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def publishable_reason(reason: object) -> str:
    """Narrow any reason to the published vocabulary.

    Anything unrecognised becomes ``unreachable``, which is the honest summary
    of every way a backend can fail to answer, and is the one direction that
    cannot leak: a reason this module has never heard of did not come from this
    module's own constants, so it came from somewhere that may have had an
    exception, a DSN, or a hostname in scope.  See :data:`PUBLISHABLE_REASONS`.

    TOTAL, over ``object`` and not over ``str | None``, because both callers
    feed it values they did not construct.  The publisher takes whatever a
    storage backend put in ``OutboxFreshness.reason``; the fleet checker takes
    whatever JSON a remote page served, where a ``list`` or a ``dict`` is one
    keystroke away.  ``x in frozenset`` raises ``TypeError`` on an unhashable
    value, and a clamp that raises on hostile input is not a clamp -- on the
    publisher it would drop the whole ``analytics`` key (which the checker
    reads as "this cloud runs older code"), and in the checker it would crash
    the run that was supposed to report the problem.
    """
    return (
        reason if isinstance(reason, str) and reason in PUBLISHABLE_REASONS else REASON_UNREACHABLE
    )


def publishable_backend(backend: object) -> str:
    """Narrow any backend name to the published vocabulary.

    Total, for the reasons in :func:`publishable_reason`.
    """
    return (
        backend if isinstance(backend, str) and backend in PUBLISHABLE_BACKENDS else BACKEND_UNKNOWN
    )


def analytics_status_unavailable(reason: str = REASON_NO_DATA) -> dict[str, Any]:
    """The section when the outbox could not be read.

    Distinct from an EMPTY outbox on purpose.  An empty outbox is the healthiest
    state there is -- everything ever enqueued has been delivered -- and
    collapsing "I could not look" into it would turn a broken database
    connection into a green check.

    Every caller that publishes an unavailable section goes through here, which
    is why the reason clamp lives here.
    """
    return {AVAILABLE_FIELD: False, REASON_FIELD: publishable_reason(reason)}


def analytics_status_section(
    *,
    oldest_enqueued_at: dt.datetime | None,
    now: dt.datetime,
    outbox_depth: int | None = None,
    backend: str = "postgres",
    seconds_since_last_delivery: float | None = None,
    dropped_total: int | None = None,
    flush_failures: int | None = None,
) -> dict[str, Any]:
    """Project the outbox's head onto the public status contract.

    ``oldest_enqueued_at`` is the single row returned by ``SELECT enqueued_at
    FROM tr_operational_analytics_outbox ORDER BY enqueued_at LIMIT 1``, or
    ``None`` when the table is empty -- which means fully drained, so the lag
    is 0.0 rather than unknown.

    The lag is clamped at 0.  ``enqueued_at`` is ``CURRENT_TIMESTAMP`` at the
    writer and ``now`` comes from the reader, so a few milliseconds of clock
    skew can otherwise publish a negative age and make every downstream
    comparison read backwards.

    The oldest row's own timestamp is NOT published.  An earlier revision
    carried it as ``oldest_enqueued_at``, and nothing read it: the checker uses
    ``drain_lag_seconds``, the runbook quotes the lag, and the value is
    ``generated_at`` minus the lag in any case.  Nothing published yet, so
    there is no consumer to break -- and this is the moment when dropping it is
    free.  An uncredentialed page should carry the fields somebody reads and
    not one more.
    """
    moment = _utc(now)
    if oldest_enqueued_at is None:
        lag = 0.0
    else:
        lag = max(0.0, (moment - _utc(oldest_enqueued_at)).total_seconds())
    return {
        AVAILABLE_FIELD: True,
        BACKEND_FIELD: publishable_backend(backend),
        DRAIN_LAG_FIELD: round(lag, 3),
        OUTBOX_DEPTH_FIELD: None if outbox_depth is None else max(0, int(outbox_depth)),
        SECONDS_SINCE_LAST_DELIVERY_FIELD: _publishable_non_negative_float(
            seconds_since_last_delivery
        ),
        DROPPED_TOTAL_FIELD: _publishable_non_negative_int(dropped_total),
        FLUSH_FAILURES_FIELD: _publishable_non_negative_int(flush_failures),
        GENERATED_AT_FIELD: _iso(moment),
    }


def _publishable_non_negative_float(value: object) -> float | None:
    """Narrow an internal reading to a finite, non-negative JSON number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return round(max(0.0, number), 3)


def _publishable_non_negative_int(value: object) -> int | None:
    """Narrow a cumulative counter without invoking arbitrary conversion code."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, value)


def analytics_status_from_reading(
    reading: OutboxFreshness | None,
    *,
    now: dt.datetime,
) -> dict[str, Any]:
    """Publish one backend's reading, unavailable included.

    ``None`` means the publisher never got a reading back -- the store raised,
    or does not implement the surface.  It publishes as ``unreachable`` rather
    than as a missing key, because the checker's "no ``analytics`` section"
    branch is reserved for a deployment running code that does not publish the
    field at all, and conflating the two would tell an operator to redeploy
    when the real problem is the database.

    ``reading.reason`` is NOT trusted here: it goes through
    :func:`analytics_status_unavailable`, which narrows it.  Both backends
    build a truncated exception string one line above the call that produces
    this reading, so "publish whatever the store said" is one careless edit
    away from putting database error text on an uncredentialed page.
    """
    if reading is None:
        return analytics_status_unavailable(REASON_UNREACHABLE)
    if not reading.available:
        return analytics_status_unavailable(reading.reason or REASON_UNREACHABLE)
    return analytics_status_section(
        oldest_enqueued_at=reading.oldest_enqueued_at,
        now=now,
        outbox_depth=reading.outbox_depth,
        backend=reading.backend,
        seconds_since_last_delivery=reading.seconds_since_last_delivery,
        dropped_total=reading.dropped_total,
        flush_failures=reading.flush_failures,
    )
