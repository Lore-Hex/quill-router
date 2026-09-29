"""Offline metadata, safety, and reporting contracts for the scheduled audit."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts import audit_spanner_schema as audit


def records():
    rows = {name: [] for name in audit.PROJECTIONS}

    def add(view, **values):
        rows[view].append({field: values.get(field, "COMMITTED" if field == "SPANNER_STATE" else None) for field in audit.PROJECTIONS[view].split()})

    add("TABLES", TABLE_NAME="t")
    add("COLUMNS", TABLE_NAME="t", COLUMN_NAME="id", ORDINAL_POSITION="1", SPANNER_TYPE="INT64", IS_NULLABLE="NO", IS_GENERATED="NEVER", IS_STORED=None)
    add("COLUMNS", TABLE_NAME="t", COLUMN_NAME="timestamp", ORDINAL_POSITION=2, SPANNER_TYPE="TIMESTAMP", IS_NULLABLE="YES", IS_GENERATED="NEVER")
    add("COLUMN_OPTIONS", TABLE_NAME="t", COLUMN_NAME="timestamp", OPTION_NAME="allow_commit_timestamp", OPTION_TYPE="BOOL", OPTION_VALUE="TRUE")
    add("INDEXES", TABLE_NAME="t", INDEX_NAME="idx", INDEX_TYPE="INDEX", IS_UNIQUE=False, IS_NULL_FILTERED=True, INDEX_STATE="READ_WRITE")
    add("INDEXES", TABLE_NAME="t", INDEX_NAME="PRIMARY_KEY", INDEX_TYPE="PRIMARY_KEY", IS_UNIQUE=True, IS_NULL_FILTERED=False, INDEX_STATE="READ_WRITE")
    for name, pos, order in [("timestamp", None, None), ("id", 1, "ASC")]:
        add("INDEX_COLUMNS", TABLE_NAME="t", INDEX_NAME="idx", COLUMN_NAME=name, ORDINAL_POSITION=pos, COLUMN_ORDERING=order)
    add("INDEX_COLUMNS", TABLE_NAME="t", INDEX_NAME="PRIMARY_KEY", COLUMN_NAME="id", ORDINAL_POSITION=1, COLUMN_ORDERING="ASC")
    for name, kind in [("check", "CHECK"), ("server_pk_123", "PRIMARY KEY"), ("fk", "FOREIGN KEY")]:
        add("TABLE_CONSTRAINTS", TABLE_NAME="t", CONSTRAINT_NAME=name, CONSTRAINT_TYPE=kind, IS_DEFERRABLE="NO", INITIALLY_DEFERRED="NO", ENFORCED="YES")
    add("CHECK_CONSTRAINTS", CONSTRAINT_NAME="check", CHECK_CLAUSE="id > 0")
    add("KEY_COLUMN_USAGE", TABLE_NAME="t", CONSTRAINT_NAME="server_pk_123", COLUMN_NAME="id", ORDINAL_POSITION=1)
    add("KEY_COLUMN_USAGE", TABLE_NAME="t", CONSTRAINT_NAME="fk", COLUMN_NAME="id", ORDINAL_POSITION=1, POSITION_IN_UNIQUE_CONSTRAINT=1)
    add("REFERENTIAL_CONSTRAINTS", CONSTRAINT_NAME="fk", UNIQUE_CONSTRAINT_SCHEMA="", UNIQUE_CONSTRAINT_NAME="server_pk_123", MATCH_OPTION="SIMPLE", UPDATE_RULE="NO ACTION", DELETE_RULE="CASCADE")
    return rows


def test_normalisation_sorts_rows_preserves_expressions_and_foreign_keys():
    rows = records()
    schema = audit.normalise(rows)
    reversed_rows = {name: list(reversed(items)) for name, items in reversed(list(rows.items()))}
    assert audit.normalise(reversed_rows) == schema
    assert list(schema) == sorted(schema)
    assert schema["column/t/id"]["IS_NULLABLE"] is False
    assert schema["column/t/timestamp"]["allow_commit_timestamp"] is True
    assert schema["constraint/t/fk"]["UNIQUE_CONSTRAINT_NAME"] == "constraint/t/PRIMARY_KEY"
    assert schema["constraint/t/fk"]["DELETE_RULE"] == "CASCADE"
    assert schema["constraint/t/fk"]["columns"][0]["POSITION_IN_UNIQUE_CONSTRAINT"] == 1
    assert [c["ORDINAL_POSITION"] for c in schema["index/t/idx"]["columns"]] == [1, None]
    rows["CHECK_CONSTRAINTS"][0]["CHECK_CLAUSE"] = "value = 'a  b'"
    assert audit.normalise(rows)["constraint/t/check"]["CHECK_CLAUSE"] == "value = 'a  b'"


def test_both_directions_dangerous_first():
    prod = audit.normalise(records())
    fixture = copy.deepcopy(prod)
    fixture["column/t/new"] = fixture.pop("column/t/timestamp")
    diffs = audit.differences(prod, fixture)
    assert [(d["object"], d["direction"]) for d in diffs] == [
        ("column/t/new", "FIXTURE-HAS / PRODUCTION-LACKS"),
        ("column/t/timestamp", "PRODUCTION-HAS / FIXTURE-LACKS"),
    ]
    assert diffs[0]["production"] is None
    assert diffs[0]["fixture"] == fixture["column/t/new"]


@pytest.mark.parametrize(("view", "field", "new"), [
    ("TABLES", "PARENT_TABLE_NAME", "parent"),
    ("TABLES", "ON_DELETE_ACTION", "CASCADE"),
    ("TABLES", "ROW_DELETION_POLICY_EXPRESSION", "OLDER_THAN(timestamp, INTERVAL 7 DAY)"),
    ("COLUMNS", "ORDINAL_POSITION", 3),
    ("COLUMNS", "SPANNER_TYPE", "STRING(MAX)"),
    ("COLUMNS", "IS_NULLABLE", "YES"),
    ("COLUMNS", "IS_GENERATED", "ALWAYS"),
    ("COLUMNS", "GENERATION_EXPRESSION", "id + 1"),
    ("COLUMNS", "IS_STORED", "YES"),
    ("COLUMNS", "COLUMN_DEFAULT", "(0)"),
    ("COLUMN_OPTIONS", "OPTION_VALUE", "FALSE"),
    ("INDEXES", "IS_UNIQUE", True),
    ("INDEXES", "IS_NULL_FILTERED", False),
    ("INDEXES", "INDEX_TYPE", "SEARCH"),
    ("INDEXES", "PARENT_TABLE_NAME", "parent"),
    ("INDEX_COLUMNS", "COLUMN_ORDERING", "DESC"),
    ("INDEX_COLUMNS", "ORDINAL_POSITION", 2),
    ("CHECK_CONSTRAINTS", "CHECK_CLAUSE", "id > 1"),
    ("TABLE_CONSTRAINTS", "ENFORCED", "NO"),
    ("REFERENTIAL_CONSTRAINTS", "DELETE_RULE", "NO ACTION"),
])
def test_attribute_mismatch(view, field, new):
    rows = records()
    prod = audit.normalise(rows)
    rows[view][0][field] = new
    diffs = audit.differences(prod, audit.normalise(rows))
    assert len(diffs) == 1
    assert diffs[0]["direction"] == "ATTRIBUTE-MISMATCH"
    assert diffs[0]["production"] != diffs[0]["fixture"]


def allowance(diff):
    return {**{k: v for k, v in diff.items() if k in {"object", "attribute", "production", "fixture"}}, "reason": "Reviewed rolling upgrade", "verified_against_production": False}


def test_allowlist_exact_match_and_changed_values_fail():
    diff = audit.differences({"column/t/id": {"IS_NULLABLE": True}}, {"column/t/id": {"IS_NULLABLE": False}})[0]
    entry = allowance(diff)
    report = audit.apply_allowlist([diff], [entry])
    assert report["exit_code"] == 0
    assert report["unverified"] == 1
    assert report["allowlisted"] == 1
    assert report["differences"][0]["reason"] == entry["reason"]
    report = audit.apply_allowlist([{**diff, "fixture": None}], [entry])
    assert report["exit_code"] == 1
    assert report["unexplained"] == 1
    assert report["stale"] == [entry]


def test_stale_allowlist_fails_even_when_schemas_match():
    entry = allowance(audit.differences({}, {"table/t": {"parent": None}})[0])
    report = audit.apply_allowlist([], [entry])
    assert report["exit_code"] == 1
    assert report["stale"] == [entry]


@pytest.mark.parametrize("entries", [{}, [{"object": "wildcard"}], [dict(object="x", attribute="x", production=1, fixture=2, reason="", verified_against_production=False)]])
def test_invalid_allowlist_is_unauditable(entries):
    with pytest.raises(ValueError):
        audit.apply_allowlist([], entries)


def test_duplicate_allowlist_rejected():
    entry = allowance(audit.differences({}, {"table/t": {}})[0])
    with pytest.raises(ValueError, match="Duplicate"):
        audit.apply_allowlist([], [entry, entry])


def fake_database(rows):
    snapshot = MagicMock()
    by_query = {audit.QUERIES[name]: [[row[f] for f in audit.PROJECTIONS[name].split()] for row in items] for name, items in rows.items()}
    snapshot.execute_sql.side_effect = lambda query, **kw: by_query[query]
    db = MagicMock()
    db.snapshot.return_value.__enter__.return_value = snapshot
    return db, snapshot


def test_reader_one_snapshot_metadata_only():
    rows = records()
    db, snapshot = fake_database(rows)
    assert audit.read_schema(db) == audit.normalise(rows)
    db.snapshot.assert_called_once_with(multi_use=True)
    assert snapshot.execute_sql.call_count == len(audit.QUERIES)
    for query in audit.QUERIES.values():
        assert query.startswith("SELECT ")
        assert " FROM INFORMATION_SCHEMA." in query
        assert "SCHEMA = ''" in query
        snapshot.execute_sql.assert_any_call(query, timeout=60)


@pytest.mark.parametrize("failure", ["missing_view", "missing_field", "empty", "invalid_bool"])
def test_incomplete_metadata_fails_closed(failure):
    rows = records()
    if failure == "missing_view":
        del rows["CHECK_CONSTRAINTS"]
    elif failure == "missing_field":
        del rows["COLUMNS"][0]["COLUMN_DEFAULT"]
    elif failure == "invalid_bool":
        rows["COLUMNS"][0]["IS_NULLABLE"] = "UNKNOWN"
    else:
        rows["TABLES"] = []
    with pytest.raises((ValueError, KeyError)):
        audit.normalise(rows)


@pytest.mark.parametrize("host", ["127.0.0.1:9010", ""])
def test_production_refuses_emulator_environment_before_client_construction(monkeypatch, host):
    from google.cloud import spanner

    client = MagicMock()
    monkeypatch.setattr(spanner, "Client", client)
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", host)
    with pytest.raises(ValueError, match="refuses SPANNER_EMULATOR_HOST"):
        audit.production_schema("p", "i", "d")
    client.assert_not_called()


def test_production_adc_client_is_read_only_and_closes(monkeypatch):
    from google.cloud import spanner

    monkeypatch.delenv("SPANNER_EMULATOR_HOST", raising=False)
    db, _ = fake_database(records())
    instance = SimpleNamespace(database=lambda name: db)
    factory = MagicMock(return_value=SimpleNamespace(instance=lambda name: instance))
    monkeypatch.setattr(spanner, "Client", factory)
    assert audit.production_schema("p", "i", "d") == audit.normalise(records())
    factory.assert_called_once_with(project="p", disable_builtin_metrics=True)
    db.close.assert_called_once()
    db.drop.assert_not_called()
    db.create.assert_not_called()


def test_subprocess_isolates_emulator_setting(monkeypatch):
    monkeypatch.delenv("SPANNER_EMULATOR_HOST", raising=False)
    captured = {}

    def run(command, **kwargs):
        captured.update(kwargs)
        assert "--expected-only" in command
        assert "SPANNER_EMULATOR_HOST" not in audit.os.environ
        return SimpleNamespace(stdout=json.dumps(audit.normalise(records())))

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.fixture_schema("127.0.0.1:9010") == audit.normalise(records())
    assert captured["env"]["SPANNER_EMULATOR_HOST"] == "127.0.0.1:9010"
    assert captured["check"] is True
    assert captured["timeout"] == 900
    assert "SPANNER_EMULATOR_HOST" not in audit.os.environ


@pytest.mark.parametrize("endpoint", ["example.com:9010", "10.0.0.1:9010", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1", "localhost:9010"])
def test_non_loopback_or_invalid_endpoint_rejected(endpoint):
    with pytest.raises(ValueError):
        audit.fixture_schema(endpoint)


def test_expected_reader_refuses_wrong_environment(monkeypatch):
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "10.0.0.1:9010")
    with pytest.raises(ValueError, match="isolated"):
        audit.expected_schema("127.0.0.1:9010")


@pytest.mark.parametrize("mode, code", [("clean", 0), ("drift", 1), ("read_failure", 2), ("fixture_failure", 2), ("bad_allowlist", 2)])
def test_main_exit_codes_json_and_step_summary(monkeypatch, tmp_path, capsys, mode, code):
    prod = audit.normalise(records())
    fixture = copy.deepcopy(prod)
    if mode == "drift":
        fixture["column/t/id"]["SPANNER_TYPE"] = "INT32"

    def read(*args):
        if mode == "read_failure":
            raise RuntimeError("metadata denied")
        return prod

    def expected(*args):
        if mode == "fixture_failure":
            raise RuntimeError("emulator unavailable")
        return fixture

    monkeypatch.setattr(audit, "production_schema", read)
    monkeypatch.setattr(audit, "fixture_schema", expected)
    path = tmp_path / "allow.json"
    path.write_text("invalid" if mode == "bad_allowlist" else "[]")
    summary_path = tmp_path / "summary.md"
    summary_path.write_text("Earlier step\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    assert audit.main(["--json", "--allowlist", str(path)]) == code
    report = json.loads(capsys.readouterr().out)
    assert report["exit_code"] == code
    rendered = summary_path.read_text()
    assert rendered.startswith("Earlier step\n")
    assert "| Status / direction | Object | Attribute | Production | Fixture | Reason |" in rendered
    if code == 2:
        assert "CANNOT AUDIT" in rendered
    else:
        assert report["target"].endswith("/databases/trusted-router")
    if mode == "drift":
        assert "ATTRIBUTE-MISMATCH" in rendered
        assert "SPANNER_TYPE" in rendered


def test_summary_escapes_cells_and_includes_stale():
    diff = audit.differences({}, {"table/t": {"policy": "a|b\n<script>"}})[0]
    stale = {**allowance(diff), "object": "table/stale"}
    report = audit.apply_allowlist([diff], [stale])
    rendered = audit.markdown(report)
    assert "a&#124;b" in rendered
    assert "&lt;script&gt;" in rendered
    assert "STALE" in rendered
    assert "table/stale" in rendered


def test_seed_allowlist_is_explicit_unverified_and_narrow():
    entries = json.loads(audit.DEFAULT_ALLOWLIST.read_text())
    assert entries
    audit.apply_allowlist([], entries)
    assert all(entry["verified_against_production"] is False for entry in entries)
    assert all("*" not in entry["object"] for entry in entries)
    assert {entry["object"] for entry in entries} >= {
        "column/tr_key_limit/day_usage", "column/tr_reservation/credit_shard",
        "column/tr_entities/ephemeral_expires_at",
        "index/tr_gateway_authorization/tr_gateway_authorization_by_gateway_request_id",
        "constraint/tr_trust_event/tr_trust_event_provider_lightning",
    }


def test_allowlist_requires_exact_json_types():
    diff = audit.differences({"column/t/id": {"IS_NULLABLE": True}}, {"column/t/id": {"IS_NULLABLE": False}})[0]
    entry = {**allowance(diff), "production": 1}
    report = audit.apply_allowlist([diff], [entry])
    assert report["exit_code"] == 1
    assert report["unexplained"] == 1
    assert report["stale"] == [entry]


def test_reader_propagates_failure_after_partial_results():
    db, snapshot = fake_database(records())
    original = snapshot.execute_sql.side_effect

    def read(query, **kwargs):
        if query == audit.QUERIES["INDEXES"]:
            raise RuntimeError("partial metadata read")
        return original(query, **kwargs)

    snapshot.execute_sql.side_effect = read
    with pytest.raises(RuntimeError, match="partial metadata"):
        audit.read_schema(db)
    assert snapshot.execute_sql.call_count > 1
    db.snapshot.return_value.__exit__.assert_called_once()


@pytest.mark.parametrize("failure", [None, "provision", "read", "close"])
def test_expected_side_provisions_in_batches_and_always_cleans_up(monkeypatch, failure):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import spanner
    from google.cloud.spanner_v1.database_sessions_manager import DatabaseSessionsManager

    from tests.conformance.spanner_ddl import DDL

    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "127.0.0.1:9010")
    original_interval = DatabaseSessionsManager._MAINTENANCE_THREAD_POLLING_INTERVAL
    db, _ = fake_database(records())
    instance = MagicMock()
    instance.database.return_value = db
    client = MagicMock()
    client.instance.return_value = instance

    def factory(**kwargs):
        assert kwargs["project"] == "tr-schema-audit"
        assert isinstance(kwargs["credentials"], AnonymousCredentials)
        assert DatabaseSessionsManager._MAINTENANCE_THREAD_POLLING_INTERVAL.total_seconds() == 0.1
        return client

    monkeypatch.setattr(spanner, "Client", factory)
    if failure == "provision":
        db.create.side_effect = RuntimeError("provision failed")
    elif failure == "read":
        db.snapshot.side_effect = RuntimeError("read failed")
    elif failure == "close":
        db.close.side_effect = RuntimeError("close failed")
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            audit.expected_schema("127.0.0.1:9010")
    else:
        assert audit.expected_schema("127.0.0.1:9010") == audit.normalise(records())
        instance.database.assert_called_once_with("fixture", ddl_statements=DDL[:20])
        assert [call.args[0] for call in db.update_ddl.call_args_list] == [DDL[offset:offset + 20] for offset in range(20, len(DDL), 20)]
    db.close.assert_called_once()
    db.drop.assert_called_once()
    instance.delete.assert_called_once()
    assert DatabaseSessionsManager._MAINTENANCE_THREAD_POLLING_INTERVAL == original_interval


def test_main_text_reports_both_directions_and_stale(monkeypatch, tmp_path, capsys):
    prod, fixture = {"table/old": {}}, {"table/new": {}}
    monkeypatch.setattr(audit, "production_schema", lambda *args: prod)
    monkeypatch.setattr(audit, "fixture_schema", lambda *args: fixture)
    entry = allowance(audit.differences({}, {"table/stale": {}})[0])
    path = tmp_path / "allow.json"
    path.write_text(json.dumps([entry]))
    assert audit.main(["--allowlist", str(path)]) == 1
    text = capsys.readouterr().out
    assert "unexplained=2 allowlisted=0 stale=1 unverified=0 exit=1" in text
    assert text.index("FIXTURE-HAS / PRODUCTION-LACKS") < text.index("PRODUCTION-HAS / FIXTURE-LACKS")
    assert "STALE table/stale object" in text


def test_child_errors_keep_diagnostics(monkeypatch):
    def run(*args, **kwargs):
        raise audit.subprocess.CalledProcessError(2, "child", output="CANNOT AUDIT: missing field", stderr="details")

    monkeypatch.setattr(audit.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="missing field.*details"):
        audit.fixture_schema("127.0.0.1:9010")


@pytest.mark.parametrize(("view", "field", "state", "obj"), [
    ("COLUMNS", "SPANNER_STATE", "WRITE_ONLY", "column/t/id"),
    ("COLUMNS", "SPANNER_STATE", "BACKFILLING", "column/t/id"),
    ("INDEXES", "INDEX_STATE", "WRITE_ONLY", "index/t/idx"),
    ("CHECK_CONSTRAINTS", "SPANNER_STATE", "VALIDATING", "constraint/t/check"),
    ("COLUMNS", "SPANNER_STATE", None, "column/t/id"),
    ("CHECK_CONSTRAINTS", "SPANNER_STATE", "UNKNOWN", "constraint/t/check"),
])
@pytest.mark.parametrize("fixture_state", ["ready", "same", "absent"])
def test_production_object_not_ready(view, field, state, obj, fixture_state):
    rows = records()
    if view == "COLUMNS":
        rows[view][0].update(IS_GENERATED="ALWAYS", GENERATION_EXPRESSION="1", IS_STORED=True)
    fixture = audit.normalise(rows)
    rows[view][0][field] = state
    production = audit.normalise(rows)
    if fixture_state == "same":
        fixture = copy.deepcopy(production)
    elif fixture_state == "absent":
        del fixture[obj]
    diffs = audit.differences(production, fixture)
    readiness = [diff for diff in diffs if diff["direction"] == "PRODUCTION OBJECT NOT READY"]
    assert len(readiness) == 1
    assert readiness[0]["object"] == obj
    assert readiness[0]["attribute"] == field
    assert readiness[0]["production"] == state
    assert readiness[0]["required_state"] == ("READ_WRITE" if field == "INDEX_STATE" else "COMMITTED")
    assert not any(diff["direction"] == "ATTRIBUTE-MISMATCH" for diff in diffs)
    report = audit.apply_allowlist(diffs, [])
    assert report["exit_code"] == 1
    assert "PRODUCTION OBJECT NOT READY" in audit.markdown(report)


INCOMPLETE_CASES = [
    ("TABLES", "empty", "TABLES", "default schema"),
    ("COLUMNS", "empty", "COLUMNS", "t"),
    ("INDEXES", "empty", "INDEXES", "t/PRIMARY_KEY"),
    ("INDEX_COLUMNS", "empty", "INDEX_COLUMNS", "t/PRIMARY_KEY"),
    ("TABLE_CONSTRAINTS", "empty", "TABLE_CONSTRAINTS", "t"),
    ("CHECK_CONSTRAINTS", "empty", "CHECK_CONSTRAINTS", "t/check"),
    ("KEY_COLUMN_USAGE", "empty", "KEY_COLUMN_USAGE", "t/server_pk_123"),
    ("REFERENTIAL_CONSTRAINTS", "empty", "REFERENTIAL_CONSTRAINTS", "t/fk"),
    ("TABLES", "table_without_columns", "COLUMNS", "empty_table"),
    ("COLUMNS", "unknown_table", "COLUMNS", "unknown"),
    ("INDEXES", "unknown_table", "INDEXES", "unknown"),
    ("INDEX_COLUMNS", "unknown_column", "INDEX_COLUMNS", "unknown"),
    ("COLUMN_OPTIONS", "unknown_column", "COLUMN_OPTIONS", "unknown"),
    ("COLUMN_OPTIONS", "unknown_table", "COLUMN_OPTIONS", "unknown"),
    ("INDEX_COLUMNS", "unknown_table", "INDEX_COLUMNS", "unknown"),
    ("TABLE_CONSTRAINTS", "unknown_table", "TABLE_CONSTRAINTS", "unknown"),
    ("CHECK_CONSTRAINTS", "unknown_constraint", "CHECK_CONSTRAINTS", "t/check"),
    ("KEY_COLUMN_USAGE", "partial", "KEY_COLUMN_USAGE", "t/fk"),
    ("REFERENTIAL_CONSTRAINTS", "unknown_constraint", "REFERENTIAL_CONSTRAINTS", "t/fk"),
    ("COLUMNS", "partial", "INDEX_COLUMNS", "t/timestamp"),
    ("INDEXES", "partial", "INDEX_COLUMNS", "idx"),
    ("INDEX_COLUMNS", "partial", "INDEX_COLUMNS", "t/idx"),
    ("TABLE_CONSTRAINTS", "partial", "CHECK_CONSTRAINTS", "check"),
    ("CHECK_CONSTRAINTS", "missing_clause", "CHECK_CONSTRAINTS", "t/check"),
    ("CHECK_CONSTRAINTS", "orphan", "CHECK_CONSTRAINTS", "orphan"),
    ("REFERENTIAL_CONSTRAINTS", "orphan", "REFERENTIAL_CONSTRAINTS", "orphan"),
    ("KEY_COLUMN_USAGE", "orphan", "KEY_COLUMN_USAGE", "orphan"),
    ("KEY_COLUMN_USAGE", "wrong_position", "INDEX_COLUMNS/KEY_COLUMN_USAGE", "t/PRIMARY_KEY"),
]


@pytest.mark.parametrize("side", ["production", "fixture"])
@pytest.mark.parametrize(("view", "damage", "error_view", "obj"), INCOMPLETE_CASES,
                         ids=[f"{view}-{damage}" for view, damage, _, _ in INCOMPLETE_CASES])
def test_incomplete_metadata_cannot_audit(monkeypatch, tmp_path, capsys, side, view, damage, error_view, obj):
    rows = records()
    if damage == "empty":
        rows[view] = []
    elif damage == "table_without_columns":
        rows[view].append({**rows[view][0], "TABLE_NAME": "empty_table"})
    elif damage in {"unknown_table", "unknown_column", "unknown_constraint"}:
        field = {"unknown_table": "TABLE_NAME", "unknown_column": "COLUMN_NAME", "unknown_constraint": "CONSTRAINT_NAME"}[damage]
        rows[view][0][field] = "unknown"
        if view == "COLUMN_OPTIONS":
            # Even options we do not compare must have a valid owner.
            rows[view][0]["OPTION_NAME"] = "other_option"
    elif damage == "missing_clause":
        rows[view][0]["CHECK_CLAUSE"] = None
    elif damage == "orphan":
        rows[view].append({**rows[view][0], "CONSTRAINT_NAME": "orphan"})
    elif damage == "wrong_position":
        rows[view][0]["ORDINAL_POSITION"] = 2
    elif view == "COLUMNS":
        rows[view].pop()
    elif view == "KEY_COLUMN_USAGE":
        rows[view].pop()
    elif view == "INDEX_COLUMNS":
        rows[view] = [row for row in rows[view] if row["INDEX_NAME"] == "PRIMARY_KEY"]
    else:
        rows[view].pop(0)
    broken_db, _ = fake_database(rows)
    good_db, _ = fake_database(records())
    monkeypatch.setattr(audit, "production_schema", lambda *args: audit.read_schema(broken_db if side == "production" else good_db))
    monkeypatch.setattr(audit, "fixture_schema", lambda *args: audit.read_schema(broken_db if side == "fixture" else good_db))
    path = tmp_path / "allow.json"
    path.write_text("[]")
    assert audit.main(["--json", "--allowlist", str(path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert error_view in report["error"]
    assert obj in report["error"]
    assert "differences" not in report
    assert audit.summary(report).startswith("CANNOT AUDIT:")


def test_optional_views_can_be_empty_consistently():
    rows = records()
    for view in ("COLUMN_OPTIONS", "CHECK_CONSTRAINTS", "REFERENTIAL_CONSTRAINTS"):
        rows[view] = []
    rows["TABLE_CONSTRAINTS"] = [row for row in rows["TABLE_CONSTRAINTS"] if row["CONSTRAINT_TYPE"] == "PRIMARY KEY"]
    rows["KEY_COLUMN_USAGE"] = [row for row in rows["KEY_COLUMN_USAGE"] if row["CONSTRAINT_NAME"] == "server_pk_123"]
    schema = audit.normalise(rows)
    assert audit.apply_allowlist(audit.differences(schema, schema), [])["exit_code"] == 0


def composite_foreign_key_records():
    rows = records()
    rows["COLUMNS"][1]["IS_NULLABLE"] = "NO"
    rows["INDEX_COLUMNS"].append({**rows["INDEX_COLUMNS"][-1], "COLUMN_NAME": "timestamp", "ORDINAL_POSITION": 2})
    rows["KEY_COLUMN_USAGE"].append({**rows["KEY_COLUMN_USAGE"][0], "COLUMN_NAME": "timestamp", "ORDINAL_POSITION": 2})
    rows["KEY_COLUMN_USAGE"].append({**rows["KEY_COLUMN_USAGE"][1], "COLUMN_NAME": "timestamp", "ORDINAL_POSITION": 2, "POSITION_IN_UNIQUE_CONSTRAINT": 2})
    return rows


@pytest.mark.parametrize("referenced_kind", ["PRIMARY KEY", "UNIQUE"])
@pytest.mark.parametrize("reverse_mapping", [False, True])
def test_complete_composite_foreign_key(referenced_kind, reverse_mapping):
    rows = composite_foreign_key_records()
    if referenced_kind == "UNIQUE":
        rows["TABLE_CONSTRAINTS"].append({**rows["TABLE_CONSTRAINTS"][1], "CONSTRAINT_NAME": "unique_key", "CONSTRAINT_TYPE": "UNIQUE"})
        rows["KEY_COLUMN_USAGE"].extend([
            {**row, "CONSTRAINT_NAME": "unique_key"} for row in rows["KEY_COLUMN_USAGE"] if row["CONSTRAINT_NAME"] == "server_pk_123"
        ])
        rows["REFERENTIAL_CONSTRAINTS"][0]["UNIQUE_CONSTRAINT_NAME"] = "unique_key"
    if reverse_mapping:
        for row in rows["KEY_COLUMN_USAGE"]:
            if row["CONSTRAINT_NAME"] == "fk":
                row["POSITION_IN_UNIQUE_CONSTRAINT"] = 3 - row["POSITION_IN_UNIQUE_CONSTRAINT"]
    schema = audit.normalise(rows)
    reversed_rows = {view: list(reversed(items)) for view, items in rows.items()}
    assert audit.apply_allowlist(audit.differences(schema, audit.normalise(reversed_rows)), [])["exit_code"] == 0


@pytest.mark.parametrize("side", ["production", "fixture", "both"])
@pytest.mark.parametrize("damage", [
    "missing_second_usage", "ordinal_gap", "ordinal_duplicate", "ordinal_null", "ordinal_zero",
    "mapping_gap", "mapping_duplicate", "mapping_null", "mapping_zero",
    "referenced_gap", "referenced_duplicate", "referenced_empty", "referenced_nonunique",
    "referenced_missing", "referenced_schema",
])
def test_incomplete_composite_foreign_key_cannot_audit(monkeypatch, tmp_path, capsys, side, damage):
    good = composite_foreign_key_records()
    broken = copy.deepcopy(good)
    if damage == "missing_second_usage":
        broken["KEY_COLUMN_USAGE"].pop()
    elif damage.startswith(("ordinal_", "mapping_")):
        field = "ORDINAL_POSITION" if damage.startswith("ordinal_") else "POSITION_IN_UNIQUE_CONSTRAINT"
        broken["KEY_COLUMN_USAGE"][-1][field] = {"gap": 3, "duplicate": 1, "null": None, "zero": 0}[damage.split("_")[1]]
    elif damage in {"referenced_gap", "referenced_duplicate"}:
        position = 3 if damage == "referenced_gap" else 1
        # Keep index and primary-key views consistent to exercise the FK check.
        broken["INDEX_COLUMNS"][-1]["ORDINAL_POSITION"] = position
        broken["KEY_COLUMN_USAGE"][-2]["ORDINAL_POSITION"] = position
    elif damage == "referenced_empty":
        broken["TABLE_CONSTRAINTS"].append({**broken["TABLE_CONSTRAINTS"][1], "CONSTRAINT_NAME": "empty_unique", "CONSTRAINT_TYPE": "UNIQUE"})
        broken["REFERENTIAL_CONSTRAINTS"][0]["UNIQUE_CONSTRAINT_NAME"] = "empty_unique"
    elif damage in {"referenced_nonunique", "referenced_missing"}:
        broken["REFERENTIAL_CONSTRAINTS"][0]["UNIQUE_CONSTRAINT_NAME"] = "check" if damage == "referenced_nonunique" else "missing"
    else:
        broken["REFERENTIAL_CONSTRAINTS"][0]["UNIQUE_CONSTRAINT_SCHEMA"] = "other_schema"
    monkeypatch.setattr(audit, "production_schema", lambda *args: audit.normalise(broken if side in {"production", "both"} else good))
    monkeypatch.setattr(audit, "fixture_schema", lambda *args: audit.normalise(broken if side in {"fixture", "both"} else good))
    path = tmp_path / "allow.json"
    path.write_text("[]")
    assert audit.main(["--json", "--allowlist", str(path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["exit_code"] == 2
    assert "fk" in report["error"]
    assert "cannot audit" in report["error"]
    assert "differences" not in report


EXPRESSION_CASES = [
    ("COLUMNS", "GENERATION_EXPRESSION", "column/t/id"),
    ("COLUMNS", "COLUMN_DEFAULT", "column/t/id"),
    ("CHECK_CONSTRAINTS", "CHECK_CLAUSE", "constraint/t/check"),
    ("TABLES", "ROW_DELETION_POLICY_EXPRESSION", "table/t"),
]


@pytest.mark.parametrize(("view", "field", "obj"), EXPRESSION_CASES)
@pytest.mark.parametrize(("left", "right"), [
    ("id > 0", "id  >  0"),
    ("id > 0", "((id > 0))"),
    ("CASE WHEN id IS NULL THEN ABS(id) ELSE 0 END", "case when id is null then abs ( id ) else 0 end"),
    ("SAFE.TIMESTAMP_SECONDS(id)", "safe.timestamp_seconds (id)"),
    ("OLDER_THAN(timestamp, INTERVAL 7 DAY)", "older_than (timestamp, interval 7 day)"),
    ("OLDER_THAN(x, INTERVAL 7 DAY)", "older_than(x, interval 7 day)"),
    ("MOD(MOD(FARM_FINGERPRINT(CONCAT(id, '#', kind)), 16) + 16, 16)", "mod(mod(farm_fingerprint(concat(id, '#', kind)), 16) + 16, 16)"),
    ("SAFE.TIMESTAMP_SECONDS(SAFE_CAST(JSON_QUERY(body, '$.expires_at') AS INT64))", "safe.timestamp_seconds(safe_cast(json_query(body, '$.expires_at') as int64))"),
])
def test_expression_formatting_is_not_drift(view, field, obj, left, right):
    rows = records()
    rows[view][0][field] = left
    production = audit.normalise(rows)
    rows[view][0][field] = right
    fixture = audit.normalise(rows)
    assert production[obj][field] == left
    assert fixture[obj][field] == right
    assert audit.differences(production, fixture) == []


@pytest.mark.parametrize(("view", "field", "obj"), EXPRESSION_CASES)
@pytest.mark.parametrize(("left", "right"), [
    ("x = 'A'", "x = 'a'"),
    ('x = B"A"', 'x = B"a"'),
    ("x = r'A  B'", "x = r'A B'"),
    ("x = 1", "x = 10"),
    ("x = 1.0", "x = 1.00"),
    ("x = 1e2", "x = 1E2"),
    ("x = 0xAF", "x = 0xaf"),
    ("`Identifier` = 0", "`identifier` = 0"),
    ("(x + 1) * 2", "x + 1 * 2"),
    ("x >= 1", "x > = 1"),
    ("(x) + (1)", "x + 1"),
    ("INT64(payload.year)", "INT64(payload.YEAR)"),
    ("INT64(payload.day)", "INT64(payload.DAY)"),
    ("INT64(payload.case)", "INT64(payload.CASE)"),
    ("INT64(payload.concat)", "INT64(payload.CONCAT)"),
    ("INT64(payload.year)", "INT64(PAYLOAD.year)"),
    ("id > 0", "ID > 0"),
    ("custom_function(id)", "CUSTOM_FUNCTION(id)"),
    ("custom.function(id)", "CUSTOM.function(id)"),
    ("concat > 0", "CONCAT > 0"),
])
def test_expression_meaning_is_preserved(view, field, obj, left, right):
    rows = records()
    rows[view][0][field] = left
    production = audit.normalise(rows)
    rows[view][0][field] = right
    fixture = audit.normalise(rows)
    diffs = audit.differences(production, fixture)
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff["object"] == obj
    assert diff["production"] == left
    assert diff["fixture"] == right
    assert diff["production_normalised"] != diff["fixture_normalised"]
    report = audit.apply_allowlist(diffs, [])
    assert report["exit_code"] == 1
    rendered = audit.markdown(report)
    assert "raw" in rendered and "normalised" in rendered
    assert audit.apply_allowlist(diffs, [allowance(diff)])["exit_code"] == 0


def test_tokeniser_keeps_escaped_and_triple_quoted_literals_atomic():
    for literal in ["'it\\'s A ( B'", "'it''s A'", '\"A ) (\"', "b\"\"\"A ) \n B\"\"\"", "r'\\x41'", "`a\\`B`"]:
        assert audit.normalise_expression(f"( x = {literal} )") == ["x", "=", literal]


def test_expression_report_text_keeps_raw_and_normalised(monkeypatch, tmp_path, capsys):
    rows = records()
    prod = audit.normalise(rows)
    rows["CHECK_CONSTRAINTS"][0]["CHECK_CLAUSE"] = "(id > 10)"
    fixture = audit.normalise(rows)
    monkeypatch.setattr(audit, "production_schema", lambda *args: prod)
    monkeypatch.setattr(audit, "fixture_schema", lambda *args: fixture)
    path = tmp_path / "allow.json"
    path.write_text("[]")
    assert audit.main(["--allowlist", str(path)]) == 1
    output = capsys.readouterr().out
    assert '"raw": "(id > 10)"' in output
    assert '"normalised": ["id", ">", "10"]' in output
