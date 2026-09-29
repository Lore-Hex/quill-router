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
        rows[view].append({field: values.get(field) for field in audit.PROJECTIONS[view].split()})

    add("TABLES", TABLE_NAME="t")
    add("COLUMNS", TABLE_NAME="t", COLUMN_NAME="id", ORDINAL_POSITION="1", SPANNER_TYPE="INT64", IS_NULLABLE="NO", IS_GENERATED="NEVER", IS_STORED=None)
    add("COLUMNS", TABLE_NAME="t", COLUMN_NAME="timestamp", ORDINAL_POSITION=2, SPANNER_TYPE="TIMESTAMP", IS_NULLABLE="YES", IS_GENERATED="NEVER")
    add("COLUMN_OPTIONS", TABLE_NAME="t", COLUMN_NAME="timestamp", OPTION_NAME="allow_commit_timestamp", OPTION_TYPE="BOOL", OPTION_VALUE="TRUE")
    add("INDEXES", TABLE_NAME="t", INDEX_NAME="idx", INDEX_TYPE="INDEX", IS_UNIQUE=False, IS_NULL_FILTERED=True, INDEX_STATE="READ_WRITE")
    for name, pos, order in [("timestamp", None, None), ("id", 1, "ASC")]:
        add("INDEX_COLUMNS", TABLE_NAME="t", INDEX_NAME="idx", COLUMN_NAME=name, ORDINAL_POSITION=pos, COLUMN_ORDERING=order)
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
    ("INDEXES", "INDEX_STATE", "WRITE_ONLY"),
    ("INDEXES", "INDEX_TYPE", "SEARCH"),
    ("INDEXES", "PARENT_TABLE_NAME", "parent"),
    ("INDEX_COLUMNS", "COLUMN_ORDERING", "DESC"),
    ("INDEX_COLUMNS", "ORDINAL_POSITION", 2),
    ("CHECK_CONSTRAINTS", "CHECK_CLAUSE", "id > 1"),
    ("TABLE_CONSTRAINTS", "ENFORCED", "NO"),
    ("REFERENTIAL_CONSTRAINTS", "DELETE_RULE", "NO ACTION"),
    ("KEY_COLUMN_USAGE", "ORDINAL_POSITION", 2),
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
    return {**{k: v for k, v in diff.items() if k != "direction"}, "reason": "Reviewed rolling upgrade", "verified_against_production": False}


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
