"""Opt-in native GoogleSQL validation; never connects to a cloud database."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from google.api_core.exceptions import AlreadyExists
from google.auth.credentials import AnonymousCredentials
from google.cloud import spanner
from google.cloud.spanner_v1 import param_types

from trusted_router.spend_windows import KeyWindowLimitExceeded, utcnow
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
