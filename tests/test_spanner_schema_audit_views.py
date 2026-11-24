"""Views in the schema audit: TABLES lists them, but they have no primary key.

The first view (tr_workspace_directory_source, 2026-11-11) made every scheduled
audit exit 2, "missing primary-key index", for ten days. A view is audited by
its columns, its definition and its security type; a base table keeps every
primary-key and constraint check.
"""
from __future__ import annotations

import copy

import pytest

from scripts import audit_spanner_schema as audit
from tests.conformance import spanner_schema_source
from tests.conformance.spanner_ddl import DDL
from tests.test_spanner_schema_audit import records

DEFINITION = "SELECT t.id AS id,\n  JSON_QUERY(t.body, '$.name') AS name\nFROM t\nWHERE t.id > 0"


def with_view(definition=DEFINITION, security="DEFINER"):
    rows = records()

    def add(view, **values):
        rows[view].append({field: values.get(field, "COMMITTED" if field == "SPANNER_STATE" else None)
                           for field in audit.PROJECTIONS[view].split()})

    add("TABLES", TABLE_NAME="v", TABLE_TYPE="VIEW")
    add("COLUMNS", TABLE_NAME="v", COLUMN_NAME="id", ORDINAL_POSITION=1, SPANNER_TYPE="INT64", IS_NULLABLE="YES", IS_GENERATED="NEVER")
    add("COLUMNS", TABLE_NAME="v", COLUMN_NAME="name", ORDINAL_POSITION=2, SPANNER_TYPE="STRING(MAX)", IS_NULLABLE="YES", IS_GENERATED="NEVER")
    add("VIEWS", TABLE_NAME="v", VIEW_DEFINITION=definition, SECURITY_TYPE=security)
    return rows


def test_a_view_without_a_primary_key_is_auditable():
    schema = audit.normalise(with_view())
    assert schema["table/v"]["TABLE_TYPE"] == "VIEW"
    assert schema["table/v"]["VIEW_DEFINITION"] == DEFINITION
    assert schema["table/v"]["SECURITY_TYPE"] == "DEFINER"
    assert schema["column/v/name"]["SPANNER_TYPE"] == "STRING(MAX)"
    assert not any(key.startswith(("index/v/", "constraint/v/")) for key in schema)
    assert audit.differences(schema, copy.deepcopy(schema)) == []


def test_a_base_table_without_a_primary_key_still_cannot_be_audited():
    rows = with_view()
    rows["INDEXES"] = [row for row in rows["INDEXES"] if row["INDEX_NAME"] != "PRIMARY_KEY"]
    rows["INDEX_COLUMNS"] = [row for row in rows["INDEX_COLUMNS"] if row["INDEX_NAME"] != "PRIMARY_KEY"]
    with pytest.raises(ValueError, match="t/PRIMARY_KEY: missing primary-key index"):
        audit.normalise(rows)


def damage_view_without_definition(rows):
    rows["VIEWS"].clear()


def damage_blank_definition(rows):
    rows["VIEWS"][0]["VIEW_DEFINITION"] = "  "


def damage_view_with_index(rows):
    rows["INDEXES"].append({**rows["INDEXES"][0], "TABLE_NAME": "v", "INDEX_NAME": "v_idx"})
    rows["INDEX_COLUMNS"].append({**rows["INDEX_COLUMNS"][0], "TABLE_NAME": "v", "INDEX_NAME": "v_idx", "COLUMN_NAME": "id"})


def damage_view_with_constraint(rows):
    rows["TABLE_CONSTRAINTS"].append({**rows["TABLE_CONSTRAINTS"][0], "TABLE_NAME": "v", "CONSTRAINT_NAME": "v_check"})
    rows["CHECK_CONSTRAINTS"].append({**rows["CHECK_CONSTRAINTS"][0], "CONSTRAINT_NAME": "v_check"})


def damage_definition_on_base_table(rows):
    rows["VIEWS"].append({**rows["VIEWS"][0], "TABLE_NAME": "t"})


def damage_definition_without_table(rows):
    rows["VIEWS"].append({**rows["VIEWS"][0], "TABLE_NAME": "unknown"})


def damage_unknown_table_type(rows):
    rows["TABLES"][0]["TABLE_TYPE"] = "MATERIALIZED VIEW"


def damage_missing_table_type(rows):
    rows["TABLES"][0]["TABLE_TYPE"] = None


def damage_only_views(rows):
    rows["TABLES"][0]["TABLE_TYPE"] = "VIEW"


def damage_view_without_columns(rows):
    rows["COLUMNS"] = [row for row in rows["COLUMNS"] if row["TABLE_NAME"] != "v"]


@pytest.mark.parametrize(("damage", "message"), [
    (damage_view_without_definition, "VIEWS v: view has no definition"),
    (damage_blank_definition, "VIEWS v: view has no definition"),
    (damage_view_with_index, "INDEXES v: view has an index or constraint"),
    (damage_view_with_constraint, "TABLE_CONSTRAINTS v: view has an index or constraint"),
    (damage_definition_on_base_table, "VIEWS t: base table has a view definition"),
    (damage_definition_without_table, "unknown TABLES table"),
    (damage_unknown_table_type, "unknown table type"),
    (damage_missing_table_type, "unknown table type"),
    (damage_only_views, "no tables visible"),
    (damage_view_without_columns, "COLUMNS v: table has no columns"),
], ids=lambda value: getattr(value, "__name__", "")[7:] or None)
def test_view_metadata_that_does_not_add_up_cannot_be_audited(damage, message):
    rows = with_view()
    damage(rows)
    with pytest.raises(ValueError, match=message):
        audit.normalise(rows)


@pytest.mark.parametrize(("production", "fixture", "drift"), [
    (dict(), dict(definition=DEFINITION.replace("'$.name'", "'$.email'")), True),
    (dict(), dict(definition=DEFINITION.replace("t.id > 0", "t.id >= 0")), True),
    (dict(), dict(security="INVOKER"), True),
    # The stored text keeps the migration's layout; only tokens are compared.
    (dict(), dict(definition=" ".join(DEFINITION.split())), False),
    (dict(), dict(definition=DEFINITION.replace("SELECT", "select").replace("FROM", "from")), False),
])
def test_view_definition_and_security_type_are_compared(production, fixture, drift):
    diffs = audit.differences(audit.normalise(with_view(**production)), audit.normalise(with_view(**fixture)))
    assert [(d["object"], d["direction"]) for d in diffs] == ([(
        "table/v", "ATTRIBUTE-MISMATCH")] if drift else [])


def test_a_view_only_in_production_is_drift():
    production = audit.normalise(with_view())
    fixture = audit.normalise(records())
    diffs = audit.differences(production, fixture)
    assert {(d["object"], d["direction"]) for d in diffs} == {
        ("table/v", "PRODUCTION-HAS / FIXTURE-LACKS"),
        ("column/v/id", "PRODUCTION-HAS / FIXTURE-LACKS"),
        ("column/v/name", "PRODUCTION-HAS / FIXTURE-LACKS"),
    }


def test_the_fixture_creates_every_reviewed_view_after_the_tables_it_reads():
    reviewed_views = [
        " ".join(text.replace(r"\$", "$").split())
        for statements in spanner_schema_source.REVIEWED_DDL_STATEMENTS.values()
        for text in statements if text.startswith("CREATE VIEW ")
    ]
    assert reviewed_views, "the workspace directory view is reviewed DDL"
    fixture_views = [" ".join(statement.split()) for statement in DDL if statement.startswith("CREATE VIEW ")]
    assert fixture_views == reviewed_views
    first_view = next(i for i, statement in enumerate(DDL) if statement.startswith("CREATE VIEW "))
    assert all(statement.startswith("CREATE VIEW ") for statement in DDL[first_view:])
    assert "$" not in "".join(DDL[first_view:]).replace("'$.", "'")  # no shell escapes left


@pytest.mark.parametrize("statement", ["CREATE VIEW v AS SELECT $COLUMN FROM t", "CREATE VIEW v AS SELECT ${COLUMN} FROM t",
                                       "CREATE VIEW v AS SELECT x FROM t WHERE y = '$NAME'", "CREATE VIEW v AS SELECT '$' FROM t"])
def test_shell_variables_are_still_refused_in_extracted_ddl(statement):
    with pytest.raises(AssertionError, match="unresolved shell variable"):
        spanner_schema_source.assert_no_shell_variables(statement, "test")


def test_quoted_json_paths_are_not_shell_variables():
    spanner_schema_source.assert_no_shell_variables("CREATE VIEW v AS SELECT JSON_QUERY(b, '$.owner_user_id') AS o FROM t", "test")
