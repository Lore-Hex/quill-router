"""Regression cases observed by the first authenticated production schema audit."""
from __future__ import annotations

import copy
import json

import pytest

from scripts import audit_spanner_schema as audit


def schema():
    return {
        "column/t/id": {"ORDINAL_POSITION": 1, "SPANNER_TYPE": "INT64", "IS_NULLABLE": False},
        "column/t/value": {"ORDINAL_POSITION": 2, "SPANNER_TYPE": "STRING(MAX)", "IS_NULLABLE": True},
        "index/t/PRIMARY_KEY": {
            "INDEX_TYPE": "PRIMARY_KEY", "INDEX_STATE": None,
            "columns": [{"COLUMN_NAME": "id", "ORDINAL_POSITION": 1, "COLUMN_ORDERING": "ASC"}],
        },
        "index/t/by_value": {
            "INDEX_TYPE": "INDEX", "INDEX_STATE": "READ_WRITE",
            "columns": [{"COLUMN_NAME": "value", "ORDINAL_POSITION": 1, "COLUMN_ORDERING": "ASC"}],
        },
        "constraint/t/PRIMARY_KEY": {
            "CONSTRAINT_TYPE": "PRIMARY KEY",
            "columns": [{"COLUMN_NAME": "id", "ORDINAL_POSITION": 1}],
        },
    }


@pytest.mark.parametrize("fixture_state", [None, "READ_WRITE"])
def test_primary_key_pseudo_index_null_state_is_not_an_outage(fixture_state):
    production = schema()
    fixture = copy.deepcopy(production)
    fixture["index/t/PRIMARY_KEY"]["INDEX_STATE"] = fixture_state
    assert audit.differences(production, fixture) == []


@pytest.mark.parametrize("key,state", [
    ("index/t/by_value", None),
    ("index/t/by_value", "WRITE_ONLY"),
    ("index/t/PRIMARY_KEY", "WRITE_ONLY"),
])
def test_only_null_primary_key_state_is_exempt_from_readiness(key, state):
    production = schema()
    fixture = copy.deepcopy(production)
    production[key]["INDEX_STATE"] = state
    diffs = audit.differences(production, fixture)
    assert [(d["object"], d["attribute"], d["direction"]) for d in diffs] == [
        (key, "INDEX_STATE", audit.NOT_READY),
    ]


def test_primary_key_name_does_not_exempt_a_secondary_index():
    production = schema()
    production["index/t/PRIMARY_KEY"]["INDEX_TYPE"] = "INDEX"
    diffs = audit.differences(production, copy.deepcopy(production))
    assert [(d["object"], d["direction"]) for d in diffs] == [
        ("index/t/PRIMARY_KEY", audit.NOT_READY),
    ]


def test_named_table_column_reordering_is_not_semantic_schema_drift():
    production = schema()
    fixture = copy.deepcopy(production)
    fixture["column/t/id"]["ORDINAL_POSITION"] = 2
    fixture["column/t/value"]["ORDINAL_POSITION"] = 1
    assert audit.differences(production, fixture) == []
    assert production["column/t/id"]["ORDINAL_POSITION"] == 1
    assert fixture["column/t/id"]["ORDINAL_POSITION"] == 2


@pytest.mark.parametrize("key", ["index/t/PRIMARY_KEY", "index/t/by_value", "constraint/t/PRIMARY_KEY"])
def test_index_and_constraint_key_order_remains_strict(key):
    production = schema()
    fixture = copy.deepcopy(production)
    fixture[key]["columns"][0]["ORDINAL_POSITION"] = 2
    diffs = audit.differences(production, fixture)
    assert [(d["object"], d["attribute"], d["direction"]) for d in diffs] == [
        (key, "columns", "ATTRIBUTE-MISMATCH"),
    ]


@pytest.mark.parametrize("attribute,value", [("SPANNER_TYPE", "STRING(MAX)"), ("IS_NULLABLE", True)])
def test_column_order_does_not_hide_real_column_changes(attribute, value):
    production = schema()
    fixture = copy.deepcopy(production)
    fixture["column/t/id"].update(ORDINAL_POSITION=2, **{attribute: value})
    diffs = audit.differences(production, fixture)
    assert [(d["object"], d["attribute"]) for d in diffs] == [("column/t/id", attribute)]


def test_verified_baseline_contains_only_ten_exact_historical_differences():
    entries = json.loads(audit.DEFAULT_ALLOWLIST.read_text())
    assert len(entries) == 10
    assert all(entry["verified_against_production"] for entry in entries)
    diffs = [{**{key: entry[key] for key in ("object", "attribute", "production", "fixture")},
              "direction": "ATTRIBUTE-MISMATCH"} for entry in entries]
    report = audit.apply_allowlist(diffs, entries)
    assert report["exit_code"] == 0
    assert report["unverified"] == 0
    for position, diff in enumerate(diffs):
        changed = copy.deepcopy(diffs)
        changed[position] = {**diff, "production": {"unexpected": True}}
        report = audit.apply_allowlist(changed, entries)
        assert report["unexplained"] == 1
        assert report["stale"] == [entries[position]]
        assert report["exit_code"] == 1
