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

Durability: two independent nodes, no quorum
--------------------------------------------

This cloud's analytics history lives on ClickHouse EBS volumes.  With one node
that is one volume holding every operational row the cloud has ever produced,
and losing it loses the history.  This is not an availability problem — the
gateway never reads ClickHouse, and the analytics write is a durable outbox row
inside the settle transaction — it is purely a durability problem.

The fix is a second node in a second region, written by extending the property
the drain already has rather than by replicating underneath it:

    SELECT batch -> write EVERY target -> DELETE the outbox rows

with the DELETE gated on *all* writes succeeding.  If any node is unreachable
the rows are simply not deleted, so they redeliver on the next sweep, and the
``ReplacingMergeTree`` on ``ingest_version`` collapses the duplicate that
redelivery creates on the nodes that already had it.  Two independent copies,
no coordination between them, and the failure mode is "the outbox grows"
instead of "data is lost".

**Why not ReplicatedReplacingMergeTree + Keeper.**  That is what the GCP
cluster runs, on a 3-node Keeper quorum.  Across exactly TWO regions a quorum
is worse than useless: two Keeper nodes cannot form a majority when either one
dies, so losing a region freezes writes on the *survivor*.  The AWS nodes
deliberately run plain ``ReplacingMergeTree``
(``006_operational_analytics_single_node.sql``) and are kept in step by this
drain.

**A failing node must not freeze the read.**  The batch SELECT has no offset,
so the DELETE is the only thing that advances it.  While any target fails
nothing is deleted, which means an unpaged drain re-reads the identical
lowest-ordered batch every sweep for the whole outage: rows sorting above that
window reach NO node, and the *healthy* node — the one holding the only copy of
this cloud's history — goes permanently stale.  So a batch that could not be
deleted steps an in-memory cursor over itself (`drain_shard_once`), and the
cursor resets at the end of the shard, which is what re-offers those rows once
the failing node returns.  Nothing is deleted by stepping over it; the outbox is
still the record of what is owed.

The cost of that is a bounded re-write: during an outage each full pass re-offers
the backlog to the nodes that are up, and `ReplacingMergeTree` collapses it.  One
rewrite per pass, not one per poll.

**Unbounded outbox growth when a node stays down.**  There is no automatic
bound, and that is deliberate: any automatic bound would have to either delete
undelivered rows — the exact loss this design exists to prevent — or stop the
drain, which does not help.  So the bound is operator-enforced and the drain's
job is to make the condition impossible to miss:

* every sweep logs ``degraded_targets=`` naming each endpoint whose last write
  failed, so "which node is behind" is never a guess;
* ``drain_lag_seconds`` is the age of the oldest undelivered row, and crossing
  ``--max-lag-seconds`` (default 1 hour) logs
  ``operational_analytics_outbox.backlog_alarm`` at ERROR.  That log line is
  the alert to page on.

The documented operator action on that alarm is one of exactly two things:

1. restore the failing node — nothing was deleted, so it catches up by itself
   (see below); or
2. decide the node is not coming back, remove its ``*_REPLICA_*`` variables
   from the drain's environment file and restart the drain.  Deletion resumes
   immediately, at the cost of that node being permanently behind.

Doing neither means the outbox grows until the operational database's storage
does, which is a much worse day than either choice above.

**Can a long-down node catch up?**  It depends on which of those two the
operator chose, and the distinction is the whole limitation:

* *Still configured, just failing* — YES, automatically and exactly.  Nothing
  is ever deleted while it fails, so the outbox is holding the node's entire
  backlog.  When it returns, the ordinary sweep delivers every missed row and
  dedup makes the overlap harmless.  A node can be down for as long as the
  operator is willing to let the outbox grow.
* *Removed from the config (option 2), or restored from a blank volume* — NO.
  Once the remaining targets ack and the rows are deleted, the outbox no longer
  holds them and nothing in this pipeline can reproduce them.  That node has a
  permanent hole for the whole window, and re-adding it to the config does NOT
  backfill it; it only resumes delivery from the moment it is re-added.

  Closing such a hole is an out-of-band copy from a node that has the rows,
  and it is safe for the same reason redelivery is:

      INSERT INTO activity_generations
      SELECT * FROM remote('<healthy-node>:9000', 'default',
                           'activity_generations', '<user>', '<password>')
      WHERE created_at >= '<hole start>' AND created_at < '<hole end>'

  ``ingest_version`` is carried through unchanged, so rows the node already has
  collapse rather than double.  This is a deliberate operator action with a
  window the operator must determine; it is not automatic, and pretending
  otherwise is how a "replicated" store quietly ends up with one real copy.

  Run it FROM PARIS, pushing (``INSERT INTO FUNCTION remote(...) SELECT``).
  A pull from Stockholm cannot connect: the Paris security group admits only
  the Paris VPC CIDR and the Paris ``default`` user is pinned to that CIDR plus
  loopback, so Stockholm is refused at both layers.  Only Paris → Stockholm is
  open.

What this does NOT protect against
----------------------------------

Stated plainly, because a durability mechanism that oversells itself is worse
than one whose edges are known:

* **An ack is a server ack, not an fsync.**  A target "succeeded" means
  ``clickhouse-client`` exited 0, i.e. the server accepted the insert.  Neither
  node sets ``fsync_after_insert``, so an accepted part is in the page cache.
  If a node takes an unclean shutdown in the seconds after it acked a batch that
  was then deleted from the outbox, that batch survives on the other node only —
  and nothing notices, because the drain has forgotten the rows and the two
  nodes are never compared to each other.  The fan-out makes *total* loss much
  less likely; it does not make a single node's just-acked writes durable.
* **Only ingested tables.**  This drain writes ``activity_generations``,
  ``synthetic_probe_samples``, both raw client telemetry tables, and quarantine
  rows (``EVENT_TABLES``). It does not write synthetic/client rollups or public
  snapshots. Nothing on this cloud produces those today — they are GCP timers —
  so both nodes hold them empty. If such a job is ever pointed at Paris, its
  output is NOT replicated by this drain and the second copy is not complete.
* **The alarm needs this process alive.**  ``backlog_alarm`` is emitted from the
  sweep loop, so it says nothing while the drain is not running.  A
  configuration error therefore exits with ``CONFIG_EXIT_CODE``, which the unit
  file refuses to restart, so a bad environment leaves a *failed* unit rather
  than a silent crash-loop with the outbox growing behind it.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clickhouse.ingest_operational_outbox import (
    ClickHouseOperationalWriter,
    OperationalOutboxRow,
    _lag_seconds,
    _utc,
    normalise_operational_event,
    quarantine_event,
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
# The same read, resumed past a key. Needed because the DELETE is the only thing
# that advances the unpaged statement above: while any target is failing nothing
# is deleted, so an unpaged drain re-reads the identical lowest-ordered batch on
# every sweep forever. The healthy node then stops receiving new rows entirely —
# a down replica starving the copy that is up — and rows sorting above that
# window reach NO node at all. Same defect and same fix as
# `list_open_credit_transfers`, which pages for exactly this reason.
#
# Scalar comparisons rather than the row-value form `(a, b) > (c, d)`: both are
# standard SQL, but only the scalar shape is already proven against Aurora DSQL
# in this codebase, and a syntax the drain cannot parse is a drain that delivers
# nothing.
SELECT_SHARD_BATCH_AFTER_SQL = (
    "SELECT shard, enqueued_at, event_kind, event_id, payload "
    "FROM tr_operational_analytics_outbox "
    "WHERE shard = %s "
    "AND (event_kind > %s OR (event_kind = %s AND event_id > %s)) "
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

# --------------------------------------------------------------------------
# ClickHouse targets: one endpoint per durable copy
# --------------------------------------------------------------------------

#: Environment prefix the control plane already uses for the ClickHouse it
#: READS from. The primary target reuses those names unchanged so the drain and
#: the reader cannot disagree about user and database; extra copies are
#: additive suffixes underneath the same prefix, so nothing existing moves.
CLICKHOUSE_ENV_PREFIX = "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE"

#: The endpoint the drain has always written to: the node on this machine.
PRIMARY_TARGET_NAME = "primary"

#: Suffixes searched for additional endpoints. `_REPLICA_HOST` is the second
#: copy (Stockholm today); the numbered forms exist so a third is a deployment
#: change rather than a code change. An unset `*_HOST` means "not configured"
#: and is skipped, which is why one endpoint stays the default.
REPLICA_ENV_SUFFIXES: tuple[str, ...] = ("REPLICA", "REPLICA_2", "REPLICA_3")

#: Per-replica fields under `{PREFIX}_{SUFFIX}_`. Written out rather than
#: inferred so a setting that is NOT one of these can be rejected as a typo
#: instead of silently ignored — see `_refuse_orphaned_replica_settings`.
REPLICA_ENV_FIELDS: tuple[str, ...] = (
    "HOST",
    "NAME",
    "PORT",
    "USER",
    "DATABASE",
    "SECURE",
    "TIMEOUT_SECONDS",
)

#: Native protocol. The HTTP port (8123) also works but the native port is what
#: clickhouse-client speaks by default.
DEFAULT_REPLICA_PORT = 9000

#: Finite by construction for remote endpoints — see ClickHouseOperationalWriter.
DEFAULT_REPLICA_TIMEOUT_SECONDS = 60.0

#: Age of the oldest undelivered row at which the backlog stops being normal
#: catch-up and becomes something an operator has to decide about.
DEFAULT_MAX_LAG_SECONDS = 3600.0

#: Failures a single target may accumulate in one sweep before the rest of that
#: sweep skips it. Bounds a wedged node's cost from "one timeout per shard" to
#: "a handful per sweep" without disabling a target over a single blip.
SWEEP_TARGET_FAILURE_LIMIT = 3

#: Exit status for a configuration error, distinct from a crash so systemd can
#: refuse to restart it (RestartPreventExitStatus in the unit file). A bad
#: environment cannot be fixed by running again: with Restart=always the drain
#: would crash-loop invisibly while the outbox grew at full rate, and the alarm
#: that is supposed to bound that growth is emitted BY the process that is not
#: running. A unit in `failed` is visible; a unit restarting every 5s is not.
CONFIG_EXIT_CODE = 78  # EX_CONFIG

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})  # noqa: S104


@dataclass(frozen=True)
class ClickHouseTarget:
    """One ClickHouse endpoint that must hold a copy of every row.

    `host` empty means the node on this machine, addressed with no connection
    flags at all — the historical deployment. Every other field is per-endpoint
    on purpose: the two nodes are independent installations that do not share a
    password, and nothing about them is required to match.
    """

    name: str
    password: str
    user: str = "tr"
    database: str = "tr"
    host: str = ""
    port: int = 0
    secure: bool = False
    timeout_seconds: float | None = None

    def writer(self) -> ClickHouseOperationalWriter:
        return ClickHouseOperationalWriter(
            password=self.password,
            user=self.user,
            database=self.database,
            host=self.host,
            port=self.port,
            secure=self.secure,
            timeout_seconds=self.timeout_seconds,
        )

    def describe(self) -> str:
        """Loggable identity. Never includes the password."""
        endpoint = f"{self.host}:{self.port}" if self.host else "local"
        return f"{self.name}@{endpoint}/{self.database}"


class TargetCircuitOpen(RuntimeError):
    """This target already failed repeatedly during the current sweep.

    Recorded exactly like any other write failure, so a batch containing one of
    these is never deleted. It is a *reason* the batch was not delivered, not a
    lesser kind of success.
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"skipped for the rest of this sweep after "
            f"{SWEEP_TARGET_FAILURE_LIMIT} consecutive failures; "
            f"nothing is deleted for {name}"
        )


class FanOutWriteError(RuntimeError):
    """At least one target did not accept the batch, so nothing may be deleted.

    Carries which targets succeeded as well as which failed, because the
    difference is the operator's whole diagnosis: "Stockholm failed, Paris has
    it" is a redelivery, "both failed" is an outage.
    """

    def __init__(
        self,
        failures: Sequence[tuple[str, BaseException]],
        *,
        succeeded: Sequence[str],
    ) -> None:
        self.failed_targets = [name for name, _ in failures]
        self.succeeded_targets = list(succeeded)
        detail = "; ".join(f"{name}: {exc}" for name, exc in failures)
        super().__init__(
            "ClickHouse fan-out incomplete, outbox rows NOT deleted "
            f"(ok={','.join(self.succeeded_targets) or '-'} "
            f"failed={','.join(self.failed_targets)}): {detail}"
        )


class FanOutOperationalWriter:
    """Writes one batch to every target and succeeds only if ALL of them do.

    The caller (`drain_shard_once`) reaches its DELETE only when `insert`
    returns normally, so raising here is exactly "leave the rows queued". That
    is the entire durability contract: two independent copies, and a partial
    write is retried rather than acknowledged.
    """

    def __init__(self, targets: Sequence[tuple[str, Any]]) -> None:
        if not targets:
            raise ValueError("at least one ClickHouse target is required")
        self._targets = list(targets)
        self.consecutive_failures: dict[str, int] = {name: 0 for name, _ in self._targets}
        self._sweep_failures: dict[str, int] = {name: 0 for name, _ in self._targets}

    def begin_sweep(self) -> None:
        """Re-arm the per-sweep breaker. Called by `drain_once` per sweep."""
        self._sweep_failures = {name: 0 for name, _ in self._targets}

    def insert(self, events: list[Any]) -> None:
        failures: list[tuple[str, BaseException]] = []
        succeeded: list[str] = []
        for name, writer in self._targets:
            if self._sweep_failures.get(name, 0) >= SWEEP_TARGET_FAILURE_LIMIT:
                # Hard-down node: stop paying its timeout on every remaining
                # shard of this sweep. A remote write is bounded by
                # DEFAULT_REPLICA_TIMEOUT_SECONDS and is issued once per target
                # per event kind per shard, so a node that stalls rather than
                # refusing would otherwise cost 32 shards x 2 kinds x 60s ~ 64
                # MINUTES for a single sweep -- during which the healthy node
                # receives nothing new and the backlog alarm, which is the only
                # bound on outbox growth, is evaluated once.
                #
                # A skip is recorded as a FAILURE, never as a success: nothing
                # may be deleted for a target that did not receive the batch.
                # The threshold is >1 so a single blip does not disable a target
                # for the rest of the sweep.
                failures.append((name, TargetCircuitOpen(name)))
                continue
            try:
                writer.insert(events)
            except Exception as exc:  # noqa: BLE001 - recorded, then re-raised below.
                # EVERY target is attempted even after one fails, rather than
                # short-circuiting on the first. Stopping at the first failure
                # would mean a down primary also delayed the healthy replica's
                # copy of THIS batch, for no gain: nothing is deleted either
                # way. (What actually keeps the healthy node current across
                # sweeps is the read cursor in `drain_shard_once`, not this
                # loop -- without it a down target freezes the read window and
                # the healthy node stops receiving new rows entirely.)
                failures.append((name, exc))
                self._sweep_failures[name] = self._sweep_failures.get(name, 0) + 1
            else:
                succeeded.append(name)
        for name in succeeded:
            self.consecutive_failures[name] = 0
        for name, _ in failures:
            self.consecutive_failures[name] += 1
        if failures:
            raise FanOutWriteError(failures, succeeded=succeeded)

    def degraded_targets(self) -> list[str]:
        return [name for name, count in self.consecutive_failures.items() if count]


def build_operational_writer(targets: Sequence[ClickHouseTarget]) -> Any:
    """The writer for `targets`: a bare one for a single copy, a fan-out for more.

    A single-endpoint deployment gets the plain `ClickHouseOperationalWriter`
    rather than a fan-out of one, so it keeps not only today's argv but today's
    exception type and today's log text. "No behaviour change for one node"
    should mean nothing at all changed, not that a wrapper happened to be
    transparent.
    """
    writers = [(target.name, target.writer()) for target in targets]
    if len(writers) == 1:
        return writers[0][1]
    return FanOutOperationalWriter(writers)


def degraded_target_names(writer: Any) -> list[str]:
    """Names of endpoints whose most recent write failed; empty for one node."""
    getter = getattr(writer, "degraded_targets", None)
    return list(getter()) if callable(getter) else []


def _env_flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _refuse_orphaned_replica_settings(env: Mapping[str, str]) -> None:
    """A replica half-configured is a replica silently not configured.

    Validation here used to run in one direction only: `_HOST` without a
    password was fatal, but a password (or a name, or a port) without a `_HOST`
    was skipped in silence. The drain then built ONE target, logged
    `copies=1 degraded_targets=-`, and — because a single target is deletable —
    started deleting rows the second node never received. Indistinguishable in
    every log line from a deliberate single-node deployment, and reached by
    misspelling one variable in a hand-edited environment file.

    So any `*_REPLICA*` setting is treated as evidence that a second copy was
    INTENDED, and a missing host for it is a startup error rather than a
    shrug. Nothing here can tell a typo from a decision, which is exactly why
    it must refuse instead of guess.
    """
    recognised: dict[str, str] = {}
    for suffix in REPLICA_ENV_SUFFIXES:
        recognised[f"CH_{suffix}_PASSWORD"] = suffix
        for field in REPLICA_ENV_FIELDS:
            recognised[f"{CLICKHOUSE_ENV_PREFIX}_{suffix}_{field}"] = suffix

    for key, value in sorted(env.items()):
        if "REPLICA" not in key or not str(value).strip():
            continue
        if not key.startswith(("CH_", CLICKHOUSE_ENV_PREFIX)):
            continue
        suffix = recognised.get(key)
        if suffix is None:
            # Spelled like replica configuration but matching nothing this code
            # reads: `..._REPLICA_HOSTS`, `..._REPLICA1_HOST`, the right name
            # under the wrong prefix. Ignoring it is what makes a typo look
            # exactly like a working two-copy deployment.
            raise ValueError(
                f"{key} is set but is not a setting this drain reads. "
                "It looks like replica configuration, so it is refused rather "
                "than ignored: an ignored replica setting means the drain runs "
                "single-node and deletes rows the second node never received."
            )
        if not env.get(f"{CLICKHOUSE_ENV_PREFIX}_{suffix}_HOST", "").strip():
            raise ValueError(
                f"{key} is set but {CLICKHOUSE_ENV_PREFIX}_{suffix}_HOST is not. "
                "A replica configured this way is silently NOT a second copy: "
                "the drain would run single-node and delete rows that never "
                "reached it."
            )


def _is_own_address(host: str) -> bool:
    """Whether `host` is an address belonging to THIS machine.

    An address can be bound only on the machine that owns it, so a UDP bind is
    the answer without a name lookup, without a packet, and without a timeout to
    get wrong. Deliberately NOT resolution-based: a hostname lookup here would
    put DNS (and, on a laptop, mDNS) on the drain's startup path.

    Literal addresses only, which is also the realistic mistake: the deployment
    addresses ClickHouse by private IP -- the provisioning script's own
    instructions paste one -- so pasting the LOCAL node's private IP is how a
    "second copy" ends up on the same EBS volume. A non-literal host is left to
    the name-based check.
    """
    import ipaddress
    import socket

    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return False
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.bind((host, 0))
    except OSError:
        return False
    return True


def clickhouse_targets_from_env(
    env: Mapping[str, str],
    *,
    local_hosts: frozenset[str] | set[str] | None = None,
) -> list[ClickHouseTarget]:
    """Read the configured endpoints, primary first.

    The primary is described by exactly the variables that already existed —
    `CH_PASSWORD` plus the control plane's `_USER` / `_DATABASE` — and every
    additional copy is a separate, optional block. So an environment that has
    never heard of replicas yields precisely one target and precisely today's
    behaviour, and adding Stockholm is additive rather than a migration.
    """
    password = env.get("CH_PASSWORD", "")
    if not password:
        raise ValueError("CH_PASSWORD is required")
    user = env.get(f"{CLICKHOUSE_ENV_PREFIX}_USER", "tr")
    database = env.get(f"{CLICKHOUSE_ENV_PREFIX}_DATABASE", "tr")
    targets = [
        ClickHouseTarget(
            name=PRIMARY_TARGET_NAME,
            password=password,
            user=user,
            database=database,
        )
    ]
    is_local = (lambda host: host in local_hosts) if local_hosts is not None else _is_own_address
    _refuse_orphaned_replica_settings(env)
    for suffix in REPLICA_ENV_SUFFIXES:
        prefix = f"{CLICKHOUSE_ENV_PREFIX}_{suffix}"
        host = env.get(f"{prefix}_HOST", "").strip()
        if not host:
            continue
        if host.lower() in _LOOPBACK_HOSTS or is_local(host):
            # A "replica" on this machine is a second copy on the SAME EBS
            # volume, which is zero additional durability while reading as two
            # copies everywhere. Refuse it at startup rather than let it be
            # discovered after the volume is gone.
            raise ValueError(
                f"{prefix}_HOST={host!r} points at this machine; a second copy "
                "on the same volume is not a second copy"
            )
        replica_password = env.get(f"CH_{suffix}_PASSWORD", "")
        if not replica_password:
            # Fail here, at startup, and not on the first insert: by then a
            # batch has already been read out of the outbox, which is the worst
            # possible moment to learn the credentials are missing.
            raise ValueError(f"CH_{suffix}_PASSWORD is required when {prefix}_HOST is set")
        targets.append(
            ClickHouseTarget(
                name=env.get(f"{prefix}_NAME", "").strip() or suffix.lower(),
                password=replica_password,
                # Default to the primary's identity but allow an override: the
                # two nodes are independent installations and need not agree.
                user=env.get(f"{prefix}_USER", "").strip() or user,
                database=env.get(f"{prefix}_DATABASE", "").strip() or database,
                host=host,
                port=int(env.get(f"{prefix}_PORT", "").strip() or DEFAULT_REPLICA_PORT),
                secure=_env_flag(env.get(f"{prefix}_SECURE", "")),
                timeout_seconds=float(
                    env.get(f"{prefix}_TIMEOUT_SECONDS", "").strip()
                    or DEFAULT_REPLICA_TIMEOUT_SECONDS
                ),
            )
        )
    names = [target.name for target in targets]
    if len(set(names)) != len(names):
        # Names are how the logs and the alarm identify which copy is behind;
        # two endpoints sharing one makes that report unreadable.
        raise ValueError(f"ClickHouse target names must be unique, got {names}")
    return targets


@dataclass(frozen=True)
class ShardDrainResult:
    shard: int
    fetched: int
    inserted: int
    deleted: int
    quarantined: int = 0


@dataclass(frozen=True)
class SweepResult:
    """One pass over every shard.

    Carries `failed_shards` because a sweep is no longer all-or-nothing: shards
    fail independently, and a caller that could not tell the difference would
    log a healthy-looking rows=0 while delivery was entirely broken.

    `degraded_targets` is every endpoint that failed at least ONE shard during
    the sweep, which is not the same question as "did the last write fail" and
    is the one an operator is actually asking.
    """

    fetched: int
    inserted: int
    rows_per_second: float
    failed_shards: int = 0
    degraded_targets: tuple[str, ...] = ()
    quarantined: int = 0


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

    def fetch_shard(
        self,
        shard: int,
        *,
        limit: int,
        after: tuple[str, str] | None = None,
    ) -> list[OperationalOutboxRow]:
        """The next `limit` rows of `shard`, optionally resumed past `after`.

        `after=None` issues the byte-identical statement this has always issued,
        so the single-node deployment reads exactly what it read before paging
        existed. Paging is used only when a batch could not be deleted.
        """
        if limit < 1:
            return []
        if after is None:
            statement, params = SELECT_SHARD_BATCH_SQL, (shard, limit)
        else:
            event_kind, event_id = after
            statement = SELECT_SHARD_BATCH_AFTER_SQL
            params = (shard, event_kind, event_kind, event_id, limit)  # type: ignore[assignment]

        def read(conn: Any) -> list[OperationalOutboxRow]:
            rows = conn.execute(statement, params).fetchall()
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
    cursors: dict[int, tuple[str, str]] | None = None,
) -> ShardDrainResult:
    """One bounded SELECT -> ClickHouse write -> DELETE for a single shard.

    The delete is reached only by the write returning normally. If the writer
    raises, this raises with it and the rows stay queued.

    This is unchanged by dual-write, and deliberately so: with several targets
    the writer is a `FanOutOperationalWriter` that returns normally only when
    EVERY target accepted the batch, so "the delete is gated on the write" and
    "the delete is gated on all copies" are the same statement. Nothing here
    knows how many copies there are.

    `cursors` is the caller's in-memory read position per shard, and exists
    because the DELETE is the only thing that advances an unpaged read. A batch
    that could not be deleted — a target down, or a payload this build cannot
    normalise — would otherwise be re-read and re-written on every sweep for
    the whole outage, while every row sorting above it reached no node at all.
    So a batch that is NOT deleted steps the cursor over itself, and a batch
    that IS deleted clears it: the rows are gone, and the next read starts at
    the head exactly as it always did.

    Stepping over a batch never forgets it. Nothing was deleted, so the rows are
    still in the outbox, and the cursor resets at the end of the shard — the
    next pass re-reads them and delivers them once the failing target is back.
    """
    after = cursors.get(shard) if cursors is not None else None
    # `after=None` calls this with exactly the historical arguments, so a source
    # that has never heard of paging (and the single-node deployment) is
    # untouched.
    rows = (
        source.fetch_shard(shard, limit=batch_size)
        if after is None
        else source.fetch_shard(shard, limit=batch_size, after=after)
    )
    if not rows:
        # End of the shard. Restart at the head next pass, which is what
        # re-offers any batch this pass stepped over.
        if cursors is not None:
            cursors.pop(shard, None)
        return ShardDrainResult(shard=shard, fetched=0, inserted=0, deleted=0)
    # A short batch means this is the shard's last one; there is nothing further
    # to make progress on, so the cursor resets whatever happens below.
    exhausted = len(rows) < batch_size
    last_key = (rows[-1].event_kind, rows[-1].event_id)
    try:
        events: list[Any] = []
        quarantined = 0
        for row in rows:
            try:
                events.extend(normalise_operational_event(row))
            except ValueError as exc:
                events.append(quarantine_event(row, exc))
                quarantined += 1
        writer.insert(events)
    except BaseException:
        if cursors is not None:
            if exhausted:
                cursors.pop(shard, None)
            else:
                cursors[shard] = last_key
        raise
    deleted = source.delete(rows)
    if cursors is not None:
        cursors.pop(shard, None)
    return ShardDrainResult(
        shard=shard,
        fetched=len(rows),
        inserted=len(events) - quarantined,
        deleted=int(deleted or 0),
        quarantined=quarantined,
    )


def drain_once(
    source: Any,
    writer: Any,
    *,
    batch_size: int,
    shard_count: int = OUTBOX_SHARDS,
    cursors: dict[int, tuple[str, str]] | None = None,
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
    quarantined = 0
    failed_shards = 0
    degraded: dict[str, None] = {}
    # Lets a fan-out stop re-dialling a target that has already failed several
    # times THIS sweep; see FanOutOperationalWriter.begin_sweep. Absent on the
    # plain single-node writer, which is why this is a getattr.
    begin_sweep = getattr(writer, "begin_sweep", None)
    if callable(begin_sweep):
        begin_sweep()
    started = time.monotonic()
    for shard in range(shard_count):
        try:
            result = drain_shard_once(
                source, writer, shard=shard, batch_size=batch_size, cursors=cursors
            )
        except Exception as exc:
            # Deliberately broad: this is a daemon sweeping independent shards,
            # and no single shard's failure is a reason to stop the others.
            failed_shards += 1
            # Accumulated over the WHOLE sweep, not read off the writer at the
            # end of it. The writer's own counter is reset by any success, so a
            # target that rejected 31 shards and accepted the 32nd would report
            # as healthy in the one field the runbook says to watch.
            for name in getattr(exc, "failed_targets", ()):
                degraded[str(name)] = None
            log.exception(
                "operational_analytics_outbox.shard_failed backend=postgres shard=%d",
                shard,
            )
            continue
        fetched += result.fetched
        inserted += result.inserted
        quarantined += result.quarantined
    elapsed = max(time.monotonic() - started, 0.000_001)
    return SweepResult(
        fetched=fetched,
        inserted=inserted,
        rows_per_second=inserted / elapsed,
        failed_shards=failed_shards,
        degraded_targets=tuple(degraded),
        quarantined=quarantined,
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
    # The backlog alarm. With more than one target the outbox is what absorbs a
    # node being down, so its depth is the signal that a node has been down
    # long enough to need a decision rather than patience.
    parser.add_argument(
        "--max-lag-seconds",
        type=float,
        default=float(
            os.environ.get("TR_OPERATIONAL_ANALYTICS_MAX_LAG_SECONDS", "")
            or DEFAULT_MAX_LAG_SECONDS
        ),
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not args.dsn:
        raise SystemExit("TR_POSTGRES_DSN (or --dsn) is required")
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
    # a batch has been read, which is the worst moment to find out — so every
    # configuration error the drain can detect is raised here, at startup.
    try:
        targets = clickhouse_targets_from_env(os.environ)
    except ValueError as exc:
        # CONFIG_EXIT_CODE, not the default 1: the unit sets
        # RestartPreventExitStatus for it, so a bad environment stops the unit
        # visibly instead of crash-looping every RestartSec while the outbox
        # grows and nothing is left running to raise the backlog alarm.
        log.error("operational_analytics_outbox.config_invalid backend=postgres error=%s", exc)
        raise SystemExit(CONFIG_EXIT_CODE) from exc
    writer = build_operational_writer(targets)
    log.info(
        "operational_analytics_outbox.targets backend=postgres copies=%d targets=%s",
        len(targets),
        ",".join(target.describe() for target in targets),
    )
    poll_seconds = max(0.1, args.poll_seconds)
    max_lag_seconds = float(args.max_lag_seconds)
    if max_lag_seconds <= 0:
        # `lag >= max > 0` used to silently disable the alarm for a
        # non-positive value. The alarm is the ONLY bound on outbox growth, so
        # a value that turns it off is a configuration error, not a setting.
        log.error(
            "operational_analytics_outbox.config_invalid backend=postgres "
            "error=--max-lag-seconds must be positive (got %s); it is the only "
            "bound on outbox growth",
            max_lag_seconds,
        )
        raise SystemExit(CONFIG_EXIT_CODE)
    # Per-shard read position, owned here so it survives ACROSS sweeps: a batch
    # that could not be deleted is stepped over, and that only helps if the next
    # sweep remembers where it got to.
    cursors: dict[int, tuple[str, str]] = {}
    while True:
        result = drain_once(
            source,
            writer,
            batch_size=max(1, int(args.batch_size)),
            cursors=cursors,
        )
        # Lag is observability, so it must never be the thing that kills the
        # drain: an error reading it says nothing about whether delivery works.
        try:
            lag_seconds = _lag_seconds(source.oldest_enqueued_at())
        except Exception:
            log.exception("operational_analytics_outbox.lag_unavailable backend=postgres")
            lag_seconds = -1.0
        # Union of "failed at some point during this sweep" and "is failing
        # right now". The first is what the operator is asking; the second
        # carries across a sweep in which the target was skipped by the breaker.
        degraded = sorted({*result.degraded_targets, *degraded_target_names(writer)})
        log.info(
            "operational_analytics_outbox.metrics backend=postgres rows=%d "
            "rows_per_second=%.3f drain_lag_seconds=%.3f failed_shards=%d "
            "copies=%d degraded_targets=%s quarantined=%d",
            result.inserted,
            result.rows_per_second,
            lag_seconds,
            result.failed_shards,
            len(targets),
            ",".join(degraded) or "-",
            result.quarantined,
        )
        # The bound on outbox growth is this line plus an operator. There is no
        # automatic one: the only automatic bounds available are "delete rows a
        # copy never received", which is the loss this whole design exists to
        # prevent, and "stop draining", which helps nobody. See the module
        # docstring for the two actions this alarm asks for.
        if lag_seconds >= max_lag_seconds > 0:
            log.error(
                "operational_analytics_outbox.backlog_alarm backend=postgres "
                "drain_lag_seconds=%.3f max_lag_seconds=%.3f degraded_targets=%s "
                "action=restore-the-node-or-drop-it-from-the-drain-config",
                lag_seconds,
                max_lag_seconds,
                ",".join(degraded) or "-",
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
