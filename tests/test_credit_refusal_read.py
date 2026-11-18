"""X-TR-Credit-Refusal-Read on the internal authorize route's credit 402
(trusted_router/credit_refusal_read.py).

The fast-path edge's decide shadow reads a workspace's credit rows after
Python refused a request for credit; a hold released between Python's read and
the shadow's makes the shadow's read cover the hold. So the 402 names the read
it stands on: on each path to it, the last read of the credit rows after the
last authorize transaction, with the read timestamp of exactly that snapshot
(never another snapshot's, never a clock reading) or none for the rebalance's
locked read; and nothing on any other answer, or when the last transaction's
refusal was followed by no read.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.fakes import spanner as fake_spanner
from tests.test_gateway_authorize_spanner_operations import (
    _lookup_body,
    _request,
    _seed_typed_gateway_store,
    fixed_operation_catalog,  # noqa: F401 - pytest fixture
)
from trusted_router import storage_gcp_credit_rebalance as rebalance_mod
from trusted_router.config import Settings
from trusted_router.credit_refusal_read import (
    CREDIT_REFUSAL_READ_HEADER,
    CreditRefusalRead,
    credit_refusal_read_headers,
)
from trusted_router.main import create_app
from trusted_router.routes.internal import gateway
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount, Workspace

WS = "ws-refusal-read"
# The fixture catalog prices _body's hold at 82,960: 42,000 prompt tokens at 1
# per million and 8,192 output tokens at 5 per million.
HOLD = 82_960

HEAL_SQL = "SELECT total_credits - total_usage - reserved, COALESCE(in_debt, FALSE) FROM tr_credit_balance"
REMEMBERED_SQL = "SELECT shard, total_credits, total_usage, reserved, COALESCE(in_debt, FALSE) FROM tr_credit_balance"
HEADROOM_SQL = "SELECT shard, total_credits, total_usage, reserved FROM tr_credit_balance"


def _rfc3339(stamp: Any) -> str:
    """A fake read timestamp written out independently of the code under test."""
    return f"{stamp:%Y-%m-%dT%H:%M:%S}.{stamp.nanosecond:09d}Z"


@pytest.fixture
def snapshot_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Every snapshot read, its statement and its snapshot's read timestamp."""
    reads: list[tuple[str, Any]] = []
    original = fake_spanner._FakeSnapshot.execute_sql

    def recording(self: Any, sql: str, **kwargs: Any) -> Any:
        rows = original(self, sql, **kwargs)
        reads.append((sql, self._transaction_read_timestamp))
        return rows

    monkeypatch.setattr(fake_spanner._FakeSnapshot, "execute_sql", recording)
    return reads


def _stamp(reads: list[tuple[str, Any]], prefix: str) -> Any:
    [stamp] = [stamp for sql, stamp in reads if sql.startswith(prefix)]
    assert stamp is not None, f"no read timestamp kept for {prefix!r}: the snapshot must be multi-use"
    return stamp


def _store(
    monkeypatch: pytest.MonkeyPatch, credits: list[int], *, marked: bool = False,
) -> tuple[Any, Any, Any]:
    """A typed store whose workspace has one credit row per amount, unused,
    tried in shard order."""
    store, database, key = _seed_typed_gateway_store(workspace_id=WS)
    store._write_entity("credit", WS, CreditAccount(workspace_id=WS, shard_count=len(credits)))
    table = database.typed[CREDIT_BALANCE_TABLE]
    table.pop((WS, 0))
    for shard, total in enumerate(credits):
        table[(WS, shard)] = {
            "workspace_id": WS, "shard": shard, "total_credits": total, "total_usage": 0, "reserved": 0,
            "in_debt": marked, "source_updated_at": None, "updated_at": None,
        }
    shards = tuple(range(len(credits)))
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: shards)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: shards)
    monkeypatch.setattr(gateway, "record_free_credit_exhausted_safely", lambda _ws: None)
    return store, database, key


def _body(key: Any) -> Any:
    body = _lookup_body(key, idempotency_key=None)
    body.estimated_input_tokens = 42_000
    body.max_output_tokens = 8192
    return body


def _refused(key: Any) -> HTTPException:
    with pytest.raises(HTTPException) as raised:
        gateway._authorize_gateway_sync(_request(), _body(key), Settings(environment="test"))
    return raised.value


def _read_header(error: HTTPException) -> str | None:
    assert error.status_code == 402, error.detail
    assert error.detail["error"]["type"] == "insufficient_credits"
    return (error.headers or {}).get(CREDIT_REFUSAL_READ_HEADER)


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_a_one_row_refusal_names_the_snapshot_that_followed_its_transaction(
    monkeypatch: pytest.MonkeyPatch, snapshot_reads: list[tuple[str, Any]],
) -> None:
    # The one row's conditional UPDATE reads nothing; the debt-mark check's
    # snapshot of every row, right after it, is what the refusal stands on.
    _, _, key = _store(monkeypatch, [50_000])

    header = _read_header(_refused(key))

    stamp = _stamp(snapshot_reads, HEAL_SQL)
    assert header == f"read=heal read_at={_rfc3339(stamp)} hold={HOLD} rows=1 sum=50000 most=50000"


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_a_remembered_workspace_names_the_snapshot_it_was_refused_on(
    monkeypatch: pytest.MonkeyPatch, snapshot_reads: list[tuple[str, Any]],
) -> None:
    _, database, key = _store(monkeypatch, [50_000])
    _refused(key)
    database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]["reserved"] = 7_000
    snapshot_reads.clear()

    header = _read_header(_refused(key))

    # The process remembered the workspace: one lock-free snapshot, no
    # transaction, and that snapshot is named.
    assert [sql for sql, _ in snapshot_reads if "tr_credit_balance" in sql] == [
        sql for sql, _ in snapshot_reads if sql.startswith(REMEMBERED_SQL)
    ]
    stamp = _stamp(snapshot_reads, REMEMBERED_SQL)
    assert header == f"read=remembered read_at={_rfc3339(stamp)} hold={HOLD} rows=1 sum=43000 most=43000"


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_several_rows_proven_exhausted_by_the_headroom_snapshot_name_it(
    monkeypatch: pytest.MonkeyPatch, snapshot_reads: list[tuple[str, Any]],
) -> None:
    _, _, key = _store(monkeypatch, [30_000, 30_000, 20_000])

    header = _read_header(_refused(key))

    # The debt-mark snapshot came first; the precheck's proves exhaustion,
    # and it is the one named.
    heal, headroom = _stamp(snapshot_reads, HEAL_SQL), _stamp(snapshot_reads, HEADROOM_SQL)
    assert heal != headroom
    assert header == f"read=headroom read_at={_rfc3339(headroom)} hold={HOLD} rows=3 sum=80000 most=30000"


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_several_rows_proven_exhausted_by_the_rebalance_name_its_locked_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, database, key = _store(monkeypatch, [50_000, 50_000])
    original = rebalance_mod.credit_headroom_precheck

    def peer_after_precheck(*args: Any, **kwargs: Any) -> Any:
        # The precheck sees the sum cover the hold, spread over two rows; a
        # peer's hold lands before the rebalance's locked read.
        verdict = original(*args, **kwargs)
        assert verdict.outcome == rebalance_mod.RebalanceOutcome.MOVED
        database.typed[CREDIT_BALANCE_TABLE][(WS, 1)]["reserved"] = 30_000
        return verdict

    monkeypatch.setattr(rebalance_mod, "credit_headroom_precheck", peer_after_precheck)

    header = _read_header(_refused(key))

    # A read inside a read-write transaction has no read timestamp.
    assert header == f"read=rebalance hold={HOLD} rows=2 sum=70000 most=50000"


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_a_refusal_whose_transaction_no_read_followed_names_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stale mark (signed sum not negative) is healed and the authorize runs
    # again: the snapshot before it does not speak for its refusal, and no
    # read follows it on one row.
    _, database, key = _store(monkeypatch, [50_000], marked=True)

    assert _read_header(_refused(key)) is None
    assert not database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]["in_debt"]


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_a_402_of_another_type_names_no_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # The key's own limit, below the hold: the key's 402, which names no read.
    store, _database, _key = _store(monkeypatch, [500_000])
    _raw, capped = store.api_keys.create(
        workspace_id=WS, name="capped", creator_user_id="user-rpc", limit_microdollars=1_000,
    )

    error = _refused(capped)

    assert (error.status_code, error.detail["error"]["type"]) == (402, "key_limit_exceeded")
    assert not error.headers


def test_a_federated_workspaces_refusal_names_no_read() -> None:
    read = CreditRefusalRead.of("remembered", HOLD, [5], 1)
    federated = Workspace(id=WS, name="Federated", owner_user_id="owner", federated_home="home")
    error = gateway._insufficient_credits_error(
        federated, reservation=HOLD, estimate=HOLD, body=_lookup_body(_FakeKey()), input_tokens=1,
        key_id="key", request_id=None, credit_read=read,
    )
    assert isinstance(error, HTTPException)
    assert error.detail["error"]["type"] == "credits_not_on_this_plane"
    assert not error.headers


class _FakeKey:
    hash = "kh"
    lookup_hash = "lookup"


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_the_http_answer_carries_the_header_and_an_admitted_one_does_not(
    monkeypatch: pytest.MonkeyPatch, snapshot_reads: list[tuple[str, Any]],
) -> None:
    _, database, key = _store(monkeypatch, [50_000])
    client = TestClient(
        create_app(Settings(environment="test"), configure_store_arg=False, init_observability=False)
    )
    payload = {
        "api_key_lookup_hash": key.lookup_hash, "model": "anthropic/claude-haiku-4.5",
        "estimated_input_tokens": 42_000, "max_output_tokens": 8192,
    }

    refused = client.post("/v1/internal/gateway/authorize", json=payload)

    assert refused.status_code == 402, refused.text
    stamp = _stamp(snapshot_reads, HEAL_SQL)
    assert refused.headers[CREDIT_REFUSAL_READ_HEADER] == (
        f"read=heal read_at={_rfc3339(stamp)} hold={HOLD} rows=1 sum=50000 most=50000"
    )
    database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]["total_credits"] = 500_000
    admitted = client.post("/v1/internal/gateway/authorize", json=payload)
    assert admitted.status_code == 200, admitted.text
    assert CREDIT_REFUSAL_READ_HEADER not in admitted.headers


def test_the_header_is_the_read_in_one_fixed_form() -> None:
    assert credit_refusal_read_headers(None) == {}
    assert CreditRefusalRead.of("heal", 1, []) is None
    snapshot = CreditRefusalRead.of("remembered", 82_074, [9_000, -1, 8_000], 1_792_000_000_000_000_007)
    assert credit_refusal_read_headers(snapshot) == {
        CREDIT_REFUSAL_READ_HEADER: (
            "read=remembered read_at=2026-10-14T17:46:40.000000007Z hold=82074 rows=3 sum=16999 most=9000"
        )
    }
    locked = CreditRefusalRead("rebalance", 82_074, 2, -40, 10)
    assert credit_refusal_read_headers(locked) == {
        CREDIT_REFUSAL_READ_HEADER: "read=rebalance hold=82074 rows=2 sum=-40 most=10"
    }
    # Never a snapshot the shadow cannot re-read, a locked read with an
    # instant it does not have, or a read of no known name: no header.
    for unnamed in (
        CreditRefusalRead("remembered", 1, 1, 0, 0),
        CreditRefusalRead("heal", 1, 1, 0, 0),
        CreditRefusalRead("headroom", 1, 1, 0, 0),
        CreditRefusalRead("rebalance", 1, 1, 0, 0, 1_792_000_000_000_000_007),
        CreditRefusalRead("lease", 1, 1, 0, 0, 1_792_000_000_000_000_007),
        CreditRefusalRead("lease", 1, 1, 0, 0),
    ):
        assert credit_refusal_read_headers(unnamed) == {}, unnamed


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_a_snapshot_with_no_read_timestamp_names_no_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # The client keeps a multi-use snapshot's read timestamp in a private
    # field; should it stop, every path names no read rather than figures
    # the shadow cannot re-read. The rebalance's locked read never had one.
    from trusted_router import storage_gcp_authorize

    monkeypatch.setattr(storage_gcp_authorize, "snapshot_read_timestamp_ns", lambda _snapshot: None)
    _, _, key = _store(monkeypatch, [50_000])
    assert _read_header(_refused(key)) is None  # heal
    assert _read_header(_refused(key)) is None  # remembered
    _, _, key = _store(monkeypatch, [30_000, 30_000, 20_000])
    assert _read_header(_refused(key)) is None  # headroom


def test_the_snapshots_a_402_names_are_multi_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake keeps no read timestamp for a single-use snapshot, as
    production code must not rely on one: each snapshot a 402 names is
    multi-use, or the route tests above lose their header."""
    store, database, _key = _store(monkeypatch, [30_000, 30_000, 20_000])
    database.snapshot_calls.clear()
    store._heal_stale_debt_mark(WS, on_read=lambda _rows, _at: None)
    rebalance_mod.credit_headroom_precheck(
        store._database, store._param_types, workspace_id=WS, shard_count=3, shard_candidates=(0, 1, 2),
        estimate=HOLD,
    )
    assert database.snapshot_calls == [{"multi_use": True}, {"multi_use": True}]


def test_a_raising_read_note_never_stops_the_heal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    store, database, _key = _store(monkeypatch, [50_000], marked=True)

    def note(_rows: list[int], _at: int | None) -> None:
        raise RuntimeError("synthetic note failure")

    with caplog.at_level(logging.WARNING):
        assert store._heal_stale_debt_mark(WS, on_read=note) is True
    assert not database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]["in_debt"]
    assert "credit refusal read not noted; the heal goes on" in caplog.text


def test_the_go_vectors_are_what_python_writes_now() -> None:
    """The edge's parser (fastpath/internal/edge/funding.go) is held to
    fastpath/testdata/credit_refusal_read_vectors_v1.json; this holds the file
    to credit_refusal_read_headers."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "fastpath" / "credit_refusal_read_vectors.py"
    spec = importlib.util.spec_from_file_location("credit_refusal_read_vectors", path)
    assert spec and spec.loader
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert generator.OUTPUT.read_text() == generator.render(), (
        "fastpath/testdata/credit_refusal_read_vectors_v1.json is stale: run "
        "`uv run python scripts/fastpath/credit_refusal_read_vectors.py` and check the edge's parser against it"
    )


def test_only_the_internal_authorize_route_writes_the_header() -> None:
    """A guard: the header is built in one place, the internal route's credit
    402. Customer-facing routes and the response middleware never call it."""
    src = Path(__file__).resolve().parents[1] / "src"
    callers = sorted(
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if "credit_refusal_read_headers(" in path.read_text()
    )
    assert callers == ["trusted_router/credit_refusal_read.py", "trusted_router/routes/internal/gateway.py"]
