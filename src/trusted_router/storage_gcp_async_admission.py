"""One strong, bounded admission read. No writes, entity scans, or fleet scans."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Literal, TypeVar

from google.api_core import exceptions as api_errors
from google.cloud.spanner_v1 import TransactionOptions, param_types

from trusted_router.services.async_settle import Admission, parse_health_record, valid_health_record
from trusted_router.storage_gcp_io import (
    _rollback_discarded_transaction,
    remaining_rpc_budget,
    spanner_rpc_budget,
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


def _begin_explicitly(transaction: Any) -> None:
    """Begin explicitly so an attempt that fails anywhere can still be rolled back.

    With the SDK's inline begin, the transaction ID arrives with the first
    read's response; a read that times out first leaves the client no ID, and
    Transaction.rollback() then sends nothing (2026-11-09 02:07Z alert, when
    the read still took a lock). Beginning first costs one round trip and takes
    no locks. The store's database wrapper (configure_spanner_rpc_deadlines)
    holds the begin to the transaction's timeout_secs with the read and commit;
    the rollback keeps its own floor.
    """
    transaction.begin()


# Control-row reads take no lock. Spanner checks at commit that the row is
# unchanged since the read and aborts the commit otherwise, so each guard and
# its write still form one serializable step (google.spanner.v1 ReadLockMode).
CONTROL_READ_LOCK_MODE = TransactionOptions.ReadWrite.ReadLockMode.OPTIMISTIC
_T = TypeVar("_T")


def _run_control(database: Any, txn: Callable[[Any], _T], transaction_tag: str) -> _T:
    """One read-modify-write of a control row that leaves no lock behind.

    The drain is the row's only writer, yet the row was hot (2026-11-13 22:08Z
    alert): under the default pessimistic mode the read held a ReaderShared
    lock until the commit returned. When a commit timed out on the client,
    Session.run_in_transaction re-raised it without a rollback, so the server
    kept the transaction and its lock. Each later attempt is younger, so under
    wound-wait it waited behind that orphan past its 500 ms deadline and left
    its own, until Spanner reaped them. Now no read holds a lock between round
    trips, and whatever ends an attempt (a read, the commit or the abort budget),
    its transaction is rolled back. A rollback after a commit that did land
    fails harmlessly.
    """
    opened: list[Any] = []

    def attempt(transaction: Any) -> _T:
        opened[:] = [transaction]  # the SDK's abort retries begin a new one
        _begin_explicitly(transaction)
        return txn(transaction)

    try:
        return database.run_in_transaction(attempt, timeout_secs=READ_DEADLINE_SECONDS,
                                           transaction_tag=transaction_tag,
                                           read_lock_mode=CONTROL_READ_LOCK_MODE)
    except BaseException:
        # Out here the runner's deadline no longer applies. Without one, the
        # rollback would get the store's 20 s transaction budget; with this
        # one it gets the rollback floor (storage_gcp_io._ROLLBACK_FLOOR_SECONDS).
        spanner_rpc_budget(READ_DEADLINE_SECONDS)(_dispose_attempt)(opened)
        raise


def _dispose_attempt(opened: list[Any]) -> None:
    # Earlier attempts in one runner call ended in Aborted, which the server
    # already released. The SDK rolls back a callback's plain exception itself.
    last = opened[-1] if opened else None
    if (last is not None and getattr(last, "committed", None) is None
            and not getattr(last, "rolled_back", False)):
        _rollback_discarded_transaction(last)


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
        rows = _query(transaction, control_statement(identity))
        if rows and now - float(json.loads(rows[0][0])["observed_at"]) < interval_seconds:
            return False
        _write_control(transaction, identity, {"observed_at": now})
        return True

    # Two drains (old and new revisions in a deploy) can both read a free
    # claim; the commit check aborts the later writer, whose retry reads the
    # winner's claim, so only one of them publishes in the interval.
    return bool(_run_control(database, txn, "tr_drain_control_claim"))


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
        previous = _query(transaction, control_statement(HEALTH_ID))
        prior = parse_health_record(previous[0][0]) if len(previous) == 1 else None
        if prior is not None and prior["observed_at"] > observed:
            return  # A slower old pass cannot overwrite a newer observation.
        # A newer record committed after this read aborts this commit; the
        # retry then reads it and returns above.
        _write_control(transaction, HEALTH_ID, value)

    _run_control(database, txn, "tr_drain_health_publish")
    logging.getLogger(__name__).debug("async_drain.health %s", json.dumps(value))
    return value
