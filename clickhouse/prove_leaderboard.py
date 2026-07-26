"""Proof: the leaderboard/route-health aggregates that TrustedRouter computes
by scanning Bigtable and json.loads-ing every row in Python can be computed in
ClickHouse SQL, and the numbers are identical.

Method
------
1. Read real ProviderBenchmarkSample rows out of production Bigtable
   (read-only, the same prefix scan the app uses).
2. Compute route health in Python EXACTLY the way
   `synthetic/route_health.py` does today: filter to synthetic, drop
   `unsupported`, drop transient failures, then failures/samples per route.
   This is the baseline — the current production answer.
3. Load the same rows into ClickHouse.
4. Compute the same thing in one SQL statement.
5. Assert the two agree, route by route.

A match means the columnar engine reproduces production semantics, so the
Python scan layer can be retired rather than reimplemented.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from collections import defaultdict

import httpx
from google.cloud import bigtable
from google.cloud.bigtable.row_set import RowSet

PROJECT = "quill-cloud-proxy"
INSTANCE = "trusted-router-logs"
TABLE = "trustedrouter-generations"
FAMILY = "m"
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "40000"))

CH_URL = os.environ.get("CH_URL", "http://localhost:18123/")
CH_AUTH = (os.environ.get("CH_USER", "tr"), os.environ.get("CH_PASSWORD", "tr"))

# Mirrors synthetic/route_health.py exactly.
TRANSIENT_TYPES = {
    "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
    "ConnectError", "ReadError", "WriteError", "RemoteProtocolError",
}
TRANSIENT_STATUSES = {429, 500, 502, 503, 504, 529}


def ch(query: str, body: bytes | None = None) -> str:
    """Run a ClickHouse statement over the HTTP interface.

    With `body`, the query goes in the querystring and the body carries rows
    (that is the shape ClickHouse wants for `INSERT ... FORMAT JSONEachRow`).
    Without it, the statement itself is the body.
    """
    if body is not None:
        response = httpx.post(
            CH_URL, params={"query": query}, content=body, auth=CH_AUTH, timeout=300
        )
    else:
        response = httpx.post(CH_URL, content=query.encode(), auth=CH_AUTH, timeout=300)
    response.raise_for_status()
    return response.text


def fetch_bigtable_samples() -> list[dict]:
    client = bigtable.Client(project=PROJECT, admin=False)
    table = client.instance(INSTANCE).table(TABLE)
    rs = RowSet()
    rs.add_row_range_with_prefix("benchmark_recent#")
    rows = []
    for i, row in enumerate(table.read_rows(row_set=rs)):
        if i >= SCAN_LIMIT:
            break
        cells = row.cells.get(FAMILY, {}).get(b"body", [])
        if not cells:
            continue
        try:
            rows.append(json.loads(cells[0].value.decode("utf-8")))
        except ValueError:
            continue
    return rows


def python_route_health(samples: list[dict]) -> dict[tuple[str, str], tuple[int, int]]:
    """The current production computation: scan, parse, loop in Python."""
    acc: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for b in samples:
        if b.get("source") != "synthetic":
            continue
        status = b.get("status")
        if status == "unsupported" or status not in {"error", "success"}:
            continue
        if status == "error" and (
            b.get("error_status") in TRANSIENT_STATUSES
            or b.get("error_type") in TRANSIENT_TYPES
        ):
            continue
        key = (b.get("provider"), b.get("model"))
        acc[key][0] += 1
        if status == "error":
            acc[key][1] += 1
    return {k: (v[0], v[1]) for k, v in acc.items()}


def to_ch_row(b: dict) -> dict:
    created = b.get("created_at", "")
    try:
        t = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.UTC)
        created_fmt = t.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except ValueError:
        return {}
    return {
        "id": str(b.get("id", "")),
        "created_at": created_fmt,
        "provider": str(b.get("provider") or ""),
        "model": str(b.get("model") or ""),
        "provider_name": str(b.get("provider_name") or ""),
        "status": str(b.get("status") or ""),
        "usage_type": str(b.get("usage_type") or ""),
        "source": str(b.get("source") or ""),
        "streamed": 1 if b.get("streamed") else 0,
        "input_tokens": int(b.get("input_tokens") or 0),
        "output_tokens": int(b.get("output_tokens") or 0),
        "total_cost_microdollars": int(b.get("total_cost_microdollars") or 0),
        "speed_tokens_per_second": b.get("speed_tokens_per_second"),
        "elapsed_milliseconds": b.get("elapsed_milliseconds"),
        "first_token_milliseconds": b.get("first_token_milliseconds"),
        "ttfb_milliseconds": b.get("ttfb_milliseconds"),
        "finish_reason": b.get("finish_reason"),
        "error_type": b.get("error_type"),
        "error_status": b.get("error_status"),
        "error_message": b.get("error_message"),
        "region": b.get("region"),
        "app": str(b.get("app") or ""),
    }


SQL_ROUTE_HEALTH = """
SELECT provider, model,
       count()                       AS samples,
       countIf(status = 'error')     AS failures
FROM provider_benchmark_samples
WHERE source = 'synthetic'
  AND status IN ('error', 'success')
  -- ifNull() is load-bearing. error_status/error_type are Nullable, and in
  -- SQL's three-valued logic `NULL IN (...)` is NULL, not false. That NULL
  -- propagates through OR/AND, survives NOT, and WHERE then DROPS the row —
  -- silently under-counting failures on exactly the routes whose errors carry
  -- no HTTP status (empty_stream, client-side aborts). Python's
  -- `None in {...}` is plainly False, so the two disagree unless NULLs are
  -- flattened first. This cost 6 routes on the first run of this proof.
  AND NOT (status = 'error' AND (
        ifNull(error_status, 0) IN (429,500,502,503,504,529)
     OR ifNull(error_type, '') IN ('ReadTimeout','ConnectTimeout','WriteTimeout','PoolTimeout',
                       'ConnectError','ReadError','WriteError','RemoteProtocolError')))
GROUP BY provider, model
FORMAT JSONEachRow
"""


def main() -> int:
    print(f"Scanning Bigtable (limit {SCAN_LIMIT})...")
    samples = fetch_bigtable_samples()
    print(f"  fetched {len(samples)} rows")
    if not samples:
        print("no rows; aborting")
        return 1

    print("Computing route health the CURRENT way (Python scan + json parse)...")
    baseline = python_route_health(samples)
    print(f"  {len(baseline)} routes")

    print("Loading into ClickHouse...")
    ch("TRUNCATE TABLE provider_benchmark_samples")
    payload = "\n".join(
        json.dumps(r) for r in (to_ch_row(b) for b in samples) if r
    ).encode()
    ch("INSERT INTO provider_benchmark_samples FORMAT JSONEachRow", payload)
    loaded = ch("SELECT count() FROM provider_benchmark_samples").strip()
    print(f"  loaded {loaded} rows")

    print("Computing the SAME thing in SQL...")
    out = ch(SQL_ROUTE_HEALTH)
    sql_result = {}
    for line in out.strip().splitlines():
        r = json.loads(line)
        sql_result[(r["provider"], r["model"])] = (int(r["samples"]), int(r["failures"]))
    print(f"  {len(sql_result)} routes")

    print("\nComparing...")
    mismatches = []
    for key in sorted(set(baseline) | set(sql_result), key=lambda k: (k[0] or "", k[1] or "")):
        py = baseline.get(key)
        sq = sql_result.get(key)
        if py != sq:
            mismatches.append((key, py, sq))

    if mismatches:
        print(f"MISMATCH on {len(mismatches)} route(s):")
        for key, py, sq in mismatches[:20]:
            print(f"  {key[0]}/{key[1]}: python={py} sql={sq}")
        return 1

    print(f"EXACT MATCH on all {len(baseline)} routes.")
    print("\nTop failing routes (from SQL, one query, no Python loop):")
    top = ch("""
        SELECT provider, model,
               count() AS samples,
               round(100 * countIf(status='error') / count(), 1) AS failure_pct,
               round(quantile(0.95)(elapsed_milliseconds)) AS p95_ms
        FROM provider_benchmark_samples
        WHERE source='synthetic' AND status IN ('error','success')
        GROUP BY provider, model
        HAVING samples >= 6
        ORDER BY failure_pct DESC, samples DESC
        LIMIT 12
        FORMAT PrettyCompactMonoBlock
    """)
    print(top)

    print("Materialized view (rollups maintained incrementally, no backfill job):")
    print(ch("""
        SELECT provider, model,
               countMerge(samples_state)   AS samples,
               countIfMerge(failures_state) AS failures,
               round(quantileMerge(0.95)(p95_elapsed_state)) AS p95_ms
        FROM route_health_hourly
        GROUP BY provider, model
        ORDER BY samples DESC
        LIMIT 8
        FORMAT PrettyCompactMonoBlock
    """))
    return 0


if __name__ == "__main__":
    sys.exit(main())
