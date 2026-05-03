from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


class _ParamTypes:
    STRING = "STRING"


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
        self._global_version = 0
        self._commit_lock = threading.Lock()
        self._ready_barrier = ready_barrier
        self.aborts = 0
        self.commits = 0

    def run_in_transaction(self, fn: Any) -> Any:
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
            return True

    def snapshot(self) -> _FakeSnapshot:
        return _FakeSnapshot(self)

    def batch(self) -> _FakeBatch:
        return _FakeBatch(self)


class _FakeTransaction:
    def __init__(self, db: FakeSpannerDatabase) -> None:
        self.db = db
        self.read_versions: dict[tuple[str, str], int] = {}
        self.read_snapshots: dict[tuple[str, str], str | None] = {}
        self.pending_writes: list[tuple] = []

    def execute_sql(
        self,
        sql: str,
        *,
        params: dict[str, Any] | None = None,
        param_types: Any = None,
    ) -> list[list[str]]:
        return _execute_sql(self.db, self, sql, params or {})

    def insert_or_update(
        self, *, table: str, columns: tuple[str, ...], values: list[tuple]
    ) -> None:
        for value_tuple in values:
            kind, entity_id, body = value_tuple[0], value_tuple[1], value_tuple[2]
            self.pending_writes.append(("upsert", table, kind, entity_id, body))

    def delete(self, table: str, keyset: _KeySet) -> None:
        for entry in keyset.keys:
            kind, entity_id = entry[0], entry[1]
            self.pending_writes.append(("delete", table, kind, entity_id))


class _FakeSnapshot:
    def __init__(self, db: FakeSpannerDatabase) -> None:
        self.db = db

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
        return None

    def insert_or_update(
        self, *, table: str, columns: tuple[str, ...], values: list[tuple]
    ) -> None:
        for value_tuple in values:
            kind, entity_id, body = value_tuple[0], value_tuple[1], value_tuple[2]
            self.pending_writes.append(("upsert", table, kind, entity_id, body))

    def delete(self, table: str, keyset: _KeySet) -> None:
        for entry in keyset.keys:
            kind, entity_id = entry[0], entry[1]
            self.pending_writes.append(("delete", table, kind, entity_id))


def _execute_sql(
    db: FakeSpannerDatabase,
    txn: _FakeTransaction | None,
    sql: str,
    params: dict[str, Any],
) -> list[list[str]]:
    kind = params.get("kind", "")
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
        return [[r.body] for (k, eid), r in db.rows.items() if k == kind and eid.startswith(prefix)]
    if "ENDS_WITH" in sql:
        suffix = params.get("suffix", "")
        return [[r.body] for (k, eid), r in db.rows.items() if k == kind and eid.endswith(suffix)]
    if "WHERE kind=@kind" in sql:
        return [[r.body] for (k, _), r in db.rows.items() if k == kind]
    raise NotImplementedError(sql)


class FakeBigtableTable:
    def __init__(self) -> None:
        self.committed: list[bytes] = []
        self.lock = threading.Lock()

    def direct_row(self, key: bytes) -> _FakeDirectRow:
        return _FakeDirectRow(key, self)

    def read_rows(self, *, start_key: bytes, end_key: bytes, limit: int) -> list[Any]:
        return []


class _FakeDirectRow:
    def __init__(self, key: bytes, table: FakeBigtableTable) -> None:
        self.key = key
        self.table = table

    def set_cell(self, *_: Any) -> None:
        return None

    def commit(self) -> None:
        with self.table.lock:
            self.table.committed.append(self.key)


def make_fake_store(
    *, ready_barrier: threading.Barrier | None = None
) -> tuple[Any, FakeSpannerDatabase, FakeBigtableTable]:
    from trusted_router.storage_gcp import SpannerBigtableStore
    from trusted_router.storage_gcp_auth_sessions import SpannerAuthSessions
    from trusted_router.storage_gcp_byok import SpannerByok
    from trusted_router.storage_gcp_email_blocks import SpannerEmailBlocks
    from trusted_router.storage_gcp_generations import SpannerGenerations
    from trusted_router.storage_gcp_io import SpannerIO
    from trusted_router.storage_gcp_keys import SpannerApiKeys
    from trusted_router.storage_gcp_oauth_codes import SpannerOAuthCodes
    from trusted_router.storage_gcp_rate_limits import SpannerRateLimits
    from trusted_router.storage_gcp_verification_tokens import SpannerVerificationTokens
    from trusted_router.storage_gcp_wallet_challenges import SpannerWalletChallenges

    db = FakeSpannerDatabase(ready_barrier=ready_barrier)
    bt = FakeBigtableTable()
    store = object.__new__(SpannerBigtableStore)
    store._spanner = _SpannerModule
    store._param_types = _ParamTypes
    store._database = db
    store._bt_table = bt
    io = SpannerIO(
        database=db,
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
    store.auth_session_store = SpannerAuthSessions(io)
    store.oauth_code_store = SpannerOAuthCodes(io)
    store.rate_limit_store = SpannerRateLimits(io)
    store.wallet_challenges = SpannerWalletChallenges(io)
    store.verification_tokens = SpannerVerificationTokens(io)
    store.email_blocks = SpannerEmailBlocks(io)
    return store, db, bt
