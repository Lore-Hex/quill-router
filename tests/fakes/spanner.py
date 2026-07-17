from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from trusted_router.storage_gcp_settle_outbox import (
    _GUARD_STATUS_SQL,
    GUARD_STATUSES,
    OUTBOX_COLUMNS,
)


class _ParamTypes:
    STRING = "STRING"
    INT64 = "INT64"
    BOOL = "BOOL"
    TIMESTAMP = "TIMESTAMP"


# Real Spanner column DEFAULTs for the typed counter tables (every counter is
# NOT NULL DEFAULT(0) in the DDL). The fake fills these on INSERT so a
# subset-column insert_or_update — which is what creation-time seeding uses,
# writing only the create-owned columns — still yields a complete row whose
# typed-DML-owned counters start at 0.
_TYPED_DEFAULTS: dict[str, dict[str, Any]] = {
    "tr_credit_balance": {"total_credits": 0, "total_usage": 0, "reserved": 0},
    "tr_key_limit": {
        "limit_micro": None,
        "usage": 0,
        "byok_usage": 0,
        "reserved": 0,
        "include_byok": True,
        # Window spend limits (config, nullable) + lazy window state (DDL:
        # usage NOT NULL DEFAULT 0, start nullable).
        "day_limit_micro": None,
        "week_limit_micro": None,
        "month_limit_micro": None,
        "day_usage": 0,
        "day_start": None,
        "week_usage": 0,
        "week_start": None,
        "month_usage": 0,
        "month_start": None,
    },
}


def _apply_upsert_typed(
    typed: dict, versions: dict, table: str, columns: Any, value_tuple: tuple, version: int
) -> None:
    """Model real Spanner insert_or_update on a typed counter table: on INSERT
    fill the NOT NULL DEFAULT columns the write omitted; on UPDATE touch ONLY
    the supplied columns and leave the rest intact. Shared by the transaction
    and batch commit paths so partial typed-row seed/update mutations behave
    identically through either writer."""
    pk = (value_tuple[0], value_tuple[1])
    incoming = dict(zip(columns, value_tuple, strict=True))
    table_rows = typed.setdefault(table, {})
    existing = table_rows.get(pk)
    row = dict(existing) if existing is not None else dict(_TYPED_DEFAULTS.get(table, {}))
    row.update(incoming)
    table_rows[pk] = row
    versions[(table, pk)] = version


@dataclass
class _KeySet:
    keys: list[tuple]


class _SpannerModule:
    COMMIT_TIMESTAMP = "COMMIT_TIMESTAMP_SENTINEL"

    @staticmethod
    def KeySet(*, keys: Iterable[tuple]) -> _KeySet:
        return _KeySet(list(keys))


@dataclass
class _Row:
    body: str
    version: int


class FakeAborted(Exception):
    pass


try:  # subclass the real exception so production `except AlreadyExists` catches it
    from google.api_core.exceptions import AlreadyExists as _AlreadyExists
except ImportError:  # pragma: no cover - google always present in the test venv
    _AlreadyExists = Exception  # type: ignore[assignment,misc]


class FakeAlreadyExists(_AlreadyExists):
    """Unique-index / duplicate-PK violation (e.g. duplicate idempotency_scope or
    reservation_id). Unlike Aborted, run_in_transaction does NOT retry this — the
    caller must convert it to the replay path (codex Step-3 #4). Subclasses the
    real google.api_core.exceptions.AlreadyExists so the same `except AlreadyExists`
    works in prod and tests."""

    def __init__(self, detail: str = "already exists") -> None:
        super().__init__(detail)


class FakeSpannerDatabase:
    """In-process Spanner replacement that simulates snapshot-isolation
    conflict-abort. Implements only the surface used by SpannerBigtableStore:
    run_in_transaction, batch, snapshot, with execute_sql / insert_or_update /
    delete underneath. Each row carries a monotonic version; on commit, if any
    row in the transaction's read-set has been modified since it was read, the
    transaction is aborted and the function is invoked again. This matches
    Spanner's optimistic concurrency contract closely enough to test the
    credit-ledger retry path."""

    def __init__(self, *, ready_barrier: threading.Barrier | None = None) -> None:
        self.rows: dict[tuple[str, str], _Row] = {}
        # Typed counter tables (tr_credit_balance, tr_key_limit): table ->
        # (pk col0, pk col1) -> {column: value}. PK is the first two columns.
        self.typed: dict[str, dict[tuple, dict]] = {}
        # Per typed-row version for conditional-DML conflict detection, so two
        # concurrent execute_update reservers serialize via abort-retry (the fake
        # analogue of the real row write lock).
        self.typed_versions: dict[tuple, int] = {}
        # tr_reservation: 1-col PK (reservation_id) + a UNIQUE index on
        # idempotency_scope. Modeled separately from the 2-col typed counters.
        self.reservations: dict[str, dict] = {}
        self.reservation_versions: dict[str, int] = {}
        self.reservation_idemp: dict[str, str] = {}  # idempotency_scope -> reservation_id
        # tr_settle_outbox: PK (authorization_id, intent_kind) -> {column: value}.
        self.settle_outbox: dict[tuple, dict] = {}
        self.settle_outbox_versions: dict[tuple, int] = {}
        self._global_version = 0
        self._commit_lock = threading.Lock()
        self._ready_barrier = ready_barrier
        self.aborts = 0
        self.commits = 0
        self.last_timeout_secs: float | None = None

    def run_in_transaction(self, fn: Any, *, timeout_secs: float | None = None) -> Any:
        # timeout_secs mirrors google-cloud-spanner's Database.run_in_transaction
        # kwarg (passed by run_in_transaction_with_retry to bound the inner retry
        # to the caller's remaining wall-clock budget). The fake commits
        # synchronously, so it records the value for assertions but does not sleep.
        self.last_timeout_secs = timeout_secs
        for attempt in range(50):
            txn = _FakeTransaction(self)
            try:
                result = fn(txn)
            except FakeAborted:
                self.aborts += 1
                continue
            if attempt == 0 and self._ready_barrier is not None:
                try:
                    self._ready_barrier.wait(timeout=10)
                except threading.BrokenBarrierError:
                    pass
            if self._try_commit(txn):
                self.commits += 1
                return result
            self.aborts += 1
        raise RuntimeError("fake spanner: exceeded retry budget")

    def _try_commit(self, txn: _FakeTransaction) -> bool:
        with self._commit_lock:
            for key, observed in txn.read_versions.items():
                if isinstance(key, tuple) and len(key) == 3 and key[0] == "typed":
                    current_version = self.typed_versions.get((key[1], key[2]), 0)
                elif isinstance(key, tuple) and len(key) == 2 and key[0] == "res":
                    current_version = self.reservation_versions.get(key[1], 0)
                elif isinstance(key, tuple) and len(key) == 2 and key[0] == "idemp":
                    # presence-based: a same-scope insert committed since our read
                    # flips this, aborting the loser so its retry raises ALREADY_EXISTS
                    current_version = 1 if key[1] in self.reservation_idemp else 0
                elif isinstance(key, tuple) and len(key) == 2 and key[0] == "outbox":
                    current_version = self.settle_outbox_versions.get(key[1], 0)
                else:
                    current = self.rows.get(key)
                    current_version = current.version if current is not None else 0
                if current_version != observed:
                    return False
            self._global_version += 1
            new_version = self._global_version
            for op in txn.pending_writes:
                if op[0] == "upsert":
                    _, _table, kind, entity_id, body = op
                    self.rows[(kind, entity_id)] = _Row(body=body, version=new_version)
                elif op[0] == "delete":
                    _, _table, kind, entity_id = op
                    self.rows.pop((kind, entity_id), None)
                elif op[0] == "upsert_typed":
                    _, table, columns, value_tuple = op
                    _apply_upsert_typed(
                        self.typed, self.typed_versions, table, columns, value_tuple, new_version
                    )
                elif op[0] == "update_typed":  # conditional-DML write
                    _, table, pk, record = op
                    self.typed.setdefault(table, {})[pk] = record
                    self.typed_versions[(table, pk)] = new_version
                elif op[0] == "insert_typed_dml":
                    _, table, pk, record = op
                    self.typed.setdefault(table, {})[pk] = record
                    self.typed_versions[(table, pk)] = new_version
                elif op[0] == "delete_typed":
                    _, table, pk = op
                    self.typed.get(table, {}).pop(pk, None)
                    self.typed_versions.pop((table, pk), None)
                elif op[0] == "insert_reservation":
                    _, record = op
                    rid = record["reservation_id"]
                    self.reservations[rid] = record
                    self.reservation_versions[rid] = new_version
                    scope = record.get("idempotency_scope")
                    if scope is not None:
                        self.reservation_idemp[scope] = rid
                elif op[0] in ("insert_settle_outbox", "update_settle_outbox"):
                    _, pk, record = op
                    self.settle_outbox[pk] = record
                    self.settle_outbox_versions[pk] = new_version
                elif op[0] == "delete_settle_outbox":
                    _, pk = op
                    self.settle_outbox.pop(pk, None)
                    self.settle_outbox_versions.pop(pk, None)
                elif op[0] == "update_reservation":
                    _, rid, record = op
                    self.reservations[rid] = record
                    self.reservation_versions[rid] = new_version
                elif op[0] == "insert_entity_dml":  # DML INSERT into tr_entities
                    _, kind, entity_id, body = op
                    self.rows[(kind, entity_id)] = _Row(body=body, version=new_version)
                elif op[0] == "update_entity_dml":  # DML UPDATE tr_entities body
                    _, kind, entity_id, body = op
                    self.rows[(kind, entity_id)] = _Row(body=body, version=new_version)
            return True

    def snapshot(self, *, multi_use: bool = False, **_kwargs: Any) -> _FakeSnapshot:
        # Models real Spanner: a single-use snapshot (the default) permits exactly
        # ONE read; a second read on it raises. Only multi_use=True allows many.
        # Prod bug fa9f5d4 was a single-use snapshot that grew a second read and
        # faulted live — the old fake "allowed repeated reads regardless" and hid it.
        return _FakeSnapshot(self, multi_use=multi_use)

    def batch(self) -> _FakeBatch:
        return _FakeBatch(self)


class _FakeTransaction:
    def __init__(self, db: FakeSpannerDatabase) -> None:
        self.db = db
        self.read_versions: dict[tuple[str, str], int] = {}
        self.read_snapshots: dict[tuple[str, str], str | None] = {}
        self.pending_writes: list[tuple] = []
        # DML+mutation mixing is forbidden in one transaction (real Spanner
        # buffers mutations after DML and DML can't see them); fail fast if both.
        self._did_mutation = False
        self._did_dml = False

    def execute_sql(
        self,
        sql: str,
        *,
        params: dict[str, Any] | None = None,
        param_types: Any = None,
    ) -> list[list[str]]:
        return _execute_sql(self.db, self, sql, params or {})

    def _reservation_current(self, rid: str) -> dict | None:
        """In-txn view of a reservation (read-your-writes) + record read version."""
        for op in reversed(self.pending_writes):
            if op[0] == "update_reservation" and op[1] == rid:
                return dict(op[2])
            if op[0] == "insert_reservation" and op[1]["reservation_id"] == rid:
                return dict(op[1])
        version_key = ("res", rid)
        if version_key not in self.read_versions:
            self.read_versions[version_key] = self.db.reservation_versions.get(rid, 0)
        rec = self.db.reservations.get(rid)
        return dict(rec) if rec is not None else None

    def _settle_outbox_current(self, pk: tuple) -> dict | None:
        """In-txn view of a settle-outbox row (read-your-writes) + read version."""
        for op in reversed(self.pending_writes):
            if op[0] in ("insert_settle_outbox", "update_settle_outbox") and op[1] == pk:
                return dict(op[2])
        version_key = ("outbox", pk)
        if version_key not in self.read_versions:
            self.read_versions[version_key] = self.db.settle_outbox_versions.get(pk, 0)
        rec = self.db.settle_outbox.get(pk)
        return dict(rec) if rec is not None else None

    def _typed_current(self, table: str, pk: tuple) -> dict | None:
        """In-txn view of a typed row for DML: sees prior DML writes
        (update_typed = read-your-writes) but NOT buffered mutations (real Spanner
        DML can't see mutations; mixing is rejected in execute_update). Records
        the read version on first read for conflict detection."""
        for op in reversed(self.pending_writes):
            if op[0] == "update_typed" and op[1] == table and op[2] == pk:
                return dict(op[3])
        version_key = ("typed", table, pk)
        if version_key not in self.read_versions:
            self.read_versions[version_key] = self.db.typed_versions.get((table, pk), 0)
        rec = self.db.typed.get(table, {}).get(pk)
        return dict(rec) if rec is not None else None

    def execute_update(
        self, sql: str, *, params: dict[str, Any] | None = None, param_types: Any = None
    ) -> int:
        """Model the conditional-DML statements used by storage_gcp_counter_dml.

        Reads the typed row into the read-set (so concurrent reservers conflict
        and serialize via abort-retry), evaluates the WHERE predicate, and
        conditionally buffers the SET. Returns the modified-row count.
        """
        if self._did_mutation:
            raise RuntimeError(
                "DML after a mutation in the same transaction — DML+mutation "
                "mixing is forbidden (see docs §5)"
            )
        self._did_dml = True
        p = params or {}
        if "UPDATE tr_credit_balance SET total_credits = total_credits + @amount" in sql:
            _require_pred(sql, "WHERE workspace_id=@ws AND shard=@shard", "credit-top-up")
            pk = (p["ws"], p["shard"])
            rec = self._typed_current("tr_credit_balance", pk)
            if rec is None:
                return 0
            new = dict(
                rec,
                total_credits=rec["total_credits"] + p["amount"],
                source_updated_at=p["now"],
                updated_at=p["now"],
            )
            self.pending_writes.append(("update_typed", "tr_credit_balance", pk, new))
            return 1
        if "UPDATE tr_credit_balance SET total_credits=total_credits-@move" in sql:
            _require_pred(
                sql,
                "(total_credits-total_usage-reserved)>=@move",
                "credit-rebalance-donor",
            )
            pk = (p["ws"], p["donor"])
            rec = self._typed_current("tr_credit_balance", pk)
            available = (
                rec["total_credits"] - rec["total_usage"] - rec["reserved"]
                if rec is not None
                else -1
            )
            if rec is None or available < p["move"]:
                return 0
            new = dict(rec, total_credits=rec["total_credits"] - p["move"])
            self.pending_writes.append(("update_typed", "tr_credit_balance", pk, new))
            return 1
        if "UPDATE tr_credit_balance SET total_credits=total_credits+@move" in sql:
            _require_pred(
                sql,
                "WHERE workspace_id=@ws AND shard=@target",
                "credit-rebalance-target",
            )
            pk = (p["ws"], p["target"])
            rec = self._typed_current("tr_credit_balance", pk)
            if rec is None:
                return 0
            new = dict(rec, total_credits=rec["total_credits"] + p["move"])
            self.pending_writes.append(("update_typed", "tr_credit_balance", pk, new))
            return 1
        if sql.startswith("INSERT INTO tr_credit_balance"):
            pk = (p["ws"], p["shard"])
            if pk in self.db.typed.get("tr_credit_balance", {}):
                raise FakeAlreadyExists(f"tr_credit_balance/{pk}")
            version_key = ("typed", "tr_credit_balance", pk)
            if version_key not in self.read_versions:
                self.read_versions[version_key] = 0
            record = dict(_TYPED_DEFAULTS["tr_credit_balance"])
            record.update(
                {
                    "workspace_id": p["ws"],
                    "shard": p["shard"],
                    "total_credits": p["total"],
                    "source_updated_at": p["now"],
                    "updated_at": p["now"],
                }
            )
            self.pending_writes.append(("insert_typed_dml", "tr_credit_balance", pk, record))
            return 1
        if "UPDATE tr_credit_balance SET reserved = reserved + @est" in sql:
            pk = (p["ws"], p["shard"])
            rec = self._typed_current("tr_credit_balance", pk)
            if rec is None:
                return 0
            if (rec["total_credits"] - rec["total_usage"] - rec["reserved"]) >= p["est"]:
                new = dict(rec, reserved=rec["reserved"] + p["est"])
                self.pending_writes.append(("update_typed", "tr_credit_balance", pk, new))
                return 1
            return 0
        if "UPDATE tr_credit_balance SET reserved = reserved - @hold" in sql:
            _require_pred(sql, "workspace_id=@ws AND shard=@shard AND reserved >= @hold", "credit-release")
            pk = (p["ws"], p["shard"])
            rec = self._typed_current("tr_credit_balance", pk)
            # mirrors the `AND reserved >= @hold` guard: underflow = 0-row no-op
            if rec is None or rec["reserved"] < p["hold"]:
                return 0
            new = dict(
                rec,
                reserved=rec["reserved"] - p["hold"],
                total_usage=rec["total_usage"] + p["actual"],
            )
            self.pending_writes.append(("update_typed", "tr_credit_balance", pk, new))
            return 1
        if "UPDATE tr_key_limit SET reserved = reserved + @est" in sql:
            pk = (p["kh"], p["shard"])
            rec = self._typed_current("tr_key_limit", pk)
            if rec is None or rec["limit_micro"] is None:
                return 0  # missing or uncapped (limit_micro IS NOT NULL fails)
            if p["is_byok"] and not rec["include_byok"]:
                return 0  # BYOK excluded from the cap
            included_byok = rec["byok_usage"] if rec["include_byok"] else 0
            avail = rec["limit_micro"] - rec["usage"] - included_byok - rec["reserved"]
            if avail >= p["est"]:
                new = dict(rec, reserved=rec["reserved"] + p["est"])
                self.pending_writes.append(("update_typed", "tr_key_limit", pk, new))
                return 1
            return 0
        if "UPDATE tr_key_limit " in sql and "reserved = reserved - @hold" in sql:
            _require_pred(sql, "key_hash=@kh AND shard=@shard AND reserved >= @hold", "key-release")
            pk = (p["kh"], p["shard"])
            rec = self._typed_current("tr_key_limit", pk)
            if rec is None or rec["reserved"] < p["hold"]:
                return 0
            byok_settle = "byok_usage = byok_usage + @actual" in sql
            col = "byok_usage" if byok_settle else "usage"
            new = dict(rec, reserved=rec["reserved"] - p["hold"])
            new[col] = rec[col] + p["actual"]
            # Lazy window bump, mirroring release_key's IF() SQL: a stale window
            # (start < floor) is replaced, a fresh one accumulates. BYOK settles
            # count only when the row's include_byok says so (wamt gate).
            if "day_usage = IF(" in sql:
                wamt = p["actual"]
                if byok_settle and not rec.get("include_byok", True):
                    wamt = 0
                for window, floor_param in (
                    ("day", "day_floor"), ("week", "week_floor"), ("month", "month_floor"),
                ):
                    floor = p[floor_param]
                    start = rec.get(f"{window}_start")
                    if start is None or start < floor:
                        new[f"{window}_usage"] = wamt
                        new[f"{window}_start"] = floor
                    else:
                        new[f"{window}_usage"] = rec.get(f"{window}_usage", 0) + wamt
            self.pending_writes.append(("update_typed", "tr_key_limit", pk, new))
            return 1
        if sql.startswith("INSERT INTO tr_reservation"):
            rid = p["reservation_id"]
            if rid in self.db.reservations:
                raise FakeAlreadyExists(rid)  # duplicate PK
            res_key = ("res", rid)
            if res_key not in self.read_versions:
                self.read_versions[res_key] = self.db.reservation_versions.get(rid, 0)
            scope = p.get("idempotency_scope")
            if scope is not None:
                if scope in self.db.reservation_idemp:
                    raise FakeAlreadyExists(scope)  # unique-index conflict (committed)
                idemp_key = ("idemp", scope)
                if idemp_key not in self.read_versions:
                    self.read_versions[idemp_key] = 0  # observed absent
            record = dict(p)
            record["settled"] = False
            record["settled_usage_type"] = None
            record["actual_micro"] = None
            self.pending_writes.append(("insert_reservation", record))
            return 1
        if "UPDATE tr_reservation SET settled=true" in sql:
            _require_pred(sql, "reservation_id=@rid AND settled=false", "reservation-claim")
            rec = self._reservation_current(p["rid"])
            if rec is None or rec["settled"]:
                return 0  # missing or already-claimed (replay)
            new = dict(
                rec, settled=True, settled_usage_type=p["sut"], actual_micro=p["actual"]
            )
            self.pending_writes.append(("update_reservation", p["rid"], new))
            return 1
        if sql.startswith("INSERT INTO tr_entities"):
            entity_key = (p["kind"], p["id"])
            if entity_key in self.db.rows:
                raise FakeAlreadyExists(f"{p['kind']}/{p['id']}")  # duplicate PK
            if entity_key not in self.read_versions:
                self.read_versions[entity_key] = 0  # observed absent
            self.pending_writes.append(("insert_entity_dml", p["kind"], p["id"], p["body"]))
            return 1
        if sql.startswith("UPDATE tr_entities SET body=@body"):
            entity_key = (p["kind"], p["id"])
            # read-your-writes within the txn, else committed
            pending = None
            for op in reversed(self.pending_writes):
                if op[0] in ("insert_entity_dml", "update_entity_dml") and (op[1], op[2]) == entity_key:
                    pending = op
                    break
            if pending is None:
                if entity_key not in self.read_versions:
                    self.read_versions[entity_key] = (
                        self.db.rows[entity_key].version if entity_key in self.db.rows else 0
                    )
                if entity_key not in self.db.rows:
                    return 0  # no such row
            self.pending_writes.append(("update_entity_dml", p["kind"], p["id"], p["body"]))
            return 1
        if sql.startswith("INSERT INTO tr_settle_outbox"):
            pk = (p["authorization_id"], p["intent_kind"])
            if pk in self.db.settle_outbox:
                raise FakeAlreadyExists(str(pk))  # duplicate PK
            vkey = ("outbox", pk)
            if vkey not in self.read_versions:
                self.read_versions[vkey] = self.db.settle_outbox_versions.get(pk, 0)
            self.pending_writes.append(("insert_settle_outbox", pk, dict(p)))
            return 1
        if sql.startswith("UPDATE tr_settle_outbox SET settle_origin="):  # enqueue refresh
            # SQL-SENSITIVE (codex #113 finding 1): assert every load-bearing
            # predicate — including the PK key — is present, so a dropped predicate
            # FAILS a test (real Spanner would update every matching row, not the
            # single pk the fake derives from params).
            _require_pred(sql, "authorization_id=@authorization_id AND intent_kind=@intent_kind", "refresh")
            _require_pred(sql, "status='pending'", "refresh")
            _require_pred(sql, "leased_until IS NULL OR leased_until < @now", "refresh")
            pk = (p["authorization_id"], p["intent_kind"])
            rec = self._settle_outbox_current(pk)
            if rec is None or rec["status"] != "pending":
                return 0
            leased = rec.get("leased_until")
            if leased is not None and leased >= p["now"]:
                return 0  # actively leased -> refresh is a no-op (finding 2 fix)
            new = dict(rec)
            for col in (
                "settle_origin", "reservation_id", "actual_cost_micro",
                "selected_endpoint_id", "model_id", "selected_usage_type", "settle_body",
            ):
                new[col] = p[col]
            new["updated_at"] = p["now"]
            self.pending_writes.append(("update_settle_outbox", pk, new))
            return 1
        if sql.startswith("UPDATE tr_settle_outbox SET lease_owner=@owner"):  # claim
            _require_pred(sql, "authorization_id=@aid AND intent_kind=@kind", "claim")
            _require_pred(sql, "status='pending'", "claim")
            _require_pred(sql, "leased_until IS NULL OR leased_until < @now", "claim")
            pk = (p["aid"], p["kind"])
            rec = self._settle_outbox_current(pk)
            if rec is None or rec["status"] != "pending":
                return 0
            leased = rec.get("leased_until")
            if leased is not None and leased >= p["now"]:
                return 0  # still held by a live lease
            new = dict(rec, lease_owner=p["owner"], leased_until=p["lease"], updated_at=p["now"])
            self.pending_writes.append(("update_settle_outbox", pk, new))
            return 1
        if sql.startswith("UPDATE tr_settle_outbox SET status='pending'"):  # park
            _require_pred(sql, "authorization_id=@aid AND intent_kind=@kind", "park")
            _require_pred(sql, "AND status='pending' AND attempts=@attempts", "park")
            _require_pred(sql, "lease_owner IS NULL OR lease_owner=@lease_owner", "park")
            pk = (p["aid"], p["kind"])
            rec = self._settle_outbox_current(pk)
            if rec is None or rec["status"] != "pending":
                return 0
            if int(rec.get("attempts", 0) or 0) != int(p["attempts"]):
                return 0
            owner = rec.get("lease_owner")
            if owner is not None and owner != p.get("lease_owner"):
                return 0
            new = dict(
                rec, status="pending", attempts=rec.get("attempts", 0), last_error=p["err"],
                next_attempt_at=p["next_at"], lease_owner=None, leased_until=None,
                updated_at=p["now"],
            )
            self.pending_writes.append(("update_settle_outbox", pk, new))
            return 1
        if sql.startswith("DELETE FROM tr_settle_outbox"):  # purge done
            _require_pred(sql, "WHERE status='done'", "purge_done")
            _require_pred(sql, "AND updated_at < @cutoff", "purge_done")
            deleted = 0
            for pk, rec in list(self.db.settle_outbox.items()):
                vkey = ("outbox", pk)
                if vkey not in self.read_versions:
                    self.read_versions[vkey] = self.db.settle_outbox_versions.get(pk, 0)
                if rec.get("status") == "done" and rec.get("updated_at") < p["cutoff"]:
                    self.pending_writes.append(("delete_settle_outbox", pk))
                    deleted += 1
            return deleted
        if sql.startswith("UPDATE tr_settle_outbox SET status=@status"):  # mark
            _require_pred(sql, "authorization_id=@aid AND intent_kind=@kind", "mark")
            _require_pred(sql, "status='pending'", "mark")
            _require_pred(sql, "lease_owner IS NULL OR lease_owner=@lease_owner", "mark")
            pk = (p["aid"], p["kind"])
            rec = self._settle_outbox_current(pk)
            if rec is None or rec["status"] != "pending":
                return 0
            owner = rec.get("lease_owner")
            if owner is not None and owner != p.get("lease_owner"):
                return 0
            new = dict(
                rec, status=p["status"], attempts=p["attempts"], last_error=p["err"],
                next_attempt_at=p["next_at"], lease_owner=None, leased_until=None,
                updated_at=p["now"],
            )
            self.pending_writes.append(("update_settle_outbox", pk, new))
            return 1
        raise NotImplementedError(sql)

    def insert_or_update(
        self, *, table: str, columns: tuple[str, ...], values: list[tuple]
    ) -> None:
        if self._did_dml:
            raise RuntimeError(
                "mutation after DML in the same transaction — DML+mutation "
                "mixing is forbidden (see docs §5)"
            )
        self._did_mutation = True
        for value_tuple in values:
            if table == "tr_entities":
                kind, entity_id, body = value_tuple[0], value_tuple[1], value_tuple[2]
                self.pending_writes.append(("upsert", table, kind, entity_id, body))
            else:
                self.pending_writes.append(("upsert_typed", table, columns, value_tuple))

    def delete(self, table: str, keyset: _KeySet) -> None:
        if self._did_dml:
            raise RuntimeError(
                "mutation after DML in the same transaction — DML+mutation "
                "mixing is forbidden (see docs §5)"
            )
        self._did_mutation = True
        for entry in keyset.keys:
            if table == "tr_entities":
                kind, entity_id = entry[0], entry[1]
                self.pending_writes.append(("delete", table, kind, entity_id))
            else:
                self.pending_writes.append(("delete_typed", table, (entry[0], entry[1])))


class _FakeSnapshot:
    def __init__(self, db: FakeSpannerDatabase, *, multi_use: bool = False) -> None:
        self.db = db
        self._multi_use = multi_use
        self._reads = 0

    def __enter__(self) -> _FakeSnapshot:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute_sql(
        self,
        sql: str,
        *,
        params: dict[str, Any] | None = None,
        param_types: Any = None,
    ) -> list[list[str]]:
        self._reads += 1
        if not self._multi_use and self._reads > 1:
            raise ValueError(
                "single-use snapshot allows only one read; use "
                "database.snapshot(multi_use=True) for multiple reads "
                "(models real Spanner — see prod fix fa9f5d4)"
            )
        return _execute_sql(self.db, None, sql, params or {})


class _FakeBatch:
    def __init__(self, db: FakeSpannerDatabase) -> None:
        self.db = db
        self.pending_writes: list[tuple] = []

    def __enter__(self) -> _FakeBatch:
        return self

    def __exit__(self, exc_type: Any, *_: Any) -> None:
        if exc_type is not None:
            return None
        with self.db._commit_lock:
            self.db._global_version += 1
            new_version = self.db._global_version
            for op in self.pending_writes:
                if op[0] == "upsert":
                    _, _table, kind, entity_id, body = op
                    self.db.rows[(kind, entity_id)] = _Row(body=body, version=new_version)
                elif op[0] == "delete":
                    _, _table, kind, entity_id = op
                    self.db.rows.pop((kind, entity_id), None)
                elif op[0] == "upsert_typed":
                    _, table, columns, value_tuple = op
                    _apply_upsert_typed(
                        self.db.typed, self.db.typed_versions, table, columns, value_tuple, new_version
                    )
                elif op[0] == "delete_typed":
                    _, table, pk = op
                    self.db.typed.get(table, {}).pop(pk, None)
                    self.db.typed_versions.pop((table, pk), None)
        return None

    def insert_or_update(
        self, *, table: str, columns: tuple[str, ...], values: list[tuple]
    ) -> None:
        for value_tuple in values:
            if table == "tr_entities":
                kind, entity_id, body = value_tuple[0], value_tuple[1], value_tuple[2]
                self.pending_writes.append(("upsert", table, kind, entity_id, body))
            else:
                self.pending_writes.append(("upsert_typed", table, columns, value_tuple))

    def delete(self, table: str, keyset: _KeySet) -> None:
        for entry in keyset.keys:
            if table == "tr_entities":
                kind, entity_id = entry[0], entry[1]
                self.pending_writes.append(("delete", table, kind, entity_id))
            else:
                self.pending_writes.append(("delete_typed", table, (entry[0], entry[1])))


def _require_pred(sql: str, needle: str, what: str) -> None:
    """Fail loudly if a load-bearing predicate is missing from the real SQL, so a
    predicate typo/drop FAILS a test instead of the fake silently enforcing the
    intended behavior in Python (codex #113 finding 1 / design MF6)."""
    if needle not in sql:
        raise AssertionError(f"tr_settle_outbox {what} query missing predicate: {needle!r}")


def _utc_datetime(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _execute_settle_outbox_sql(
    db: FakeSpannerDatabase,
    txn: _FakeTransaction | None,
    sql: str,
    params: dict[str, Any],
) -> list[list[Any]]:
    """Model tr_settle_outbox reads. SQL-SENSITIVE: each branch asserts the
    predicates it relies on are present in the real query, so a dropped guard/
    status/key predicate fails a test rather than silently matching."""
    p = params
    if sql.startswith("SELECT attempts, lease_owner FROM tr_settle_outbox"):
        _require_pred(sql, "authorization_id=@aid AND intent_kind=@kind", "mark-read")
        _require_pred(sql, "status='pending'", "mark-read")
        pk = (p["aid"], p["kind"])
        # Txn-aware read (finding 3): read-your-writes + register the read version
        # exactly like the reservation path, instead of peeking committed state.
        rec = txn._settle_outbox_current(pk) if txn is not None else db.settle_outbox.get(pk)
        if rec is None or rec.get("status") != "pending":
            return []
        return [[rec.get("attempts", 0), rec.get("lease_owner")]]
    if "WHERE status='pending' AND next_attempt_at <= @now" in sql:  # due scan
        now = p["now"]
        limit = int(p.get("limit", 100))
        rows = [
            rec for rec in db.settle_outbox.values()
            if rec.get("status") == "pending"
            and rec.get("next_attempt_at") is not None
            and rec["next_attempt_at"] <= now
        ]
        rows.sort(key=lambda r: r.get("next_attempt_at") or "")
        return [[rec.get(c) for c in OUTBOX_COLUMNS] for rec in rows[:limit]]
    if "SELECT COUNT(*) FROM tr_settle_outbox" in sql:  # reaper-guard predicate (has_intent)
        _require_pred(sql, "authorization_id=@aid", "has_intent")
        _require_pred(sql, f"status IN ({_GUARD_STATUS_SQL})", "has_intent")
        aid = p["aid"]
        # Committed-state read is correct for the in-txn guard too: enqueue
        # commits in its own txn, and the reaper txn never writes outbox rows.
        n = sum(
            1 for rec in db.settle_outbox.values()
            if rec.get("authorization_id") == aid and rec.get("status") in GUARD_STATUSES
        )
        return [[n]]
    if "WHERE authorization_id=@aid AND intent_kind=@kind" in sql:  # get by PK
        rec = db.settle_outbox.get((p["aid"], p["kind"]))
        return [[rec.get(c) for c in OUTBOX_COLUMNS]] if rec is not None else []
    raise NotImplementedError(sql)


def _execute_sql(
    db: FakeSpannerDatabase,
    txn: _FakeTransaction | None,
    sql: str,
    params: dict[str, Any],
) -> list[list[str]]:
    kind = params.get("kind", "")
    # Reaper scan: expired unsettled reservations. This must precede the generic
    # tr_settle_outbox dispatcher because the guarded scan names both tables; match
    # the more specific query first.
    if "FROM tr_reservation WHERE settled=false AND expires_at" in sql:
        _require_pred(
            sql,
            "SELECT reservation_id, authorization_id FROM tr_reservation",
            "reaper-scan",
        )
        _require_pred(sql, "expires_at < @now", "reaper-scan")
        _require_pred(sql, "LIMIT @limit", "reaper-scan")
        guarded = "NOT EXISTS" in sql
        if guarded:
            _require_pred(
                sql,
                "o.authorization_id = tr_reservation.authorization_id",
                "reaper-scan-guard",
            )
            _require_pred(sql, f"o.status IN ({_GUARD_STATUS_SQL})", "reaper-scan-guard")
        now = params["now"]
        limit = int(params.get("limit", 100))
        out: list[list] = []
        for rid, rec in db.reservations.items():
            exp = rec.get("expires_at")
            if rec.get("settled") or exp is None or _utc_datetime(exp) >= _utc_datetime(now):
                continue
            if guarded:
                # Model NOT EXISTS semantics before LIMIT (MF6): a dropped guard
                # predicate must fail tests instead of letting frozen rows consume
                # the reaper's scan window.
                aid = rec.get("authorization_id")
                if any(
                    row.get("authorization_id") == aid
                    and row.get("status") in GUARD_STATUSES
                    for row in db.settle_outbox.values()
                ):
                    continue
            out.append([rid, rec.get("authorization_id")])
            if len(out) >= limit:
                break
        return out
    # tr_settle_outbox (durable settle outbox) — modeled explicitly so a guard/
    # column/status typo makes a test FAIL rather than silently matching a
    # generic branch (the substring-collision hazard the design flags).
    if "tr_settle_outbox" in sql:
        return _execute_settle_outbox_sql(db, txn, sql, params)
    # Repair: any OPEN holds on a nonzero shard? (checked first — its query string
    # contains the generic count substrings below.)
    if "key_shard!=0" in sql:
        kh = params["kh"]
        return [[sum(
            1 for rec in db.reservations.values()
            if rec.get("key_hash") == kh and not rec.get("settled") and rec.get("key_shard", 0) != 0
        )]]
    if "ws_shard!=0" in sql:
        ws = params["ws"]
        return [[sum(
            1 for rec in db.reservations.values()
            if rec.get("workspace_id") == ws and not rec.get("settled") and rec.get("ws_shard", 0) != 0
        )]]
    # Open typed holds for this workspace. Checked BEFORE the generic count
    # below, which this query string contains.
    if "COUNT(*) FROM tr_reservation WHERE workspace_id=@ws AND settled = false" in sql:
        ws = params["ws"]
        return [[sum(
            1 for rec in db.reservations.values()
            if rec.get("workspace_id") == ws and not rec.get("settled")
        )]]
    if "COUNT(*) FROM tr_reservation WHERE key_hash=@kh AND settled = false" in sql:
        kh = params["kh"]
        return [[sum(
            1 for rec in db.reservations.values()
            if rec.get("key_hash") == kh and not rec.get("settled")
        )]]
    # Flip-reconcile: does this workspace have ANY typed reservation history?
    if "COUNT(*) FROM tr_reservation WHERE workspace_id=@ws" in sql:
        ws = params["ws"]
        return [[sum(1 for rec in db.reservations.values() if rec.get("workspace_id") == ws)]]
    # Repair: open holds for ONE scope (checked before the grouped sums below,
    # which match the same SUM(...) substring).
    if "SUM(credit_reserved_micro)" in sql and "workspace_id=@ws" in sql:
        ws = params["ws"]
        return [[sum(
            rec.get("credit_reserved_micro") or 0 for rec in db.reservations.values()
            if rec.get("workspace_id") == ws and not rec.get("settled") and rec.get("ws_shard", 0) == 0
        )]]
    if "SUM(key_reserved_micro)" in sql and "key_hash=@kh" in sql:
        kh = params["kh"]
        return [[sum(
            rec.get("key_reserved_micro") or 0 for rec in db.reservations.values()
            if rec.get("key_hash") == kh and not rec.get("settled") and rec.get("key_shard", 0) == 0
        )]]
    # Invariant auditor: open typed-origin holds summed by (scope, shard).
    if "SUM(credit_reserved_micro)" in sql:
        sums: dict[tuple, int] = {}
        for rec in db.reservations.values():
            if not rec.get("settled") and rec.get("workspace_id") is not None:
                grp = (
                    rec["workspace_id"],
                    rec.get("credit_shard"),
                    rec.get("ws_shard", 0),
                )
                sums[grp] = sums.get(grp, 0) + (rec.get("credit_reserved_micro") or 0)
        return [
            [ws, credit_shard, ws_shard, total]
            for (ws, credit_shard, ws_shard), total in sums.items()
        ]
    if "SUM(key_reserved_micro)" in sql:
        ksums: dict[tuple, int] = {}
        for rec in db.reservations.values():
            if not rec.get("settled") and rec.get("key_hash") is not None:
                grp = (rec["key_hash"], rec.get("key_shard", 0))
                ksums[grp] = ksums.get(grp, 0) + (rec.get("key_reserved_micro") or 0)
        return [[kh, shard, total] for (kh, shard), total in ksums.items()]
    # tr_reservation reads (idempotency replay + by-id for settle/reaper).
    if "FROM tr_reservation WHERE idempotency_scope=@scope" in sql:
        scope = params["scope"]
        rid = None
        if txn is not None:
            for op in reversed(txn.pending_writes):
                if op[0] == "insert_reservation" and op[1].get("idempotency_scope") == scope:
                    rid = op[1]["reservation_id"]
                    break
            if rid is None:
                rid = db.reservation_idemp.get(scope)
            idemp_key = ("idemp", scope)
            if idemp_key not in txn.read_versions:
                txn.read_versions[idemp_key] = 1 if scope in db.reservation_idemp else 0
        else:
            rid = db.reservation_idemp.get(scope)
        if rid is None:
            return []
        rec = txn._reservation_current(rid) if txn is not None else db.reservations.get(rid)
        if rec is None:
            return []
        cols = [c.strip() for c in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
        return [[rec.get(c) for c in cols]]
    if "FROM tr_reservation WHERE reservation_id=@rid" in sql:
        rid = params["rid"]
        rec = txn._reservation_current(rid) if txn is not None else db.reservations.get(rid)
        if rec is None:
            return []
        cols = [c.strip() for c in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
        return [[rec.get(c) for c in cols]]
    # Typed key-limit point-read (reserve_key 0-row classification). Honors the
    # WHERE, so it must precede the full-scan branch below.
    if "FROM tr_key_limit WHERE key_hash=@kh" in sql:
        # `shard` may be a literal 0 in the SQL (window/typed-usage point reads)
        # rather than a bound param (reserve_key classification).
        pk = (params["kh"], params.get("shard", 0))
        rec = (
            txn._typed_current("tr_key_limit", pk)
            if txn is not None
            else db.typed.get("tr_key_limit", {}).get(pk)
        )
        if rec is None:
            return []
        cols = [c.strip() for c in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
        return [[rec.get(c) for c in cols]]
    # Typed counter tables: full scan (Step 2 reconcile) OR a single-row read by
    # pk (the typed_balance overlay uses WHERE <pk_col>=@pk AND shard=0).
    for typed_table in ("tr_credit_balance", "tr_key_limit"):
        if f"FROM {typed_table}" in sql:
            cols = [c.strip() for c in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
            items = list(db.typed.get(typed_table, {}).items())
            if "@pk" in sql and "pk" in params:
                pk_col = "workspace_id" if typed_table == "tr_credit_balance" else "key_hash"
                items = [(pk, rec) for pk, rec in items if rec.get(pk_col) == params["pk"]]
                if "shard=0" in sql.replace(" ", ""):
                    items = [(pk, rec) for pk, rec in items if rec.get("shard", 0) == 0]
                if "shard<@shard_count" in sql.replace(" ", ""):
                    items = [
                        (pk, rec) for pk, rec in items
                        if 0 <= int(rec.get("shard", 0)) < int(params["shard_count"])
                    ]
                if "ORDER BY shard" in sql:
                    items.sort(key=lambda item: int(item[1].get("shard", 0)))
            recs = [
                txn._typed_current(typed_table, pk) if txn is not None else dict(rec)
                for pk, rec in items
            ]
            recs = [rec for rec in recs if rec is not None]
            return [[rec.get(c) for c in cols] for rec in recs]
    if "AND id=@id" in sql:
        entity_id = params["id"]
        if txn is not None:
            for op in reversed(txn.pending_writes):
                if op[0] == "upsert" and op[2] == kind and op[3] == entity_id:
                    return [[op[4]]]
                if op[0] == "delete" and op[2] == kind and op[3] == entity_id:
                    return []
            if (kind, entity_id) in txn.read_snapshots:
                snapshot = txn.read_snapshots[(kind, entity_id)]
                return [[snapshot]] if snapshot is not None else []
        row = db.rows.get((kind, entity_id))
        if row is None:
            if txn is not None:
                txn.read_snapshots[(kind, entity_id)] = None
                txn.read_versions[(kind, entity_id)] = 0
            return []
        if txn is not None:
            txn.read_snapshots[(kind, entity_id)] = row.body
            txn.read_versions[(kind, entity_id)] = row.version
        return [[row.body]]
    if "STARTS_WITH" in sql:
        prefix = params.get("prefix", "")
        rows = [(eid, r.body) for (k, eid), r in db.rows.items() if k == kind and eid.startswith(prefix)]
        rows.sort(key=lambda item: item[0])
        if "LIMIT @limit" in sql:
            rows = rows[: int(params["limit"])]
        return [[body] for _, body in rows]
    if "ENDS_WITH" in sql:
        suffix = params.get("suffix", "")
        rows = [(eid, r.body) for (k, eid), r in db.rows.items() if k == kind and eid.endswith(suffix)]
        rows.sort(key=lambda item: item[0])
        if "LIMIT @limit" in sql:
            rows = rows[: int(params["limit"])]
        return [[body] for _, body in rows]
    if "SELECT id, body FROM tr_entities WHERE kind=@kind" in sql:
        rows = [(eid, r.body) for (k, eid), r in db.rows.items() if k == kind]
        rows.sort(key=lambda item: item[0])
        return [[entity_id, body] for entity_id, body in rows]
    if "WHERE kind=@kind" in sql:
        rows = [(eid, r.body) for (k, eid), r in db.rows.items() if k == kind]
        rows.sort(key=lambda item: item[0])
        if "LIMIT @limit" in sql:
            rows = rows[: int(params["limit"])]
        return [[body] for _, body in rows]
    raise NotImplementedError(sql)


class FakeBigtableTable:
    def __init__(self) -> None:
        self.committed: list[bytes] = []
        self.rows: dict[bytes, dict[str, dict[bytes, list[Any]]]] = {}
        self.reads: list[tuple[bytes, bytes, int]] = []
        self.lock = threading.Lock()

    def direct_row(self, key: bytes) -> _FakeDirectRow:
        return _FakeDirectRow(key, self)

    def read_rows(self, *, start_key: bytes, end_key: bytes, limit: int) -> list[Any]:
        with self.lock:
            self.reads.append((start_key, end_key, limit))
            keys = [key for key in sorted(self.rows) if start_key <= key < end_key]
            return [_FakeReadRow(self.rows[key]) for key in keys[:limit]]


class _FakeCell:
    def __init__(self, value: bytes) -> None:
        self.value = value


class _FakeReadRow:
    def __init__(self, cells: dict[str, dict[bytes, list[Any]]]) -> None:
        self.cells = cells


class _FakeDirectRow:
    def __init__(self, key: bytes, table: FakeBigtableTable) -> None:
        self.key = key
        self.table = table
        self.cells: dict[str, dict[bytes, list[Any]]] = {}

    def set_cell(self, family: str, qualifier: bytes, value: bytes) -> None:
        self.cells.setdefault(family, {})[qualifier] = [_FakeCell(value)]

    def commit(self) -> None:
        with self.table.lock:
            self.table.committed.append(self.key)
            self.table.rows[self.key] = self.cells


def make_fake_store(
    *, ready_barrier: threading.Barrier | None = None
) -> tuple[Any, FakeSpannerDatabase, FakeBigtableTable]:
    from trusted_router.storage_gcp import SpannerBigtableStore
    from trusted_router.storage_gcp_auth_sessions import SpannerAuthSessions
    from trusted_router.storage_gcp_broadcast import SpannerBroadcastDestinations
    from trusted_router.storage_gcp_byok import SpannerByok
    from trusted_router.storage_gcp_email_blocks import SpannerEmailBlocks
    from trusted_router.storage_gcp_generations import SpannerGenerations
    from trusted_router.storage_gcp_io import SpannerIO
    from trusted_router.storage_gcp_keys import SpannerApiKeys
    from trusted_router.storage_gcp_oauth_codes import SpannerOAuthCodes
    from trusted_router.storage_gcp_rate_limits import SpannerRateLimits
    from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
    from trusted_router.storage_gcp_verification_tokens import SpannerVerificationTokens
    from trusted_router.storage_gcp_wallet_challenges import SpannerWalletChallenges

    db = FakeSpannerDatabase(ready_barrier=ready_barrier)
    bt = FakeBigtableTable()
    store = object.__new__(SpannerBigtableStore)
    store._spanner = _SpannerModule
    store._param_types = _ParamTypes
    store._database = db
    store._bt_table = bt
    from trusted_router.storage_gcp_credit_shards import CreditShardCountCache

    store._credit_shard_counts = CreditShardCountCache()
    io = SpannerIO(
        database=db,
        spanner_module=_SpannerModule,
        write_entity_batch=store._write_entity_batch,
        read_entity_tx=store._read_entity_tx,
        write_entity_tx=store._write_entity_tx,
        write_entity=store._write_entity,
        read_entity=store._read_entity,
        list_entities=store._list_entities,
        delete_entities=store._delete_entities,
        delete_entities_tx=store._delete_entities_tx,
    )
    store.api_keys = SpannerApiKeys(io)
    store.generation_store = SpannerGenerations(
        io,
        bt_table=bt,
        generation_family=store.generation_family,
        add_usage_to_key=store.api_keys.add_usage,
    )
    store.byok_store = SpannerByok(io)
    store.broadcast_store = SpannerBroadcastDestinations(io)
    store.settle_outbox = SpannerSettleOutbox(store._database, store._param_types)
    store.auth_session_store = SpannerAuthSessions(io)
    store.oauth_code_store = SpannerOAuthCodes(io)
    store.rate_limit_store = SpannerRateLimits(io)
    store.wallet_challenges = SpannerWalletChallenges(io)
    store.verification_tokens = SpannerVerificationTokens(io)
    store.email_blocks = SpannerEmailBlocks(io)
    return store, db, bt
