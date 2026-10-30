"""The read timestamp of the window check's snapshot, on the internal authorize
route's window refusal (docs/design/fast-admission-e2-key-windows.md section
8.2 item 3).

The fast-path edge's decide shadow re-reads tr_key_limit at exactly the
instant Python's window check read it, so the header must name the timestamp
of the snapshot whose rows the refusal summed: never a fresh clock reading,
never rounded to the microsecond, only on the internal authorize route, and
absent where no single snapshot was read.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router import storage_gcp_authorize
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.spend_windows import (
    KEY_WINDOW_READ_TIMESTAMP_HEADER,
    decide_key_window_limits,
    key_window_read_timestamp_headers,
    spend_window_headers,
    utcnow,
    window_floors,
)
from trusted_router.storage import STORE, configure_store
from trusted_router.storage_gcp_authorize import (
    key_window_limit_decision,
    snapshot_read_timestamp_ns,
)

KEY_LIMIT_SQL = "SELECT shard, day_usage, day_start, week_usage, week_start"


def _rfc3339(stamp) -> str:
    """A client read timestamp written out independently of the code under test."""
    return f"{stamp:%Y-%m-%dT%H:%M:%S}.{stamp.nanosecond:09d}Z"


def _ns(stamp) -> int:
    pb = stamp.timestamp_pb()
    return pb.seconds * 1_000_000_000 + pb.nanos


def _windowed_store(*, used: int, budget_strict: bool = False):
    store, db = make_fake_store()
    workspace = store.create_workspace("owner", "windows", trial_credit_microdollars=5_000_000)
    _, key = store.create_api_key(
        workspace_id=workspace.id,
        creator_user_id="owner",
        name="windowed",
        limit_daily_microdollars=1_000_000,
        budget_strict=budget_strict,
    )
    row = db.typed["tr_key_limit"][(key.hash, 0)]
    row["day_usage"] = used
    row["day_start"] = window_floors(utcnow())["daily"]
    configure_store(store)
    client = TestClient(
        create_app(Settings(environment="test"), configure_store_arg=False, init_observability=False)
    )
    return client, db, key


def _authorize(client: TestClient, key_hash: str):
    return client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "anthropic/claude-opus-4.7",
            "estimated_input_tokens": 10,
            "max_output_tokens": 10,
        },
    )


def test_the_window_check_carries_its_own_snapshots_read_timestamp() -> None:
    store, db = make_fake_store()
    day = window_floors(utcnow())["daily"]
    db.typed.setdefault("tr_key_limit", {})[("kh", 0)] = {
        "key_hash": "kh", "shard": 0, "day_usage": 900, "day_start": day,
        "week_usage": 0, "week_start": None, "month_usage": 0, "month_start": None,
    }
    # Another key's check first (no rows), so the check's snapshot is not
    # simply the first one.
    assert key_window_limit_decision(
        store._database, store._param_types, key_hash="other", estimate=200,
        window_limits={"daily": 1_000},
    ) is None
    decision = key_window_limit_decision(
        store._database, store._param_types, key_hash="kh", estimate=200,
        window_limits={"daily": 1_000},
    )
    assert decision is not None and not decision.allowed and decision.remaining == 100
    assert len(db.snapshot_read_timestamps) == 2
    assert db.snapshot_sql[-1].startswith(KEY_LIMIT_SQL)
    used = db.snapshot_read_timestamps[-1]
    assert decision.read_timestamp_ns == _ns(used)
    assert decision.read_timestamp_ns % 1_000 != 0  # the digits below the microsecond survive
    assert key_window_read_timestamp_headers(decision) == {KEY_WINDOW_READ_TIMESTAMP_HEADER: _rfc3339(used)}


def test_the_internal_window_refusal_names_the_read_timestamp_it_evaluated() -> None:
    client, db, key = _windowed_store(used=1_000_000)
    seen = []
    real = storage_gcp_authorize.key_window_limit_decision

    def spy(*args, **kwargs):
        before = len(db.snapshot_read_timestamps)
        decision = real(*args, **kwargs)
        seen.append((decision, db.snapshot_read_timestamps[before:]))
        return decision

    with mock.patch.object(storage_gcp_authorize, "key_window_limit_decision", spy):
        refused = _authorize(client, key.hash)
    assert refused.status_code == 429, refused.text
    assert refused.json()["error"]["type"] == "key_window_limit_exceeded"
    assert refused.headers["RateLimit-Remaining"] == "0"
    [(decision, began)] = seen
    # The check's one snapshot, and the header is exactly its read timestamp.
    assert len(began) == 1
    assert decision.read_timestamp_ns == _ns(began[0])
    assert refused.headers[KEY_WINDOW_READ_TIMESTAMP_HEADER] == _rfc3339(began[0])


def test_an_admitted_authorize_and_the_strict_path_carry_no_read_timestamp() -> None:
    client, _db, key = _windowed_store(used=0)
    admitted = _authorize(client, key.hash)
    assert admitted.status_code == 200, admitted.text
    assert "RateLimit-Remaining" in admitted.headers
    assert KEY_WINDOW_READ_TIMESTAMP_HEADER not in admitted.headers

    # The strict path decides inside its read-write transaction: no single
    # snapshot timestamp, so no header on its refusal.
    client, db, key = _windowed_store(used=0, budget_strict=True)
    db.typed["tr_key_limit"][(key.hash, 0)]["reserved"] = 1_000_000
    refused = _authorize(client, key.hash)
    assert refused.status_code == 429, refused.text
    assert refused.headers["RateLimit-Remaining"] == "0"
    assert KEY_WINDOW_READ_TIMESTAMP_HEADER not in refused.headers


def test_customer_routes_never_carry_the_read_timestamp(client: TestClient) -> None:
    user = STORE.ensure_user("read-timestamp@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.credit_workspace_once(workspace.id, 5_000_000, "read-timestamp-test")
    raw_key, key = STORE.create_api_key(
        workspace_id=workspace.id, name="limited", creator_user_id=user.id,
        limit_daily_microdollars=1_000_000,
    )
    STORE.api_keys.add_usage(key.hash, 1_000_000, is_byok=False)
    rejected = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"model": "meta-llama/llama-3.1-8b-instruct",
              "messages": [{"role": "user", "content": "Say pong."}], "max_tokens": 1_000},
    )
    assert rejected.status_code == 429, rejected.text
    assert "RateLimit-Remaining" in rejected.headers
    assert KEY_WINDOW_READ_TIMESTAMP_HEADER not in rejected.headers
    # The customer-facing serializer, which the response middleware and the
    # public routes use, never writes it, even for a decision that has one.
    decision = decide_key_window_limits(
        {"daily": 1_000}, {"daily": 1_000}, 1, now=dt.datetime(2026, 10, 30, 12, tzinfo=dt.UTC)
    )
    assert decision is not None
    stamped = storage_gcp_authorize.replace(decision, read_timestamp_ns=1)
    assert KEY_WINDOW_READ_TIMESTAMP_HEADER not in spend_window_headers(stamped, retry_after=True)
    assert stamped == decision  # metadata only


def test_the_header_is_rfc3339_with_nanoseconds() -> None:
    decision = decide_key_window_limits(
        {"daily": 1_000}, {"daily": 0}, 1, now=dt.datetime(2026, 10, 30, 12, tzinfo=dt.UTC)
    )
    assert decision is not None
    assert key_window_read_timestamp_headers(decision) == {}
    stamped = storage_gcp_authorize.replace(decision, read_timestamp_ns=1_792_000_000_000_000_007)
    assert key_window_read_timestamp_headers(stamped) == {
        KEY_WINDOW_READ_TIMESTAMP_HEADER: "2026-10-14T17:46:40.000000007Z"
    }


def test_the_real_client_snapshot_keeps_the_read_timestamp_where_we_read_it() -> None:
    """Against google-cloud-spanner's own Snapshot, with no network: the read
    timestamp the first result set's transaction carries is what
    snapshot_read_timestamp_ns returns, to the nanosecond, and later result
    sets (no metadata, or a transaction without one) leave it. A client
    version that renames or stops keeping Snapshot._transaction_read_timestamp
    fails here, not silently in production (every candidate would page as
    read_at_none)."""
    from google.cloud.spanner_v1 import PartialResultSet, ResultSetMetadata, Transaction
    from google.cloud.spanner_v1.snapshot import Snapshot
    from google.protobuf.timestamp_pb2 import Timestamp

    snapshot = Snapshot(mock.MagicMock(), multi_use=True)
    assert snapshot_read_timestamp_ns(snapshot) is None  # not begun yet
    first = Transaction(id=b"txn", read_timestamp=Timestamp(seconds=1_792_000_000, nanos=123_456_789))
    snapshot._update_for_result_set_pb(PartialResultSet(metadata=ResultSetMetadata(transaction=first)))
    assert snapshot_read_timestamp_ns(snapshot) == 1_792_000_000_123_456_789
    snapshot._update_for_result_set_pb(PartialResultSet())
    snapshot._update_for_result_set_pb(PartialResultSet(metadata=ResultSetMetadata()))
    snapshot._update_for_result_set_pb(
        PartialResultSet(metadata=ResultSetMetadata(transaction=Transaction(id=b"txn")))
    )
    assert snapshot_read_timestamp_ns(snapshot) == 1_792_000_000_123_456_789
    # An explicit begin records it the same way.
    begun = Snapshot(mock.MagicMock(), multi_use=True)
    begun._update_for_transaction_pb(
        Transaction(id=b"begun", read_timestamp=Timestamp(seconds=1_792_000_001, nanos=7))
    )
    assert snapshot_read_timestamp_ns(begun) == 1_792_000_001_000_000_007


def test_a_snapshot_without_a_read_timestamp_gives_none() -> None:
    assert snapshot_read_timestamp_ns(SimpleNamespace()) is None
    assert snapshot_read_timestamp_ns(mock.MagicMock()) is None
    naive = SimpleNamespace(_transaction_read_timestamp=dt.datetime(2026, 10, 30))
    assert snapshot_read_timestamp_ns(naive) is None
    plain = SimpleNamespace(_transaction_read_timestamp=dt.datetime(2026, 10, 30, 0, 0, 0, 5, tzinfo=dt.UTC))
    assert snapshot_read_timestamp_ns(plain) == 1_793_318_400_000_005_000
