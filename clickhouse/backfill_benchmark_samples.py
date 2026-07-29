"""Backfill ProviderBenchmarkSample rows from Bigtable into ClickHouse.

Runs ON the ClickHouse node: the VM has cloud-platform scope so it can read
Bigtable directly, and ClickHouse is on localhost — so this needs no tunnel and
no inbound path to the database.

Scope (deliberately narrow, per docs/storage-portability/analytics-ingestion.md):

* This is the HISTORICAL/RECONCILIATION source. It is NOT the live CDC
  mechanism — the Bigtable row key is derived from `created_at`, not commit
  time, so a high-water-mark cursor over it would permanently miss rows that
  commit late. Live ingestion needs a durable outbox or Change Streams.
* Ingestion is AT-LEAST-ONCE. Re-running re-inserts rows. The table is
  ReplacingMergeTree, which collapses on merge, so `FINAL` (or a GROUP BY) is
  required for exact counts until a merge happens. There is deliberately no
  AggregatingMergeTree view attached: an MV would double-count on every replay
  because it processes each inserted block before replacement.

Verification prints per-day counts from BOTH sides so a mismatch is visible
immediately rather than after a cutover.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import struct
import subprocess
from collections import Counter
from typing import Any

from google.cloud import bigtable
from google.cloud.bigtable.row_set import RowSet

PROJECT = "quill-cloud-proxy"
INSTANCE = "trusted-router-logs"
TABLE = "trustedrouter-generations"
FAMILIES = ("benchmark", "m")
CH_DB = "tr"
CH_TABLE = "provider_benchmark_samples"


def ch(sql: str, stdin: bytes | None = None) -> str:
    """Run a query through clickhouse-client on localhost."""
    password = os.environ["CH_PASSWORD"]
    cmd = [
        "clickhouse-client",
        "--user",
        "tr",
        "--password",
        password,
        "--database",
        CH_DB,
        "--query",
        sql,
    ]
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, callers pass literal SQL
        cmd, input=stdin, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise SystemExit(f"clickhouse error: {result.stderr.decode()[:400]}")
    return result.stdout.decode()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        # The ClickHouse column is Float32. Round here so backfill, outbox
        # ingestion, and reconciliation fingerprint the same stored value.
        return struct.unpack("!f", struct.pack("!f", parsed))[0]
    except (OverflowError, TypeError, ValueError):
        return None


def normalise(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Canonical row shape. Shared coercions matter: a historical string
    "429" must become the same UInt16 the query path compares against, or the
    two disagree by construction."""
    created = raw.get("created_at") or ""
    try:
        parsed = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    parsed = parsed.astimezone(dt.UTC)

    status_code = _optional_int(raw.get("error_status"))

    provider, model = raw.get("provider"), raw.get("model")
    if not provider or not model:
        return None

    return {
        "id": str(raw.get("id") or ""),
        "created_at": parsed.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "provider": str(provider),
        "model": str(model),
        "provider_name": str(raw.get("provider_name") or ""),
        "status": str(raw.get("status") or ""),
        "usage_type": str(raw.get("usage_type") or ""),
        "source": str(raw.get("source") or ""),
        "streamed": 1 if raw.get("streamed") else 0,
        "input_tokens": int(raw.get("input_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or 0),
        "total_cost_microdollars": int(raw.get("total_cost_microdollars") or 0),
        "speed_tokens_per_second": _optional_float(raw.get("speed_tokens_per_second")),
        "elapsed_milliseconds": _optional_int(raw.get("elapsed_milliseconds")),
        "first_token_milliseconds": _optional_int(raw.get("first_token_milliseconds")),
        "ttfb_milliseconds": _optional_int(raw.get("ttfb_milliseconds")),
        "finish_reason": raw.get("finish_reason"),
        "error_type": raw.get("error_type"),
        "error_status": status_code,
        "error_message": raw.get("error_message"),
        "region": raw.get("region"),
        "app": str(raw.get("app") or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200_000)
    parser.add_argument("--batch", type=int, default=20_000)
    args = parser.parse_args()

    client = bigtable.Client(project=PROJECT, admin=False)
    table = client.instance(INSTANCE).table(TABLE)
    rs = RowSet()
    rs.add_row_range_with_prefix("benchmark_recent#")

    source_days: Counter[str] = Counter()
    batch: list[dict] = []
    read = written = skipped = 0

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        payload = "\n".join(json.dumps(r) for r in batch).encode()
        ch(f"INSERT INTO {CH_TABLE} FORMAT JSONEachRow", stdin=payload)
        batch = []

    print(f"scanning Bigtable (limit {args.limit})...", flush=True)
    for index, row in enumerate(table.read_rows(row_set=rs)):
        if index >= args.limit:
            break
        cells = []
        for family in FAMILIES:
            cells = row.cells.get(family, {}).get(b"body", [])
            if cells:
                break
        if not cells:
            continue
        try:
            raw = json.loads(cells[0].value.decode("utf-8"))
        except ValueError:
            continue
        read += 1
        record = normalise(raw)
        if record is None:
            skipped += 1
            continue
        source_days[record["created_at"][:10]] += 1
        batch.append(record)
        written += 1
        if len(batch) >= args.batch:
            flush()
            print(f"  {written} rows...", flush=True)
    flush()

    print(f"\nread={read} written={written} skipped_unparseable={skipped}")

    # Verification: per-day counts from BOTH sides. FINAL because
    # ReplacingMergeTree only collapses on merge, so a re-run would otherwise
    # read high until merges catch up.
    print("\nday          bigtable   clickhouse   delta")
    ch_rows = ch(
        f"SELECT toDate(created_at) AS d, count() FROM {CH_TABLE} FINAL "  # noqa: S608 - module constant
        "GROUP BY d ORDER BY d DESC FORMAT TSV"
    )
    ch_days = {line.split("\t")[0]: int(line.split("\t")[1])
               for line in ch_rows.strip().splitlines() if "\t" in line}
    mismatches = 0
    for day in sorted(set(source_days) | set(ch_days), reverse=True)[:14]:
        src, dst = source_days.get(day, 0), ch_days.get(day, 0)
        flag = "" if src == dst else "  <-- MISMATCH"
        if src != dst:
            mismatches += 1
        print(f"{day}   {src:>8}   {dst:>10}   {dst - src:>5}{flag}")
    print(f"\n{'OK: every scanned day matches' if not mismatches else f'{mismatches} day(s) differ'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
