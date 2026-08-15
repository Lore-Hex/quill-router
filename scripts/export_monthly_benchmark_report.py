#!/usr/bin/env python3
"""Export one monthly benchmark report from retained Bigtable metadata."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from google.cloud import bigtable

from trusted_router.monthly_benchmarks import MonthlyBenchmarkAccumulator
from trusted_router.storage_models import ProviderBenchmarkSample


def _sample(row: Any) -> ProviderBenchmarkSample | None:
    for family in ("benchmark", "m"):
        cells = row.cells.get(family, {}).get(b"body", [])
        if cells:
            raw = json.loads(cells[0].value.decode("utf-8"))
            allowed = {field.name for field in dataclasses.fields(ProviderBenchmarkSample)}
            return ProviderBenchmarkSample(
                **{key: value for key, value in raw.items() if key in allowed}
            )
    return None


def export_month(
    month: str,
    *,
    project: str,
    instance: str,
    table_name: str,
) -> dict[str, Any]:
    client = bigtable.Client(project=project, admin=False)
    table = client.instance(instance).table(table_name)
    prefix = f"benchmark#{month}".encode()
    accumulator = MonthlyBenchmarkAccumulator(month)
    for index, row in enumerate(
        table.read_rows(start_key=prefix, end_key=prefix + b"~"),
        start=1,
    ):
        sample = _sample(row)
        if sample is not None:
            accumulator.add(sample)
        if index % 100_000 == 0:
            print(f"{month}: scanned {index:,} rows", file=sys.stderr)
    return accumulator.report()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("month", nargs="+", help="UTC month in YYYY-MM format")
    parser.add_argument("--project", default="quill-cloud-proxy")
    parser.add_argument("--instance", default="trusted-router-logs")
    parser.add_argument("--table", default="trustedrouter-generations")
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the report JSON to this file instead of stdout.",
    )
    args = parser.parse_args()
    reports = [
        export_month(
            month,
            project=args.project,
            instance=args.instance,
            table_name=args.table,
        )
        for month in args.month
    ]
    payload = json.dumps(reports, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
