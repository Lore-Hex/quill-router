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
    "COLUMNS": "TABLE_NAME COLUMN_NAME ORDINAL_POSITION SPANNER_TYPE IS_NULLABLE IS_GENERATED GENERATION_EXPRESSION IS_STORED COLUMN_DEFAULT",
    "COLUMN_OPTIONS": "TABLE_NAME COLUMN_NAME OPTION_NAME OPTION_TYPE OPTION_VALUE",
    "INDEXES": "TABLE_NAME INDEX_NAME INDEX_TYPE IS_UNIQUE IS_NULL_FILTERED INDEX_STATE PARENT_TABLE_NAME",
    "INDEX_COLUMNS": "TABLE_NAME INDEX_NAME COLUMN_NAME ORDINAL_POSITION COLUMN_ORDERING",
    "TABLE_CONSTRAINTS": "TABLE_NAME CONSTRAINT_NAME CONSTRAINT_TYPE IS_DEFERRABLE INITIALLY_DEFERRED ENFORCED",
    "CHECK_CONSTRAINTS": "CONSTRAINT_NAME CHECK_CLAUSE",
    "KEY_COLUMN_USAGE": "TABLE_NAME CONSTRAINT_NAME COLUMN_NAME ORDINAL_POSITION POSITION_IN_UNIQUE_CONSTRAINT",
    "REFERENTIAL_CONSTRAINTS": "CONSTRAINT_NAME UNIQUE_CONSTRAINT_SCHEMA UNIQUE_CONSTRAINT_NAME MATCH_OPTION UPDATE_RULE DELETE_RULE",
}
QUERIES = {
    name: f"SELECT {', '.join(projection.split())} FROM INFORMATION_SCHEMA.{name} "  # noqa: S608 -- fixed metadata projections only
    f"WHERE {'CONSTRAINT_SCHEMA' if name in {'CHECK_CONSTRAINTS', 'REFERENTIAL_CONSTRAINTS'} else 'TABLE_SCHEMA'} = ''"
    for name, projection in PROJECTIONS.items()
}
BOOL_FIELDS = {"IS_NULLABLE", "IS_STORED", "IS_UNIQUE", "IS_NULL_FILTERED", "IS_DEFERRABLE", "INITIALLY_DEFERRED", "ENFORCED"}


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
    # Expressions deliberately remain byte-exact: collapsing spaces inside a
    # SQL string literal can hide real check/generation/default changes.
    return raw


def normalise(rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> Schema:
    if set(rows) != set(PROJECTIONS):
        raise ValueError("Incomplete metadata read")
    data = {}
    for name, records in rows.items():
        fields = PROJECTIONS[name].split()
        data[name] = [{field: value(field, row[field]) for field in fields} for row in records]
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


def differences(production: Schema, fixture: Schema) -> list[dict[str, Any]]:
    result = []
    for key in sorted(production.keys() | fixture.keys()):
        if key not in production:
            result.append(dict(object=key, attribute="object", production=None, fixture=fixture[key], direction="FIXTURE-HAS / PRODUCTION-LACKS"))
        elif key not in fixture:
            result.append(dict(object=key, attribute="object", production=production[key], fixture=None, direction="PRODUCTION-HAS / FIXTURE-LACKS"))
        else:
            for attr in sorted(production[key].keys() | fixture[key].keys()):
                prod, expected = production[key].get(attr), fixture[key].get(attr)
                if prod != expected:
                    result.append(dict(object=key, attribute=attr, production=prod, fixture=expected, direction="ATTRIBUTE-MISMATCH"))
    order = {"FIXTURE-HAS / PRODUCTION-LACKS": 0, "PRODUCTION-HAS / FIXTURE-LACKS": 1, "ATTRIBUTE-MISMATCH": 2}
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
        lines.append("| " + " | ".join(cell(part) for part in (row["status"] + " / " + row["direction"], row["object"], row["attribute"], row["production"], row["fixture"], row["reason"])) + " |")
    for entry in report.get("stale", []):
        lines.append("| " + " | ".join(cell(part) for part in ("STALE", entry["object"], entry["attribute"], entry["production"], entry["fixture"], entry["reason"])) + " |")
    return "\n".join(lines) + "\n"


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
            print(f"{row['status']} {row['direction']} {row['object']} {row['attribute']}: production={json.dumps(row['production'], sort_keys=True)} fixture={json.dumps(row['fixture'], sort_keys=True)} {row['reason']}")
        for entry in report.get("stale", []):
            print(f"STALE {entry['object']} {entry['attribute']}: {entry['reason']}")
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
