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

The signal that *is* reachable is the outbox, and it is strictly better than a
ClickHouse timestamp for this purpose.  Rows are deleted from the outbox only
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

The control plane is the natural publisher because it is already the only
public thing that holds a DSQL connection, and the query is an index seek on
``tr_operational_analytics_outbox_enqueued_at_idx`` (``ORDER BY enqueued_at
LIMIT 1``), not a scan.  Note the asymmetry deliberately: ``outbox_depth`` is
optional, because ``count(*)`` over a large backlog is the expensive question
and the lag already answers the important one.

Nothing here does IO.  The caller reads the two numbers and calls
:func:`analytics_status_section`; :mod:`clickhouse.check_aws_analytics_freshness`
reads the published result back with no credentials at all.
"""

from __future__ import annotations

import datetime as dt
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
OLDEST_ENQUEUED_AT_FIELD = "oldest_enqueued_at"
GENERATED_AT_FIELD = "generated_at"

#: Mirrors ``clickhouse.ingest_operational_outbox_postgres.DEFAULT_MAX_LAG_SECONDS``.
#: The drain logs ``backlog_alarm`` at this age; a checker that tolerated more
#: would be quieter than the process it is watching.
DEFAULT_MAX_DRAIN_LAG_SECONDS = 3600.0

#: ``reason`` values for the unavailable form.
REASON_NO_DATA = "no_data"
REASON_UNREACHABLE = "unreachable"


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def _iso(value: dt.datetime) -> str:
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def analytics_status_unavailable(reason: str = REASON_NO_DATA) -> dict[str, Any]:
    """The section when the outbox could not be read.

    Distinct from an EMPTY outbox on purpose.  An empty outbox is the healthiest
    state there is -- everything ever enqueued has been delivered -- and
    collapsing "I could not look" into it would turn a broken database
    connection into a green check.
    """
    return {AVAILABLE_FIELD: False, REASON_FIELD: reason}


def analytics_status_section(
    *,
    oldest_enqueued_at: dt.datetime | None,
    now: dt.datetime,
    outbox_depth: int | None = None,
    backend: str = "postgres",
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
    """
    moment = _utc(now)
    if oldest_enqueued_at is None:
        lag = 0.0
        oldest_iso: str | None = None
    else:
        oldest = _utc(oldest_enqueued_at)
        lag = max(0.0, (moment - oldest).total_seconds())
        oldest_iso = _iso(oldest)
    return {
        AVAILABLE_FIELD: True,
        BACKEND_FIELD: backend,
        DRAIN_LAG_FIELD: round(lag, 3),
        OUTBOX_DEPTH_FIELD: None if outbox_depth is None else max(0, int(outbox_depth)),
        OLDEST_ENQUEUED_AT_FIELD: oldest_iso,
        GENERATED_AT_FIELD: _iso(moment),
    }
