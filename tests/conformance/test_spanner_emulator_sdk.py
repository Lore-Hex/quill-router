"""Offline SDK-boundary contract; recorder originals never contact a server."""
from __future__ import annotations

import pytest
from google.cloud.spanner_v1.database import BatchSnapshot, Database
from google.cloud.spanner_v1.snapshot import _SnapshotBase
from google.cloud.spanner_v1.transaction import Transaction

from tests.conformance.spanner_emulator import (
    NULL_FILTERED_HINT,
    NULL_FILTERED_INDEXES,
    emulator_sdk_shim,
    emulator_sql,
)

# Independent of the shim's installation list so dropping an entry is caught.
SDK_METHODS = [
    (_SnapshotBase, "execute_sql", "sql"),
    (Transaction, "execute_update", "dml"),
    (Transaction, "batch_update", "statements"),
    (Database, "execute_partitioned_dml", "dml"),
    (BatchSnapshot, "execute_sql", "sql"),
]
SDK_IDS = [f"{cls.__name__}.{method}" for cls, method, _ in SDK_METHODS]


@pytest.mark.parametrize(("cls", "method", "argument"), SDK_METHODS, ids=SDK_IDS)
@pytest.mark.parametrize("style", ["positional", "keyword", "mixed"])
def test_sdk_null_filtered_hint_and_passthrough(monkeypatch, cls, method, argument, style):
    calls = []
    result = object()
    receiver = object()
    params, types, options = {"id": "value"}, {"id": object()}, {"request_tag": "offline"}

    def original(self, *args, **kwargs):
        calls.append((self, args, kwargs))
        return result

    monkeypatch.setattr(cls, method, original)
    # Cover every DDL index, existing hints, and byte-sensitive nonmatches.
    plain = " \nSELECT @id /* preserve whitespace */\t"
    queries = [(plain, plain), ("SELECT tr_receipt_key_versions_suffix", "SELECT tr_receipt_key_versions_suffix")]
    for index in sorted(NULL_FILTERED_INDEXES):
        sql = f"SELECT @id FROM example@{{FORCE_INDEX={index}}}"  # noqa: S608 - synthetic SDK recorder input
        hinted = sql[:-1] + ", " + NULL_FILTERED_HINT + "}"
        queries.extend([(sql, hinted), (hinted, hinted)])

    with emulator_sdk_shim():
        for sql, expected in queries:
            kwargs = {"request_options": options}
            if argument == "statements":
                value = [(sql, params, types), (plain, params, types), sql, plain]
                expected_value = [(expected, params, types), (plain, params, types), expected, plain]
            else:
                value, expected_value = sql, expected
                if style != "positional":
                    kwargs.update(params=params, param_types=types)
            if style == "keyword":
                args = ()
                kwargs[argument] = value
            elif style == "positional":
                args = (value, options) if argument == "statements" else (value, params, types)
                if argument == "statements":
                    kwargs = {"last_statement": True}
            else:
                args = (value,)
            assert getattr(cls, method)(receiver, *args, **kwargs) is result
            actual_self, actual_args, actual_kwargs = calls[-1]
            assert actual_self is receiver
            actual_value = actual_args[0] if args else actual_kwargs[argument]
            assert actual_value == expected_value
            if argument == "statements":
                assert actual_value[0][1] is params and actual_value[0][2] is types
                assert actual_value[1][1] is params and actual_value[1][2] is types
            if args:
                assert all(actual is supplied for actual, supplied in zip(actual_args[1:], args[1:], strict=True))
                assert actual_kwargs == kwargs
            else:
                assert not actual_args
                assert actual_kwargs.keys() == kwargs.keys()
            for key in kwargs.keys() - {argument}:
                assert actual_kwargs[key] is kwargs[key]
    assert getattr(cls, method) is original


def test_batch_snapshot_forwarding_and_transaction_inheritance_hint_once(monkeypatch):
    calls = []

    def original(self, sql, **kwargs):
        calls.append((sql, kwargs))
        return calls

    def forward(self, *args, **kwargs):
        return _SnapshotBase.execute_sql(self, *args, **kwargs)

    monkeypatch.setattr(_SnapshotBase, "execute_sql", original)
    monkeypatch.setattr(BatchSnapshot, "execute_sql", forward)
    sql = "SELECT kid FROM tr_entities@{FORCE_INDEX=tr_receipt_key_versions}"
    params = {"id": "value"}
    with emulator_sdk_shim():
        for cls in (BatchSnapshot, Transaction):
            assert cls.execute_sql(object(), sql=sql, params=params) is calls
            assert calls[-1][0] == sql[:-1] + ", " + NULL_FILTERED_HINT + "}"
            assert calls[-1][1]["params"] is params


@pytest.mark.parametrize("failure", ["skip", "reject"])
def test_resources_require_emulators_before_sdk_changes(monkeypatch, failure):
    from datetime import timedelta
    from unittest.mock import Mock

    from google.cloud import spanner
    from google.cloud.spanner_v1.database_sessions_manager import DatabaseSessionsManager

    from tests.conformance import spanner_emulator

    methods = [*SDK_METHODS, (Database, "snapshot", "kwargs")]
    originals = [getattr(cls, method) for cls, method, _ in methods]
    interval = timedelta(minutes=7)
    monkeypatch.setattr(DatabaseSessionsManager, "_MAINTENANCE_THREAD_POLLING_INTERVAL", interval)
    client = Mock()
    monkeypatch.setattr(spanner, "Client", client)
    error = pytest.skip.Exception if failure == "skip" else AssertionError

    def require():
        assert [getattr(cls, method) for cls, method, _ in methods] == originals
        assert DatabaseSessionsManager._MAINTENANCE_THREAD_POLLING_INTERVAL is interval
        raise error("emulators unavailable")

    monkeypatch.setattr(spanner_emulator, "require_emulators", require)
    with pytest.raises(error, match="emulators unavailable"), spanner_emulator.emulator_resources():
        pytest.fail("must not enter resources")
    client.assert_not_called()
    assert [getattr(cls, method) for cls, method, _ in methods] == originals
    assert DatabaseSessionsManager._MAINTENANCE_THREAD_POLLING_INTERVAL is interval


@pytest.mark.parametrize("verb", ["SELECT * FROM", "UPDATE", "DELETE FROM"])
def test_null_filtered_hint_is_merged_only_into_force_index_blocks(verb):
    index = "tr_receipt_key_versions"
    block = f"@{{ INDEX_STRATEGY = FORCE_INDEX_UNION, FoRcE_InDeX = {index.upper()} }}"
    other = "@{FORCE_INDEX=tr_credit_movement_by_time}"
    sql = f"{verb} tr_entities{block} JOIN tr_entities@{{FORCE_INDEX={index}}} ON TRUE {other}"
    expected = sql.replace(block, block[:-1] + ", " + NULL_FILTERED_HINT + "}").replace(
        f"@{{FORCE_INDEX={index}}}", f"@{{FORCE_INDEX={index}, {NULL_FILTERED_HINT}}}")
    assert emulator_sql(sql) == expected
    assert emulator_sql(expected) == expected
    for plain in (f"SELECT {index}", f"SELECT '{index}'", f"SELECT 1 /* {index} */",
                  f"SELECT * FROM t@{{other={index}}}", f"SELECT * FROM t@{{FORCE_INDEX={index}_suffix}}"):  # noqa: S608 - synthetic recorder input
        assert emulator_sql(plain) == plain
    existing = f"SELECT * FROM t@{{force_index={index}, SPANNER_EMULATOR.disable_query_null_filtered_index_check = TRUE}}"  # noqa: S608 - synthetic recorder input
    assert emulator_sql(existing) == existing
    assert emulator_sql(existing.replace("= TRUE", "= false")) == existing.replace("= TRUE", "= true")


@pytest.mark.parametrize("failure", [False, True], ids=["normal-exit", "exception-exit"])
def test_sdk_snapshot_drops_only_staleness_and_restores(monkeypatch, failure):
    from datetime import timedelta

    calls = []
    receiver, result = object(), object()

    def original(self, **kwargs):
        calls.append((self, kwargs))
        return result

    monkeypatch.setattr(Database, "snapshot", original)
    retained = {"multi_use": True, "read_timestamp": object(), "min_read_timestamp": object(),
                "other_option": object()}
    try:
        with emulator_sdk_shim():
            for bounds in ({}, {"exact_staleness": timedelta(seconds=30)},
                           {"max_staleness": timedelta(seconds=60)},
                           {"exact_staleness": timedelta(seconds=5), "max_staleness": timedelta(seconds=5)}):
                for options in ({}, retained):
                    assert Database.snapshot(receiver, **bounds, **options) is result
                    actual_self, actual_options = calls[-1]
                    assert actual_self is receiver
                    assert actual_options == options
                    assert all(actual_options[key] is value for key, value in options.items())
            if failure:
                raise RuntimeError("body failure")
    except RuntimeError as exc:
        assert failure and str(exc) == "body failure"
    assert Database.snapshot is original
