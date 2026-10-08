"""Shadow-only complete-key transactions and indexed day reads; no money writes."""
from __future__ import annotations

import datetime as dt
import json
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from google.cloud.spanner_v1 import param_types as pt

from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.async_settle_shadow_evidence import (
    CONTROL,
    COUNTER,
    KINDS,
    SAMPLE,
    retired_day,
    validate_manifest,
    validate_sample,
)
from trusted_router.detached_jws import canonical
from trusted_router.storage_gcp_io import spanner_rpc_deadline

FINALIZATION_SQL = ("SELECT settled, finalization_outcome, finalized_cost_microdollars "
                    "FROM tr_gateway_authorization WHERE authorization_id=@authorization_id")
POINT_SQL = "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id"
DAY_SQL = ("SELECT id, body FROM tr_entities WHERE kind=@kind AND id>=@day_start "
           "AND id<@next_day_start AND id>@after_id ORDER BY id LIMIT @page_size")

# The same bound fences both transaction completion and the final write check.
WRITE_BUDGET_SECONDS = .2
RETENTION_FENCE = "retention-v1"


class RetirementBoundary(ValueError):
    """The key retires before the bounded transaction can finish."""


Statement = tuple[str, dict[str, Any], dict[str, Any]]


def finalization_statement(authorization_id: str) -> Statement:
    return (FINALIZATION_SQL,
            {"authorization_id": authorization_id}, {"authorization_id": pt.STRING})


def point_statement(kind: str, identity: str) -> Statement:
    if kind not in KINDS:
        raise ValueError("shadow kind")
    return (POINT_SQL,
            {"kind": kind, "id": identity}, {"kind": pt.STRING, "id": pt.STRING})


def day_statement(kind: str, day: str, after_id: str = "", page_size: int = 200) -> Statement:
    parsed = dt.date.fromisoformat(day)
    if kind not in KINDS or parsed.isoformat() != day or not 1 <= page_size <= 200:
        raise ValueError("shadow range")
    start, end = day + "/", (parsed + dt.timedelta(days=1)).isoformat() + "/"
    if after_id and not start <= after_id < end:
        raise ValueError("shadow cursor")
    return (DAY_SQL,
            dict(kind=kind, day_start=start, next_day_start=end, after_id=after_id, page_size=page_size),
            dict(kind=pt.STRING, day_start=pt.STRING, next_day_start=pt.STRING, after_id=pt.STRING, page_size=pt.INT64))


class EvidenceStore:
    def __init__(self, database: Any) -> None:
        self.database = database
        self.rejections: Counter[tuple[str, str]] = Counter()

    @staticmethod
    def query(reader: Any, statement: Statement, deadline: float) -> list[Any]:
        remaining = min(.2, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("shadow budget")
        sql, params, types = statement
        if sql not in (FINALIZATION_SQL, POINT_SQL, DAY_SQL):
            raise ValueError("unknown shadow statement")
        return list(reader.execute_sql(
            FINALIZATION_SQL if sql == FINALIZATION_SQL else POINT_SQL if sql == POINT_SQL else DAY_SQL,
            params=params, param_types=types, timeout=remaining,
                                       retry=None, request_options={"priority": "PRIORITY_LOW"}))

    def transaction(self, callback: Callable[[Any], Any], deadline: float) -> Any:
        if time.monotonic() >= deadline:
            raise TimeoutError("shadow budget")
        rpc_deadline = min(deadline, time.monotonic() + WRITE_BUDGET_SECONDS)
        attempted = False
        def once(tx: Any) -> Any:
            nonlocal attempted
            if attempted:
                # The deadline wrapper can extend timeout_secs=0. The SDK may
                # re-enter on Aborted, but it must never repeat reads/writes or
                # commit a second attempt. Runtime counts this as a store drop.
                raise RuntimeError("shadow_transaction_retry")
            attempted = True
            return callback(tx)
        with spanner_rpc_deadline(rpc_deadline):
            result = self.database.run_in_transaction(once, timeout_secs=0,
                commit_request_options={"priority": "PRIORITY_LOW"})
            if time.monotonic() >= rpc_deadline:
                raise TimeoutError("shadow commit budget")
            return result

    def write(self, tx: Any, kind: str, identity: str, body: dict[str, Any], deadline: float) -> None:
        # Every insert site, including operator control writes, uses this path.
        # Serialize first, then validate the key day and stamp updated_at from
        # one clock observation. Refuse the final transaction-budget interval
        # before retirement: cleanup must not overtake a buffered old-day insert.
        encoded = canonical(body).decode()
        # Cleanup advances this permanent, content-free policy watermark before
        # scanning even an empty range. A writer reads it in its write transaction:
        # cleanup either follows that commit and removes it, or invalidates the
        # writer's read (SDK retries are fenced off). This also covers late/unknown
        # commits for which a client RPC deadline alone cannot prove absence.
        rows = self.query(tx, point_statement(CONTROL, RETENTION_FENCE), deadline)
        cutoff = None
        if rows:
            fence = json.loads(rows[0][0])
            if set(fence) != {"v", "retired_before"} or fence["v"] != 1:
                raise ValueError("retention fence")
            cutoff = dt.date.fromisoformat(fence["retired_before"])
        observed_at = dt.datetime.now(dt.UTC)
        if kind == CONTROL and identity == RETENTION_FENCE:
            # The watermark is policy metadata, not a day evidence row. It is
            # never removed by day cleanup and can only advance to a retired day.
            if (set(body) != {"v", "retired_before"} or body["v"] != 1
                    or body["retired_before"] != (observed_at.date() - dt.timedelta(days=30)).isoformat()):
                raise ValueError("retention fence")
            if cutoff is not None and cutoff >= dt.date.fromisoformat(body["retired_before"]):
                return
        else:
            self.check_write_day(kind, identity, observed_at, cutoff)
        tx.insert_or_update(table="tr_entities", columns=("kind", "id", "body", "updated_at"),
                            values=[(kind, identity, encoded, observed_at)])

    def check_write_day(self, kind: str, identity: str, observed_at: dt.datetime,
                        cutoff: dt.date | None) -> None:
        day = identity.split("/", 1)[0]
        if kind not in KINDS:
            raise ValueError("shadow kind")
        if retired_day(day, observed_at) or cutoff is not None and dt.date.fromisoformat(day) < cutoff:
            self.rejections[kind, "proof_expired"] += 1
            raise ValueError("proof_expired")
        if retired_day(day, observed_at + dt.timedelta(seconds=WRITE_BUDGET_SECONDS)):
            self.rejections[kind, "proof_expired"] += 1
            raise RetirementBoundary("proof_expired")

    def booking(self, authorization_id: str, deadline: float) -> Booking:
        with self.database.snapshot() as snapshot:
            rows = self.query(snapshot, finalization_statement(authorization_id), deadline)
        if len(rows) != 1:
            return Booking()
        settled, outcome, amount = rows[0]
        if settled is not True or outcome not in {"settled", "refunded"}:
            return Booking(outcome="pending")
        valid = type(amount) is int and 0 <= amount < 1 << 63
        return Booking(amount if valid else None, outcome, True)

    def reserve(self, day: str, deadline: float) -> int:
        identity = day + "/cap-v1"
        dt.date.fromisoformat(day)
        def run(tx: Any) -> int:
            rows = self.query(tx, point_statement(CONTROL, identity), deadline)
            current = json.loads(rows[0][0]) if rows else dict(v=1, limit=100000, reserved=0, updated_at_us=0)
            if (set(current) != {"v", "limit", "reserved", "updated_at_us"} or current["v"] != 1
                    or current["limit"] != 100000 or type(current["reserved"]) is not int
                    or not 0 <= current["reserved"] <= 100000):
                raise ValueError("invalid cap")
            granted = min(100, 100000 - current["reserved"])
            if granted:
                current.update(reserved=current["reserved"] + granted, updated_at_us=int(time.time()*1e6))
                self.write(tx, CONTROL, identity, current, deadline)
            return granted
        return int(self.transaction(run, deadline))

    def insert_sample(self, identity: str, body: dict[str, Any], deadline: float) -> str:
        validate_sample(body, identity)
        def run(tx: Any) -> str:
            rows = self.query(tx, point_statement(SAMPLE, identity), deadline)
            if rows:
                previous = json.loads(rows[0][0])
                validate_sample(previous, identity)
                if previous["booking"]["attempted_kind"] != body["booking"]["attempted_kind"]:
                    return "winner_polarity"
                # A verified terminal identifies the attempt. A later diagnostic
                # classification cannot rewrite the first durable observation.
                if previous["payload_hash"] != body["payload_hash"] or (
                        body["payload_hash"] is None and previous["classification"] != body["classification"]):
                    return "conflict"
                return "duplicate"
            try:
                self.write(tx, SAMPLE, identity, body, deadline)
            except RetirementBoundary:
                return "retired"
            return "inserted"
        return str(self.transaction(run, deadline))

    def flush(self, identity: str, body: dict[str, Any], deadline: float) -> None:
        if len(canonical(body)) > 65536:
            raise ValueError("counter_overflow")
        def run(tx: Any) -> None:
            rows = self.query(tx, point_statement(COUNTER, identity), deadline)
            if rows and json.loads(rows[0][0])["sequence"] >= body["sequence"]:
                return
            self.write(tx, COUNTER, identity, body, deadline)
        self.transaction(run, deadline)

    def publish_manifest(self, body: dict[str, Any], deadline: float) -> None:
        identity = body["day"] + "/manifest-v1"
        validate_manifest(body, identity)
        def run(tx: Any) -> None:
            self.write(tx, CONTROL, identity, body, deadline)
        self.transaction(run, deadline)

    def day(self, kind: str, day: str) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        cursor = ""
        while True:
            with self.database.snapshot() as snapshot:
                rows = self.query(snapshot, day_statement(kind, day, cursor), time.monotonic() + .2)
            if any(not cursor < row[0] < (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat() + "/" for row in rows):
                raise ValueError("invalid page")
            result.extend((row[0], row[1]) for row in rows)
            if len(rows) < 200:
                return result
            cursor = rows[-1][0]

    def cleanup(self, kind: str, day: str, after_id: str = "") -> str:
        from google.cloud.spanner_v1 import KeySet
        if not retired_day(day, dt.datetime.now(dt.UTC)):
            raise ValueError("retained day")
        # Validate the complete bounded range before advancing policy metadata.
        day_statement(kind, day, after_id)
        deadline, cursor = time.monotonic() + 30, after_id
        def fence(tx: Any) -> None:
            body = dict(v=1, retired_before=(dt.datetime.now(dt.UTC).date() - dt.timedelta(days=30)).isoformat())
            self.write(tx, CONTROL, RETENTION_FENCE, body, deadline)
        self.transaction(fence, deadline)
        while time.monotonic() < deadline:
            started = time.monotonic()
            with self.database.snapshot() as snapshot:
                rows = self.query(snapshot, day_statement(kind, day, cursor), min(deadline, started + .2))
            if not rows:
                break
            keys = [(kind, row[0]) for row in rows]
            def delete(tx: Any, keys: list[tuple[str, str]] = keys) -> None:
                tx.delete("tr_entities", KeySet(keys=keys))
            self.transaction(delete, deadline)
            cursor = rows[-1][0]
            time.sleep(max(0, .2 - (time.monotonic() - started)))
        return cursor
