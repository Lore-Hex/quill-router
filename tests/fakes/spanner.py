from __future__ import annotations

import datetime as dt
import json
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

    @staticmethod
    def Array(element_type: Any) -> tuple[str, Any]:
        return ("ARRAY", element_type)


# Real Spanner column DEFAULTs for the typed counter tables (every counter is
# NOT NULL DEFAULT(0) in the DDL). The fake fills these on INSERT so a
# subset-column insert_or_update — which is what creation-time seeding uses,
# writing only the create-owned columns — still yields a complete row whose
# typed-DML-owned counters start at 0.
_TYPED_DEFAULTS: dict[str, dict[str, Any]] = {
    "tr_credit_balance": {"total_credits": 0, "total_usage": 0, "reserved": 0},
    "tr_earnings_balance": {"total_earned": 0, "total_transferred": 0},
    "tr_user_lifetime_topup": {"total_microdollars": 0},
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


try:  # subclass the real exception so production handlers see the prod type
    from google.api_core.exceptions import FailedPrecondition as _FailedPrecondition
except ImportError:  # pragma: no cover - google always present in the test venv
    _FailedPrecondition = Exception  # type: ignore[assignment,misc]


class FakeFailedPrecondition(_FailedPrecondition):
    """Non-retryable statement rejection (e.g. writing PENDING_COMMIT_TIMESTAMP()
    into a column without allow_commit_timestamp). run_in_transaction does NOT
    retry it; the callback either handles it or the whole transaction fails."""

    def __init__(self, detail: str = "failed precondition") -> None:
        super().__init__(detail)


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
        # tr_gateway_authorization: bounded per-request state keyed by
        # authorization_id. Kept separate from generic tr_entities so tests
        # catch accidental fallback writes after the typed cutover.
        self.gateway_authorizations: dict[str, dict] = {}
        self.gateway_authorization_versions: dict[str, int] = {}
        # Metadata-only typed generation records and durable ClickHouse handoff.
        self.generation_records: dict[str, dict] = {}
        self.operational_analytics_outbox: list[dict] = []
        # tr_settle_outbox: PK (authorization_id, intent_kind) -> {column: value}.
        self.settle_outbox: dict[tuple, dict] = {}
        self.settle_outbox_versions: dict[tuple, int] = {}
        # Per-authorization RANGE version: bumped whenever ANY outbox row of an
        # authorization is inserted/updated/deleted at commit. The MF2 guard
        # count and the sibling/EXISTS predicates are range reads over an
        # authorization's rows — including the ABSENCE of rows — and real
        # Spanner serializes them against a concurrent enqueue commit. Per-row
        # versions cannot represent "no row existed", so guard reads record
        # this key and _try_commit validates it: an enqueue that lands between
        # the in-txn zero-count and the claim commit aborts the claim.
        self.settle_outbox_auth_versions: dict[str, int] = {}
        # Per-KIND version for tr_entities, the entity-table analogue of
        # settle_outbox_auth_versions above. A paged PK-prefix scan
        # ("WHERE kind=@kind AND id>@after") is a RANGE read whose result
        # depends on rows that are absent as much as on rows that are present,
        # and per-row versions cannot represent "no row was there". Without
        # this, `list_open_credit_transfers` — a read-WRITE transaction that
        # DELETEs the stale queue rows it scans — would commit happily even
        # though a concurrent commit changed the range under it, where real
        # Spanner would abort it.
        self.entity_kind_versions: dict[str, int] = {}
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
                elif isinstance(key, tuple) and len(key) == 2 and key[0] == "outbox_auth":
                    # Range read over one authorization's outbox rows (MF2 guard
                    # count / sibling predicates): any commit touching the range
                    # since the read aborts this transaction.
                    current_version = self.settle_outbox_auth_versions.get(key[1], 0)
                elif isinstance(key, tuple) and len(key) == 2 and key[0] == "entity_kind":
                    # Paged range read over one kind's tr_entities rows: any
                    # commit touching that kind since the read aborts this
                    # transaction, so a scan-then-DELETE cannot act on a range
                    # that moved under it.
                    current_version = self.entity_kind_versions.get(key[1], 0)
                elif isinstance(key, tuple) and len(key) == 2 and key[0] == "gateway_auth":
                    current_version = self.gateway_authorization_versions.get(
                        key[1],
                        0,
                    )
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
                    self.entity_kind_versions[kind] = new_version
                elif op[0] == "delete":
                    _, _table, kind, entity_id = op
                    self.rows.pop((kind, entity_id), None)
                    self.entity_kind_versions[kind] = new_version
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
                    self.settle_outbox_auth_versions[pk[0]] = new_version
                elif op[0] == "delete_settle_outbox":
                    _, pk = op
                    self.settle_outbox.pop(pk, None)
                    self.settle_outbox_versions.pop(pk, None)
                    self.settle_outbox_auth_versions[pk[0]] = new_version
                elif op[0] == "update_reservation":
                    _, rid, record = op
                    self.reservations[rid] = record
                    self.reservation_versions[rid] = new_version
                elif op[0] in (
                    "insert_gateway_authorization",
                    "update_gateway_authorization",
                ):
                    _, authorization_id, record = op
                    self.gateway_authorizations[authorization_id] = record
                    self.gateway_authorization_versions[authorization_id] = new_version
                elif op[0] in ("insert_generation", "upsert_generation"):
                    _, generation_id, record = op
                    self.generation_records[generation_id] = record
                elif op[0] == "insert_operational_analytics_outbox":
                    self.operational_analytics_outbox.append(dict(op[1]))
                elif op[0] == "insert_entity_dml":  # DML INSERT into tr_entities
                    _, kind, entity_id, body = op
                    self.rows[(kind, entity_id)] = _Row(body=body, version=new_version)
                    self.entity_kind_versions[kind] = new_version
                elif op[0] == "update_entity_dml":  # DML UPDATE tr_entities body
                    _, kind, entity_id, body = op
                    self.rows[(kind, entity_id)] = _Row(body=body, version=new_version)
                    self.entity_kind_versions[kind] = new_version
                elif op[0] == "delete_entity_dml":  # DML DELETE from tr_entities
                    _, kind, entity_id = op
                    self.rows.pop((kind, entity_id), None)
                    self.entity_kind_versions[kind] = new_version
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
        # Row values pinned at FIRST read, keyed like read_versions. Real
        # Spanner read-write transactions are serializable: every statement in
        # one transaction sees ONE consistent view, and an external commit that
        # invalidates it surfaces as Aborted at commit time — never as a row
        # changing between two statements. Serving repeat reads from the pinned
        # snapshot (with _try_commit's version validation unchanged) reproduces
        # exactly that: the txn body stays self-consistent, and the conflict
        # becomes an abort-and-retry. Without this, a concurrent commit landing
        # between a plan read and its guarded DML made the guard fail MID-txn —
        # a phantom _RebalanceInvariantError real Spanner cannot produce
        # (surfaced as a rare credit-shard stress flake).
        self.row_snapshots: dict[tuple, dict | None] = {}
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

    def _pinned_read(
        self,
        version_key: tuple,
        current_version: int,
        rec: dict | None,
    ) -> dict | None:
        """First read pins version + value; repeat reads return the pin.

        Pin independently of read_versions: some INSERT handlers record an
        absence version directly without a value snapshot, and a later read
        through here must not KeyError (nor observe fresher state) for that key.
        """
        if version_key not in self.read_versions:
            self.read_versions[version_key] = current_version
        if version_key not in self.row_snapshots:
            self.row_snapshots[version_key] = dict(rec) if rec is not None else None
        snap = self.row_snapshots[version_key]
        return dict(snap) if snap is not None else None

    def _reservation_current(self, rid: str) -> dict | None:
        """In-txn view of a reservation (read-your-writes) + record read version."""
        for op in reversed(self.pending_writes):
            if op[0] == "update_reservation" and op[1] == rid:
                return dict(op[2])
            if op[0] == "insert_reservation" and op[1]["reservation_id"] == rid:
                return dict(op[1])
        return self._pinned_read(
            ("res", rid),
            self.db.reservation_versions.get(rid, 0),
            self.db.reservations.get(rid),
        )

    def _settle_outbox_current(self, pk: tuple) -> dict | None:
        """In-txn view of a settle-outbox row (read-your-writes) + read version."""
        for op in reversed(self.pending_writes):
            if op[0] == "delete_settle_outbox" and op[1] == pk:
                return None
            if op[0] in ("insert_settle_outbox", "update_settle_outbox") and op[1] == pk:
                return dict(op[2])
        return self._pinned_read(
            ("outbox", pk),
            self.db.settle_outbox_versions.get(pk, 0),
            self.db.settle_outbox.get(pk),
        )

    def _has_guarded_outbox_intent(self, authorization_id: str) -> bool:
        """Evaluate the correlated pending/dead EXISTS against the in-txn view."""
        # Range read (absence included) — record the per-authorization range
        # version so a concurrent enqueue/status-flip aborts this txn at commit.
        range_key = ("outbox_auth", authorization_id)
        if range_key not in self.read_versions:
            self.read_versions[range_key] = self.db.settle_outbox_auth_versions.get(
                authorization_id, 0
            )
        pks = set(self.db.settle_outbox)
        pks.update(
            op[1]
            for op in self.pending_writes
            if op[0]
            in (
                "insert_settle_outbox",
                "update_settle_outbox",
                "delete_settle_outbox",
            )
        )
        return any(
            rec is not None
            and rec.get("authorization_id") == authorization_id
            and rec.get("status") in GUARD_STATUSES
            for pk in pks
            if (rec := self._settle_outbox_current(pk)) is not None
        )

    def _gateway_authorization_current(
        self,
        authorization_id: str,
    ) -> dict | None:
        for op in reversed(self.pending_writes):
            if (
                op[0]
                in (
                    "insert_gateway_authorization",
                    "update_gateway_authorization",
                )
                and op[1] == authorization_id
            ):
                return dict(op[2])
        return self._pinned_read(
            ("gateway_auth", authorization_id),
            self.db.gateway_authorization_versions.get(authorization_id, 0),
            self.db.gateway_authorizations.get(authorization_id),
        )

    def _typed_current(self, table: str, pk: tuple) -> dict | None:
        """In-txn view of a typed row for DML: sees prior DML writes
        (update_typed = read-your-writes) but NOT buffered mutations (real Spanner
        DML can't see mutations; mixing is rejected in execute_update). Records
        the read version on first read for conflict detection."""
        for op in reversed(self.pending_writes):
            if op[0] in ("insert_typed_dml", "update_typed") and op[1] == table and op[2] == pk:
                return dict(op[3])
        return self._pinned_read(
            ("typed", table, pk),
            self.db.typed_versions.get((table, pk), 0),
            self.db.typed.get(table, {}).get(pk),
        )

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
        if "UPDATE tr_credit_balance SET total_credits = total_credits - @amt" in sql:
            _require_pred(
                sql,
                "AND (total_credits - total_usage - reserved) >= @amt",
                "guarded-workspace-debit",
            )
            pk = (p["ws"], 0)
            rec = self._typed_current("tr_credit_balance", pk)
            available = (
                rec["total_credits"] - rec["total_usage"] - rec["reserved"]
                if rec is not None
                else -1
            )
            if rec is None or available < p["amt"]:
                return 0
            new = dict(
                rec,
                total_credits=rec["total_credits"] - p["amt"],
                updated_at=p["now"],
            )
            self.pending_writes.append(("update_typed", "tr_credit_balance", pk, new))
            return 1
        if "UPDATE tr_credit_balance SET total_usage = total_usage + @amt" in sql:
            # Federated settlement's usage booking: UNCONDITIONAL by design.
            # The spend already happened on a peer plane while this one was
            # unreachable; a headroom predicate here would lose the debit.
            # Negative available balance is the honest ledger.
            _require_pred(sql, "WHERE workspace_id = @ws AND shard = 0", "federated-usage-booking")
            pk = (p["ws"], 0)
            rec = self._typed_current("tr_credit_balance", pk)
            if rec is None:
                return 0
            new = dict(rec, total_usage=rec["total_usage"] + p["amt"])
            self.pending_writes.append(("update_typed", "tr_credit_balance", pk, new))
            return 1
        if "UPDATE tr_credit_balance SET total_credits = total_credits + @amount" in sql:
            _require_pred(sql, "WHERE workspace_id=@ws AND shard=@shard", "credit-top-up")
            # Tracked by storage_gcp_credit_shard_admin / storage_gcp_counters;
            # dropping it silently stops stamping the column this fake then
            # happily reproduces from p["now"].
            _require_pred(sql, "source_updated_at=@now", "credit-top-up")
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
        if "INSERT INTO tr_credit_balance (workspace_id, shard, total_credits, total_usage, reserved, updated_at)" in sql:
            # Federated settlement's recreate-on-missing path: a fixed shard 0
            # with total_usage seeded to the booked amount, no source_updated_at.
            pk = (p["ws"], 0)
            if pk in self.db.typed.get("tr_credit_balance", {}):
                raise FakeAlreadyExists(f"tr_credit_balance/{pk}")
            record = dict(_TYPED_DEFAULTS["tr_credit_balance"])
            record.update(
                {
                    "workspace_id": p["ws"],
                    "shard": 0,
                    "total_credits": 0,
                    "total_usage": p["amt"],
                    "reserved": 0,
                    "updated_at": p["now"],
                }
            )
            self.pending_writes.append(("insert_typed_dml", "tr_credit_balance", pk, record))
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
            _require_pred(
                sql, "workspace_id=@ws AND shard=@shard AND reserved >= @hold", "credit-release"
            )
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
        if sql.startswith("UPDATE tr_earnings_balance SET total_earned"):
            pk = (p["user_id"], 0)
            rec = self._typed_current("tr_earnings_balance", pk)
            if rec is None:
                return 0
            new = dict(
                rec,
                total_earned=rec["total_earned"] + p["amount"],
                updated_at=p.get("now", dt.datetime.now(dt.UTC)),
            )
            self.pending_writes.append(("update_typed", "tr_earnings_balance", pk, new))
            return 1
        if sql.startswith("UPDATE tr_earnings_balance SET total_transferred"):
            _require_pred(
                sql,
                "AND (total_earned - total_transferred) >= @amount",
                "guarded-earnings-transfer",
            )
            pk = (p["user_id"], 0)
            rec = self._typed_current("tr_earnings_balance", pk)
            if rec is None or rec["total_earned"] - rec["total_transferred"] < p["amount"]:
                return 0
            new = dict(
                rec,
                total_transferred=rec["total_transferred"] + p["amount"],
                updated_at=p["now"],
            )
            self.pending_writes.append(("update_typed", "tr_earnings_balance", pk, new))
            return 1
        if sql.startswith("INSERT INTO tr_earnings_balance"):
            pk = (p["user_id"], 0)
            if self._typed_current("tr_earnings_balance", pk) is not None:
                raise FakeAlreadyExists(f"tr_earnings_balance/{pk}")
            record = dict(
                _TYPED_DEFAULTS["tr_earnings_balance"],
                user_id=p["user_id"],
                shard=0,
                total_earned=p.get("amount", 0),
                updated_at=p.get("now", dt.datetime.now(dt.UTC)),
            )
            self.pending_writes.append(("insert_typed_dml", "tr_earnings_balance", pk, record))
            return 1
        if sql.startswith("UPDATE tr_user_lifetime_topup"):
            pk = (p["user_id"],)
            rec = self._typed_current("tr_user_lifetime_topup", pk)
            if rec is None:
                return 0
            new = dict(
                rec,
                total_microdollars=rec["total_microdollars"] + p["amount"],
                updated_at=p["now"],
            )
            self.pending_writes.append(("update_typed", "tr_user_lifetime_topup", pk, new))
            return 1
        if sql.startswith("INSERT INTO tr_user_lifetime_topup"):
            pk = (p["user_id"],)
            if self._typed_current("tr_user_lifetime_topup", pk) is not None:
                raise FakeAlreadyExists(f"tr_user_lifetime_topup/{pk}")
            record = {
                "user_id": p["user_id"],
                "total_microdollars": p["amount"],
                "updated_at": p["now"],
            }
            self.pending_writes.append(("insert_typed_dml", "tr_user_lifetime_topup", pk, record))
            return 1
        if sql.startswith(("INSERT INTO tr_credit_movement", "INSERT OR IGNORE INTO tr_credit_movement")):
            pk = (p["account_id"], p["movement_id"])
            if self._typed_current("tr_credit_movement", pk) is not None:
                if sql.startswith("INSERT OR IGNORE"):
                    return 0
                raise FakeAlreadyExists(f"tr_credit_movement/{pk}")
            # Strict on purpose: created_at is NOT a commit-timestamp column,
            # so real Spanner requires a client TIMESTAMP param here. A writer
            # that omits it (or writes PENDING_COMMIT_TIMESTAMP()) must fail
            # in the fake exactly as it would in prod.
            if "PENDING_COMMIT_TIMESTAMP" in sql:
                raise FakeFailedPrecondition(
                    "tr_credit_movement.created_at: allow_commit_timestamp is not set"
                )
            record = {
                "account_id": p["account_id"],
                "movement_id": p["movement_id"],
                "kind": p["kind"],
                "amount_microdollars": p["amount"],
                "counterparty_account_id": p["counterparty"],
                "custom_model_id": p["custom_model_id"],
                "authorization_id": p["authorization_id"],
                "created_at": p["created_at"],
            }
            self.pending_writes.append(("insert_typed_dml", "tr_credit_movement", pk, record))
            return 1
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
                    ("day", "day_floor"),
                    ("week", "week_floor"),
                    ("month", "month_floor"),
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
            record["terminal_at"] = None
            self.pending_writes.append(("insert_reservation", record))
            return 1
        if (
            sql.startswith("UPDATE tr_reservation SET terminal_at=@terminal_at")
            and "IN UNNEST" in sql
        ):
            _require_pred(
                sql,
                "reservation_id IN UNNEST(@ids)",
                "reservation-terminal-backfill",
            )
            _require_pred(
                sql,
                "AND settled AND terminal_at IS NULL",
                "reservation-terminal-backfill",
            )
            _require_pred(
                sql,
                "AND NOT EXISTS (SELECT 1 FROM tr_settle_outbox o",
                "reservation-terminal-backfill",
            )
            _require_pred(
                sql,
                "o.authorization_id = tr_reservation.authorization_id",
                "reservation-terminal-backfill",
            )
            _require_pred(
                sql,
                f"o.status IN ({_GUARD_STATUS_SQL})",
                "reservation-terminal-backfill",
            )
            ids = p.get("ids")
            if not isinstance(ids, list):
                raise AssertionError("reservation-terminal-backfill requires an @ids array binding")
            updated = 0
            for rid in ids:
                rec = self._reservation_current(str(rid))
                if (
                    rec is None
                    or not rec.get("settled")
                    or rec.get("terminal_at") is not None
                    or self._has_guarded_outbox_intent(str(rec.get("authorization_id")))
                ):
                    continue
                new = dict(rec, terminal_at=p["terminal_at"])
                self.pending_writes.append(("update_reservation", str(rid), new))
                updated += 1
            return updated
        if "UPDATE tr_reservation SET settled=true" in sql:
            _require_pred(sql, "reservation_id=@rid AND settled=false", "reservation-claim")
            guarded = "tr_settle_outbox" in sql
            if guarded:
                _require_pred(
                    sql,
                    "terminal_at = IF(EXISTS (SELECT 1 FROM tr_settle_outbox o",
                    "reservation-claim-retention",
                )
                _require_pred(
                    sql,
                    "o.authorization_id = tr_reservation.authorization_id",
                    "reservation-claim-retention",
                )
                _require_pred(
                    sql,
                    f"o.status IN ({_GUARD_STATUS_SQL})",
                    "reservation-claim-retention",
                )
            else:
                _require_pred(
                    sql,
                    "settled_usage_type=@sut, terminal_at=@terminal_at",
                    "reservation-claim-retention-unguarded",
                )
            rec = self._reservation_current(p["rid"])
            if rec is None or rec["settled"]:
                return 0  # missing or already-claimed (replay)
            terminal_at = p["terminal_at"]
            if guarded and self._has_guarded_outbox_intent(str(rec["authorization_id"])):
                terminal_at = None
            new = dict(
                rec,
                settled=True,
                settled_usage_type=p["sut"],
                actual_micro=p["actual"],
                terminal_at=terminal_at,
            )
            self.pending_writes.append(("update_reservation", p["rid"], new))
            return 1
        if sql.startswith("UPDATE tr_reservation SET terminal_at=@terminal_at"):
            _require_pred(
                sql,
                "reservation_id=@rid AND settled=true AND terminal_at IS NULL",
                "reservation-complete",
            )
            guarded = "tr_settle_outbox" in sql
            if guarded:
                _require_pred(
                    sql,
                    "AND NOT EXISTS (SELECT 1 FROM tr_settle_outbox o",
                    "reservation-complete-retention",
                )
                _require_pred(
                    sql,
                    "o.authorization_id = tr_reservation.authorization_id",
                    "reservation-complete-retention",
                )
                _require_pred(
                    sql,
                    f"o.status IN ({_GUARD_STATUS_SQL})",
                    "reservation-complete-retention",
                )
            rec = self._reservation_current(p["rid"])
            if rec is None or not rec.get("settled") or rec.get("terminal_at") is not None:
                return 0
            if guarded and self._has_guarded_outbox_intent(str(rec["authorization_id"])):
                return 0
            new = dict(rec, terminal_at=p["terminal_at"])
            self.pending_writes.append(("update_reservation", p["rid"], new))
            return 1
        if sql.startswith("UPDATE tr_reservation SET terminal_at=NULL"):
            _require_pred(
                sql,
                "reservation_id=@rid AND terminal_at IS NOT NULL",
                "reservation-clear",
            )
            rec = self._reservation_current(p["rid"])
            if rec is None or rec.get("terminal_at") is None:
                return 0
            new = dict(rec, terminal_at=None)
            self.pending_writes.append(("update_reservation", p["rid"], new))
            return 1
        if sql.startswith("INSERT INTO tr_gateway_authorization"):
            authorization_id = p["authorization_id"]
            if authorization_id in self.db.gateway_authorizations:
                raise FakeAlreadyExists(authorization_id)
            version_key = ("gateway_auth", authorization_id)
            if version_key not in self.read_versions:
                self.read_versions[version_key] = 0
            record = dict(p)
            record["settled"] = False
            record["terminal_at"] = None
            self.pending_writes.append(("insert_gateway_authorization", authorization_id, record))
            return 1
        if sql.startswith("INSERT INTO tr_generation"):
            generation_id = str(p["generation_id"])
            if generation_id in self.db.generation_records:
                raise FakeAlreadyExists(generation_id)
            self.pending_writes.append(("insert_generation", generation_id, dict(p)))
            return 1
        if sql.startswith("INSERT OR UPDATE INTO tr_generation"):
            generation_id = str(p["generation_id"])
            self.pending_writes.append(("upsert_generation", generation_id, dict(p)))
            return 1
        if sql.startswith("INSERT INTO tr_operational_analytics_outbox"):
            self.pending_writes.append(
                ("insert_operational_analytics_outbox", dict(p))
            )
            return 1
        if sql.startswith("UPDATE tr_gateway_authorization SET settled=true, payload=@payload"):
            authorization_id = p["authorization_id"]
            rec = self._gateway_authorization_current(authorization_id)
            if rec is None:
                return 0
            new = dict(rec, settled=True, payload=p["payload"])
            self.pending_writes.append(("update_gateway_authorization", authorization_id, new))
            return 1
        if sql.startswith("UPDATE tr_gateway_authorization SET terminal_at=@terminal_at"):
            _require_pred(
                sql,
                "AND settled=true AND terminal_at IS NULL",
                "authorization-complete",
            )
            guarded = "tr_settle_outbox" in sql
            if guarded:
                _require_pred(
                    sql,
                    "AND NOT EXISTS (SELECT 1 FROM tr_settle_outbox o",
                    "authorization-complete-retention",
                )
                _require_pred(
                    sql,
                    "o.authorization_id = tr_gateway_authorization.authorization_id",
                    "authorization-complete-retention",
                )
                _require_pred(
                    sql,
                    f"o.status IN ({_GUARD_STATUS_SQL})",
                    "authorization-complete-retention",
                )
            authorization_id = p["authorization_id"]
            rec = self._gateway_authorization_current(authorization_id)
            if rec is None or not rec.get("settled") or rec.get("terminal_at") is not None:
                return 0
            if guarded and self._has_guarded_outbox_intent(authorization_id):
                return 0
            new = dict(rec, terminal_at=p["terminal_at"])
            self.pending_writes.append(("update_gateway_authorization", authorization_id, new))
            return 1
        if sql.startswith("UPDATE tr_gateway_authorization SET terminal_at=NULL"):
            _require_pred(
                sql,
                "authorization_id=@authorization_id AND terminal_at IS NOT NULL",
                "authorization-clear",
            )
            authorization_id = p["authorization_id"]
            rec = self._gateway_authorization_current(authorization_id)
            if rec is None or rec.get("terminal_at") is None:
                return 0
            new = dict(rec, terminal_at=None)
            self.pending_writes.append(("update_gateway_authorization", authorization_id, new))
            return 1
        if sql.startswith(
            "UPDATE tr_gateway_authorization SET settled=true, terminal_at=@terminal_at"
        ):
            _require_pred(sql, "AND settled=false", "authorization-reaper-close")
            authorization_id = p["authorization_id"]
            rec = self._gateway_authorization_current(authorization_id)
            if rec is None or rec.get("settled"):
                return 0
            new = dict(
                rec,
                settled=True,
                terminal_at=p["terminal_at"],
                payload=None,
            )
            self.pending_writes.append(("update_gateway_authorization", authorization_id, new))
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
                if (
                    op[0] in ("insert_entity_dml", "update_entity_dml")
                    and (op[1], op[2]) == entity_key
                ):
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
        if sql.startswith("DELETE FROM tr_entities"):
            entity_key = (p["kind"], p["id"])
            # Read-your-writes inside the txn, else committed state — the same
            # ordering the UPDATE branch above uses.
            present: bool | None = None
            for op in reversed(self.pending_writes):
                if op[0] in ("insert_entity_dml", "update_entity_dml") and (
                    (op[1], op[2]) == entity_key
                ):
                    present = True
                    break
                if op[0] == "delete_entity_dml" and (op[1], op[2]) == entity_key:
                    present = False
                    break
            if present is None:
                # The delete's predicate reads the row, so the row joins the
                # read set: a concurrent commit that inserts or rewrites it
                # since this read must abort us rather than let a stale "it
                # wasn't there" stand.
                if entity_key not in self.read_versions:
                    self.read_versions[entity_key] = (
                        self.db.rows[entity_key].version if entity_key in self.db.rows else 0
                    )
                present = entity_key in self.db.rows
            self.pending_writes.append(("delete_entity_dml", p["kind"], p["id"]))
            return 1 if present else 0
        if sql.startswith("INSERT INTO tr_settle_outbox"):
            pk = (p["authorization_id"], p["intent_kind"])
            if pk in self.db.settle_outbox:
                raise FakeAlreadyExists(str(pk))  # duplicate PK
            vkey = ("outbox", pk)
            if vkey not in self.read_versions:
                self.read_versions[vkey] = self.db.settle_outbox_versions.get(pk, 0)
            record = dict(p)
            # Production defines this as a generated stored column. The fake
            # only needs the same non-null/range contract, not FarmHash parity.
            shard_source = f"{p['authorization_id']}#{p['intent_kind']}".encode()
            record["queue_shard"] = sum(shard_source) % 16
            self.pending_writes.append(("insert_settle_outbox", pk, record))
            return 1
        if sql.startswith("UPDATE tr_settle_outbox SET settle_origin="):  # enqueue refresh
            # SQL-SENSITIVE (codex #113 finding 1): assert every load-bearing
            # predicate — including the PK key — is present, so a dropped predicate
            # FAILS a test (real Spanner would update every matching row, not the
            # single pk the fake derives from params).
            _require_pred(
                sql, "authorization_id=@authorization_id AND intent_kind=@intent_kind", "refresh"
            )
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
                "settle_origin",
                "reservation_id",
                "actual_cost_micro",
                "selected_endpoint_id",
                "model_id",
                "selected_usage_type",
                "settle_body",
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
            _require_pred(
                sql,
                "(@lease_owner IS NULL AND lease_owner IS NULL) OR "
                "(@lease_owner IS NOT NULL AND lease_owner=@lease_owner)",
                "park",
            )
            pk = (p["aid"], p["kind"])
            rec = self._settle_outbox_current(pk)
            if rec is None or rec["status"] != "pending":
                return 0
            if int(rec.get("attempts", 0) or 0) != int(p["attempts"]):
                return 0
            owner = rec.get("lease_owner")
            if owner != p.get("lease_owner"):
                return 0
            new = dict(
                rec,
                status="pending",
                attempts=rec.get("attempts", 0),
                last_error=p["err"],
                next_attempt_at=p["next_at"],
                lease_owner=None,
                leased_until=None,
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
            _require_pred(
                sql,
                "(@lease_owner IS NULL AND lease_owner IS NULL) OR "
                "(@lease_owner IS NOT NULL AND lease_owner=@lease_owner)",
                "mark",
            )
            pk = (p["aid"], p["kind"])
            rec = self._settle_outbox_current(pk)
            if rec is None or rec["status"] != "pending":
                return 0
            owner = rec.get("lease_owner")
            if owner != p.get("lease_owner"):
                return 0
            new = dict(
                rec,
                status=p["status"],
                attempts=p["attempts"],
                last_error=p["err"],
                next_attempt_at=p["next_at"],
                lease_owner=None,
                leased_until=None,
                updated_at=p["now"],
                terminal_at=p["terminal_at"],
                settle_body=None if p["done"] else rec.get("settle_body"),
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
                    # A batch commit is as visible to a concurrent range scan as
                    # a transactional one, so it must move the per-kind version
                    # too — otherwise a scanning transaction misses it entirely.
                    self.db.entity_kind_versions[kind] = new_version
                elif op[0] == "delete":
                    _, _table, kind, entity_id = op
                    self.db.rows.pop((kind, entity_id), None)
                    self.db.entity_kind_versions[kind] = new_version
                elif op[0] == "upsert_typed":
                    _, table, columns, value_tuple = op
                    _apply_upsert_typed(
                        self.db.typed,
                        self.db.typed_versions,
                        table,
                        columns,
                        value_tuple,
                        new_version,
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
        raise AssertionError(f"{what} query missing load-bearing predicate: {needle!r}")


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
    if sql.startswith("SELECT attempts, lease_owner FROM tr_settle_outbox") or sql.startswith(
        "SELECT attempts, lease_owner, reservation_id FROM tr_settle_outbox"
    ):
        _require_pred(sql, "authorization_id=@aid AND intent_kind=@kind", "mark-read")
        _require_pred(sql, "status='pending'", "mark-read")
        pk = (p["aid"], p["kind"])
        # Txn-aware read (finding 3): read-your-writes + register the read version
        # exactly like the reservation path, instead of peeking committed state.
        rec = txn._settle_outbox_current(pk) if txn is not None else db.settle_outbox.get(pk)
        if rec is None or rec.get("status") != "pending":
            return []
        values = [rec.get("attempts", 0), rec.get("lease_owner")]
        if sql.startswith("SELECT attempts, lease_owner, reservation_id FROM tr_settle_outbox"):
            values.append(rec.get("reservation_id"))
        return [values]
    if "next_attempt_at <= @now" in sql and "ORDER BY next_attempt_at" in sql:
        _require_pred(
            sql,
            "FORCE_INDEX=tr_settle_outbox_due_v2",
            "due-scan-index",
        )
        _require_pred(sql, "queue_shard IS NOT NULL", "due-scan-shard")
        _require_pred(sql, "next_attempt_at IS NOT NULL", "due-scan-sparse")
        _require_pred(sql, "status='pending'", "due-scan-status")
        now = p["now"]
        limit = int(p.get("limit", 100))
        rows = [
            rec
            for rec in db.settle_outbox.values()
            if rec.get("status") == "pending"
            and rec.get("queue_shard") is not None
            and rec.get("next_attempt_at") is not None
            and rec["next_attempt_at"] <= now
        ]
        rows.sort(key=lambda r: r.get("next_attempt_at") or "")
        return [[rec.get(c) for c in OUTBOX_COLUMNS] for rec in rows[:limit]]
    if "SELECT COUNT(*) FROM tr_settle_outbox" in sql and "intent_kind != @kind" in sql:
        _require_pred(sql, "authorization_id=@aid", "sibling-guard")
        _require_pred(sql, "intent_kind != @kind", "sibling-guard")
        _require_pred(
            sql,
            f"status IN ({_GUARD_STATUS_SQL})",
            "sibling-guard",
        )
        aid = p["aid"]
        kind = p["kind"]
        if txn is not None:
            # Range read over the authorization's rows (absence included) —
            # same serialization contract as the MF2 guard count above.
            range_key = ("outbox_auth", aid)
            if range_key not in txn.read_versions:
                txn.read_versions[range_key] = db.settle_outbox_auth_versions.get(aid, 0)
        count = 0
        pks = set(db.settle_outbox)
        if txn is not None:
            pks.update(
                op[1]
                for op in txn.pending_writes
                if op[0]
                in (
                    "insert_settle_outbox",
                    "update_settle_outbox",
                    "delete_settle_outbox",
                )
            )
        for pk in pks:
            rec = txn._settle_outbox_current(pk) if txn is not None else db.settle_outbox.get(pk)
            if (
                rec is not None
                and rec.get("authorization_id") == aid
                and rec.get("intent_kind") != kind
                and rec.get("status") in GUARD_STATUSES
            ):
                count += 1
        return [[count]]
    if "SELECT COUNT(*) FROM tr_settle_outbox" in sql:  # MF2 guard count / has_intent
        _require_pred(sql, "authorization_id=@aid", "has_intent")
        _require_pred(sql, f"status IN ({_GUARD_STATUS_SQL})", "has_intent")
        aid = p["aid"]
        if txn is not None:
            # MF2: inside settle_atomic's claim transaction this range read —
            # including its ABSENCE result — must serialize against a
            # concurrent enqueue commit. Record the per-authorization range
            # version so _try_commit aborts the claim if any outbox row of this
            # authorization commits after the count. (An earlier comment argued
            # committed-state reads were fine here; a diagnostic disproved it —
            # an enqueue landing between the zero-count and the claim commit
            # slipped through unvalidated.)
            range_key = ("outbox_auth", aid)
            if range_key not in txn.read_versions:
                txn.read_versions[range_key] = db.settle_outbox_auth_versions.get(aid, 0)
            pks = set(db.settle_outbox)
            pks.update(
                op[1]
                for op in txn.pending_writes
                if op[0]
                in (
                    "insert_settle_outbox",
                    "update_settle_outbox",
                    "delete_settle_outbox",
                )
            )
            n = sum(
                1
                for pk in pks
                if pk[0] == aid
                and (rec := txn._settle_outbox_current(pk)) is not None
                and rec.get("status") in GUARD_STATUSES
            )
            return [[n]]
        n = sum(
            1
            for rec in db.settle_outbox.values()
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
    if (
        "FROM tr_credit_movement " in sql
        and "WHERE kind='custom_model_payout' AND created_at>=@since" in sql
    ):
        movements = [
            dict(rec)
            for rec in db.typed.get("tr_credit_movement", {}).values()
            if rec["kind"] == "custom_model_payout"
            and rec["created_at"] >= params["since"]
        ]
        movements.sort(
            key=lambda rec: (
                rec["created_at"],
                rec["account_id"],
                rec["movement_id"],
            )
        )
        columns = [
            column.strip()
            for column in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")
        ]
        return [[rec.get(column) for column in columns] for rec in movements]
    if "FROM tr_credit_movement@{FORCE_INDEX=tr_credit_movement_by_time}" in sql:
        records = list(db.typed.get("tr_credit_movement", {}).items())
        visible = [
            txn._typed_current("tr_credit_movement", pk) if txn is not None else dict(rec)
            for pk, rec in records
        ]
        movements = [rec for rec in visible if rec is not None]
        movements = [rec for rec in movements if rec["account_id"] == params["account_id"]]
        if "kind IN UNNEST(@kinds)" in sql:
            movements = [rec for rec in movements if rec["kind"] in params["kinds"]]
        if "created_at < @before" in sql:
            movements = [rec for rec in movements if rec["created_at"] < params["before"]]
        if "kind='custom_model_payout'" in sql:
            totals: dict[str, int] = {}
            for rec in movements:
                custom_model_id = rec.get("custom_model_id")
                if (
                    rec["kind"] == "custom_model_payout"
                    and custom_model_id is not None
                    and rec["created_at"] >= params["since"]
                ):
                    totals[str(custom_model_id)] = totals.get(str(custom_model_id), 0) + int(
                        rec["amount_microdollars"]
                    )
            return [[model_id, total] for model_id, total in sorted(totals.items())]
        movements.sort(
            key=lambda rec: (rec["created_at"], rec["movement_id"]),
            reverse=True,
        )
        movements = movements[: int(params["limit"])]
        columns = [
            column.strip() for column in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")
        ]
        return [[rec.get(column) for column in columns] for rec in movements]
    if "FROM tr_earnings_balance" in sql:
        pk = (params["user_id"], 0)
        rec = (
            txn._typed_current("tr_earnings_balance", pk)
            if txn is not None
            else db.typed.get("tr_earnings_balance", {}).get(pk)
        )
        if rec is None:
            return []
        columns = [
            column.strip() for column in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")
        ]
        return [[rec.get(column) for column in columns]]
    if "FROM tr_user_lifetime_topup" in sql:
        pk = (params["user_id"],)
        rec = (
            txn._typed_current("tr_user_lifetime_topup", pk)
            if txn is not None
            else db.typed.get("tr_user_lifetime_topup", {}).get(pk)
        )
        return [] if rec is None else [[rec["total_microdollars"]]]
    if sql.startswith("SELECT payload FROM tr_generation"):
        generation = db.generation_records.get(str(params["generation_id"]))
        return [[str(generation["payload"])]] if generation is not None else []
    if sql.startswith("SELECT payload FROM tr_gateway_authorization "):
        rows = [
            rec
            for rec in db.gateway_authorizations.values()
            if rec.get("settled")
            and rec.get("created_at") >= params["since"]
            and rec.get("payload")
        ]
        rows.sort(key=lambda rec: (rec["created_at"], rec["authorization_id"]))
        return [[str(rec["payload"])] for rec in rows]
    # Guarded legacy terminal_at backfill. These narrow handlers intentionally
    # assert every real predicate they model (MF6); a production SQL regression
    # must fail tests instead of being repaired by the fake's Python filtering.
    if sql.startswith("SELECT reservation_id FROM tr_reservation"):
        _require_pred(
            sql,
            "WHERE settled AND terminal_at IS NULL",
            "reservation-terminal-backfill-scan",
        )
        _require_pred(
            sql,
            "AND NOT EXISTS (SELECT 1 FROM tr_settle_outbox o",
            "reservation-terminal-backfill-scan",
        )
        _require_pred(
            sql,
            "o.authorization_id = tr_reservation.authorization_id",
            "reservation-terminal-backfill-scan",
        )
        _require_pred(
            sql,
            f"o.status IN ({_GUARD_STATUS_SQL})",
            "reservation-terminal-backfill-scan",
        )
        _require_pred(
            sql,
            "ORDER BY reservation_id LIMIT @batch",
            "reservation-terminal-backfill-scan",
        )
        rows = [
            [rid]
            for rid, rec in sorted(db.reservations.items())
            if rec.get("settled")
            and rec.get("terminal_at") is None
            and not any(
                outbox.get("authorization_id") == rec.get("authorization_id")
                and outbox.get("status") in GUARD_STATUSES
                for outbox in db.settle_outbox.values()
            )
        ]
        return rows[: int(params["batch"])]
    if sql.startswith("SELECT COUNT(*) FROM tr_reservation") and not params:
        if "AND EXISTS (SELECT 1 FROM tr_settle_outbox o" in sql:
            _require_pred(
                sql,
                "WHERE settled AND terminal_at IS NULL",
                "reservation-terminal-backfill-excluded-count",
            )
            _require_pred(
                sql,
                "o.authorization_id = tr_reservation.authorization_id",
                "reservation-terminal-backfill-excluded-count",
            )
            _require_pred(
                sql,
                f"o.status IN ({_GUARD_STATUS_SQL})",
                "reservation-terminal-backfill-excluded-count",
            )
            return [
                [
                    sum(
                        1
                        for rec in db.reservations.values()
                        if rec.get("settled")
                        and rec.get("terminal_at") is None
                        and any(
                            outbox.get("authorization_id") == rec.get("authorization_id")
                            and outbox.get("status") in GUARD_STATUSES
                            for outbox in db.settle_outbox.values()
                        )
                    )
                ]
            ]
        if "terminal_at IS NULL" in sql:
            _require_pred(
                sql,
                "WHERE settled AND terminal_at IS NULL",
                "reservation-terminal-backfill-candidate-count",
            )
            return [
                [
                    sum(
                        1
                        for rec in db.reservations.values()
                        if rec.get("settled") and rec.get("terminal_at") is None
                    )
                ]
            ]
        if "terminal_at IS NOT NULL" in sql:
            _require_pred(
                sql,
                "WHERE settled AND terminal_at IS NOT NULL",
                "reservation-terminal-backfill-armed-count",
            )
            return [
                [
                    sum(
                        1
                        for rec in db.reservations.values()
                        if rec.get("settled") and rec.get("terminal_at") is not None
                    )
                ]
            ]
        _require_pred(
            sql,
            "WHERE NOT settled",
            "reservation-terminal-backfill-open-count",
        )
        return [[sum(1 for rec in db.reservations.values() if not rec.get("settled"))]]
    if sql.startswith("SELECT COUNT(*) FROM tr_gateway_authorization"):
        _require_pred(
            sql,
            "WHERE settled AND terminal_at IS NULL",
            "gateway-authorization-terminal-backfill-cross-check",
        )
        return [
            [
                sum(
                    1
                    for rec in db.gateway_authorizations.values()
                    if rec.get("settled") and rec.get("terminal_at") is None
                )
            ]
        ]
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
                    row.get("authorization_id") == aid and row.get("status") in GUARD_STATUSES
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
    if "FROM tr_gateway_authorization" in sql:
        authorization_id = params["authorization_id"]
        rec = (
            txn._gateway_authorization_current(authorization_id)
            if txn is not None
            else db.gateway_authorizations.get(authorization_id)
        )
        if rec is None:
            return []
        cols = [col.strip() for col in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
        return [[rec.get(col) for col in cols]]
    # Repair: any OPEN holds on a nonzero shard? (checked first — its query string
    # contains the generic count substrings below.)
    if "key_shard!=0" in sql:
        kh = params["kh"]
        return [
            [
                sum(
                    1
                    for rec in db.reservations.values()
                    if rec.get("key_hash") == kh
                    and not rec.get("settled")
                    and rec.get("key_shard", 0) != 0
                )
            ]
        ]
    if "ws_shard!=0" in sql:
        ws = params["ws"]
        return [
            [
                sum(
                    1
                    for rec in db.reservations.values()
                    if rec.get("workspace_id") == ws
                    and not rec.get("settled")
                    and rec.get("ws_shard", 0) != 0
                )
            ]
        ]
    if (
        "FROM tr_reservation WHERE workspace_id=@ws AND settled=false" in sql
        and "GROUP BY credit_shard, ws_shard" in sql
    ):
        ws = params["ws"]
        groups: dict[tuple[Any, Any], list[int]] = {}
        for rec in db.reservations.values():
            if rec.get("workspace_id") != ws or rec.get("settled"):
                continue
            group = (rec.get("credit_shard"), rec.get("ws_shard", 0))
            values = groups.setdefault(group, [0, 0])
            values[0] += 1
            values[1] += int(rec.get("credit_reserved_micro") or 0)
        return [
            [credit_shard, ws_shard, values[0], values[1]]
            for (credit_shard, ws_shard), values in groups.items()
        ]
    if (
        "FROM tr_reservation WHERE key_hash=@kh AND settled=false" in sql
        and "GROUP BY key_shard" in sql
    ):
        kh = params["kh"]
        groups: dict[Any, list[int]] = {}
        for rec in db.reservations.values():
            if rec.get("key_hash") != kh or rec.get("settled"):
                continue
            shard = rec.get("key_shard", 0)
            values = groups.setdefault(shard, [0, 0])
            values[0] += 1
            values[1] += int(rec.get("key_reserved_micro") or 0)
        return [[shard, values[0], values[1]] for shard, values in groups.items()]
    # Open typed holds for this workspace. Checked BEFORE the generic count
    # below, which this query string contains.
    if "COUNT(*) FROM tr_reservation WHERE workspace_id=@ws AND settled = false" in sql:
        ws = params["ws"]
        return [
            [
                sum(
                    1
                    for rec in db.reservations.values()
                    if rec.get("workspace_id") == ws and not rec.get("settled")
                )
            ]
        ]
    if "COUNT(*) FROM tr_reservation WHERE key_hash=@kh AND settled = false" in sql:
        kh = params["kh"]
        return [
            [
                sum(
                    1
                    for rec in db.reservations.values()
                    if rec.get("key_hash") == kh and not rec.get("settled")
                )
            ]
        ]
    # Flip-reconcile: does this workspace have ANY typed reservation history?
    if "COUNT(*) FROM tr_reservation WHERE workspace_id=@ws" in sql:
        ws = params["ws"]
        return [[sum(1 for rec in db.reservations.values() if rec.get("workspace_id") == ws)]]
    # Repair: open holds for ONE scope (checked before the grouped sums below,
    # which match the same SUM(...) substring).
    if "SUM(credit_reserved_micro)" in sql and "workspace_id=@ws" in sql:
        ws = params["ws"]
        return [
            [
                sum(
                    rec.get("credit_reserved_micro") or 0
                    for rec in db.reservations.values()
                    if rec.get("workspace_id") == ws
                    and not rec.get("settled")
                    and rec.get("ws_shard", 0) == 0
                )
            ]
        ]
    if "SUM(key_reserved_micro)" in sql and "key_hash=@kh" in sql:
        kh = params["kh"]
        return [
            [
                sum(
                    rec.get("key_reserved_micro") or 0
                    for rec in db.reservations.values()
                    if rec.get("key_hash") == kh
                    and not rec.get("settled")
                    and rec.get("key_shard", 0) == 0
                )
            ]
        ]
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
    if "legacy_reshard_guard" in sql:
        cutoff = params["cutoff"]
        grouped: dict[tuple[str | None, str | None], list[int]] = {}
        for (row_kind, _entity_id), row in db.rows.items():
            if row_kind != "reservation":
                continue
            body = json.loads(row.body)
            if body.get("settled") is not False:
                continue
            key = (body.get("workspace_id"), body.get("key_hash"))
            counts = grouped.setdefault(key, [0, 0])
            raw_created = body.get("created_at")
            try:
                created = dt.datetime.fromisoformat(str(raw_created).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    raise ValueError("naive timestamp")
            except (TypeError, ValueError):
                counts[0] += 1
                continue
            counts[0 if created >= cutoff else 1] += 1
        return [
            [workspace_id, key_hash, counts[0], counts[1]]
            for (workspace_id, key_hash), counts in sorted(
                grouped.items(), key=lambda item: repr(item[0])
            )
        ]
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
    compact_sql = sql.replace(" ", "")
    if "FROM tr_key_limit WHERE key_hash=@kh" in sql and "shard<@shard_count" in compact_sql:
        items = [
            (pk, rec)
            for pk, rec in db.typed.get("tr_key_limit", {}).items()
            if rec.get("key_hash") == params["kh"]
            and 0 <= int(rec.get("shard", 0)) < int(params["shard_count"])
        ]
        items.sort(key=lambda item: int(item[1].get("shard", 0)))
        recs = [
            txn._typed_current("tr_key_limit", pk) if txn is not None else dict(rec)
            for pk, rec in items
        ]
        cols = [c.strip() for c in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
        return [[rec.get(c) for c in cols] for rec in recs if rec is not None]
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
            pk_col = "workspace_id" if typed_table == "tr_credit_balance" else "key_hash"
            if "shard_count" in params:
                # A full sharded read of one tenant: the credit-escrow headroom
                # read, and tr_key_limit's reshard read on the same shape. The
                # clauses below are re-implemented in Python only IF present in
                # the SQL, so a dropped clause would silently become a no-op
                # instead of a failure. Losing the pk bound returns EVERY
                # tenant's shard rows keyed by shard number — the escrow would
                # then plan against another workspace's balance; losing the
                # shard bound or the ordering breaks the completeness check
                # that makes an incomplete shard set fail closed.
                what = f"{typed_table}-sharded-read"
                _require_pred(sql, f"WHERE {pk_col}=@pk", what)
                _require_pred(sql, "shard>=0 AND shard<@shard_count", what)
                _require_pred(sql, "ORDER BY shard", what)
            if "@pk" in sql and "pk" in params:
                items = [(pk, rec) for pk, rec in items if rec.get(pk_col) == params["pk"]]
                if "shard=0" in sql.replace(" ", ""):
                    items = [(pk, rec) for pk, rec in items if rec.get("shard", 0) == 0]
                if "shard<@shard_count" in sql.replace(" ", ""):
                    items = [
                        (pk, rec)
                        for pk, rec in items
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
    if "AND id>@after" in sql:
        # Paged PK-prefix scan of one kind (the credit-transfer recovery
        # queue). Reads committed rows plus this transaction's own pending
        # entity DML, so a queue row deleted earlier in the SAME transaction
        # does not reappear in a later page.
        #
        # SQL-SENSITIVE. Losing `kind=@kind` turns this into a scan of the
        # WHOLE entity table, which in production is a read-write range lock
        # over ~14.8M rows; losing `ORDER BY id` makes the `after_id` cursor
        # meaningless, because Spanner guarantees no order without it, and the
        # recovery walk then skips escrowed transfers forever — the exact
        # failure paging was added to prevent.
        _require_pred(sql, "kind=@kind", "credit-transfer-queue-scan")
        _require_pred(sql, "ORDER BY id", "credit-transfer-queue-scan")
        after = str(params.get("after", ""))
        visible = {eid: r.body for (k, eid), r in db.rows.items() if k == kind}
        if txn is not None:
            # RANGE read: record the per-kind version so a concurrent commit
            # touching this kind aborts us, exactly as the outbox range reads
            # above do. The scan drives DELETEs, so acting on a stale range
            # would delete against state this transaction never saw.
            range_key = ("entity_kind", kind)
            if range_key not in txn.read_versions:
                txn.read_versions[range_key] = db.entity_kind_versions.get(kind, 0)
            for op in txn.pending_writes:
                if op[0] in ("insert_entity_dml", "update_entity_dml") and op[1] == kind:
                    visible[op[2]] = op[3]
                elif op[0] == "delete_entity_dml" and op[1] == kind:
                    visible.pop(op[2], None)
        rows = sorted((eid, body) for eid, body in visible.items() if eid > after)
        if "LIMIT @limit" in sql:
            rows = rows[: int(params["limit"])]
        cols = [c.strip() for c in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
        return [[(eid if c == "id" else body) for c in cols] for eid, body in rows]
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
        rows = [
            (eid, r.body) for (k, eid), r in db.rows.items() if k == kind and eid.startswith(prefix)
        ]
        rows.sort(key=lambda item: item[0])
        if "LIMIT @limit" in sql:
            rows = rows[: int(params["limit"])]
        columns = [
            column.strip() for column in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")
        ]
        return [
            [(entity_id if column == "id" else body) for column in columns]
            for entity_id, body in rows
        ]
    if "ENDS_WITH" in sql:
        suffix = params.get("suffix", "")
        rows = [
            (eid, r.body) for (k, eid), r in db.rows.items() if k == kind and eid.endswith(suffix)
        ]
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

    def read_rows(
        self,
        *,
        start_key: bytes,
        end_key: bytes,
        limit: int,
        **_kwargs: Any,
    ) -> list[Any]:
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

    def set_cell(
        self,
        family: str,
        qualifier: bytes,
        value: bytes,
        timestamp: Any | None = None,
    ) -> None:
        _ = timestamp
        self.cells.setdefault(family, {})[qualifier] = [_FakeCell(value)]

    def commit(self) -> None:
        with self.table.lock:
            self.table.committed.append(self.key)
            merged = {
                family: {qualifier: list(cells) for qualifier, cells in qualifiers.items()}
                for family, qualifiers in self.table.rows.get(self.key, {}).items()
            }
            for family, qualifiers in self.cells.items():
                merged.setdefault(family, {}).update(qualifiers)
            self.table.rows[self.key] = merged


def make_fake_store(
    *,
    ready_barrier: threading.Barrier | None = None,
    request_record_write_mode: str = "legacy",
    operational_analytics_outbox_enabled: bool = False,
    generation_records_enabled: bool = False,
    bigtable_writes_enabled: bool = True,
) -> tuple[Any, FakeSpannerDatabase, FakeBigtableTable]:
    from trusted_router.storage_gcp import SpannerBigtableStore
    from trusted_router.storage_gcp_attribution import SpannerAcquisitionAttribution
    from trusted_router.storage_gcp_auth_sessions import SpannerAuthSessions
    from trusted_router.storage_gcp_broadcast import SpannerBroadcastDestinations
    from trusted_router.storage_gcp_byok import SpannerByok
    from trusted_router.storage_gcp_custom_models import SpannerCustomModels
    from trusted_router.storage_gcp_email_blocks import SpannerEmailBlocks
    from trusted_router.storage_gcp_generations import SpannerGenerations
    from trusted_router.storage_gcp_group_buy import SpannerBedrockGroupBuy
    from trusted_router.storage_gcp_io import SpannerIO
    from trusted_router.storage_gcp_keys import SpannerApiKeys
    from trusted_router.storage_gcp_oauth_codes import SpannerOAuthCodes
    from trusted_router.storage_gcp_operational_analytics_outbox import (
        SpannerOperationalAnalyticsOutbox,
    )
    from trusted_router.storage_gcp_rate_limits import SpannerRateLimits
    from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
    from trusted_router.storage_gcp_user_models import SpannerUserProvidedModels
    from trusted_router.storage_gcp_verification_tokens import SpannerVerificationTokens
    from trusted_router.storage_gcp_video_jobs import SpannerVideoJobs
    from trusted_router.storage_gcp_wallet_challenges import SpannerWalletChallenges

    db = FakeSpannerDatabase(ready_barrier=ready_barrier)
    bt = FakeBigtableTable()
    store = object.__new__(SpannerBigtableStore)
    store._spanner = _SpannerModule
    store._param_types = _ParamTypes
    store._database = db
    store._bt_table = bt
    store.request_record_write_mode = request_record_write_mode
    store._generation_records_enabled = generation_records_enabled
    store._bigtable_writes_enabled = bigtable_writes_enabled
    # `object.__new__` skips __init__, so every attribute the real constructor
    # sets has to be set here too. These two drive the analytics READ path
    # (_analytics_read); without them any read-side test AttributeErrors
    # instead of exercising the store. Defaults mirror the real signature.
    store._bigtable_enabled = True
    store._analytics_read_mode = "bigtable"
    store._analytics_dual_read_grace_seconds = 0
    store._operational_analytics = None
    from trusted_router.storage_gcp_credit_shards import CreditShardCountCache

    store._credit_shard_counts = CreditShardCountCache()
    io = SpannerIO(
        database=db,
        spanner_module=_SpannerModule,
        param_types=_ParamTypes,
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
    store.acquisition_store = SpannerAcquisitionAttribution(io)
    store.bedrock_group_buy_store = SpannerBedrockGroupBuy(io)
    store._operational_analytics_outbox = (
        SpannerOperationalAnalyticsOutbox(db, _ParamTypes)
        if operational_analytics_outbox_enabled
        else None
    )
    store.generation_store = SpannerGenerations(
        io,
        bt_table=bt,
        param_types=_ParamTypes,
        generation_records_enabled=generation_records_enabled,
        bigtable_writes_enabled=bigtable_writes_enabled,
        activity_family=store.activity_family,
        benchmark_family=store.benchmark_family,
        legacy_family=store.legacy_generation_family,
        add_usage_to_key=store.api_keys.add_usage,
        operational_analytics_outbox=store._operational_analytics_outbox,
    )
    store.byok_store = SpannerByok(io)
    store.custom_model_store = SpannerCustomModels(io)
    store.user_model_store = SpannerUserProvidedModels(io)
    store.broadcast_store = SpannerBroadcastDestinations(io)
    store.video_job_store = SpannerVideoJobs(io)
    store.settle_outbox = SpannerSettleOutbox(store._database, store._param_types)
    store.auth_session_store = SpannerAuthSessions(io)
    store.oauth_code_store = SpannerOAuthCodes(io)
    store.rate_limit_store = SpannerRateLimits(io)
    store.wallet_challenges = SpannerWalletChallenges(io)
    store.verification_tokens = SpannerVerificationTokens(io)
    store.email_blocks = SpannerEmailBlocks(io)
    return store, db, bt
