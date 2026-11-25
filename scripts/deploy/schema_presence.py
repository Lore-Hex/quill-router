"""Batch live INFORMATION_SCHEMA presence checks, never application reads."""

import json
import re
import subprocess
import sys


def snapshot(database: str, instance: str, project: str, tables: list[str]) -> str:
    if not tables or not all(re.fullmatch(r"[a-z_][a-z_0-9]*", t) for t in tables):
        raise ValueError("invalid metadata scope")
    names = ",".join(f"'{t}'" for t in tables)
    sql = f"""SELECT c.table_name, c.column_name FROM INFORMATION_SCHEMA.COLUMNS AS c
              JOIN INFORMATION_SCHEMA.TABLES AS t USING (table_catalog, table_schema, table_name)
              WHERE c.table_schema='' AND c.table_name IN ({names})
                AND t.table_type='BASE TABLE' AND t.spanner_state='COMMITTED'
                AND c.spanner_state='COMMITTED'"""  # noqa: S608 - validated identifiers only
    args = ["gcloud", "spanner", "databases", "execute-sql", database, "--instance", instance,
            "--format=json", "--sql", sql]
    if project:
        args.extend(["--project", project])
    result = subprocess.run(args, check=True, capture_output=True, timeout=15)  # noqa: S603
    rows = json.loads(result.stdout)["rows"]
    if not isinstance(rows, list) or len(rows) > 4096:
        raise ValueError("unexpected schema metadata")
    found = set()
    for row in rows:
        if (not isinstance(row, list) or len(row) != 2 or row[0] not in tables
                or not re.fullmatch(r"[a-z_][a-z_0-9]*", row[1])):
            raise ValueError("unexpected schema metadata row")
        found.update([row[0], ".".join(row)])
    return "|" + "|".join(sorted(found)) + "|"


if __name__ == "__main__":
    try:
        print(snapshot(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]))
    except (IndexError, KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        print("Schema snapshot unavailable; using individual checks.", file=sys.stderr)
        sys.exit(1)
