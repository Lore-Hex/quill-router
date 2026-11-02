"""Opt-in native GoogleSQL validation; never connects to a cloud database."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from google.api_core.exceptions import AlreadyExists
from google.auth.credentials import AnonymousCredentials
from google.cloud import spanner
from google.cloud.spanner_v1 import param_types

from trusted_router.spend_windows import KeyWindowLimitExceeded, utcnow
from trusted_router.storage_gcp_authorize import key_window_limit_decision
from trusted_router.storage_gcp_strict_budget import reserve_strict_key


@pytest.fixture
def database(monkeypatch):
    import os

    host = os.environ.get("TR_STRICT_SPANNER_EMULATOR_HOST", "")
    if not host:
        pytest.skip("native Spanner emulator not configured")
    assert host.startswith(("127.0.0.1:", "localhost:")), "local emulator only"
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", host)
    client = spanner.Client(project="tr-strict-tests", credentials=AnonymousCredentials())
    instance = client.instance(
        "strict-tests", configuration_name="projects/tr-strict-tests/instanceConfigs/emulator-config"
    )
    if not instance.exists():
        try:
            instance.create().result(timeout=30)
        except AlreadyExists:
            pass
    columns = [
        "key_hash STRING(128) NOT NULL",
        "shard INT64 NOT NULL",
        "limit_micro INT64",
        "usage INT64 NOT NULL",
        "byok_usage INT64 NOT NULL",
        "reserved INT64 NOT NULL",
        "include_byok BOOL NOT NULL",
    ]
    for prefix in ("day", "week", "month"):
        columns.extend(
            [f"{prefix}_limit_micro INT64", f"{prefix}_usage INT64", f"{prefix}_start TIMESTAMP"]
        )
    database = instance.database(
        "strict-" + uuid4().hex[:12],
        ddl_statements=[
            "CREATE TABLE tr_key_limit (" + ",".join(columns) + ") PRIMARY KEY (key_hash, shard)",
        ],
    )
    database.create().result(timeout=30)
    try:
        yield database
    finally:
        database.drop()


def seed(database, *, prefix="day", held=0, used=0, include_byok=True):
    with database.batch() as batch:
        batch.insert(
            "tr_key_limit",
            columns=(
                "key_hash",
                "shard",
                "usage",
                "byok_usage",
                "reserved",
                "include_byok",
                f"{prefix}_limit_micro",
                f"{prefix}_usage",
                f"{prefix}_start",
            ),
            values=[("key", 0, used, 0, held, include_byok, 100, used, utcnow())],
        )


def reserve(database, amount, *, byok=False):
    return database.run_in_transaction(
        lambda txn: reserve_strict_key(
            txn,
            param_types,
            "key",
            amount,
            is_byok=byok,
            enforce_windows=True,
        )
    )


@pytest.mark.parametrize("prefix", ["day", "week", "month"])
def test_native_strict_windows_include_usage_and_holds(database, prefix):
    seed(database, prefix=prefix, held=30, used=20)
    outcome, decision = reserve(database, 40)
    assert outcome == "accepted"
    assert decision.remaining == 50
    with pytest.raises(KeyWindowLimitExceeded):
        reserve(database, 11)
    with database.snapshot() as snapshot:
        assert (
            list(
                snapshot.execute_sql(
                    "SELECT reserved FROM tr_key_limit WHERE key_hash='key' AND shard=0"
                )
            )[0][0]
            == 70
        )


def test_native_concurrent_strict_reservations(database):
    seed(database)

    def attempt(_):
        try:
            return reserve(database, 60)[0] == "accepted"
        except KeyWindowLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sum(executor.map(attempt, range(4))) == 1


def test_native_byok_exclusion_does_not_reserve(database):
    seed(database, include_byok=False)
    assert reserve(database, 200, byok=True)[0] == "no_hold"
    assert reserve(database, 100)[0] == "accepted"


def test_native_window_check_names_its_snapshots_read_timestamp(database):
    """key_window_limit_decision's read_timestamp_ns is the instant its rows are
    as of: a read pinned to it sees exactly the rows the decision summed, not a
    settle committed after it, which a strong read does see (key windows
    section 8.2 item 3)."""
    from google.protobuf.timestamp_pb2 import Timestamp
    from proto.datetime_helpers import DatetimeWithNanoseconds

    seed(database, used=20)
    before = utcnow()
    decision = key_window_limit_decision(
        database, param_types, key_hash="key", estimate=90, window_limits={"daily": 100}
    )
    assert decision is not None and not decision.allowed and decision.remaining == 80
    assert decision.read_timestamp_ns is not None
    seconds, nanos = divmod(decision.read_timestamp_ns, 1_000_000_000)
    pinned = DatetimeWithNanoseconds.from_timestamp_pb(Timestamp(seconds=seconds, nanos=nanos))
    assert abs((pinned - before).total_seconds()) < 60
    with database.batch() as batch:
        batch.update("tr_key_limit", columns=("key_hash", "shard", "day_usage"), values=[("key", 0, 70)])
    usage = "SELECT day_usage FROM tr_key_limit WHERE key_hash='key' AND shard=0"
    with database.snapshot(read_timestamp=pinned) as snapshot:
        assert [list(row) for row in snapshot.execute_sql(usage)] == [[20]]
    with database.snapshot() as snapshot:
        assert [list(row) for row in snapshot.execute_sql(usage)] == [[70]]
    later = key_window_limit_decision(
        database, param_types, key_hash="key", estimate=90, window_limits={"daily": 100}
    )
    assert later is not None and later.remaining == 30
    assert later.read_timestamp_ns is not None and later.read_timestamp_ns > decision.read_timestamp_ns
