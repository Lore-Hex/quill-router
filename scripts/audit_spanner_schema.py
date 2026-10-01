"""Read-only production metadata audit against a disposable loopback fixture.

Exit 0: no unexplained drift; 1: drift or stale exceptions; 2: cannot audit.
Run as ``python -m scripts.audit_spanner_schema`` from the repository root.
"""
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = ROOT / "tests/conformance/spanner_schema_drift_allowlist.json"
Schema = dict[str, dict[str, Any]]

# Explicit projections fail closed when a server cannot expose any required field.
# All reads are metadata in the GoogleSQL default schema, never application rows.
PROJECTIONS = {
    "TABLES": "TABLE_NAME PARENT_TABLE_NAME ON_DELETE_ACTION ROW_DELETION_POLICY_EXPRESSION",
    "COLUMNS": "TABLE_NAME COLUMN_NAME ORDINAL_POSITION SPANNER_TYPE IS_NULLABLE IS_GENERATED GENERATION_EXPRESSION IS_STORED COLUMN_DEFAULT SPANNER_STATE",
    "COLUMN_OPTIONS": "TABLE_NAME COLUMN_NAME OPTION_NAME OPTION_TYPE OPTION_VALUE",
    "INDEXES": "TABLE_NAME INDEX_NAME INDEX_TYPE IS_UNIQUE IS_NULL_FILTERED INDEX_STATE PARENT_TABLE_NAME",
    "INDEX_COLUMNS": "TABLE_NAME INDEX_NAME COLUMN_NAME ORDINAL_POSITION COLUMN_ORDERING",
    "TABLE_CONSTRAINTS": "TABLE_NAME CONSTRAINT_NAME CONSTRAINT_TYPE IS_DEFERRABLE INITIALLY_DEFERRED ENFORCED",
    "CHECK_CONSTRAINTS": "CONSTRAINT_NAME CHECK_CLAUSE SPANNER_STATE",
    "KEY_COLUMN_USAGE": "TABLE_NAME CONSTRAINT_NAME COLUMN_NAME ORDINAL_POSITION POSITION_IN_UNIQUE_CONSTRAINT",
    "REFERENTIAL_CONSTRAINTS": "CONSTRAINT_NAME UNIQUE_CONSTRAINT_SCHEMA UNIQUE_CONSTRAINT_NAME MATCH_OPTION UPDATE_RULE DELETE_RULE",
}
QUERIES = {
    name: f"SELECT {', '.join(projection.split())} FROM INFORMATION_SCHEMA.{name} "  # noqa: S608 -- fixed metadata projections only
    f"WHERE {'CONSTRAINT_SCHEMA' if name in {'CHECK_CONSTRAINTS', 'REFERENTIAL_CONSTRAINTS'} else 'TABLE_SCHEMA'} = ''"
    for name, projection in PROJECTIONS.items()
}
BOOL_FIELDS = {"IS_NULLABLE", "IS_STORED", "IS_UNIQUE", "IS_NULL_FILTERED", "IS_DEFERRABLE", "INITIALLY_DEFERRED", "ENFORCED"}
EXPRESSION_FIELDS = {"GENERATION_EXPRESSION", "COLUMN_DEFAULT", "CHECK_CLAUSE", "ROW_DELETION_POLICY_EXPRESSION"}
STATE_FIELDS = {"SPANNER_STATE": "COMMITTED", "INDEX_STATE": "READ_WRITE"}
NOT_READY = "PRODUCTION OBJECT NOT READY"
# Fixed GoogleSQL reserved keywords, plus DAY for the schema's TTL intervals.
# In particular, YEAR is not reserved: payload.year is a case-sensitive path.
SQL_KEYWORDS = frozenset("""
ALL AND ANY ARRAY AS ASC ASSERT_ROWS_MODIFIED AT BETWEEN BY CASE CAST COLLATE
CONTAINS CREATE CROSS CUBE CURRENT DEFAULT DEFINE DESC DISTINCT
ELSE END ENUM ESCAPE EXCEPT EXCLUDE EXISTS EXTRACT FALSE FETCH FOLLOWING FOR FROM
FULL GROUP GROUPING GROUPS HASH HAVING IF IGNORE IN INNER INTERSECT INTERVAL INTO
IS JOIN LATERAL LEFT LIKE LIMIT LOOKUP MERGE NATURAL NEW NO NOT NULL NULLS OF ON
OR ORDER OUTER OVER PARTITION PRECEDING PROTO QUALIFY RANGE RECURSIVE RESPECT RIGHT
ROLLUP ROWS SELECT SET SOME STRUCT TABLESAMPLE THEN TO TREAT TRUE
UNBOUNDED UNION UNNEST USING WHEN WHERE WINDOW WITH WITHIN DAY
""".split())
# Reviewed against the generated conformance schema: expressions use CONCAT,
# FARM_FINGERPRINT, MOD, JSON_QUERY, SAFE_CAST, INT64 and SAFE.TIMESTAMP_SECONDS;
# TTLs use OLDER_THAN. ABS and INT64 conversion are also covered by audit probes.
# Never infer a built-in from an arbitrary word followed by '('.
SQL_FUNCTIONS = frozenset({
    "ABS", "CONCAT", "FARM_FINGERPRINT", "INT64", "JSON_QUERY", "MOD",
    "OLDER_THAN", "SAFE_CAST", "SAFE.TIMESTAMP_SECONDS",
})
SQL_TYPES = frozenset({"INT64"})  # SAFE_CAST(... AS INT64) in the generated schema.
SQL_TOKEN = re.compile(
    r"(?P<space>\s+|--[^\n]*|/\*[\s\S]*?\*/)"
    r"|(?P<quoted>(?i:rb|br|r|b)?(?:'''(?:\\[\s\S]|(?!''')[^\\])*'''"
    r'|"""(?:\\[\s\S]|(?!""")[^\\])*"""'
    r"|'(?:\\[\s\S]|''|[^'\\])*'|\"(?:\\[\s\S]|\"\"|[^\"\\])*\")"
    r"|`(?:\\[\s\S]|``|[^`\\])*`)"
    r"|(?P<number>0[xX][0-9a-fA-F]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"|(?P<word>[A-Za-z_][A-Za-z_0-9]*)"
    r"|(?P<symbol><=|>=|!=|<>|\|\||<<|>>|[+*/%=<>&|^~.,:;()\[\]{}?-])"
)


def normalise_expression(raw: str) -> list[str]:
    """Token comparison only; never rewrite literals or reassociate operations."""
    tokens: list[str] = []
    kinds = []
    position = 0
    while position < len(raw):
        match = SQL_TOKEN.match(raw, position)
        if match is None:
            raise ValueError(f"Cannot tokenise SQL expression at {position}: {raw!r}")
        if match.lastgroup != "space":
            tokens.append(match[0])
            kinds.append(match.lastgroup)
        position = match.end()
    for i, token in enumerate(tokens):
        if kinds[i] != "word" or (i > 0 and tokens[i - 1] == "."):
            continue
        end = i + 1
        while end + 1 < len(tokens) and tokens[end] == "." and kinds[end + 1] == "word":
            end += 2
        if end < len(tokens) and tokens[end] == "(" and "".join(tokens[i:end]).upper() in SQL_FUNCTIONS:
            tokens[i:end] = [part.upper() for part in tokens[i:end]]
        elif end == i + 1 and (token.upper() in SQL_KEYWORDS or (i > 0 and tokens[i - 1] == "AS" and token.upper() in SQL_TYPES)):
            tokens[i] = token.upper()
    # Strip only pairs enclosing the entire expression, never (a+b)*c or
    # (a)+(b). Quoted parentheses were consumed as a single token above.
    while tokens and tokens[0] == "(" and tokens[-1] == ")":
        depth = 0
        for _index, lexeme in enumerate(tokens):
            depth += (lexeme == "(") - (lexeme == ")")
            if depth == 0:
                break
        if _index != len(tokens) - 1:
            break
        tokens = tokens[1:-1]
    return tokens


def value(field: str, raw: Any) -> Any:
    if raw is None:
        return None
    if field in BOOL_FIELDS or field == "OPTION_VALUE":
        if isinstance(raw, bool):
            return raw
        if str(raw).upper() in {"YES", "TRUE"}:
            return True
        if str(raw).upper() in {"NO", "FALSE"}:
            return False
        if field in BOOL_FIELDS:
            raise ValueError(f"Invalid {field}: {raw!r}")
    if field in {"ORDINAL_POSITION", "POSITION_IN_UNIQUE_CONSTRAINT"}:
        return int(raw)
    # Keep raw expressions in the schema/report; tokenise only for comparison.
    return raw


def validate_completeness(data: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    """Cross-check views before treating absence as schema drift on either side.

    Optional views can legitimately be empty (e.g. COLUMN_OPTIONS). We can
    detect dangling references, not an option omitted without any other trace.
    """
    def fail(view: str, obj: Any, detail: str) -> None:
        raise ValueError(f"{view} {obj}: {detail}; cannot audit")

    def keyed(view: str, *fields: str) -> dict[tuple[Any, ...], Mapping[str, Any]]:
        result = {}
        for row in data[view]:
            key = tuple(row[field] for field in fields)
            if key in result:
                fail(view, key, "duplicate object")
            result[key] = row
        return result

    tables = keyed("TABLES", "TABLE_NAME")
    columns = keyed("COLUMNS", "TABLE_NAME", "COLUMN_NAME")
    indexes = keyed("INDEXES", "TABLE_NAME", "INDEX_NAME")
    index_columns = keyed("INDEX_COLUMNS", "TABLE_NAME", "INDEX_NAME", "COLUMN_NAME")
    constraints = keyed("TABLE_CONSTRAINTS", "CONSTRAINT_NAME")
    checks = keyed("CHECK_CONSTRAINTS", "CONSTRAINT_NAME")
    keys = keyed("KEY_COLUMN_USAGE", "CONSTRAINT_NAME", "COLUMN_NAME")
    references = keyed("REFERENTIAL_CONSTRAINTS", "CONSTRAINT_NAME")
    keyed("COLUMN_OPTIONS", "TABLE_NAME", "COLUMN_NAME", "OPTION_NAME")
    if not tables:
        fail("TABLES", "<default schema>", "no tables visible")
    for view in ("COLUMNS", "INDEXES", "TABLE_CONSTRAINTS"):
        for row in data[view]:
            if (row["TABLE_NAME"],) not in tables:
                fail(view, dict(row), "unknown TABLES table")
    for (table,) in tables:
        if not any(t == table for t, _ in columns):
            fail("COLUMNS", table, "table has no columns")
        if (table, "PRIMARY_KEY") not in indexes:
            fail("INDEXES", f"{table}/PRIMARY_KEY", "missing primary-key index")
        primary = [row for row in constraints.values() if row["TABLE_NAME"] == table and row["CONSTRAINT_TYPE"] == "PRIMARY KEY"]
        if len(primary) != 1:
            fail("TABLE_CONSTRAINTS", table, "expected one primary-key constraint")
        pk_name = primary[0]["CONSTRAINT_NAME"]
        pk_columns = {(row["COLUMN_NAME"], row["ORDINAL_POSITION"]) for row in keys.values() if row["CONSTRAINT_NAME"] == pk_name}
        indexed = {(row["COLUMN_NAME"], row["ORDINAL_POSITION"]) for row in index_columns.values() if row["TABLE_NAME"] == table and row["INDEX_NAME"] == "PRIMARY_KEY"}
        if not pk_columns:
            fail("KEY_COLUMN_USAGE", f"{table}/{pk_name}", "primary key has no columns")
        if pk_columns != indexed:
            fail("INDEX_COLUMNS/KEY_COLUMN_USAGE", f"{table}/PRIMARY_KEY", "primary-key columns disagree")
    for table, name in indexes:
        if not any(t == table and idx == name for t, idx, _ in index_columns):
            fail("INDEX_COLUMNS", f"{table}/{name}", "index has no columns")
    for view in ("INDEX_COLUMNS", "COLUMN_OPTIONS", "KEY_COLUMN_USAGE"):
        for row in data[view]:
            obj = f"{row['TABLE_NAME']}/{row['COLUMN_NAME']}"
            if (row["TABLE_NAME"], row["COLUMN_NAME"]) not in columns:
                fail(view, obj, "unknown table or column in COLUMNS")
            if view == "INDEX_COLUMNS" and (row["TABLE_NAME"], row["INDEX_NAME"]) not in indexes:
                fail(view, f"{obj}/{row['INDEX_NAME']}", "unknown INDEXES index")
            if view == "KEY_COLUMN_USAGE":
                constraint = constraints.get((row["CONSTRAINT_NAME"],))
                if constraint is None or constraint["TABLE_NAME"] != row["TABLE_NAME"]:
                    fail(view, f"{obj}/{row['CONSTRAINT_NAME']}", "missing or mismatched TABLE_CONSTRAINTS table")
    for (name,), row in constraints.items():
        obj = f"{row['TABLE_NAME']}/{name}"
        if row["CONSTRAINT_TYPE"] == "CHECK":
            clause = checks.get((name,), {}).get("CHECK_CLAUSE")
            if not isinstance(clause, str) or not clause.strip():
                fail("CHECK_CONSTRAINTS", obj, "missing check clause")
        if row["CONSTRAINT_TYPE"] == "FOREIGN KEY":
            if (name,) not in references:
                fail("REFERENTIAL_CONSTRAINTS", obj, "missing foreign-key reference")
            if not any(key[0] == name for key in keys):
                fail("KEY_COLUMN_USAGE", obj, "foreign key has no columns")
    for view, records in (("CHECK_CONSTRAINTS", checks), ("REFERENTIAL_CONSTRAINTS", references)):
        for (name,), row in records.items():
            constraint = constraints.get((name,))
            kind = "CHECK" if view == "CHECK_CONSTRAINTS" else "FOREIGN KEY"
            if constraint is None or constraint["CONSTRAINT_TYPE"] != kind:
                fail(view, name, "missing or mismatched TABLE_CONSTRAINTS constraint/table")
            if view == "REFERENTIAL_CONSTRAINTS":
                if row["UNIQUE_CONSTRAINT_SCHEMA"] != "":
                    fail(view, name, "referenced schema is outside the audited default schema")
                referenced = constraints.get((row["UNIQUE_CONSTRAINT_NAME"],))
                if referenced is None or referenced["CONSTRAINT_TYPE"] not in {"PRIMARY KEY", "UNIQUE"}:
                    fail(view, name, "missing or non-unique referenced TABLE_CONSTRAINTS key")
                foreign_columns = [key for key in keys.values() if key["CONSTRAINT_NAME"] == name]
                unique_columns = [key for key in keys.values() if key["CONSTRAINT_NAME"] == row["UNIQUE_CONSTRAINT_NAME"]]
                positions = list(range(1, len(unique_columns) + 1))
                # Count as well as set membership matters: duplicate, missing,
                # null, zero and out-of-range positions must all fail closed.
                for label, actual in (
                    ("referenced key", [key["ORDINAL_POSITION"] for key in unique_columns]),
                    ("foreign key", [key["ORDINAL_POSITION"] for key in foreign_columns]),
                    ("referenced mapping", [key["POSITION_IN_UNIQUE_CONSTRAINT"] for key in foreign_columns]),
                ):
                    if not positions or len(actual) != len(positions) or set(actual) != set(positions):
                        fail("KEY_COLUMN_USAGE", name, f"{label} must cover positions 1..{len(positions)} exactly once")


def normalise(rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> Schema:
    if set(rows) != set(PROJECTIONS):
        raise ValueError(f"Incomplete metadata views: {sorted(set(rows) ^ set(PROJECTIONS))}")
    data = {}
    for name, records in rows.items():
        fields = PROJECTIONS[name].split()
        data[name] = []
        for row in records:
            missing = set(fields) - row.keys()
            if missing:
                raise ValueError(f"{name} {dict(row)}: missing fields {sorted(missing)}")
            data[name].append({field: value(field, row[field]) for field in fields})
    validate_completeness(data)
    result: Schema = {}

    def add(key: str, row: Mapping[str, Any], omit: set[str]) -> None:
        if key in result:
            raise ValueError(f"Duplicate metadata object: {key}")
        result[key] = {k: v for k, v in row.items() if k not in omit}

    for row in data["TABLES"]:
        add(f"table/{row['TABLE_NAME']}", row, {"TABLE_NAME"})
    if not result:
        raise ValueError("No default-schema tables visible; cannot audit")
    for row in data["COLUMNS"]:
        add(f"column/{row['TABLE_NAME']}/{row['COLUMN_NAME']}", row, {"TABLE_NAME", "COLUMN_NAME"})
    for row in data["COLUMN_OPTIONS"]:
        if row["OPTION_NAME"] == "allow_commit_timestamp":
            key = f"column/{row['TABLE_NAME']}/{row['COLUMN_NAME']}"
            result[key]["allow_commit_timestamp"] = row["OPTION_VALUE"]
    for row in data["INDEXES"]:
        key = f"index/{row['TABLE_NAME']}/{row['INDEX_NAME']}"
        add(key, row, {"TABLE_NAME", "INDEX_NAME"})
        result[key]["columns"] = []
    for row in data["INDEX_COLUMNS"]:
        result[f"index/{row['TABLE_NAME']}/{row['INDEX_NAME']}"]["columns"].append(
            {k: v for k, v in row.items() if k not in {"TABLE_NAME", "INDEX_NAME"}}
        )
    # Primary-key constraint names may be server-generated. Referencing keys
    # must resolve to the same canonical identity on both databases.
    constraints = {
        row["CONSTRAINT_NAME"]: f"constraint/{row['TABLE_NAME']}/"
        + ("PRIMARY_KEY" if row["CONSTRAINT_TYPE"] == "PRIMARY KEY" else row["CONSTRAINT_NAME"])
        for row in data["TABLE_CONSTRAINTS"]
    }
    for row in data["TABLE_CONSTRAINTS"]:
        key = constraints[row["CONSTRAINT_NAME"]]
        add(key, row, {"TABLE_NAME", "CONSTRAINT_NAME"})
        result[key]["columns"] = []
    for row in data["CHECK_CONSTRAINTS"]:
        result[constraints[row["CONSTRAINT_NAME"]]]["CHECK_CLAUSE"] = row["CHECK_CLAUSE"]
        result[constraints[row["CONSTRAINT_NAME"]]]["SPANNER_STATE"] = row["SPANNER_STATE"]
    for row in data["KEY_COLUMN_USAGE"]:
        result[constraints[row["CONSTRAINT_NAME"]]]["columns"].append(
            {k: v for k, v in row.items() if k not in {"TABLE_NAME", "CONSTRAINT_NAME"}}
        )
    for row in data["REFERENTIAL_CONSTRAINTS"]:
        attrs = result[constraints[row["CONSTRAINT_NAME"]]]
        attrs.update({k: v for k, v in row.items() if k != "CONSTRAINT_NAME"})
        if row["UNIQUE_CONSTRAINT_SCHEMA"] == "":
            attrs["UNIQUE_CONSTRAINT_NAME"] = constraints[row["UNIQUE_CONSTRAINT_NAME"]]
    for attrs in result.values():
        if "columns" in attrs:
            attrs["columns"].sort(key=lambda col: (col["ORDINAL_POSITION"] is None, col["ORDINAL_POSITION"] or 0, col["COLUMN_NAME"]))
    return {key: dict(sorted(attrs.items())) for key, attrs in sorted(result.items())}


def read_schema(database: Any) -> Schema:
    rows = {}
    # multi_use is essential: otherwise each SELECT gets a different snapshot.
    with database.snapshot(multi_use=True) as snapshot:
        for name, query in QUERIES.items():
            fields = PROJECTIONS[name].split()
            rows[name] = [dict(zip(fields, row, strict=True)) for row in snapshot.execute_sql(query, timeout=60)]
    return normalise(rows)


def production_schema(project: str, instance: str, database: str) -> Schema:
    if "SPANNER_EMULATOR_HOST" in os.environ:
        raise ValueError("Production audit refuses SPANNER_EMULATOR_HOST; unset it and use --emulator-host")
    from google.cloud import spanner

    client = spanner.Client(project=project, disable_builtin_metrics=True)
    target = client.instance(instance).database(database)
    # This short-lived audit otherwise waits ten minutes in the pinned SDK's
    # close() thread join. Change only this disposable handle's polling cadence.
    target.sessions_manager._MAINTENANCE_THREAD_POLLING_INTERVAL = timedelta(milliseconds=100)
    try:
        return read_schema(target)
    finally:
        target.close()


def loopback_endpoint(endpoint: str) -> str:
    host, separator, port = endpoint.rpartition(":")
    if not separator or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("Expected loopback emulator host:port")
    # Numeric loopback only: no DNS resolution or hostname rebinding.
    if not ipaddress.ip_address(host.strip("[]")).is_loopback:
        raise ValueError("Emulator must be loopback")
    return endpoint


def expected_schema(endpoint: str) -> Schema:
    endpoint = loopback_endpoint(endpoint)
    if os.environ.get("SPANNER_EMULATOR_HOST") != endpoint:
        raise ValueError("Expected-side reader requires its isolated emulator environment")
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import spanner
    from google.cloud.spanner_v1.database_sessions_manager import DatabaseSessionsManager

    from tests.conformance.spanner_ddl import DDL

    # Bound emulator SDK teardown, matching the conformance harness. This patch
    # exists only in the child, never in the process holding production ADC.
    with patch.object(DatabaseSessionsManager, "_MAINTENANCE_THREAD_POLLING_INTERVAL", timedelta(milliseconds=100)):
        project = "tr-schema-audit"
        client = spanner.Client(project=project, credentials=AnonymousCredentials(), disable_builtin_metrics=True)
        instance = client.instance("audit-" + uuid4().hex[:12], configuration_name=f"projects/{project}/instanceConfigs/emulator-config")
        target = None
        try:
            instance.create().result(timeout=60)
            target = instance.database("fixture", ddl_statements=DDL[:20])
            target.create().result(timeout=120)
            for offset in range(20, len(DDL), 20):
                target.update_ddl(DDL[offset:offset + 20]).result(timeout=120)
            return read_schema(target)
        finally:
            try:
                if target is not None:
                    try:
                        target.close()
                    finally:
                        target.drop()
            finally:
                instance.delete()


def fixture_schema(endpoint: str) -> Schema:
    endpoint = loopback_endpoint(endpoint)
    child_env = dict(os.environ, SPANNER_EMULATOR_HOST=endpoint)
    child_env.pop("GITHUB_STEP_SUMMARY", None)
    # Only this process receives the emulator setting. Never change os.environ
    # in the ADC/production process, even temporarily.
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed module, no shell
            [sys.executable, "-m", "scripts.audit_spanner_schema", "--expected-only", "--emulator-host", endpoint],
            cwd=ROOT, env=child_env, check=True, capture_output=True, text=True, timeout=900,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Expected-side reader failed: {exc.stdout} {exc.stderr}") from exc
    parsed = json.loads(completed.stdout)
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("Expected-side subprocess returned no schema")
    return parsed


def comparison_value(attribute: str, raw: Any) -> Any:
    if attribute in EXPRESSION_FIELDS and raw is not None:
        return normalise_expression(raw)
    if attribute == "object" and isinstance(raw, dict):
        return {key: comparison_value(key, val) for key, val in raw.items()}
    return raw


def differences(production: Schema, fixture: Schema) -> list[dict[str, Any]]:
    result = []
    # Readiness is a production property even when both servers return the same
    # transitional state, or an object exists only in production.
    for key, attrs in sorted(production.items()):
        for field, final in STATE_FIELDS.items():
            # PRIMARY_KEY is a metadata pseudo-index, not a backfilled index.
            if (field == "INDEX_STATE" and attrs.get(field) is None
                    and key.startswith("index/") and key.endswith("/PRIMARY_KEY")
                    and attrs.get("INDEX_TYPE") == "PRIMARY_KEY"):
                continue
            if field in attrs and attrs[field] != final:
                result.append(dict(object=key, attribute=field, production=attrs[field], fixture=fixture.get(key, {}).get(field), direction=NOT_READY, required_state=final))
    for key in sorted(production.keys() | fixture.keys()):
        if key not in production:
            result.append(dict(object=key, attribute="object", production=None, fixture=fixture[key], direction="FIXTURE-HAS / PRODUCTION-LACKS"))
        elif key not in fixture:
            result.append(dict(object=key, attribute="object", production=production[key], fixture=None, direction="PRODUCTION-HAS / FIXTURE-LACKS"))
        else:
            for attr in sorted(production[key].keys() | fixture[key].keys()):
                if attr in STATE_FIELDS:
                    continue
                # Named table columns can be added in different migration order.
                # Index/constraint key order lives in `columns` and stays strict.
                if key.startswith("column/") and attr == "ORDINAL_POSITION":
                    continue
                prod, expected = production[key].get(attr), fixture[key].get(attr)
                if comparison_value(attr, prod) != comparison_value(attr, expected):
                    result.append(dict(object=key, attribute=attr, production=prod, fixture=expected, direction="ATTRIBUTE-MISMATCH"))
    for diff in result:
        if diff["attribute"] in EXPRESSION_FIELDS or diff["attribute"] == "object":
            for side in ("production", "fixture"):
                diff[side + "_normalised"] = comparison_value(diff["attribute"], diff[side])
    order = {"FIXTURE-HAS / PRODUCTION-LACKS": 0, "PRODUCTION-HAS / FIXTURE-LACKS": 1, NOT_READY: 2, "ATTRIBUTE-MISMATCH": 3}
    return sorted(result, key=lambda item: (order[item["direction"]], item["object"], item["attribute"]))


def exact_value(left: Any, right: Any) -> bool:
    # Python equality considers True == 1; reviewed JSON values must match types.
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def apply_allowlist(diffs: list[dict[str, Any]], entries: Any) -> dict[str, Any]:
    if not isinstance(entries, list):
        raise ValueError("Allowlist must be a list")
    seen = set()
    for entry in entries:
        required = {"object", "attribute", "production", "fixture", "reason", "verified_against_production"}
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError("Invalid allowlist entry fields")
        identity = (entry["object"], entry["attribute"])
        if not all(isinstance(entry[field], str) and entry[field].strip() for field in ("object", "attribute", "reason")) or type(entry["verified_against_production"]) is not bool:
            raise ValueError("Allowlist requires an object, attribute, reason and verification boolean")
        if identity in seen or exact_value(entry["production"], entry["fixture"]):
            raise ValueError(f"Duplicate or non-difference allowlist entry: {identity}")
        seen.add(identity)
    matched = set()
    report_rows = []
    for diff in diffs:
        match = next((i for i, entry in enumerate(entries) if all(exact_value(entry[field], diff[field]) for field in ("object", "attribute", "production", "fixture"))), None)
        row = dict(diff, status="UNEXPLAINED", reason="")
        if match is not None:
            matched.add(match)
            row.update(status="ALLOWLISTED", reason=entries[match]["reason"], verified_against_production=entries[match]["verified_against_production"])
        report_rows.append(row)
    stale = [entry for i, entry in enumerate(entries) if i not in matched]
    unexplained = sum(row["status"] == "UNEXPLAINED" for row in report_rows)
    unverified = sum(row.get("verified_against_production") is False for row in report_rows)
    return dict(exit_code=1 if unexplained or stale else 0, differences=report_rows, stale=stale,
                unexplained=unexplained, allowlisted=len(matched), unverified=unverified)


def summary(report: Mapping[str, Any]) -> str:
    if report["exit_code"] == 2:
        return f"CANNOT AUDIT: {report['error']}"
    return (f"Spanner schema audit: unexplained={report['unexplained']} "
            f"allowlisted={report['allowlisted']} stale={len(report['stale'])} "
            f"unverified={report['unverified']} exit={report['exit_code']}")


def markdown(report: Mapping[str, Any]) -> str:
    def cell(item: Any) -> str:
        text = item if isinstance(item, str) else json.dumps(item, sort_keys=True)
        return html.escape(text).replace("|", "&#124;").replace("\n", "<br>")

    lines = ["## Spanner schema audit", "", cell(summary(report)), "",
             "| Status / direction | Object | Attribute | Production | Fixture | Reason |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in report.get("differences", []):
        lines.append("| " + " | ".join(cell(part) for part in (row["status"] + " / " + row["direction"], row["object"], row["attribute"], reported_value(row, "production"), reported_value(row, "fixture"), row["reason"])) + " |")
    for entry in report.get("stale", []):
        lines.append("| " + " | ".join(cell(part) for part in ("STALE", entry["object"], entry["attribute"], entry["production"], entry["fixture"], entry["reason"])) + " |")
    return "\n".join(lines) + "\n"


def reported_value(row: Mapping[str, Any], side: str) -> Any:
    if side + "_normalised" in row:
        return {"raw": row[side], "normalised": row[side + "_normalised"]}
    return row[side]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.getenv("TR_GCP_PROJECT_ID", "quill-cloud-proxy"))
    parser.add_argument("--instance", default=os.getenv("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6"))
    parser.add_argument("--database", default=os.getenv("TR_SPANNER_DATABASE_ID", "trusted-router"))
    parser.add_argument("--emulator-host", default=os.getenv("TR_SCHEMA_AUDIT_EMULATOR_HOST", "127.0.0.1:9010"))
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--expected-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.expected_only:
            print(json.dumps(expected_schema(args.emulator_host), sort_keys=True))
            return 0
        production = production_schema(args.project, args.instance, args.database)
        fixture = fixture_schema(args.emulator_host)
        report = apply_allowlist(differences(production, fixture), json.loads(args.allowlist.read_text()))
        report["target"] = f"projects/{args.project}/instances/{args.instance}/databases/{args.database}"
    except Exception as exc:
        report = dict(exit_code=2, error=str(exc))
    try:
        if path := os.getenv("GITHUB_STEP_SUMMARY"):
            with Path(path).open("a", encoding="utf-8") as output:
                output.write(markdown(report))
    except OSError as exc:
        report = dict(exit_code=2, error=f"Cannot write step summary: {exc}")
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(summary(report))
        for row in report.get("differences", []):
            print(f"{row['status']} {row['direction']} {row['object']} {row['attribute']}: production={json.dumps(reported_value(row, 'production'), sort_keys=True)} fixture={json.dumps(reported_value(row, 'fixture'), sort_keys=True)} {row['reason']}")
        for entry in report.get("stale", []):
            print(f"STALE {entry['object']} {entry['attribute']}: {entry['reason']}")
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
