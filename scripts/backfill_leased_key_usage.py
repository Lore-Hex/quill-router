"""Plan the booking of the fast path pilot's settled charges to their keys.

Until #1842 the leased finalize wrote a terminal's records and no key usage
(docs/design/fast-admission-pilot.md), so `tr_key_limit.usage` and the
day/week/month counters of the pilot's keys miss every charge the leases
booked. This plans the one correction, from the control plane's own log
lines, and prints one UPDATE per key for the operator to run (one statement
each, through `gcloud spanner databases execute-sql`, under a named
identity); it writes nothing itself.

Input: the pilot's log lines, as

    gcloud logging read 'resource.type="cloud_run_revision" AND
      textPayload:"fastpath pilot" AND timestamp>="2026-10-17T04:50:00Z"'
      --format='value(timestamp,textPayload)'

A settle counts when its `fastpath pilot terminal won ... kind=settle
won=settle` line is before `--until` (the fix's deploy, less a margin) and
no line says the drain was left to finish it (`terminal undecided`,
`records deferred`, `leased intent undecided`), since a finalize after the
deploy booked the key itself. Its windows are those of its settle time, the
log line's, as `release_key` books into the window containing the
settlement. The authorizations are point-read by primary key, in bounded
chunks, for the key and the recorded cost, which must equal the log's
charge. The UPDATE rolls a stale window forward as `release_key`'s window
SQL does. The key's counters are printed with the plan, so the operator can
check the row after the statement ran, and a lost answer to the statement
is settled by reading the row, not by running it again.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from typing import Any

from trusted_router.spend_windows import window_floors

PROJECT = "quill-cloud-proxy"
INSTANCE = "trusted-router-nam6"
DATABASE = "trusted-router"
READ_ACCOUNT = "tr-ops-local@quill-cloud-proxy.iam.gserviceaccount.com"
CHUNK = 200
GCLOUD = shutil.which("gcloud") or "gcloud"
_WON = re.compile(r"^(\S+)\s.*fastpath pilot terminal won authorization_id=(gwa-[0-9a-f]{32}) kind=settle won=settle charge=(\d+)")
_LATER = re.compile(
    r"(?:fastpath pilot terminal undecided, left to the drain|fastpath pilot records deferred after a decided terminal"
    r"|settle outbox leased intent undecided) authorization_id=(gwa-[0-9a-f]{32})"
)
_ID = re.compile(r"^gwa-[0-9a-f]{32}$")
# Key hashes are `key_` and a URL-safe base64 digest (storage_models.ApiKey).
_KEY_HASH = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _when(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def parse_log(lines: list[str]) -> tuple[dict[str, tuple[dt.datetime, int]], set[str]]:
    """Each settled leased authorization with its settle time and the charge
    the owner booked; and the authorizations the drain was left to finish."""
    settled: dict[str, tuple[dt.datetime, int]] = {}
    deferred: set[str] = set()
    for line in lines:
        won = _WON.search(line)
        if won is not None:
            settled[won.group(2)] = (_when(won.group(1)), int(won.group(3)))
            continue
        later = _LATER.search(line)
        if later is not None:
            deferred.add(later.group(1))
    return settled, deferred


def _sql(sql: str, *, account: str, timeout: str = "5m") -> list[dict[str, Any]]:
    out = subprocess.run(  # noqa: S603 - gcloud, arguments built here
        [
            GCLOUD, "spanner", "databases", "execute-sql", DATABASE, f"--instance={INSTANCE}",
            f"--project={PROJECT}", f"--account={account}", "--format=json", f"--timeout={timeout}",
            f"--sql={sql}",
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out) if out.strip() else {}
    if "rows" not in data:
        return []
    columns = [field["name"] for field in data["metadata"]["rowType"]["fields"]]
    return [dict(zip(columns, row, strict=True)) for row in data["rows"]]


def read_authorizations(ids: list[str], *, account: str) -> list[dict[str, Any]]:
    """Point reads by primary key, CHUNK at a time: the one bounded read of
    tr_gateway_authorization (AGENTS.md's Spanner query rules)."""
    for authorization_id in ids:
        if not _ID.match(authorization_id):
            raise ValueError(f"not an authorization id: {authorization_id!r}")
    rows: list[dict[str, Any]] = []
    for start in range(0, len(ids), CHUNK):
        chunk = ", ".join(f"'{i}'" for i in ids[start:start + CHUNK])
        rows.extend(_sql(
            "SELECT authorization_id, key_hash, workspace_id, settled, finalization_outcome, "  # noqa: S608 - ids checked against _ID above
            "finalized_cost_microdollars FROM tr_gateway_authorization "
            f"WHERE authorization_id IN ({chunk})",
            account=account,
        ))
    return rows


def read_key_counters(key_hash: str, *, account: str) -> dict[str, Any] | None:
    if not _KEY_HASH.match(key_hash):
        raise ValueError(f"not a key hash: {key_hash!r}")
    rows = _sql(
        "SELECT usage, byok_usage, reserved, day_usage, day_start, week_usage, week_start, month_usage, month_start "  # noqa: S608 - the hash is checked above
        f"FROM tr_key_limit WHERE key_hash = '{key_hash}' AND shard = 0",
        account=account,
    )
    return rows[0] if rows else None


def plan(
    rows: list[dict[str, Any]],
    settled: dict[str, tuple[dt.datetime, int]],
    deferred: set[str],
    *,
    until: dt.datetime,
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Per key: the lifetime amount and each window's, from the settles
    before `until` the drain was not left to finish; and what was left out,
    with why."""
    floors = window_floors(now)
    per_key: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"usage": 0, "day": 0, "week": 0, "month": 0, "count": 0},
    )
    skipped: list[str] = []
    seen: set[str] = set()
    for row in rows:
        aid = str(row["authorization_id"])
        seen.add(aid)
        if aid not in settled:
            skipped.append(f"{aid}: not in the log")
            continue
        settled_at, charge = settled[aid]
        cost = int(row["finalized_cost_microdollars"] or 0)
        if not row["settled"] or row["finalization_outcome"] != "settled":
            skipped.append(f"{aid}: not a settled authorization ({row['finalization_outcome']})")
            continue
        if aid in deferred:
            skipped.append(f"{aid}: left to the drain, which may have booked it")
            continue
        if settled_at >= until:
            skipped.append(f"{aid}: settled at {settled_at.isoformat()} on or after --until")
            continue
        if cost != charge:
            skipped.append(f"{aid}: the record's cost {cost} is not the log's charge {charge}")
            continue
        if cost <= 0:
            continue
        amounts = per_key[(str(row["workspace_id"]), str(row["key_hash"]))]
        amounts["usage"] += cost
        amounts["count"] += 1
        for window, floor in (("day", floors["daily"]), ("week", floors["weekly"]), ("month", floors["monthly"])):
            if settled_at >= floor:
                amounts[window] += cost
    for aid in settled:
        if aid not in seen:
            skipped.append(f"{aid}: no authorization row")
    return (
        [{"workspace_id": w, "key_hash": k, **a} for (w, k), a in sorted(per_key.items())],
        skipped,
    )


def update_sql(entry: dict[str, Any], *, now: dt.datetime) -> str:
    """One UPDATE per key, shard 0 (the leased scope row is unsharded): the
    lifetime usage and each window's amount, rolling a stale window forward
    to its current floor as release_key's window SQL does."""
    floors = window_floors(now)
    if not _KEY_HASH.match(entry["key_hash"]):
        raise ValueError(f"not a key hash: {entry['key_hash']!r}")

    def ts(value: dt.datetime) -> str:
        return "TIMESTAMP '" + value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "'"

    sets = [f"usage = usage + {int(entry['usage'])}"]
    for window, floor in (("day", floors["daily"]), ("week", floors["weekly"]), ("month", floors["monthly"])):
        amount = int(entry[window])
        sets.append(
            f"{window}_usage = IF({window}_start IS NULL OR {window}_start < {ts(floor)}, {amount}, "
            f"COALESCE({window}_usage, 0) + {amount})"
        )
        sets.append(f"{window}_start = IF({window}_start IS NULL OR {window}_start < {ts(floor)}, {ts(floor)}, {window}_start)")
    return (
        "UPDATE tr_key_limit SET " + ", ".join(sets)  # noqa: S608 - the key hash is checked above; amounts and floors are ints and timestamps
        + f" WHERE key_hash = '{entry['key_hash']}' AND shard = 0"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log_file", help="the pilot's control plane log lines (see the module docstring)")
    parser.add_argument("--until", required=True, help="ISO time; settles at or after it are the finalize's own")
    parser.add_argument("--account", default=READ_ACCOUNT, help="the gcloud identity for the reads")
    args = parser.parse_args(argv)
    until = _when(args.until)
    if until.tzinfo is None:
        parser.error("--until needs a timezone")
    now = dt.datetime.now(dt.UTC)
    with open(args.log_file, encoding="utf-8") as handle:
        settled, deferred = parse_log(handle.read().splitlines())
    print(f"settled leased authorizations in the log: {len(settled)}; left to the drain: {len(deferred)}")
    rows = read_authorizations(sorted(settled), account=args.account)
    entries, skipped = plan(rows, settled, deferred, until=until, now=now)
    for line in skipped:
        print("skipped", line)
    print(f"keys: {len(entries)}, skipped: {len(skipped)}, planned at {now.isoformat()}")
    for entry in entries:
        print(json.dumps(entry))
        print("counters now:", json.dumps(read_key_counters(entry["key_hash"], account=args.account)))
        print(update_sql(entry, now=now))
    print("nothing written: run each UPDATE once, as one gcloud execute-sql statement, before the day's window turns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
