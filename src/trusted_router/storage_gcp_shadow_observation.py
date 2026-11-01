"""Thin native adapter for independent health and immutable receipt checkpoints."""
from __future__ import annotations

import copy
import datetime as dt
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from trusted_router.async_settle_shadow_health_state import (
    CYCLE_SECONDS,
    claim_allowed,
    publish_allowed,
    summarize,
)
from trusted_router.async_settle_shadow_journal import (
    CERTIFICATE_KIND,
    CLAIM_KIND,
    CLOSE_RESULT_KIND,
    CLOSURE_KIND,
    CONTROL_KIND,
    COUNTER_KIND,
    EMPTY_CHAIN,
    HEALTH_KIND,
    SAMPLE_KIND,
    Row,
    reserve_cap,
)
from trusted_router.async_settle_shadow_v2_a1_deltas import counter_projection, without
from trusted_router.async_settle_shadow_v2_a1_health import HEALTH_CLAIM
from trusted_router.async_settle_shadow_v2_evidence import (
    TRANSACTION_BODY_BYTES,
    transaction_bodies,
)
from trusted_router.async_settle_shadow_v2_schema import POLICY, require
from trusted_router.detached_jws import canonical
from trusted_router.storage_gcp_async_admission import HEALTH_ROW_LIMIT, unresolved_statement
from trusted_router.storage_gcp_shadow_io import (
    PUBLISHER_STOP_CLEANUP_SECONDS,
    PublisherTransactions,
    Sessions,
    configure,
    elapsed_clock,
    finish_transaction_rpcs,
    owned_database,
    rpc_scope,
    timestamp_ns,
)

WRITE_SECONDS = 2.
READ_SECONDS = 1.
FLUSH_SECONDS = 4.
# Observer reads: the RPC gets a generous gRPC deadline and its stream is fully
# consumed, so a slow tail completes OK on the server instead of ending in a
# client DEADLINE_EXCEEDED/CANCELLED (2026-10-23: >1/min of those tripped the
# Spanner API-failure alert). Freshness is unchanged: the observer's install
# cut-off (250 ms health, 500 ms workspace) still rejects a late result as a
# late_install, and health_usable still requires read_elapsed <= 250 ms.
OBSERVER_READ_RPC_SECONDS = 2.
T = TypeVar("T")
Statement = tuple[str, dict[str, Any], dict[str, Any]]


POINT_SQL = "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id"
CLAIM_SQL = "SELECT (SELECT body FROM tr_entities WHERE kind=@kind AND id=@id) AS body, CURRENT_TIMESTAMP() AS claim_at"
HEALTH_SQL = "SELECT body, updated_at FROM tr_entities WHERE kind=@kind AND id=@id"
# The shared v1 builder supplies bindings; require exact SQL agreement before
# dispatch. Explicit native statements keep the static shape guard effective.
UNRESOLVED_SQL = ("SELECT unresolved_at, actual_cost_micro, status FROM tr_settle_outbox"
                  "@{FORCE_INDEX=tr_settle_outbox_unresolved} "
                  "WHERE unresolved_at IS NOT NULL ORDER BY unresolved_at LIMIT @limit")


def entity_point_statement(kind: str, identity: str) -> Statement:
    from google.cloud.spanner_v1 import param_types
    return POINT_SQL, {"kind": kind, "id": identity}, {"kind": param_types.STRING, "id": param_types.STRING}


@dataclass(frozen=True)
class Budget:
    deadline: float
    clock: Callable[[], float] = elapsed_clock

    def remaining(self, cap: float) -> float:
        remaining = min(cap, self.deadline - self.clock())
        if remaining <= 0:
            raise TimeoutError("shadow_budget")
        return remaining

    def child(self, cap: float) -> Budget:
        return Budget(min(self.deadline, self.clock() + cap), self.clock)


class UnknownCommit(RuntimeError):
    pass


class ContentionLoss(RuntimeError):
    """The transaction lost its claim or aborted; it definitively did not commit."""


def shadow_health_claim_read() -> Statement:
    _, params, types = entity_point_statement(CLAIM_KIND, "local/publish")
    return CLAIM_SQL, params, types


def shadow_health_publish_fence() -> Statement:
    return entity_point_statement(CLAIM_KIND, "local/publish")


def shadow_health_previous() -> Statement:
    return entity_point_statement(HEALTH_KIND, "local/fleet")


def read_shadow_health_statement() -> Statement:
    _, params, types = entity_point_statement(HEALTH_KIND, "local/fleet")
    return HEALTH_SQL, params, types


class ObservationStore:
    def __init__(self, database: Any, *, clock: Callable[[], float] = elapsed_clock,
                 wall: Callable[[], float] = time.time) -> None:
        self.workspace_database = owned_database(database, lane="workspace")
        self.database, self.clock, self.wall = owned_database(database), clock, wall
        self.last_claim: dict[str, Any] | None = None
        self.won: tuple[str, int] | None = None  # (boot, slot) of the last claim committed here.
        # Scheduling hints only; neither proves ownership. Unknown commits must
        # be reconciled from the claim row in the next slot, never retried here.
        self.claim_attempt: tuple[str, int] | None = None
        self.claim_unknown = False
        self.last_publication: tuple[str, int] | None = None
        self.unpublished_since: int | None = None
        self.owns_publisher_database = False
        self.publisher_transactions: PublisherTransactions | None = None
        self.publisher_lane_closed = False
        self.publisher_cleanup_lock = threading.Lock()
        self.startup_retention = False
        self.startup_claim_raw: bytes | None = None
        self.startup_publication_raw: bytes | None = None
        self.reconciliations: dict[str, set[tuple[str, ...]]] = {}
        self.used_reconciliations: dict[str, int] = {}
        self.transport = configure(self.database)
        self.workspace_transport = configure(self.workspace_database)
        # Partial offline transports can exercise sessions without snapshot reads.
        self.supports_runtime_stop = callable(getattr(self.database, "snapshot", None))

    def publisher_lane(self) -> ObservationStore:
        """A worker owns its mutable hints and session; retirement cannot hit readers.

        Cloning the adapter preserves explicit offline transports. Native clients
        receive a distinct Sessions manager/API, including on worker replacement.
        No state from an abandoned worker is subsequently merged into this lane.
        """
        lane = copy.copy(self)
        lane.database = owned_database(self.database, lane="publisher")
        lane.owns_publisher_database = lane.database is not self.database
        lane.workspace_database = lane.database  # Preparation owns only this lane.
        lane.transport = lane.workspace_transport = configure(lane.database)
        lane.last_claim = lane.won = lane.claim_attempt = lane.last_publication = None
        lane.claim_unknown = False
        lane.unpublished_since = None
        lane.publisher_lane_closed = False
        lane.publisher_cleanup_lock = threading.Lock()
        return lane

    def close_publisher_lane(self) -> bool:
        """The retired worker owns deletion and client closure, including retries."""
        with self.publisher_cleanup_lock:
            return self._close_publisher_lane()

    def _close_publisher_lane(self) -> bool:
        if self.publisher_lane_closed:
            return True
        if not self.owns_publisher_database:
            return True  # Explicit offline transports may share their database.
        manager = self.database._sessions_manager
        stopping = self.publisher_transactions is not None and self.publisher_transactions.stopped_at is not None
        if not manager.close(self.clock, PUBLISHER_STOP_CLEANUP_SECONDS if stopping else 5.):
            return False
        try:
            self.database.spanner_api.transport.close()
        except Exception:
            return False
        self.publisher_lane_closed = True
        return True

    def stop_publisher_transaction(self, transaction: Any) -> None:
        """Shutdown-only fallback when a worker cannot resume after RPC cancel.

        Admission is already terminal. Rollback and session deletion operate on
        known identities, without waiting for the worker's checkout lock. The
        worker may later return, but cannot dispatch another transaction RPC.
        """
        with finish_transaction_rpcs(), rpc_scope(self.clock()+2, 2, self.clock):
            if not transaction.rolled_back and transaction.committed is None:
                transaction.rollback()
        if not self.owns_publisher_database:
            self.close_publisher_lane()
            return
        with self.publisher_cleanup_lock:
            if self.publisher_lane_closed:
                return
            manager = self.database._sessions_manager
            with rpc_scope(self.clock()+PUBLISHER_STOP_CLEANUP_SECONDS, PUBLISHER_STOP_CLEANUP_SECONDS, self.clock):
                session = manager.session or manager.retired
                if session is not None:
                    session.delete()
                    manager.session = manager.retired = None
            self.database.spanner_api.transport.close()
            self.publisher_lane_closed = True

    def session_ready(self, *, workspace: bool = False) -> bool:
        database = self.workspace_database if workspace else self.database
        manager = getattr(database, "_sessions_manager", None)
        return not isinstance(manager, Sessions) or manager.ready

    def session_recovery_due(self) -> bool:
        return any(isinstance(manager, Sessions) and manager.recovery_due(self.clock)
                   for manager in (getattr(self.database, "_sessions_manager", None),
                                   getattr(self.workspace_database, "_sessions_manager", None)))

    def prepare_session(self, boot: str | None = None) -> bool:
        manager = getattr(self.database, "_sessions_manager", None)
        ready = not isinstance(manager, Sessions) or manager.prepare(self.clock, boot)
        workspace = getattr(self.workspace_database, "_sessions_manager", None)
        if isinstance(workspace, Sessions) and workspace is not manager:
            # Preserve the boot identity; the separate tr_lane label isolates
            # reconciliation under Spanner's substring label matching.
            workspace_boot = None if boot is None else f"{boot}-workspace"
            workspace_ready = workspace.prepare(self.clock, workspace_boot)
            # Prepare both even when the first lane failed or is quarantined;
            # each retains its own five-second budget and recovery window.
            ready = ready and workspace_ready
        return ready

    def query(self, reader: Any, statement: Statement, budget: Budget, cap: float) -> list[Any]:
        sql, params, types = statement
        require(sql in (POINT_SQL, CLAIM_SQL), "unregistered_statement")
        with rpc_scope(budget.deadline, cap, budget.clock):
            rows = list(reader.execute_sql(POINT_SQL if sql == POINT_SQL else CLAIM_SQL, params=params, param_types=types, retry=None,
                        timeout=budget.remaining(cap), request_options={"priority": "PRIORITY_LOW"}))
        budget.remaining(cap)
        return rows

    def point(self, reader: Any, statement: Statement, budget: Budget, cap: float) -> Row | None:
        rows = self.query(reader, statement, budget, cap)
        require(len(rows) <= 1, "point_bound")
        if not rows:
            return None
        params = statement[1]
        body = rows[0][0]
        require(type(body) is str, "row_encoding")
        row = Row(params["kind"], params["id"], body.encode("utf-8"))
        row.value()
        return row

    def read(self, kind: str, key: str, budget: Budget) -> Row | None:
        budget = budget.child(READ_SECONDS)
        with rpc_scope(budget.deadline, READ_SECONDS, budget.clock):
            with self.database.snapshot() as snapshot:
                return self.point(snapshot, entity_point_statement(kind, key), budget, READ_SECONDS)

    def transaction(self, callback: Callable[[Any], T], budget: Budget, cap: float, *, publisher: bool = False) -> T:
        from google.api_core.exceptions import Aborted

        attempted = False
        committing = False
        aborted = False
        loss: ContentionLoss | None = None
        native_tx: Any = None
        def record_abort(error: BaseException) -> None:
            nonlocal aborted
            seen: set[int] = set()
            cause: BaseException | None = error
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                if isinstance(cause, Aborted):
                    aborted = True
                    break
                cause = cause.__cause__
        def once(tx: Any) -> T:
            nonlocal attempted, committing, aborted, loss, native_tx
            if attempted:
                # Session re-enters only after ABORTED. Do not run a second attempt.
                aborted = True
                raise ContentionLoss("transaction_aborted")
            attempted = True
            native_tx = tx
            if publisher and self.publisher_transactions is not None:
                self.publisher_transactions.transaction = tx
                self.publisher_transactions.lane = self
            try:
                budget.remaining(cap)
                if (publisher and self.publisher_transactions is not None
                        and getattr(getattr(self.database, "spanner_api", None), "transport", None) is not None):
                    # Know the transaction identity even if its first claim or
                    # fence read blocks before yielding metadata. Shutdown can
                    # then roll it back instead of abandoning an implicit begin.
                    tx.begin()
                result = callback(tx)
                budget.remaining(cap)
            except Exception as error:
                # SDK rollback can replace this exception. Retain the abort now.
                if isinstance(error, ContentionLoss):
                    loss = error
                record_abort(error)
                raise
            committing = True
            return result
        try:
            with rpc_scope(budget.deadline, cap, budget.clock):
                try:
                    result = self.database.run_in_transaction(once, timeout_secs=budget.remaining(cap),
                        commit_request_options={"priority": "PRIORITY_LOW"}, transaction_tag="tr_shadow_v2")
                except Exception:
                    # The SDK rolls back callback errors, but not Commit errors.
                    # A shutdown cancellation needs cleanup in either phase.
                    if (publisher and committing and self.publisher_transactions is not None
                            and self.publisher_transactions.stopped_at is not None
                            and native_tx is not None and not getattr(native_tx, "rolled_back", False)):
                        native_tx.rollback()
                    raise
            budget.remaining(cap)
            return result
        except ContentionLoss:
            raise
        except Exception as error:
            # The bounded transport keeps SDK retry loops stopped with a plain
            # exception, retaining ABORTED as its cause for this classification.
            record_abort(error)
            if loss is not None:
                raise loss from None
            if aborted:
                raise ContentionLoss("transaction_aborted") from None
            if committing:
                raise UnknownCommit("shadow_commit_unknown") from None
            raise

    def write(self, tx: Any, rows: list[Row], budget: Budget) -> None:
        # Bodies are immutable schema-serialized bytes; exact retries never rebuild.
        for row in rows:
            row.value()
        budget.remaining(WRITE_SECONDS)
        from google.cloud.spanner_v1 import COMMIT_TIMESTAMP
        tx.insert_or_update(table="tr_entities", columns=("kind", "id", "body", "updated_at"),
            values=[(r.kind, r.key, r.body.decode("utf-8"), (COMMIT_TIMESTAMP if r.kind == HEALTH_KIND else dt.datetime.fromtimestamp(self.wall(), dt.UTC))) for r in rows])

    def read_shadow_claim(self, budget: Budget) -> dict[str, Any] | None:
        row = self.read(CLAIM_KIND, "local/publish", budget.child(CYCLE_SECONDS))
        return row.value() if row is not None else None

    def read_shadow_publication(self, budget: Budget) -> dict[str, Any] | None:
        """Lock-free point read of the fleet health row (same-owner renewal)."""
        row = self.read(HEALTH_KIND, "local/fleet", budget.child(CYCLE_SECONDS))
        return row.value() if row is not None else None

    def claim(self, boot: str, slot: int, claim_us: int, budget: Budget) -> bool:
        self.last_claim = None
        self.claim_attempt = (boot, slot)
        self.claim_unknown = False
        if self.unpublished_since is None:
            self.unpublished_since = slot
        original = None
        def run(tx: Any) -> bool:
            nonlocal original
            rows = self.query(tx, shadow_health_claim_read(), budget, CYCLE_SECONDS)
            require(len(rows) == 1, "point_bound")
            body, server = rows[0]
            value = dict(v=2, policy_version=POLICY, contract_revision="a1", authority="local",
                         slot=slot, claim_at_ns=timestamp_ns(server), owner_boot_id=boot)
            HEALTH_CLAIM(value)  # The contract rejects skew before any write/scan.
            self.last_claim = value
            old = Row(CLAIM_KIND, "local/publish", body.encode()).value() if body is not None else None
            if (not claim_allowed(old, slot) or old is not None
                    and old["owner_boot_id"] != boot and old["slot"] > slot-2):
                # Returning False still sends an empty Commit and holds the
                # hot row until it completes. A loss must roll back instead;
                # like ABORTED it is recorded once, never retried in this slot.
                raise ContentionLoss("slot_already_claimed")
            if old is None or old["owner_boot_id"] != boot:
                self.unpublished_since = slot
            row = Row.build(CLAIM_KIND, "local/publish", value)
            self.write(tx, [row], budget)
            if self.startup_retention:
                original = row.body
            return True
        try:
            with finish_transaction_rpcs(self.publisher_transactions):
                landed = self.transaction(run, budget, CYCLE_SECONDS, publisher=True)
        except UnknownCommit:
            self.claim_unknown = True
            raise
        if landed:
            self.won = (boot, slot)
        if landed and self.startup_retention:
            self.startup_claim_raw = original
        return landed

    def publish(self, value: dict[str, Any], budget: Budget) -> bool:
        row = Row.build(HEALTH_KIND, "local/fleet", value)
        def run(tx: Any) -> bool:
            fence = self.point(tx, shadow_health_publish_fence(), budget, CYCLE_SECONDS)
            old = self.point(tx, shadow_health_previous(), budget, CYCLE_SECONDS)
            if not publish_allowed(fence.value() if fence else None, old.value() if old else None, value):
                return False
            self.write(tx, [row], budget)
            return True
        with finish_transaction_rpcs(self.publisher_transactions):
            landed = self.transaction(run, budget, CYCLE_SECONDS, publisher=True)
        if landed:
            self.last_publication = (value["publisher_boot_id"], value["slot"])
            self.unpublished_since = value["slot"] + 1
        if landed and self.startup_retention:
            self.startup_publication_raw = row.body
        return landed

    def snapshot_query(self, statement: Statement, budget: Budget, cap: float,
                       *, min_read_timestamp: Any = None) -> tuple[list[Any], int]:
        with rpc_scope(budget.deadline, cap, budget.clock):
            options = {} if min_read_timestamp is None else {"min_read_timestamp": min_read_timestamp}
            with self.database.snapshot(**options) as snapshot:
                sql, params, types = statement
                require(sql in (HEALTH_SQL, UNRESOLVED_SQL), "unregistered_statement")
                stream = snapshot.execute_sql(HEALTH_SQL if sql == HEALTH_SQL else UNRESOLVED_SQL, params=params, param_types=types, retry=None,
                    timeout=budget.remaining(cap), request_options={"priority": "PRIORITY_LOW"})
                rows = list(stream)
                stamp = timestamp_ns(stream.metadata.transaction.read_timestamp)
            budget.remaining(cap)
            return rows, stamp

    def observe(self, boot: str, region: str, revision: str, slot: int, budget: Budget,
                *, claim_at_ns: int) -> dict[str, Any]:
        from google.protobuf.timestamp_pb2 import Timestamp
        from proto.datetime_helpers import DatetimeWithNanoseconds

        # proto-plus preserves nanos on the wire; api_core's similarly named
        # datetime is marshalled as ordinary datetime and loses sub-microseconds.
        # Spanner reports read timestamps at microsecond precision, including
        # when it waits for this bound. Ceil (never floor) the bound so even a
        # read exactly at floor_us(bound) is >= the original nanosecond claim.
        # Stored claims, historical traces and the a1 ordering rules stay exact.
        seconds, nanos = divmod(((claim_at_ns + 999) // 1000) * 1000, 1_000_000_000)
        bound = DatetimeWithNanoseconds.from_timestamp_pb(Timestamp(seconds=seconds, nanos=nanos))
        rows, observed_ns = self.snapshot_query(unresolved_statement(), budget, CYCLE_SECONDS,
                                               min_read_timestamp=bound)
        require(len(rows) <= HEALTH_ROW_LIMIT + 1, "unresolved_bound")
        return summarize(rows, boot=boot, region=region, revision=revision, slot=slot,
            observed_ns=observed_ns, age_seconds=self.wall(), row_limit=HEALTH_ROW_LIMIT)

    def read_shadow_health(self) -> dict[str, Any] | None:
        started = self.clock()
        budget = Budget(started + OBSERVER_READ_RPC_SECONDS, self.clock)
        rows, read_ns = self.snapshot_query(read_shadow_health_statement(), budget, OBSERVER_READ_RPC_SECONDS)
        require(len(rows) <= 1, "point_bound")
        if not rows:
            return None
        body, committed = rows[0]
        value = Row(HEALTH_KIND, "local/fleet", body.encode()).value()
        committed_ns = timestamp_ns(committed)
        ended = self.clock()
        # No 250 ms budget check here: the caller's install cut-off classifies
        # a slow reply as late, and the elapsed time travels with the value.
        return dict(body=value, committed_at_ns=committed_ns, read_at_ns=read_ns,
                    read_elapsed_ns=round((ended-started)*1_000_000_000), reply_at=ended)

    def close_result(self, row: Row, budget: Budget) -> None:
        require(row.kind == CLOSE_RESULT_KIND, "close_result_kind")
        row.value()
        budget = budget.child(WRITE_SECONDS)
        def run(tx: Any) -> None:
            self.retention(tx, [row], budget)
            old = self.point(tx, entity_point_statement(row.kind, row.key), budget, READ_SECONDS)
            if old is not None:
                require(old == row, "immutable_conflict")
                return
            tx.insert(table="tr_entities", columns=("kind", "id", "body", "updated_at"),
                      values=[(row.kind, row.key, row.body.decode(), dt.datetime.fromtimestamp(self.wall(), dt.UTC))])
        self.transaction(run, budget, WRITE_SECONDS)

    def retention(self, tx: Any, rows: list[Row], budget: Budget) -> None:
        fence = self.point(tx, entity_point_statement(CONTROL_KIND, "retention-v2"), budget, READ_SECONDS)
        cutoff = fence.value()["retired_through"] if fence else ""
        # Same v1 day-retention predicate; protect the entire transaction horizon.
        from trusted_router.async_settle_shadow_evidence import retired_day
        end = dt.datetime.fromtimestamp(self.wall() + budget.remaining(WRITE_SECONDS), dt.UTC)
        for row in rows:
            if row.key != "retention-v2":
                day = row.key.split("/", 1)[0]
                require(day > cutoff and not retired_day(day, end), "proof_expired")

    def allocate(self, day: str, *, terminal: bool, event: bool, budget: Budget) -> None:
        key = day + "/cap-v2"
        budget = budget.child(WRITE_SECONDS)
        def run(tx: Any) -> None:
            old = self.point(tx, entity_point_statement(CONTROL_KIND, key), budget, READ_SECONDS)
            value = old.value() if old else dict(v=2, policy_version=POLICY, contract_revision="a1", day=day, terminal_reserved=0, event_reserved=0)
            row = Row.build(CONTROL_KIND, key, reserve_cap(value, terminal=terminal, event=event))
            self.retention(tx, [row], budget)
            self.write(tx, [row], budget)
        self.transaction(run, budget, WRITE_SECONDS)

    def flush(self, pages: list[Row], counter: Row, closure: Row | None, budget: Budget) -> None:
        transaction_bodies([p.body for p in pages], canonical(counter_projection(counter.value())),
            canonical(without(closure.value(), "contract_revision", "authorization_obligations")) if closure else None)
        require(sum(len(r.body) for r in [*pages, counter, *([closure] if closure else [])]) <= TRANSACTION_BODY_BYTES,
                "transaction_size")
        if closure is not None:
            cv, end = counter.value(), closure.value()
            require(cv["closed"] and end["counter_sha256"] == counter.digest
                    and cv["durable_event_hwm"] == end["durable_event_hwm"] == cv["event_seq"]
                    and cv["checkpoint_seq"] == end["final_checkpoint_seq"]
                    and end["receipt_chain_sha256"] == (cv["receipt_chain_sha256"] or EMPTY_CHAIN), "closure_counter")
        rows = [*pages, counter, *([closure] if closure else [])]
        budget = budget.child(WRITE_SECONDS)
        reductions: dict[str, int] = {}
        def run(tx: Any) -> None:
            self.retention(tx, rows, budget)
            for row in rows:
                old = self.point(tx, entity_point_statement(row.kind, row.key), budget, READ_SECONDS)
                if old is None:
                    continue
                if row.kind == COUNTER_KIND:
                    previous, value = old.value(), row.value()
                    require(value["checkpoint_seq"] >= previous["checkpoint_seq"], "checkpoint_regression")
                    if value["checkpoint_seq"] == previous["checkpoint_seq"]:
                        require(row.body == old.body, "checkpoint_conflict")
                    else:
                        credit = len(self.reconciliations.get(row.key, set())) - self.used_reconciliations.get(row.key, 0)
                        monotonic_counter(previous, value, reconciled=credit)
                        reductions[row.key] = max(0, previous["unreconciled_commits"]-value["unreconciled_commits"])
                else:
                    require(row.body == old.body, "immutable_conflict")
            self.write(tx, rows, budget)
        try:
            self.transaction(run, budget, WRITE_SECONDS)
        except UnknownCommit:
            # A possibly consumed proof is stranded with the frozen checkpoint.
            for key, count in reductions.items():
                self.used_reconciliations[key] = self.used_reconciliations.get(key, 0) + count
            raise
        else:
            for key, count in reductions.items():
                self.used_reconciliations[key] = self.used_reconciliations.get(key, 0) + count
            if counter.value()["unreconciled_commits"] == 0:
                self.reconciliations.pop(counter.key, None)
                self.used_reconciliations.pop(counter.key, None)

    def retain(self, rows: tuple[Row, ...], budget: Budget) -> None:
        """Retry an exact retained sample/certificate batch; never replace identity."""
        require(len(rows) <= 2 and sum(len(r.body) for r in rows) <= 256*1024, "batch_bound")
        require(all(r.kind in (SAMPLE_KIND, CERTIFICATE_KIND) for r in rows), "batch_kind")
        budget = budget.child(WRITE_SECONDS)
        def run(tx: Any) -> None:
            self.retention(tx, list(rows), budget)
            for row in rows:
                old = self.point(tx, entity_point_statement(row.kind, row.key), budget, READ_SECONDS)
                require(old is None or old == row, "immutable_conflict")
            self.write(tx, list(rows), budget)
        self.transaction(run, budget, WRITE_SECONDS)

    def reconcile(self, rows: list[Row], budget: Budget, *, writer_key: str | None = None) -> bool:
        # A miss is still unknown. Never overwrite an uncertain transaction with
        # a new sequence until every exact body has been independently observed.
        for row in rows:
            found = self.read(row.kind, row.key, budget)
            if found is None:
                return False
            require(found.body == row.body, "unknown_commit_conflict")
        writer = next((r.key for r in rows if r.kind == COUNTER_KIND), writer_key)
        if writer is not None and rows and not any(r.kind == CLOSURE_KIND for r in rows):
            require(writer in self.reconciliations or len(self.reconciliations) < 3, "reconciliation_days")
            proofs = self.reconciliations.setdefault(writer, set())
            proof = tuple(r.digest for r in rows)
            require(proof in proofs or len(proofs) < 33, "reconciliation_tasks")
            proofs.add(proof)
        return True

    def runtime_stop(self, row: Row | None, budget: Budget) -> Row | None:
        """Reviewed stop/clear path: one key, one transaction, first stop wins.

        A lost commit acknowledgement is safe to retry. Clear deletes only this
        latch; ordinary immutable controls and retention remain unchanged.
        """
        from google.cloud.spanner_v1 import KeySet

        if row is not None:
            require(row.kind == CONTROL_KIND and row.key == "stop-v2", "stop_identity")
            row.value()
        budget = budget.child(WRITE_SECONDS)
        def run(tx: Any) -> Row | None:
            old = self.point(tx, entity_point_statement(CONTROL_KIND, "stop-v2"), budget, READ_SECONDS)
            if row is None:
                if old is not None:
                    tx.delete("tr_entities", KeySet(keys=[(CONTROL_KIND, "stop-v2")]))
                return None
            if old is not None:
                return old
            self.write(tx, [row], budget)
            return row
        return self.transaction(run, budget, WRITE_SECONDS)

    def read_runtime_stop(self) -> Row | None:
        return self.read(CONTROL_KIND, "stop-v2", Budget(self.clock()+READ_SECONDS, self.clock))

    def control(self, row: Row, budget: Budget) -> None:
        require(row.kind == CONTROL_KIND and row.key != "stop-v2", "control_kind")
        budget = budget.child(WRITE_SECONDS)
        def run(tx: Any) -> None:
            self.retention(tx, [row], budget)
            old = self.point(tx, entity_point_statement(row.kind, row.key), budget, READ_SECONDS)
            if old is not None:
                if row.key == "retention-v2":
                    require(row.value()["retired_through"] >= old.value()["retired_through"], "retention_regression")
                else:
                    require(row.body == old.body, "control_conflict")
            self.write(tx, [row], budget)
        self.transaction(run, budget, WRITE_SECONDS)


def monotonic_counter(previous: dict[str, Any], value: dict[str, Any], *, reconciled: int = 0) -> None:
    require((previous["day"], previous["boot"], previous["region"], previous["router_revision"]) ==
            (value["day"], value["boot"], value["region"], value["router_revision"]), "writer_conflict")
    require(not previous["closed"], "closed_counter")
    require(previous["unreconciled_commits"] - value["unreconciled_commits"] <= reconciled, "unproven_reconciliation")
    _monotonic_counts(previous, value)
    # P4a reserves on the request thread and finalizes exactly once with its
    # final class. No reclassification: every cumulative counter is monotonic.
    for name in ("event_seq", "durable_event_hwm", "receipt_count", "loss_count", "reset_count",
                 "checkpoint_attempts", "checkpoint_failures", "checkpoint_recoveries", "unknown_commits",
                 "irrecoverable_losses", "authorize_attempts", "authorize_fresh", "authorize_replay",
                 "settle_attempts", "refund_attempts"):
        require(value[name] >= previous[name], "counter_regression")
    for name in ("first_gap_at_us", "last_mismatch_at_us"):
        require(previous[name] is None or value[name] is not None and value[name] >= previous[name], "reset_erasure")


def _monotonic_counts(previous: Any, value: Any, name: str = "") -> None:
    """Pure cumulative-count dominance, including bounded dimensional arrays."""
    if name == "unreconciled_commits":
        return  # Exact-key reconciliation is the only caller permitted to reduce it.
    if type(previous) is int and type(value) is int:
        require(value >= previous, "counter_regression")
    elif type(previous) is dict:
        require(type(value) is dict and previous.keys() <= value.keys(), "counter_erasure")
        for key, child in previous.items():
            _monotonic_counts(child, value[key], key)
    elif type(previous) is list:
        require(type(value) is list, "counter_erasure")
        if not previous:
            return
        if type(previous[0]) is dict:
            from trusted_router.detached_jws import canonical
            def identity(row: dict[str, Any]) -> bytes:
                return canonical({k: row[k] for k in ("phase", "dimensions", "adapter", "route_type", "streamed", "reason") if k in row})
            current = {identity(row): row for row in value}
            for row in previous:
                require(identity(row) in current, "counter_erasure")
                _monotonic_counts(row, current[identity(row)])
        else:
            require(len(value) == len(previous), "counter_erasure")
            for old, new in zip(previous, value, strict=True):
                _monotonic_counts(old, new)
    elif type(previous) is bool and previous:
        require(value is True, "counter_erasure")


def startup_instance() -> str:
    """One bounded platform instance read for the released startup mapping."""
    import urllib.request

    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/id",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=1) as response:  # noqa: S310 - fixed platform URL
        raw = response.read(129)
    return raw.decode("ascii")
