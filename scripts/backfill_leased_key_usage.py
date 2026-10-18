"""Book the fast path pilot's settled charges to their keys' usage counters.

Until #1842 the leased finalize wrote a terminal's records and no key usage
(docs/design/fast-admission-pilot.md), so `tr_key_limit.usage` and the
day/week/month counters of the pilot's keys miss every charge the leases
booked. This books them once, from the control plane's own log lines
(`fastpath pilot terminal won ... kind=settle`), which name each settled
authorization: the authorizations are point-read by primary key, in bounded
chunks, and each key gets ONE UPDATE with the lifetime amount and, per
window, the amount of the charges made since that window's current floor,
rolling a stale window forward as `release_key` does. Charges created at or
after `--until` (the fix's deploy) are left out, since the finalize books
them itself.

Dry-run (the default) reads with the read-only ops identity and prints the
plan. `--apply` needs a separately named gcloud identity. Run it once.
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
_LINE = re.compile(r"authorization_id=(gwa-[0-9a-f]{32}) kind=settle won=settle charge=(\d+)")
_ID = re.compile(r"^gwa-[0-9a-f]{32}$")
# Key hashes are `key_` and a URL-safe base64 digest (storage_models.ApiKey).
_KEY_HASH = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def parse_log(lines: list[str]) -> dict[str, int]:
    """Settled leased authorizations and the charge the owner booked, from
    the control plane's `fastpath pilot terminal won` lines."""
    charges: dict[str, int] = {}
    for line in lines:
        match = _LINE.search(line)
        if match is None:
            continue
        charges[match.group(1)] = int(match.group(2))
    return charges


def _sql(sql: str, *, account: str, timeout: str = "5m") -> list[dict[str, Any]]:
    out = subprocess.run(  # noqa: S603,S607 - gcloud from PATH, arguments built here
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
            "finalized_cost_microdollars, created_at FROM tr_gateway_authorization "
            f"WHERE authorization_id IN ({chunk})",
            account=account,
        ))
    return rows


def plan(
    rows: list[dict[str, Any]], charges: dict[str, int], *, until: dt.datetime, now: dt.datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Per key: the lifetime amount and each window's, from the settled
    authorizations created before `until`; and the rows left out, with why."""
    floors = window_floors(now)
    per_key: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"usage": 0, "day": 0, "week": 0, "month": 0, "count": 0})
    skipped: list[str] = []
    seen: set[str] = set()
    for row in rows:
        aid = str(row["authorization_id"])
        seen.add(aid)
        created = dt.datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        cost = int(row["finalized_cost_microdollars"] or 0)
        if not row["settled"] or row["finalization_outcome"] != "settled":
            skipped.append(f"{aid}: not a settled authorization ({row['finalization_outcome']})")
            continue
        if cost != charges.get(aid):
            skipped.append(f"{aid}: the record's cost {cost} is not the log's charge {charges.get(aid)}")
            continue
        if created >= until:
            skipped.append(f"{aid}: created at {created.isoformat()} on or after --until")
            continue
        if cost <= 0:
            continue
        amounts = per_key[(str(row["workspace_id"]), str(row["key_hash"]))]
        amounts["usage"] += cost
        amounts["count"] += 1
        for window, floor in (("day", floors["daily"]), ("week", floors["weekly"]), ("month", floors["monthly"])):
            if created >= floor:
                amounts[window] += cost
    for aid in charges:
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
    parser.add_argument("log_file", help="control plane log lines (gcloud logging read ... --format='value(timestamp,textPayload)')")
    parser.add_argument("--until", required=True, help="ISO time; charges created at or after it are the finalize's own")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--account", help="the gcloud identity for --apply; dry-run reads as " + READ_ACCOUNT)
    args = parser.parse_args(argv)
    if args.apply and not args.account:
        parser.error("--apply needs --account")
    until = dt.datetime.fromisoformat(args.until.replace("Z", "+00:00"))
    if until.tzinfo is None:
        parser.error("--until needs a timezone")
    now = dt.datetime.now(dt.UTC)
    with open(args.log_file, encoding="utf-8") as handle:
        charges = parse_log(handle.read().splitlines())
    print(f"settled leased authorizations in the log: {len(charges)}")
    rows = read_authorizations(sorted(charges), account=args.account if args.apply else READ_ACCOUNT)
    entries, skipped = plan(rows, charges, until=until, now=now)
    for line in skipped:
        print("skipped", line)
    print(f"keys: {len(entries)}, skipped: {len(skipped)}")
    for entry in entries:
        print(json.dumps(entry))
        print(update_sql(entry, now=now))
    if not args.apply:
        print("dry-run: nothing written")
        return 0
    for entry in entries:
        result = subprocess.run(  # noqa: S603,S607 - gcloud from PATH, arguments built here
            [
                GCLOUD, "spanner", "databases", "execute-sql", DATABASE, f"--instance={INSTANCE}",
                f"--project={PROJECT}", f"--account={args.account}", f"--sql={update_sql(entry, now=now)}",
            ],
            check=True, capture_output=True, text=True,
        )
        print(entry["key_hash"], result.stdout.strip() or result.stderr.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
