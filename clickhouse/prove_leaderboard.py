"""Differential proof: route-health evaluation in ClickHouse SQL agrees with
the Python implementation that runs in production today.

What this DOES prove
--------------------
Given an identical set of input rows, the SQL translation of
`synthetic/route_health.py` produces the same per-route sample/failure counts
and the same set of FLAGGED routes as the Python original — including the
48-hour cutoff, the newest-N-per-route window, the synthetic-only filter, the
`unsupported` exclusion, the transient-failure exclusion, and the
min-samples / failure-rate thresholds.

That is the risky part of the migration: the predicate and aggregation logic.
It is where the NULL-handling bug (see below) lived.

What this does NOT prove
------------------------
* It does not exercise the full public leaderboard aggregator
  (`synthetic/leaderboard.py`), which additionally blends organic traffic,
  computes exact nearest-rank percentiles, and ranks providers. ClickHouse's
  `quantile()` is approximate and has NOT been reconciled against that exact
  implementation.
* It compares two computations over the SAME slice of rows. It is not a test
  of the ingestion path.

To keep the first point honest, the script ASSERTS that the scanned slice
fully covers the evaluation window (see `assert_window_covered`) — otherwise
both sides could agree perfectly on a biased sample while disagreeing with
production.

Safety
------
`--load` TRUNCATEs the local analytics table and rebuilds the materialized
view. This script is a LOCAL PROOF HARNESS. Do not point it at a production
ClickHouse: use `--no-load` to compare against already-present data.
"""

from __future__ import annotations

import argparse
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

CH_URL = os.environ.get("CH_URL", "http://localhost:18123/")
CH_AUTH = (os.environ.get("CH_USER", "tr"), os.environ.get("CH_PASSWORD", "tr"))

# These mirror synthetic/route_health.py exactly.
TRANSIENT_TYPES = {
    "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
    "ConnectError", "ReadError", "WriteError", "RemoteProtocolError",
}
TRANSIENT_STATUSES = {429, 500, 502, 503, 504, 529}
SAMPLES_PER_ROUTE_LIMIT = 48
WINDOW_HOURS = 48
MIN_SAMPLES = 6
FAILURE_THRESHOLD = 0.95

SQL_TRANSIENT = """(
        ifNull(error_status, 0) IN (429,500,502,503,504,529)
     OR ifNull(error_type, '') IN ('ReadTimeout','ConnectTimeout','WriteTimeout','PoolTimeout',
                                   'ConnectError','ReadError','WriteError','RemoteProtocolError'))"""


def ch(query: str, body: bytes | None = None, params: dict[str, str] | None = None) -> str:
    """Run a ClickHouse statement over the HTTP interface.

    `params` are bound SERVER-SIDE via ClickHouse's `{name:Type}` placeholders
    (sent as `param_<name>`), so no value is ever spliced into SQL text.
    """
    query_params: dict[str, str] = {}
    if params:
        query_params.update({f"param_{k}": v for k, v in params.items()})
    if body is not None:
        query_params["query"] = query
        response = httpx.post(
            CH_URL, params=query_params, content=body, auth=CH_AUTH, timeout=300
        )
    else:
        response = httpx.post(
            CH_URL, params=query_params or None, content=query.encode(), auth=CH_AUTH, timeout=300
        )
    response.raise_for_status()
    return response.text


# --------------------------------------------------------------------------
# Normalisation — SHARED by both sides.
# --------------------------------------------------------------------------


def normalise(raw: dict) -> dict | None:
    """Coerce one Bigtable JSON blob into the canonical shape.

    Both the Python baseline and the ClickHouse load consume the output of
    this function, so a coercion cannot make the two sides disagree by
    construction. That matters: `error_status` arrives as an int today but a
    historical string `"429"` would be non-transient to Python's `in {...}`
    and transient to ClickHouse's UInt16 column — an invisible divergence if
    each side did its own parsing.

    Returns None for rows that cannot be placed in time; production route
    health skips those too (`_parse_created_at` returning None).
    """
    created = raw.get("created_at") or ""
    try:
        parsed = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    parsed = parsed.astimezone(dt.UTC)

    error_status = raw.get("error_status")
    if isinstance(error_status, str):
        error_status = int(error_status) if error_status.isdigit() else None
    elif error_status is not None:
        error_status = int(error_status)

    provider = raw.get("provider")
    model = raw.get("model")
    if not provider or not model:
        # Ungrouped rows would land in an empty-string bucket on one side and
        # a None bucket on the other. Production keys routes by real ids.
        return None

    return {
        "id": str(raw.get("id") or ""),
        "created_at": parsed,
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
        "speed_tokens_per_second": raw.get("speed_tokens_per_second"),
        "elapsed_milliseconds": raw.get("elapsed_milliseconds"),
        "first_token_milliseconds": raw.get("first_token_milliseconds"),
        "ttfb_milliseconds": raw.get("ttfb_milliseconds"),
        "finish_reason": raw.get("finish_reason"),
        "error_type": raw.get("error_type"),
        "error_status": error_status,
        "error_message": raw.get("error_message"),
        "region": raw.get("region"),
        "app": str(raw.get("app") or ""),
    }


def fetch_bigtable_samples(limit: int) -> list[dict]:
    client = bigtable.Client(project=PROJECT, admin=False)
    table = client.instance(INSTANCE).table(TABLE)
    rs = RowSet()
    rs.add_row_range_with_prefix("benchmark_recent#")
    rows: list[dict] = []
    for i, row in enumerate(table.read_rows(row_set=rs)):
        if i >= limit:
            break
        cells = row.cells.get(FAMILY, {}).get(b"body", [])
        if not cells:
            continue
        try:
            raw = json.loads(cells[0].value.decode("utf-8"))
        except ValueError:
            continue
        record = normalise(raw)
        if record is not None:
            rows.append(record)
    return rows


def assert_window_covered(rows: list[dict], cutoff: dt.datetime) -> None:
    """The scanned slice must extend back past the evaluation window.

    The Bigtable scan is newest-first ACROSS ALL ROUTES, so a slice that stops
    inside the window would truncate busy routes and leave sparse ones intact
    — a biased sample. Both sides would still agree (they read the same
    slice), so the comparison would look perfect while diverging from what
    production, which reads per-route, actually sees. Checking coverage is
    what makes the agreement meaningful.
    """
    oldest = min(r["created_at"] for r in rows)
    if oldest > cutoff:
        raise SystemExit(
            f"scan does not cover the {WINDOW_HOURS}h window: oldest row {oldest.isoformat()} "
            f"is newer than cutoff {cutoff.isoformat()}. Increase --limit."
        )
    print(
        f"  window coverage OK: oldest row {oldest.isoformat()} "
        f"predates the {WINDOW_HOURS}h cutoff"
    )


def python_route_health(
    rows: list[dict], cutoff: dt.datetime
) -> dict[tuple[str, str], tuple[int, int]]:
    """Reproduce synthetic/route_health.py evaluate_route_health().

    Order of operations is load-bearing and mirrors production: the store
    returns the newest `SAMPLES_PER_ROUTE_LIMIT` rows for a route across ALL
    sources, and only then does the Python loop apply the synthetic / status /
    transient filters. Filtering before truncating would consider older rows
    production never sees.
    """
    by_route: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_route[(r["provider"], r["model"])].append(r)

    out: dict[tuple[str, str], tuple[int, int]] = {}
    for route, route_rows in by_route.items():
        route_rows.sort(key=lambda r: r["created_at"], reverse=True)
        newest = route_rows[:SAMPLES_PER_ROUTE_LIMIT]

        samples = failures = 0
        for r in newest:
            if r["source"] != "synthetic":
                continue
            if r["created_at"] < cutoff:
                continue
            status = r["status"]
            if status == "unsupported" or status not in {"error", "success"}:
                continue
            if status == "error" and (
                r["error_status"] in TRANSIENT_STATUSES
                or r["error_type"] in TRANSIENT_TYPES
            ):
                continue
            samples += 1
            if status == "error":
                failures += 1
        if samples:
            out[route] = (samples, failures)
    return out


def sql_route_health(cutoff: dt.datetime) -> dict[tuple[str, str], tuple[int, int]]:
    """Same evaluation, expressed once in SQL.

    row_number() reproduces "newest N per route", and is computed BEFORE the
    status/transient filters so the truncation matches production's ordering.
    """
    # Values are bound server-side via ClickHouse `{name:Type}` placeholders —
    # nothing is spliced into SQL text. The transient predicate is a fixed
    # fragment shared with the schema, not caller input.
    query = """
    WITH ranked AS (
        SELECT provider, model, status, source, created_at, error_status, error_type,
               row_number() OVER (PARTITION BY provider, model ORDER BY created_at DESC) AS rn
        FROM provider_benchmark_samples
    )
    SELECT provider, model,
           count()                   AS samples,
           countIf(status = 'error') AS failures
    FROM ranked
    WHERE rn <= {route_limit:UInt32}
      AND source = 'synthetic'
      AND created_at >= {cutoff:DateTime64(3, 'UTC')}
      AND status IN ('error', 'success')
      AND NOT (status = 'error' AND (
            ifNull(error_status, 0) IN (429,500,502,503,504,529)
         OR ifNull(error_type, '') IN ('ReadTimeout','ConnectTimeout','WriteTimeout','PoolTimeout',
                                       'ConnectError','ReadError','WriteError','RemoteProtocolError')))
    GROUP BY provider, model
    FORMAT JSONEachRow
    """
    params = {
        "route_limit": str(SAMPLES_PER_ROUTE_LIMIT),
        "cutoff": cutoff.strftime("%Y-%m-%d %H:%M:%S"),
    }
    result: dict[tuple[str, str], tuple[int, int]] = {}
    for line in ch(query, params=params).strip().splitlines():
        r = json.loads(line)
        result[(r["provider"], r["model"])] = (int(r["samples"]), int(r["failures"]))
    return result


def flagged(health: dict[tuple[str, str], tuple[int, int]]) -> set[tuple[str, str]]:
    """Production's actual output: which routes raise an alert."""
    return {
        route
        for route, (samples, failures) in health.items()
        if samples >= MIN_SAMPLES and failures / samples >= FAILURE_THRESHOLD
    }


def _materialized_view_ddl() -> str:
    """The CREATE MATERIALIZED VIEW statement, read from the schema file.

    Read rather than duplicated so the view rebuilt here can never drift from
    the one a real deployment applies.
    """
    schema_path = os.path.join(os.path.dirname(__file__), "001_provider_benchmark_samples.sql")
    with open(schema_path) as handle:
        raw = handle.read()
    # Strip full-line comments before splitting: the prose contains semicolons.
    code = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("--"))
    for statement in (s.strip() for s in code.split(";")):
        if statement.upper().startswith("CREATE MATERIALIZED VIEW"):
            return statement
    raise SystemExit("no CREATE MATERIALIZED VIEW found in the schema file")


def load(rows: list[dict]) -> None:
    """Reload the LOCAL proof table, rebuilding the materialized view with it.

    The view is dropped and RECREATED rather than left in place: an MV runs
    per INSERT BLOCK and does NOT inherit the source table's
    ReplacingMergeTree collapsing, so reloading without rebuilding silently
    doubles every aggregate. Verified the hard way — three loader runs against
    30,832 source rows left 92,500 samples in the view.

    Order matters and is easy to get wrong in the other direction: the view
    must be recreated BEFORE the insert. A materialized view only observes
    rows inserted after it exists, so recreating it afterwards would leave a
    permanently empty view — which is exactly the bug an earlier version of
    this function shipped (it dropped the view and never brought it back).
    """
    ch("TRUNCATE TABLE provider_benchmark_samples")
    ch("DROP TABLE IF EXISTS route_health_hourly")
    ch(_materialized_view_ddl())
    payload = "\n".join(
        json.dumps({**r, "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]})
        for r in rows
    ).encode()
    ch("INSERT INTO provider_benchmark_samples FORMAT JSONEachRow", payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=int(os.environ.get("SCAN_LIMIT", "40000")))
    parser.add_argument(
        "--no-load",
        action="store_true",
        help="compare against data already in ClickHouse (does not truncate)",
    )
    args = parser.parse_args()

    print(f"Scanning Bigtable (limit {args.limit})...")
    rows = fetch_bigtable_samples(args.limit)
    print(f"  {len(rows)} usable rows")
    if not rows:
        print("no rows; aborting")
        return 1

    newest = max(r["created_at"] for r in rows)
    cutoff = newest - dt.timedelta(hours=WINDOW_HOURS)
    assert_window_covered(rows, cutoff)

    if not args.no_load:
        print("Loading into ClickHouse (local proof table)...")
        load(rows)
        loaded = ch("SELECT count() FROM provider_benchmark_samples").strip()
        print(f"  loaded {loaded} rows")

    print("Evaluating route health the CURRENT way (Python)...")
    baseline = python_route_health(rows, cutoff)
    print(f"  {len(baseline)} routes, {len(flagged(baseline))} flagged")

    print("Evaluating the SAME rules in SQL...")
    sql_result = sql_route_health(cutoff)
    print(f"  {len(sql_result)} routes, {len(flagged(sql_result))} flagged")

    mismatches = [
        (route, baseline.get(route), sql_result.get(route))
        for route in sorted(set(baseline) | set(sql_result))
        if baseline.get(route) != sql_result.get(route)
    ]
    if mismatches:
        print(f"\nMISMATCH on {len(mismatches)} route(s):")
        for route, py, sq in mismatches[:20]:
            print(f"  {route[0]}/{route[1]}: python={py} sql={sq}")
        return 1

    py_flagged, sql_flagged = flagged(baseline), flagged(sql_result)
    if py_flagged != sql_flagged:
        print(f"\nFLAGGED SET MISMATCH: only-python={py_flagged ^ sql_flagged}")
        return 1

    print(f"\nEXACT MATCH: {len(baseline)} routes, identical counts.")
    print(f"Identical flagged set ({len(py_flagged)} routes would alert):")
    for provider, model in sorted(py_flagged):
        samples, failures = baseline[(provider, model)]
        print(f"  {provider}/{model}: {failures}/{samples}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
