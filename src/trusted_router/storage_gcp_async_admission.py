"""One strong, bounded admission read. No writes, entity scans, or fleet scans."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Literal

from google.api_core import exceptions as api_errors
from google.cloud.spanner_v1 import param_types

from trusted_router.services.async_settle import Admission, parse_health_record, valid_health_record
from trusted_router.storage_gcp_io import (
    _rollback_on_api_error,
    remaining_rpc_budget,
    spanner_rpc_deadline,
)

# São Paulo to nam6 measured ~150 ms per round trip; allow reads and read+commit.
READ_DEADLINE_SECONDS = 0.5
ROW_LIMIT = 1000
logger = logging.getLogger(__name__)
_ReadOperation = Literal["admission", "drain_health", "drain_grace"]
_read_failure_lock = threading.Lock()
_read_failure_logs: dict[tuple[_ReadOperation, str], tuple[float, int]] = {}


def _report_read_failure(operation: _ReadOperation, error: Exception, started: float) -> None:
    # Only fixed categories leave this module: exceptions may contain SQL,
    # credentials and customer data. Never attach the exception or stack.
    reason = "other"
    for cls, label in ((api_errors.DeadlineExceeded, "deadline_exceeded"),
                       (api_errors.Cancelled, "cancelled"),
                       (api_errors.Aborted, "aborted"),
                       (api_errors.ServiceUnavailable, "unavailable"),
                       (api_errors.InternalServerError, "internal"),
                       (api_errors.PermissionDenied, "permission_denied"),
                       (api_errors.ResourceExhausted, "resource_exhausted")):
        if isinstance(error, cls):
            reason = label
            break
    now = time.monotonic()
    key = (operation, reason)
    # Bounded to three operations times eight reasons, regardless of customers.
    # Count repeats without letting one cause suppress another kind of failure.
    with _read_failure_lock:
        next_log, failures = _read_failure_logs.get(key, (0., 0))
        failures += 1
        if now < next_log:
            _read_failure_logs[key] = (next_log, failures)
            return
        _read_failure_logs[key] = (now + 60, 0)
    logger.warning("async admission read failed operation=%s reason=%s elapsed_ms=%d failures=%d",
                   operation, reason, max(0, round((now-started)*1000)), failures)


def _snapshot_rows(database: Any, statement: tuple[str, dict[str, Any], dict[str, Any]],
                   operation: _ReadOperation, deadline_seconds: float = READ_DEADLINE_SECONDS) -> list[Any]:
    started = time.monotonic()
    sql, params, types = statement
    try:
        # retry=None does not disable the SDK's result-stream restarts. Keep
        # one deadline active through iteration, rather than renewing on restart.
        with spanner_rpc_deadline(started + deadline_seconds):
            with database.snapshot() as snapshot:
                return list(snapshot.execute_sql(
                    sql, params=params, param_types=types, retry=None,
                    timeout=remaining_rpc_budget(deadline_seconds),
                    request_options={"priority": "PRIORITY_LOW", "request_tag": "tr_async_" + operation}))
    except Exception as error:
        _report_read_failure(operation, error, started)
        raise


def admission_statement(workspace_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    # LIMIT is INSIDE the aggregate. A sentinel row rejects truncation rather
    # than treating a partial SUM as the full exposure. Pending includes leases;
    # dead unresolved rows remain exposure. Shard zero carries replicated trust.
    return (
        "SELECT pending.n, pending.amount, trust.trust_tier FROM "
        "(SELECT COUNT(*) AS n, SUM(actual_cost_micro) AS amount FROM "
        "(SELECT actual_cost_micro FROM tr_settle_outbox@{FORCE_INDEX=tr_settle_outbox_workspace_status} "
        "WHERE workspace_id=@ws AND status IN ('pending', 'dead') LIMIT 1001)) AS pending "
        "CROSS JOIN (SELECT trust_tier FROM tr_credit_balance "
        "WHERE workspace_id=@ws AND shard=0 AND trust_latched_at IS NULL "
        "AND COALESCE(ARRAY_LENGTH(billing_pause_causes), 0)=0) AS trust",
        {"ws": workspace_id}, {"ws": param_types.STRING},
    )


def read_admission(database: Any, workspace_id: str, *,
                   deadline_seconds: float = READ_DEADLINE_SECONDS) -> Admission:
    """``deadline_seconds`` is the RPC deadline. Money admission keeps 500 ms;
    the shadow observer passes a generous one and applies its own install
    cut-off, so its slow tail never ends as a Spanner client deadline error."""
    rows = _snapshot_rows(database, admission_statement(workspace_id), "admission", deadline_seconds)
    if len(rows) != 1 or rows[0][0] > ROW_LIMIT:
        raise ValueError("admission unavailable")
    count, amount, tier = rows[0]
    if type(count) is not int or count < 0 or type(tier) is not int:
        raise ValueError("admission unavailable")
    if count == 0 and amount is None:
        amount = 0
    if type(amount) is not int or not 0 <= amount <= (1 << 63) - 1:
        raise ValueError("admission unavailable")
    return Admission(amount, tier)


# Fixed primary keys; one record per standalone billing database/authority.
HEALTH_KIND = "settle_drain_control"
HEALTH_ID = "fleet-v1"
HOUSEKEEPING_ID = "housekeeping-v1"
HEALTH_PUBLISH_ID = "health-publish-v1"
HEALTH_ROW_LIMIT = 10_000


def control_statement(identity: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return ("SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
            {"kind": HEALTH_KIND, "id": identity},
            {"kind": param_types.STRING, "id": param_types.STRING})


def unresolved_statement() -> tuple[str, dict[str, Any], dict[str, Any]]:
    # Sparse covering index includes old rows without workspace ownership, dead
    # rows and active leases. A sentinel makes truncation explicitly unhealthy.
    return (
        "SELECT unresolved_at, actual_cost_micro, status FROM tr_settle_outbox"
        "@{FORCE_INDEX=tr_settle_outbox_unresolved} "
        "WHERE unresolved_at IS NOT NULL ORDER BY unresolved_at LIMIT @limit",
        {"limit": HEALTH_ROW_LIMIT + 1}, {"limit": param_types.INT64},
    )


# Admission requires the published p95 age to be at most this (eligible() and
# decode_health() in services/async_settle.py).
HEALTH_P95_LIMIT_SECONDS = 5
# The longest grace a settle intent's writer gives its own inline settle before
# the drain may take the row: routes/internal/gateway.py enqueues its two-commit
# and fast-path pilot intents due 60 s after it writes them. A row due sooner
# is read as due; a row written due later still counts from 60 s.
INLINE_GRACE_SECONDS = 60
GRACE_ROW_LIMIT = 100


def grace_statement(since: str, until: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Unresolved rows that may still be in their inline grace, created in [since, until).

    A non-async pending row the drain has neither attempted nor parked: a failed
    apply counts an attempt, and both a failure and a park write last_error.
    The range is on the unresolved index; the other columns come from the base
    rows of that range only.
    """
    return (
        "SELECT unresolved_at, next_attempt_at FROM tr_settle_outbox"
        "@{FORCE_INDEX=tr_settle_outbox_unresolved} "
        "WHERE unresolved_at IS NOT NULL AND unresolved_at >= @since AND unresolved_at < @until "
        "AND status='pending' AND async_version IS NULL AND attempts=0 AND last_error IS NULL "
        "ORDER BY unresolved_at LIMIT @limit",
        {"since": since, "until": until, "limit": GRACE_ROW_LIMIT},
        {"since": param_types.TIMESTAMP, "until": param_types.TIMESTAMP, "limit": param_types.INT64},
    )


def _timestamp(value: Any) -> float:
    """Seconds since the epoch of a stored timestamp; a naive one is refused."""
    import datetime as dt

    stamp = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("naive timestamp")
    return stamp.timestamp()


def _ages_from_due(reader: Any, observed: float, ages: list[float]) -> list[float]:
    """The unresolved ages, with a row in its inline grace counted from when it falls due.

    A row is in its grace while it is not an async row, is pending, and the drain
    has neither attempted nor parked it: its writer settles it inline and gave
    the drain the row only from next_attempt_at, at most INLINE_GRACE_SECONDS
    after writing it. Until then its age is not drain lag, so it counts from
    that due time instead of from its creation. Every other row, dead, retried
    and parked ones included, keeps its creation age.

    Only a row whose creation age is over HEALTH_P95_LIMIT_SECONDS and at most
    INLINE_GRACE_SECONDS + HEALTH_P95_LIMIT_SECONDS (plus a second's margin) can
    be over the limit by creation age but not by due age; younger rows are under
    it either way and older ones over it. So the base rows are read only for
    that range, and only when it holds at least one and at most GRACE_ROW_LIMIT
    rows (the same snapshot's backlog says how many); with more, every age
    stays a creation age. A truncated backlog cannot say how many, and its
    record is incomplete and never admits, so it is not read. If the read
    fails, every age stays a creation age too: the evidence before this read
    existed, and a record still goes out.
    """
    import datetime as dt
    import time

    if len(ages) > HEALTH_ROW_LIMIT:
        return ages
    span = INLINE_GRACE_SECONDS + HEALTH_P95_LIMIT_SECONDS + 1
    in_range = sum(HEALTH_P95_LIMIT_SECONDS < age <= span for age in ages)
    if not 0 < in_range <= GRACE_ROW_LIMIT:
        return ages
    def iso(seconds: float) -> str:
        return dt.datetime.fromtimestamp(seconds, dt.UTC).isoformat().replace("+00:00", "Z")
    started = time.monotonic()
    try:
        rows = _query(reader, grace_statement(iso(observed - span), iso(observed - HEALTH_P95_LIMIT_SECONDS)))
    except Exception as error:  # any failure: publish the creation ages, never no record
        _report_read_failure("drain_grace", error, started)
        return ages
    due: dict[float, list[float]] = {}
    for row in rows:
        try:
            created, next_attempt = row
            start = _timestamp(created)
            # Never before creation: a row's due age is at most its creation age.
            due_at = max(start, min(_timestamp(next_attempt), start + INLINE_GRACE_SECONDS))
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue  # an unreadable row keeps its creation age
        # Keyed by the creation age exactly as the caller computed it, so this
        # replaces one of the equal ages, whichever row it was.
        due.setdefault(observed - start, []).append(max(0.0, observed - due_at))
    out = []
    for age in ages:
        replacements = due.get(age)
        out.append(replacements.pop() if replacements else age)
    return out


def _query(reader: Any, statement: tuple[str, dict[str, Any], dict[str, Any]]) -> list[Any]:
    sql, params, types = statement
    return list(reader.execute_sql(sql, params=params, param_types=types,
                timeout=READ_DEADLINE_SECONDS, retry=None, request_options={"priority": "PRIORITY_LOW"}))


def read_health(database: Any) -> dict[str, Any] | None:
    rows = _snapshot_rows(database, control_statement(HEALTH_ID), "drain_health")
    if len(rows) != 1:
        return None
    return parse_health_record(rows[0][0])


def _begin_before_lock(transaction: Any) -> None:
    """Begin explicitly so a read that times out can still be rolled back.

    With the SDK's inline begin, the transaction ID arrives with the first
    read's response; a read that times out first leaves the client no ID, and
    Transaction.rollback() then sends nothing. The server-side transaction and
    its lock on the control row stay until Spanner reaps them, and every later
    claim queues behind the orphan (2026-11-09 02:07Z alert). Beginning first
    costs one round trip and takes no locks. The store's database wrapper
    (configure_spanner_rpc_deadlines) holds the begin to the transaction's
    timeout_secs with the read and commit; the rollback keeps its own floor.
    """
    transaction.begin()


def _write_control(transaction: Any, identity: str, value: dict[str, Any]) -> None:
    import datetime as dt
    import json

    transaction.insert_or_update(table="tr_entities",
        columns=("kind", "id", "body", "updated_at"),
        values=[(HEALTH_KIND, identity, json.dumps(value, allow_nan=False), dt.datetime.now(dt.UTC))])


def claim_housekeeping(database: Any) -> bool:
    return _claim_cadence(database, HOUSEKEEPING_ID, 300)


def claim_health_publish(database: Any, interval_seconds: float) -> bool:
    from trusted_router.services.async_settle import CACHE_SECONDS

    if not 0 < interval_seconds <= CACHE_SECONDS:
        raise ValueError("health publish interval must be within cache freshness")
    return _claim_cadence(database, HEALTH_PUBLISH_ID, interval_seconds)


def _claim_cadence(database: Any, identity: str, interval_seconds: float) -> bool:
    import json
    import time

    now = time.time()

    def txn(transaction: Any) -> bool:
        _begin_before_lock(transaction)
        rows = _query(transaction, control_statement(identity))
        if rows and now - float(json.loads(rows[0][0])["observed_at"]) < interval_seconds:
            return False
        _write_control(transaction, identity, {"observed_at": now})
        return True

    # The SDK skips rollback on API errors; with the explicit begin, this
    # wrapper's rollback releases the claim row's lock after a timed-out read.
    return bool(database.run_in_transaction(_rollback_on_api_error(txn),
        timeout_secs=READ_DEADLINE_SECONDS, transaction_tag="tr_drain_control_claim"))


def publish_health(database: Any) -> dict[str, Any]:
    """Cover unresolved work, including NULL-age rows via the epoch sentinel.

    Logical index costs even when fast mode is off: inline done INSERT 0;
    pending/dead INSERT +1; lease-only/retry/park 0; pending->dead updates
    stored status; pending->done deletes 1. The due index already incurs
    comparable maintenance on these transitions.

    The p50 and p95 ages count a row in its inline grace from when it falls
    due (_ages_from_due); the oldest age is every row's creation age.
    """
    import json
    import math
    import time

    observed = time.time()  # read START, never refresh old evidence on receipt
    # One snapshot for both reads: the grace rows are read as of the backlog.
    with database.snapshot(multi_use=True) as snapshot:
        rows = _query(snapshot, unresolved_statement())
        complete = len(rows) <= HEALTH_ROW_LIMIT
        ages = []
        amount = dead = 0
        for created, micro, status in rows:
            age = observed  # Unknown age sorts as oldest, including NULL's epoch sentinel.
            if type(micro) is not int or micro < 0:
                complete, micro = False, 0
            amount += micro
            if type(status) is not str or status not in ("pending", "dead"):
                complete = False
            dead += status == "dead"
            try:
                stamp = _timestamp(created)
                age = observed - stamp
                if not math.isfinite(age) or age < 0:
                    raise ValueError("invalid unresolved observation")
                # An epoch (or earlier) timestamp cannot establish a known age.
                if stamp <= 0:
                    complete = False
            except (TypeError, ValueError, AttributeError, OverflowError):
                complete, age = False, observed
            ages.append(age)
        due_ages = sorted(_ages_from_due(snapshot, observed, ages))
    def percentile(p: float) -> float:
        return due_ages[max(0, math.ceil(len(due_ages) * p) - 1)] if due_ages else 0.0
    value = dict(v=1, authority="local", observed_at=observed,
                 worker_heartbeat=time.time(), complete=complete,
                 sample_count=len(ages), backlog_count=len(rows), frozen_micro=amount,
                 p50_age_seconds=percentile(.5), p95_age_seconds=percentile(.95),
                 oldest_unresolved_age_seconds=max(ages, default=0), dead_count=dead)

    if not valid_health_record(value):
        raise ValueError("invalid health publication")

    def txn(transaction: Any) -> None:
        _begin_before_lock(transaction)
        previous = _query(transaction, control_statement(HEALTH_ID))
        prior = parse_health_record(previous[0][0]) if len(previous) == 1 else None
        if prior is not None and prior["observed_at"] > observed:
            return  # A slower old pass cannot overwrite a newer observation.
        _write_control(transaction, HEALTH_ID, value)

    database.run_in_transaction(_rollback_on_api_error(txn), timeout_secs=READ_DEADLINE_SECONDS,
                                transaction_tag="tr_drain_health_publish")
    logging.getLogger(__name__).debug("async_drain.health %s", json.dumps(value))
    return value
