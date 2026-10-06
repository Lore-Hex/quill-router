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
    retained = {"multi_use": True, "transaction_id": b"transaction"}
    try:
        with emulator_sdk_shim():
            for bounds in ({}, {"exact_staleness": timedelta(seconds=30)}):
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


@pytest.mark.parametrize("options", ["conflicting-bounds", "multi-use-max"])
def test_sdk_snapshot_original_invalid_options_still_raise(options):
    # Adapted from review2_probes.py: use the real SDK, mocking only acquisition.
    from datetime import timedelta
    from unittest.mock import Mock

    kwargs = {"max_staleness": timedelta(seconds=5)}
    kwargs.update({"exact_staleness": timedelta(seconds=5)} if options == "conflicting-bounds"
                  else {"multi_use": True})
    database = Mock()
    for shim in (False, True, False):
        from contextlib import nullcontext

        with emulator_sdk_shim() if shim else nullcontext():
            with pytest.raises(ValueError), Database.snapshot(database, **kwargs):
                pytest.fail("invalid original options reached a snapshot")


@pytest.mark.parametrize("bound", ["exact_staleness", "max_staleness"])
def test_sdk_valid_staleness_becomes_strong_only_inside_shim(bound):
    from datetime import timedelta
    from unittest.mock import Mock

    database = Mock()
    options = {bound: timedelta(seconds=5)}
    with Database.snapshot(database, **options) as snapshot:
        assert not snapshot._strong
    with emulator_sdk_shim(), Database.snapshot(database, **options) as snapshot:
        assert snapshot._strong
        assert snapshot._exact_staleness is None and snapshot._max_staleness is None
    with Database.snapshot(database, **options) as snapshot:
        assert not snapshot._strong


@pytest.mark.parametrize(("method", "args", "kwargs", "seconds", "multi_use"), [
    ("earnings_summary", ("user-display",), {"allow_stale": True}, 5, False),
    ("list_credit_movements", ("user:user-display",), {}, 30, False),
    ("custom_model_earnings_by_model", ("user-display",), {"since": "2026-01-01T00:00:00Z"}, 60, False),
    ("get_lifetime_topup_microdollars", ("user-display",), {"allow_stale": True}, 5, False),
    ("typed_key_usage", ("key-display",), {"allow_stale": True}, 5, True),
])
def test_production_staleness_callers_remain_valid(method, args, kwargs, seconds, multi_use):
    from datetime import timedelta
    from unittest.mock import Mock

    from tests.fakes.spanner import make_fake_store

    store, fake = make_fake_store()
    getattr(store, method)(*args, **kwargs)
    expected = {"exact_staleness": timedelta(seconds=seconds)}
    if multi_use:
        expected["multi_use"] = True
    assert fake.snapshot_calls == [expected]
    database = Mock()
    with Database.snapshot(database, **expected) as snapshot:
        assert snapshot._exact_staleness == expected["exact_staleness"]
        assert snapshot._multi_use == multi_use
    with emulator_sdk_shim(), Database.snapshot(database, **expected) as snapshot:
        assert snapshot._strong and snapshot._multi_use == multi_use


_QUOTED_HINTS = [
    prefix + delimiter + "@{FORCE_INDEX=tr_receipt_key_versions}" + delimiter
    for prefix in ("", "r", "R", "b", "B", "rb", "rB", "br", "BR")
    for delimiter in ("'", '"', "'''", '"""')
] + [
    "`@{FORCE_INDEX=tr_receipt_key_versions}`",
    "-- @{FORCE_INDEX=tr_receipt_key_versions}\n",
    "# @{FORCE_INDEX=tr_receipt_key_versions}\n",
    "/* @{FORCE_INDEX=tr_receipt_key_versions} */",
    r"'escaped\' @{FORCE_INDEX=tr_receipt_key_versions}'",
    '"escaped\\" @{FORCE_INDEX=tr_receipt_key_versions}"',
    "'''one ' two ''\n@{FORCE_INDEX=tr_receipt_key_versions}'''",
    '"""one " two ""\n@{FORCE_INDEX=tr_receipt_key_versions}"""',
]


@pytest.mark.parametrize("quoted", _QUOTED_HINTS)
def test_sql_quoted_hints_and_comments_are_byte_identical(quoted):
    from tests.conformance.spanner_emulator import names_null_filtered_index

    sql = "SELECT " + quoted + " AS literal_value"
    assert emulator_sql(sql) == sql
    assert not names_null_filtered_index(sql)
    real = "SELECT * FROM t@{FORCE_INDEX=tr_receipt_key_versions}"  # noqa: S608
    mixed = sql + "\nUNION ALL " + real
    assert names_null_filtered_index(mixed)
    assert emulator_sql(mixed) == sql + "\nUNION ALL " + real[:-1] + ", " + NULL_FILTERED_HINT + "}"


@pytest.mark.parametrize("quoted", _QUOTED_HINTS)
def test_acceptance_hint_guard_ignores_quoted_references(monkeypatch, quoted):
    from tests.conformance import test_spanner_sql_acceptance as acceptance

    # Include one actual hint so the inventory nonempty assertion remains useful.
    cases = [acceptance.SQLCase("quoted", [("SELECT " + quoted, {}, {})]),
             acceptance.SQLCase("real", [("SELECT * FROM t@{FORCE_INDEX=tr_receipt_key_versions}", {}, {})])]  # noqa: S608
    monkeypatch.setattr(acceptance, "all_cases", lambda: cases)
    acceptance.test_null_filtered_hint_set_matches_registered_statements()


def test_hint_option_in_comment_is_not_rewritten():
    comment = "/* spanner_emulator.disable_query_null_filtered_index_check=false */"
    sql = "SELECT * FROM t@{FORCE_INDEX=tr_receipt_key_versions " + comment + "}"  # noqa: S608
    assert emulator_sql(sql) == sql[:-1] + ", " + NULL_FILTERED_HINT + "}"


@pytest.mark.parametrize("bound", ["exact_staleness", "max_staleness"])
def test_sdk_snapshot_invalid_duration_serialization_still_raises(bound):
    from unittest.mock import Mock

    from google.cloud.spanner_v1.snapshot import Snapshot

    # 3.69.1 accepts ints at construction, then rejects them at use time.
    original = Snapshot(session=None, **{bound: 5})
    with pytest.raises(AttributeError):
        original._build_transaction_selector_pb()
    with emulator_sdk_shim(), pytest.raises(AttributeError):
        with Database.snapshot(Mock(), **{bound: 5}) as snapshot:
            snapshot._build_transaction_selector_pb()


@pytest.mark.parametrize("sql", ["SELECT 'unclosed", 'SELECT "unclosed',
                                 "SELECT '''unclosed", "SELECT `unclosed", "SELECT /* unclosed"],
                         ids=["single-quote", "double-quote", "triple-quote", "backtick", "block-comment"])
def test_unterminated_sql_regions_fail_closed(sql):
    from tests.conformance.spanner_emulator import names_null_filtered_index, sql_code

    for check in (sql_code, names_null_filtered_index, emulator_sql):
        with pytest.raises(ValueError, match="unterminated SQL"):
            check(sql)


@pytest.mark.parametrize("option", ["", ", spanner_emulator.disable_query_null_filtered_index_check=false"])
def test_backtick_force_index_detection_rewrite_and_guard(monkeypatch, option):
    from tests.conformance import test_spanner_sql_acceptance as acceptance
    from tests.conformance.spanner_emulator import names_null_filtered_index

    sql = "SELECT * FROM t@{FORCE_INDEX=`tr_receipt_key_versions`" + option + "}"  # noqa: S608
    assert names_null_filtered_index(sql)
    rewritten = emulator_sql(sql)
    assert "FORCE_INDEX=`tr_receipt_key_versions`" in rewritten
    assert NULL_FILTERED_HINT in rewritten
    assert emulator_sql(rewritten) == rewritten
    monkeypatch.setattr(acceptance, "all_cases", lambda: [acceptance.SQLCase("backtick", [(sql, {}, {})])])
    acceptance.test_null_filtered_hint_set_matches_registered_statements()
    # Prove that the independent acceptance guard rejects a missing adaptation.
    monkeypatch.setattr(acceptance, "emulator_sql", lambda statement: statement)
    with pytest.raises(AssertionError):
        acceptance.test_null_filtered_hint_set_matches_registered_statements()
