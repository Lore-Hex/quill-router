"""Refuse to install workers on a cluster node whose tables would not replicate.

Every canonical table on the GCP cluster is ``Replicated*``. The installer
still applies the single-node schema files ``001``/``002`` because ``002``
also creates the node-local ``_staging`` tables the rollup jobs need. On a
freshly rebuilt node their ``CREATE TABLE IF NOT EXISTS`` would instead create
non-replicated canonical tables, and the drain would then write rows that never
reach the other replicas (docs/design/clickhouse-high-availability.md, G7).

Run this before those files. It passes only when every required table already
exists with a ``Replicated*`` engine, and no other MergeTree-family table in the
database is non-replicated except the node-local ``_staging`` and
``_local_backup`` tables.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable, Mapping

from clickhouse.local_clickhouse import ClickHouse

NODE_LOCAL_SUFFIXES = ("_staging", "_local_backup")
TABLES_SQL = (
    "SELECT name, engine FROM system.tables "
    "WHERE database = currentDatabase() ORDER BY name FORMAT TabSeparated"
)


def parse_tables(tsv: str) -> dict[str, str]:
    tables: dict[str, str] = {}
    for line in tsv.splitlines():
        if not line:
            continue
        name, separator, engine = line.partition("\t")
        if not separator or not name or not engine:
            raise ValueError(f"unexpected system.tables row: {line!r}")
        tables[name] = engine
    return tables


def replication_problems(tables: Mapping[str, str], required: Iterable[str]) -> list[str]:
    """One line per table that is missing or would not replicate; empty when safe."""
    required = list(required)
    problems: list[str] = []
    for name in required:
        engine = tables.get(name)
        if engine is None:
            problems.append(f"{name}: missing")
        elif not engine.startswith("Replicated"):
            problems.append(f"{name}: {engine}, not replicated")
    for name, engine in sorted(tables.items()):
        if name in required or name.endswith(NODE_LOCAL_SUFFIXES):
            continue
        if engine.endswith("MergeTree") and not engine.startswith("Replicated"):
            problems.append(f"{name}: {engine}, not replicated")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("required", nargs="+", help="tables that must exist and replicate")
    args = parser.parse_args(argv)
    password = os.environ.get("CH_PASSWORD", "")
    if not password:
        raise SystemExit("CH_PASSWORD is required")
    tables = parse_tables(ClickHouse(password=password).query(TABLES_SQL))
    problems = replication_problems(tables, args.required)
    if problems:
        print(
            "refusing: this node's tables would not replicate. Create each missing "
            "table as a replica first: take SHOW CREATE TABLE from a healthy replica "
            "and run it as CREATE TABLE IF NOT EXISTS ... ON CLUSTER trustedrouter. "
            "Then rerun this installer:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"all {len(args.required)} required tables replicate; no other table is unreplicated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
