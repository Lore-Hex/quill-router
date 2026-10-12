"""Thin native adapter for independent health and immutable receipt checkpoints."""
from __future__ import annotations

import datetime as dt
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
    CLOSURE_KIND,
    CONTROL_KIND,
    COUNTER_KIND,
    EMPTY_CHAIN,
    HEALTH_KIND,
    SAMPLE_KIND,
    Row,
    reserve_cap,
)
from trusted_router.async_settle_shadow_v2_evidence import transaction_bodies
from trusted_router.async_settle_shadow_v2_schema import POLICY, require
from trusted_router.storage_gcp_async_admission import HEALTH_ROW_LIMIT, unresolved_statement
from trusted_router.storage_gcp_io import (
    configure_spanner_rpc_deadlines,
    entity_point_statement,
    spanner_no_retry,
    spanner_rpc_deadline,
)

WRITE_SECONDS = 2.
READ_SECONDS = 1.
FLUSH_SECONDS = 4.
T = TypeVar("T")
Statement = tuple[str, dict[str, Any], dict[str, Any]]


@dataclass(frozen=True)
class Budget:
    deadline: float
    clock: Callable[[], float] = time.monotonic

    def remaining(self, cap: float) -> float:
        remaining = min(cap, self.deadline - self.clock())
        if remaining <= 0:
            raise TimeoutError("shadow_budget")
        return remaining

    def child(self, cap: float) -> Budget:
        return Budget(min(self.deadline, self.clock() + cap), self.clock)


class UnknownCommit(RuntimeError):
    pass


def shadow_health_claim_read() -> Statement:
    return entity_point_statement(CLAIM_KIND, "local/publish")


def shadow_health_publish_fence() -> Statement:
    return entity_point_statement(CLAIM_KIND, "local/publish")


def shadow_health_previous() -> Statement:
    return entity_point_statement(HEALTH_KIND, "local/fleet")


def read_shadow_health_statement() -> Statement:
    return entity_point_statement(HEALTH_KIND, "local/fleet")


class ObservationStore:
    def __init__(self, database: Any, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.database, self.clock, self.wall = database, clock, wall
        self.reconciliations: dict[str, set[tuple[str, ...]]] = {}
        self.used_reconciliations: dict[str, int] = {}
        configure_spanner_rpc_deadlines(database)

    def query(self, reader: Any, statement: Statement, budget: Budget, cap: float) -> list[Any]:
        sql, params, types = statement
        with spanner_no_retry(), spanner_rpc_deadline(budget.deadline, max_rpc_seconds=cap):
            rows = list(reader.execute_sql(sql, params=params, param_types=types, retry=None,
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
        with self.database.snapshot() as snapshot:
            return self.point(snapshot, entity_point_statement(kind, key), budget.child(READ_SECONDS), READ_SECONDS)

    def transaction(self, callback: Callable[[Any], T], budget: Budget, cap: float) -> T:
        attempted = False
        committing = False
        def once(tx: Any) -> T:
            nonlocal attempted, committing
            require(not attempted, "transaction_retry")
            attempted = True
            budget.remaining(cap)
            result = callback(tx)
            budget.remaining(cap)
            committing = True
            return result
        try:
            with spanner_no_retry(), spanner_rpc_deadline(budget.deadline, max_rpc_seconds=cap):
                result = self.database.run_in_transaction(once, timeout_secs=budget.remaining(cap),
                    commit_request_options={"priority": "PRIORITY_LOW"}, transaction_tag="tr_shadow_v2")
            budget.remaining(cap)
            return result
        except Exception:
            if committing:
                raise UnknownCommit("shadow_commit_unknown") from None
            raise

    def write(self, tx: Any, rows: list[Row], budget: Budget) -> None:
        # Bodies are immutable schema-serialized bytes; exact retries never rebuild.
        for row in rows:
            row.value()
        budget.remaining(WRITE_SECONDS)
        tx.insert_or_update(table="tr_entities", columns=("kind", "id", "body", "updated_at"),
            values=[(r.kind, r.key, r.body.decode("utf-8"), dt.datetime.fromtimestamp(self.wall(), dt.UTC)) for r in rows])

    def claim(self, boot: str, slot: int, claim_us: int, budget: Budget) -> bool:
        row = Row.build(CLAIM_KIND, "local/publish", dict(v=2, policy_version=POLICY,
            authority="local", slot=slot, claim_at_us=claim_us, owner_boot_id=boot))
        def run(tx: Any) -> bool:
            old = self.point(tx, shadow_health_claim_read(), budget, CYCLE_SECONDS)
            if not claim_allowed(old.value() if old else None, slot):
                return False
            self.write(tx, [row], budget)
            return True
        return self.transaction(run, budget, CYCLE_SECONDS)

    def publish(self, value: dict[str, Any], budget: Budget) -> bool:
        row = Row.build(HEALTH_KIND, "local/fleet", value)
        def run(tx: Any) -> bool:
            fence = self.point(tx, shadow_health_publish_fence(), budget, CYCLE_SECONDS)
            old = self.point(tx, shadow_health_previous(), budget, CYCLE_SECONDS)
            if not publish_allowed(fence.value() if fence else None, old.value() if old else None, value):
                return False
            self.write(tx, [row], budget)
            return True
        return self.transaction(run, budget, CYCLE_SECONDS)

    def observe(self, boot: str, region: str, revision: str, slot: int, budget: Budget) -> dict[str, Any]:
        observed_us = int(self.wall()*1_000_000)
        with self.database.snapshot() as snapshot:
            rows = self.query(snapshot, unresolved_statement(), budget, CYCLE_SECONDS)
        require(len(rows) <= HEALTH_ROW_LIMIT + 1, "unresolved_bound")
        return summarize(rows, boot=boot, region=region, revision=revision, slot=slot,
            observed_us=observed_us, heartbeat_us=int(self.wall()*1_000_000), row_limit=HEALTH_ROW_LIMIT)

    def read_shadow_health(self) -> dict[str, Any] | None:
        budget = Budget(self.clock() + CYCLE_SECONDS, self.clock)
        with self.database.snapshot() as snapshot:
            row = self.point(snapshot, read_shadow_health_statement(), budget, CYCLE_SECONDS)
        return row.value() if row else None

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
            value = old.value() if old else dict(v=2, policy_version=POLICY, day=day, terminal_reserved=0, event_reserved=0)
            row = Row.build(CONTROL_KIND, key, reserve_cap(value, terminal=terminal, event=event))
            self.retention(tx, [row], budget)
            self.write(tx, [row], budget)
        self.transaction(run, budget, WRITE_SECONDS)

    def flush(self, pages: list[Row], counter: Row, closure: Row | None, budget: Budget) -> None:
        transaction_bodies([p.body for p in pages], counter.body, closure.body if closure else None)
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

    def control(self, row: Row, budget: Budget) -> None:
        require(row.kind == CONTROL_KIND, "control_kind")
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
    # Nullable timestamps and closed-partition reclassification are not counts.
    # P4 must append completion events; cumulative attempt/loss/reset totals stay.
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
