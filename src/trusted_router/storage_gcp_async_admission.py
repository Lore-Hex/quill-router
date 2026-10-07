"""One strong, bounded admission read. No writes, entity scans, or fleet scans."""
from __future__ import annotations

from typing import Any

from google.cloud.spanner_v1 import param_types

from trusted_router.services.async_settle import Admission, parse_health_record, valid_health_record

ROW_LIMIT = 1000


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


def read_admission(database: Any, workspace_id: str) -> Admission:
    sql, params, types = admission_statement(workspace_id)
    with database.snapshot() as snapshot:
        rows = list(snapshot.execute_sql(sql, params=params, param_types=types,
                                        timeout=0.2, retry=None,
                                        request_options={"priority": "PRIORITY_LOW"}))
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


def _query(reader: Any, statement: tuple[str, dict[str, Any], dict[str, Any]]) -> list[Any]:
    sql, params, types = statement
    return list(reader.execute_sql(sql, params=params, param_types=types,
                timeout=0.2, retry=None, request_options={"priority": "PRIORITY_LOW"}))


def read_health(database: Any) -> dict[str, Any] | None:
    with database.snapshot() as snapshot:
        rows = _query(snapshot, control_statement(HEALTH_ID))
    if len(rows) != 1:
        return None
    return parse_health_record(rows[0][0])


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

    return bool(database.run_in_transaction(txn, timeout_secs=0.2))


def publish_health(database: Any) -> dict[str, Any]:
    """Cover unresolved work, including NULL-age rows via the epoch sentinel.

    Logical index costs even when fast mode is off: inline done INSERT 0;
    pending/dead INSERT +1; lease-only/retry/park 0; pending->dead updates
    stored status; pending->done deletes 1. The due index already incurs
    comparable maintenance on these transitions.
    """
    import datetime as dt
    import json
    import logging
    import math
    import time

    observed = time.time()  # read START, never refresh old evidence on receipt
    with database.snapshot() as snapshot:
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
            stamp = created if isinstance(created, dt.datetime) else dt.datetime.fromisoformat(created.replace('Z', '+00:00'))
            age = observed - stamp.timestamp()
            if stamp.tzinfo is None or not math.isfinite(age) or age < 0:
                raise ValueError("invalid unresolved observation")
            # An epoch (or earlier) timestamp cannot establish a known age.
            if stamp.timestamp() <= 0:
                complete = False
        except (TypeError, ValueError, AttributeError, OverflowError):
            complete, age = False, observed
        ages.append(age)
    ages.sort()
    def percentile(p: float) -> float:
        return ages[max(0, math.ceil(len(ages) * p) - 1)] if ages else 0.0
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
        _write_control(transaction, HEALTH_ID, value)

    database.run_in_transaction(txn, timeout_secs=0.2)
    logging.getLogger(__name__).info("async_drain.health %s", json.dumps(value))
    return value
