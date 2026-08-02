"""Drain tenant activity and synthetic metadata from Postgres to ClickHouse.

Sibling of :mod:`clickhouse.ingest_operational_outbox`, which does the same job
against Spanner.  The ClickHouse side — column allowlists, normalisation,
JSONEachRow batching, the writer — is imported from that module rather than
re-implemented, so the two clouds cannot drift into writing different rows.
Only the *source* is new.

Two things differ from the Spanner source and both are load-bearing:

**No cursor.**  Spanner rows carry ``PENDING_COMMIT_TIMESTAMP()``, a totally
ordered commit stamp a drain can checkpoint on.  Postgres has no equivalent:
``now()`` is transaction-start time, so a transaction that starts earlier can
commit later and appear *behind* a checkpoint that has already moved past it.
A drain on a timestamp cursor would silently skip those rows forever.  So the
queue is the table: select, write, delete what was written.

**At-least-once, deliberately.**  The order is SELECT -> ClickHouse insert ->
DELETE, never the reverse.  A crash between the insert and the delete
redelivers the row on the next pass; the ClickHouse tables are
``ReplacingMergeTree`` keyed on the event, so a redelivery deduplicates.
Deleting first would trade a duplicate that ClickHouse collapses for a loss
nothing can recover.

Aurora DSQL reports every optimistic-concurrency abort as SQLSTATE 40001, which
is routine rather than exceptional, so each statement runs under a retry.

**Failures are contained, not fatal.**  This is a daemon whose silence looks
exactly like success — nothing downstream notices undelivered rows except the
lag metric — so it never exits on an error it could survive.  Shards are swept
independently and a failing one is logged and counted rather than allowed to
unwind the sweep, because a single undeliverable row would otherwise stop
delivery for all 32 shards.  Non-retryable errors drop the connection so the
next pass reconnects; DSQL expires long-lived connections, so that path is
ordinary rather than exceptional.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from clickhouse.ingest_operational_outbox import (
    ClickHouseOperationalWriter,
    OperationalOutboxRow,
    _lag_seconds,
    _utc,
    normalise_operational_event,
)
from trusted_router.postgres_dsn import (
    aws_dsql_connection_details,
    dsn_has_password,
    dsql_token_is_admin,
)

# The writer's own constant, not the Spanner drain's copy of it. A drain that
# sweeps fewer shards than the writer hashes into never reads — and never
# deletes — the rows in the shards above its count, and loses them silently
# while reporting healthy. Importing the producer's value makes that
# unrepresentable rather than merely unlikely.
from trusted_router.storage_operational_analytics import (
    OPERATIONAL_ANALYTICS_OUTBOX_SHARDS as OUTBOX_SHARDS,
)

#: Aurora DSQL surfaces every OCC abort here; stock Postgres uses it for
#: serialization failures. Both mean "rolled back whole, try again".
SERIALIZATION_FAILURE = "40001"
DEADLOCK_DETECTED = "40P01"
RETRYABLE_SQLSTATES = frozenset({SERIALIZATION_FAILURE, DEADLOCK_DETECTED})

# Table name written out rather than interpolated: it is fixed, and a literal
# keeps the statements obviously injection-free. Every value is bound.
# Ordered by the PRIMARY KEY's own prefix — (shard, event_kind, event_id) — so
# the LIMIT is served by a bounded range scan. Ordering by `enqueued_at`
# instead would be a full scan of the shard plus a sort on every poll, because
# no index leads with it; that cost grows with the backlog, which is exactly
# when the drain must go faster rather than slower. Delivery order does not
# matter here: the delete is by key and ClickHouse dedups by event.
SELECT_SHARD_BATCH_SQL = (
    "SELECT shard, enqueued_at, event_kind, event_id, payload "
    "FROM tr_operational_analytics_outbox "
    "WHERE shard = %s "
    "ORDER BY event_kind, event_id "
    "LIMIT %s"
)
DELETE_BY_KEY_SQL = (
    "DELETE FROM tr_operational_analytics_outbox "
    "WHERE shard = %s AND event_kind = %s AND event_id = %s"
)
# One statement for the whole table, not one per shard: the lag metric used to
# cost 32 transactions per poll even when the queue was empty. Backed by
# tr_operational_analytics_outbox_enqueued_at_idx, so this is an index seek.
SELECT_OLDEST_SQL = (
    "SELECT enqueued_at FROM tr_operational_analytics_outbox ORDER BY enqueued_at LIMIT 1"
)

log = logging.getLogger("trusted_router.operational_analytics_ingest_postgres")


@dataclass(frozen=True)
class ShardDrainResult:
    shard: int
    fetched: int
    inserted: int
    deleted: int


@dataclass(frozen=True)
class SweepResult:
    """One pass over every shard.

    Carries `failed_shards` because a sweep is no longer all-or-nothing: shards
    fail independently, and a caller that could not tell the difference would
    log a healthy-looking rows=0 while delivery was entirely broken.
    """

    fetched: int
    inserted: int
    rows_per_second: float
    failed_shards: int = 0


def _is_retryable(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) in RETRYABLE_SQLSTATES


def retry_serialization(
    operation: Any,
    *,
    attempts: int = 8,
    sleep: Any = time.sleep,
) -> Any:
    """Run `operation`, retrying only rollbacks the server asked us to retry.

    Every other error propagates. In particular a ClickHouse failure must NOT
    be retried in here — the caller has to see it so the delete does not run.
    """
    if attempts < 1:
        raise ValueError("attempts must be positive")
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            # Anything the server did not label 40001/40P01 propagates.
            if not _is_retryable(exc):
                raise
            last = exc
            sleep(min(0.05 * (2**attempt), 1.0))
    raise RuntimeError(
        f"Postgres transaction rolled back {attempts} times (SQLSTATE 40001/40P01)"
    ) from last


class PostgresOperationalOutboxSource:
    """Reads and deletes outbox rows on a Postgres-wire database.

    Holds no cursor and no state: everything it needs is in the table.
    """

    def __init__(
        self,
        *,
        dsn: str,
        iam_auth: str = "",
        iam_region: str = "",
        attempts: int = 8,
    ) -> None:
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn
        self._iam_auth = iam_auth
        self._iam_region = iam_region
        self._attempts = attempts
        self._connection: Any = None

    # -- connection ---------------------------------------------------------

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _connect(self) -> Any:
        if not self._iam_auth:
            # Symmetric with the IAM branch's own guard below. `--dsn` is an
            # argv value: a password in it is readable by every local user via
            # `ps`/`/proc/<pid>/cmdline` and lands in shell history. libpq
            # reads PGPASSWORD (or ~/.pgpass) without any of that exposure.
            if dsn_has_password(self._dsn):
                raise ValueError(
                    "DSN must not contain a password; set PGPASSWORD instead so the "
                    "secret does not appear in argv"
                )
            return self._psycopg.connect(self._dsn, autocommit=False)
        if self._iam_auth != "aws-dsql":
            raise ValueError(
                f"Unsupported --iam-auth value {self._iam_auth!r}; expected 'aws-dsql' or empty"
            )
        import boto3

        hostname, region = aws_dsql_connection_details(
            self._dsn,
            region_override=self._iam_region,
            setting="--dsn",
        )
        client = boto3.client("dsql", region_name=region)
        # This process runs on the ClickHouse analytics host, which must not
        # hold credentials for the operational database beyond the one table it
        # drains. `generate_db_connect_admin_auth_token` is unconditionally
        # superuser-equivalent — it would let anything on this box read raw
        # member emails and workspace ids out of tr_entities, precisely the
        # identifiers analytics_surrogate() exists to keep off this host.
        # Deploy with a DSN whose user is a role granted only
        # `SELECT, DELETE ON tr_operational_analytics_outbox` and it gets the
        # scoped token instead.
        mint = (
            client.generate_db_connect_admin_auth_token
            if dsql_token_is_admin(self._dsn)
            else client.generate_db_connect_auth_token
        )
        token = mint(Hostname=hostname, Region=region, ExpiresIn=900)
        return self._psycopg.connect(self._dsn, password=token, autocommit=False)

    def _live_connection(self) -> Any:
        # Reused across statements: a fresh connect per shard would be 64+
        # handshakes per sweep, and on DSQL each one also mints an IAM token.
        if self._connection is None or getattr(self._connection, "closed", False):
            self._connection = self._connect()
        return self._connection

    def _run(self, operation: Any) -> Any:
        def attempt() -> Any:
            conn = self._live_connection()
            try:
                with conn.transaction():
                    return operation(conn)
            except Exception as exc:
                # A 40001 rollback leaves the connection healthy and is
                # retried on it. Anything else may have broken the socket
                # (DSQL also expires connections), so drop it and let the
                # next call reconnect rather than reusing a dead handle.
                if not _is_retryable(exc):
                    self._discard_connection()
                raise

        return retry_serialization(attempt, attempts=self._attempts)

    def _discard_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        # Already broken; closing is best-effort cleanup, not a result.
        with contextlib.suppress(Exception):
            connection.close()

    # -- source surface -----------------------------------------------------

    def fetch_shard(self, shard: int, *, limit: int) -> list[OperationalOutboxRow]:
        if limit < 1:
            return []

        def read(conn: Any) -> list[OperationalOutboxRow]:
            rows = conn.execute(SELECT_SHARD_BATCH_SQL, (shard, limit)).fetchall()
            return [
                OperationalOutboxRow(
                    shard=int(row[0]),
                    commit_ts=_utc(row[1]),
                    event_kind=str(row[2]),
                    event_id=str(row[3]),
                    payload=str(row[4]),
                )
                for row in rows
            ]

        return list(self._run(read))

    def delete(self, rows: list[OperationalOutboxRow]) -> int:
        """Delete exactly the rows named, by primary key.

        Keyed on (shard, event_kind, event_id) — NOT on the timestamp — so a
        row enqueued between this drain's SELECT and its DELETE survives to be
        picked up on the next pass instead of being swept away unread.
        """
        if not rows:
            return 0
        keys = [(row.shard, row.event_kind, row.event_id) for row in rows]

        def remove(conn: Any) -> int:
            deleted = 0
            for shard, event_kind, event_id in keys:
                cursor = conn.execute(DELETE_BY_KEY_SQL, (shard, event_kind, event_id))
                deleted += int(cursor.rowcount)
            return deleted

        return int(self._run(remove))

    def oldest_enqueued_at(self) -> dt.datetime | None:
        def read(conn: Any) -> Any:
            return conn.execute(SELECT_OLDEST_SQL).fetchone()

        row = self._run(read)
        return None if row is None else _utc(row[0])


def drain_shard_once(
    source: Any,
    writer: Any,
    *,
    shard: int,
    batch_size: int,
) -> ShardDrainResult:
    """One bounded SELECT -> ClickHouse write -> DELETE for a single shard.

    The delete is reached only by the write returning normally. If the writer
    raises, this raises with it and the rows stay queued.
    """
    rows = source.fetch_shard(shard, limit=batch_size)
    if not rows:
        return ShardDrainResult(shard=shard, fetched=0, inserted=0, deleted=0)
    events = [normalise_operational_event(row) for row in rows]
    writer.insert(events)
    deleted = source.delete(rows)
    return ShardDrainResult(
        shard=shard,
        fetched=len(rows),
        inserted=len(events),
        deleted=int(deleted or 0),
    )


def drain_once(
    source: Any,
    writer: Any,
    *,
    batch_size: int,
    shard_count: int = OUTBOX_SHARDS,
) -> SweepResult:
    """Sweep every shard once, bounding each shard's batch.

    A shard that fails does not stop the sweep. One undeliverable row — a
    payload missing an allowlisted column, say, because a column was added
    while rows from the previous build were still queued — makes
    `normalise_operational_event` raise, and letting that unwind the loop
    would stop delivery for every *other* shard too, activity included, over
    one bad synthetic event. Failures are contained to their own shard,
    counted, and logged; the rows stay queued because nothing deleted them.
    """
    fetched = 0
    inserted = 0
    failed_shards = 0
    started = time.monotonic()
    for shard in range(shard_count):
        try:
            result = drain_shard_once(source, writer, shard=shard, batch_size=batch_size)
        except Exception:
            # Deliberately broad: this is a daemon sweeping independent shards,
            # and no single shard's failure is a reason to stop the others.
            failed_shards += 1
            log.exception(
                "operational_analytics_outbox.shard_failed backend=postgres shard=%d",
                shard,
            )
            continue
        fetched += result.fetched
        inserted += result.inserted
    elapsed = max(time.monotonic() - started, 0.000_001)
    return SweepResult(
        fetched=fetched,
        inserted=inserted,
        rows_per_second=inserted / elapsed,
        failed_shards=failed_shards,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=os.environ.get("TR_POSTGRES_DSN", ""))
    parser.add_argument("--iam-auth", default=os.environ.get("TR_POSTGRES_IAM_AUTH", ""))
    parser.add_argument("--iam-region", default=os.environ.get("TR_POSTGRES_IAM_REGION", ""))
    # No --shards knob. The shard count is a property of the *writer* — rows are
    # hashed into OUTBOX_SHARDS buckets at enqueue — so a drain told to sweep
    # fewer would never read or delete the rows above its count and would lose
    # them permanently while its lag metric still read zero.
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not args.dsn:
        raise SystemExit("TR_POSTGRES_DSN (or --dsn) is required")
    password = os.environ.get("CH_PASSWORD", "")
    if not password:
        raise SystemExit("CH_PASSWORD is required")
    source = PostgresOperationalOutboxSource(
        dsn=args.dsn,
        iam_auth=args.iam_auth,
        iam_region=args.iam_region,
    )
    # Take the ClickHouse identity from the SAME env vars the control plane
    # reads, so the drain and the reader cannot disagree about which cluster,
    # user, and database they mean. Defaults stay "tr"/"tr" so the GCP path is
    # untouched; the AWS-EU node is "default"/"default" because its schema is
    # applied unqualified. Getting this wrong fails authentication only after
    # a batch has been read, which is the worst moment to find out.
    writer = ClickHouseOperationalWriter(
        password=password,
        user=os.environ.get("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER", "tr"),
        database=os.environ.get("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE", "tr"),
    )
    poll_seconds = max(0.1, args.poll_seconds)
    while True:
        result = drain_once(
            source,
            writer,
            batch_size=max(1, int(args.batch_size)),
        )
        # Lag is observability, so it must never be the thing that kills the
        # drain: an error reading it says nothing about whether delivery works.
        try:
            lag_seconds = _lag_seconds(source.oldest_enqueued_at())
        except Exception:
            log.exception("operational_analytics_outbox.lag_unavailable backend=postgres")
            lag_seconds = -1.0
        log.info(
            "operational_analytics_outbox.metrics backend=postgres rows=%d "
            "rows_per_second=%.3f drain_lag_seconds=%.3f failed_shards=%d",
            result.inserted,
            result.rows_per_second,
            lag_seconds,
            result.failed_shards,
        )
        if args.once:
            return 1 if result.failed_shards else 0
        # Back off after a failing sweep as well as an empty one. Without this
        # a hard-down ClickHouse or database would spin the loop as fast as the
        # errors return.
        if result.fetched == 0 or result.failed_shards:
            time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
