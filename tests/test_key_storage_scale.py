from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import make_fake_store
from trusted_router.storage_codec import json_body
from trusted_router.storage_models import ApiKey
from trusted_router.storage_postgres import PostgresStore


def _key(store, name="key"):
    return store.api_keys.create(workspace_id="ws-owner", name=name, creator_user_id=None)


@pytest.mark.parametrize("bulk", [False, True])
def test_spanner_delete_is_one_atomic_commit(monkeypatch, bulk):
    store, db = make_fake_store()
    raw, key = _key(store)
    before = db.commits
    original = db.run_in_transaction

    def fail_after_mutations(operation, **kwargs):
        def fail(tx):
            operation(tx)
            raise RuntimeError("rollback after all mutations")

        return original(fail, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(db, "run_in_transaction", fail_after_mutations)
        with pytest.raises(RuntimeError, match="rollback after all mutations"):
            if bulk:
                store.delete_keys(key.workspace_id, [key.hash])
            else:
                store.delete_key(key.hash)
    assert db.commits == before
    assert store.get_key_by_raw(raw) is not None
    assert [r.api_key.hash for r in store.list_api_keys_with_usage(key.workspace_id)] == [key.hash]
    assert ("api_key_lookup", key.lookup_hash) in db.rows
    if bulk:
        assert store.delete_keys(key.workspace_id, [key.hash]) == {key.hash: True}
    else:
        assert store.delete_key(key.hash) is True
    assert db.commits == before + 1
    assert store.get_key_by_raw(raw) is None
    assert store.list_api_keys_with_usage(key.workspace_id) == []
    assert ("api_key", key.hash) not in db.rows
    assert ("api_key_lookup", key.lookup_hash) not in db.rows
    assert ("api_key_by_workspace", f"{key.workspace_id}#{key.hash}") not in db.rows
    # Revocation must not erase accounting used by in-flight settlement.
    assert (key.hash, 0) in db.typed["tr_key_limit"]


def test_spanner_bulk_deletes_use_bounded_transactions(monkeypatch):
    store, db = make_fake_store()
    hashes = [_key(store, str(index))[1].hash for index in range(205)]
    foreign = store.api_keys.create(workspace_id="foreign", name="foreign", creator_user_id=None)[1]
    before = db.commits
    before_reads = db.transaction_execute_sql_calls
    batches = []
    original = db.run_in_transaction

    def count(operation, **kwargs):
        def capture(tx):
            result = operation(tx)
            batches.append(len(result))
            return result

        return original(capture, **kwargs)

    monkeypatch.setattr(db, "run_in_transaction", count)
    results = store.delete_keys("ws-owner", [*hashes, hashes[0], foreign.hash, "missing"])
    assert results == {**dict.fromkeys(hashes, True), foreign.hash: False, "missing": False}
    assert db.commits == before + 3
    assert batches == [100, 100, 7]
    assert db.transaction_execute_sql_calls - before_reads == 3
    assert store.list_api_keys_with_usage("ws-owner") == []
    assert store.get_key_by_hash(foreign.hash) is not None


def test_spanner_page_keeps_all_shards_of_selected_key():
    store, db = make_fake_store()
    keys = [_key(store, str(index))[1] for index in range(3)]
    for key in keys:
        key.created_at = "2026-01-01T00:00:00Z"
        key.usage_shard_count = 3
        store._write_entity("api_key", key.hash, key)
        base = db.typed["tr_key_limit"][(key.hash, 0)]
        for shard in range(3):
            db.typed["tr_key_limit"][(key.hash, shard)] = {
                **base,
                "key_hash": key.hash,
                "shard": shard,
                "usage": 10 + shard,
            }
    before = db.snapshot_execute_sql_calls
    page = store.list_api_keys_with_usage("ws-owner", limit=1, offset=1)
    assert db.snapshot_execute_sql_calls == before + 1
    assert len(page) == 1
    assert page[0].api_key.hash == sorted(k.hash for k in keys)[1]
    assert page[0].usage_microdollars == 33
    sql = db.snapshot_sql[-1]
    assert sql.index("LIMIT @limit OFFSET @offset") < sql.index("LEFT JOIN tr_key_limit")


class _PgResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


@pytest.mark.parametrize("bulk", [False, True])
def test_postgres_delete_owns_all_mutations_in_one_transaction(monkeypatch, bulk):
    key = ApiKey(
        hash="own",
        salt="",
        secret_hash="",
        lookup_hash="lookup-own",
        name="own",
        label="",
        workspace_id="ws-owner",
        creator_user_id=None,
    )
    foreign = replace(key, hash="foreign", lookup_hash="lookup-foreign", workspace_id="ws-other")
    transactions = []

    class Connection:
        def __init__(self):
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def execute(self, sql, params):
            self.calls.append((sql, params))
            if sql.startswith("SELECT"):
                assert "ORDER BY id FOR UPDATE" in sql
                return _PgResult(
                    [(k.hash, json_body(k)) for k in (key, foreign) if k.hash in params[1]]
                    + (
                        [("corrupt", json_body(replace(key, hash="wrong-id")))]
                        if "corrupt" in params[1]
                        else []
                    )
                )
            return _PgResult([])

    def transaction(self, operation):
        conn = Connection()
        transactions.append(conn)
        return operation(conn)

    monkeypatch.setattr(PostgresStore, "_run_transaction", transaction)
    store = PostgresStore.__new__(PostgresStore)
    if bulk:
        assert store.delete_keys("ws-owner", ["own", "foreign", "missing", "own", "corrupt"]) == {
            "own": True,
            "foreign": False,
            "missing": False,
            "corrupt": False,
        }
    else:
        assert store.delete_key("own") is True
    assert len(transactions) == 1
    calls = transactions[0].calls
    assert len(calls) == 5
    assert calls[1:] == [
        ("DELETE FROM tr_entities WHERE kind = %s AND id = ANY(%s)", ("api_key", ["own"])),
        (
            "DELETE FROM tr_entities WHERE kind = %s AND id = ANY(%s)",
            ("api_key_lookup", ["lookup-own"]),
        ),
        (
            "DELETE FROM tr_entities WHERE kind = %s AND id = ANY(%s)",
            ("api_key_by_workspace", ["ws-owner#own"]),
        ),
        (
            "DELETE FROM tr_key_limit WHERE workspace_id = %s AND key_hash = ANY(%s)",
            ("ws-owner", ["own"]),
        ),
    ]


def test_postgres_bulk_batches_are_bounded(monkeypatch):
    batches = []

    def batch(self, hashes, workspace_id):
        assert workspace_id == "ws-owner"
        batches.append(hashes)
        return dict.fromkeys(hashes, False)

    monkeypatch.setattr(PostgresStore, "_delete_keys_batch", batch)
    store = PostgresStore.__new__(PostgresStore)
    hashes = [f"key-{index:04}" for index in range(1000)]
    assert store.delete_keys("ws-owner", [*hashes, hashes[0]]) == dict.fromkeys(hashes, False)
    assert list(map(len, batches)) == [100] * 10
    assert [key for batch in batches for key in batch] == hashes


def test_spanner_bulk_delete_rejects_noncanonical_key_body():
    store, db = make_fake_store()
    raw, key = _key(store)
    store._write_entity("api_key", "corrupt", replace(key, hash="wrong-id"))
    before = dict(db.rows)
    assert store.delete_keys(key.workspace_id, ["corrupt"]) == {"corrupt": False}
    assert db.rows == before
    assert store.get_key_by_raw(raw) is not None


@pytest.mark.parametrize("bulk", [False, True])
def test_postgres_delete_rolls_back_all_indexes_on_failure(monkeypatch, bulk):
    conn = sqlite_postgres_conn()
    execute = conn.execute

    def array_membership(sql, params=(), **kwargs):
        # The offline Postgres harness executes real transactions in SQLite.
        # Translate bound text-array membership; keep the deletion SQL and
        # all transaction boundaries, including the injected failure, intact.
        sql = sql.replace("= ANY(%s)", "IN (SELECT value FROM json_each(%s))")
        params = tuple(json.dumps(value) if isinstance(value, list) else value for value in params)
        return execute(sql, params, **kwargs)

    monkeypatch.setattr(conn, "execute", array_membership)
    store = postgres_store_on(conn)
    raw, key = store.create_api_key(
        workspace_id="ws-owner",
        name="atomic",
        creator_user_id=None,
    )
    entities = [
        ("api_key", key.hash),
        ("api_key_lookup", key.lookup_hash),
        ("api_key_by_workspace", f"ws-owner#{key.hash}"),
    ]
    conn.fail_on = "DELETE FROM tr_key_limit"
    with pytest.raises(RuntimeError, match="connection reset mid-transaction"):
        if bulk:
            store.delete_keys("ws-owner", [key.hash])
        else:
            store.delete_key(key.hash)
    conn.fail_on = None
    assert all(conn.has_entity(*entity) for entity in entities)
    assert store.get_key_by_raw(raw) is not None
    assert len(store.list_api_keys_with_usage("ws-owner")) == 1
    if bulk:
        assert store.delete_keys("ws-owner", [key.hash]) == {key.hash: True}
    else:
        assert store.delete_key(key.hash) is True
    assert not any(conn.has_entity(*entity) for entity in entities)
    assert store.get_key_by_raw(raw) is None
    assert store.list_api_keys_with_usage("ws-owner") == []
    assert conn.execute("SELECT count(*) FROM tr_key_limit").fetchone()[0] == 0
