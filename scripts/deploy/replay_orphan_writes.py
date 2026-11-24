"""One-shot replay of post-cutover orphan writes from the old regional
Spanner instance (`trusted-router`) into the new multi-region instance
(`trusted-router-nam6`).

Background: the Stage-1 cutover Phase D flipped Cloud Run's
`TR_SPANNER_INSTANCE_ID` to `trusted-router-nam6`, but a deploy that
ran shortly after used `--set-env-vars` (which REPLACES env vars) with
the old `_lib.sh` default of `trusted-router`. That reverted
us-central1+europe-west4 to the regional instance until I caught it
and re-flipped. During the ~2-hour split-write window, traffic
landing on those two regions wrote to the OLD instance instead of
nam6.

This script copies the orphan rows. It is:

  - Idempotent: uses INSERT OR UPDATE so re-running is safe.
  - Time-bounded: only copies rows updated after the cutover-time
    cutoff (the export snapshot timestamp). Rows older than that
    were already in nam6 from the Avro import.
  - Selective: skips `rate_limit` because that table is windowed
    counter state — replaying old counters into a new window would
    corrupt the current rate-limit state. The 92 orphan rate_limit
    rows just get dropped; affected workspaces saw a slightly higher
    burst limit during the split-write window, then state resets.
  - Guarded for API keys: an api_key record is written only after its
    target key is read in the same transaction, and never when it would
    change the spend configuration of a key the target holds (a raw write
    bypasses apply_key_patch, so it could give a limitless strict key a
    limit, and tr_key_limit's mirror); refused records are reported.

Usage:
    cd quill-router
    uv run python scripts/deploy/replay_orphan_writes.py --apply

Without --apply, dry-run mode prints the would-replay rows but does
nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from typing import Any

import google.oauth2.credentials
from google.cloud import spanner

from trusted_router.storage_key_patch import KEY_LIMIT_FIELDS

PROJECT_ID = os.environ.get("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
SOURCE_INSTANCE_ID = "trusted-router"
TARGET_INSTANCE_ID = "trusted-router-nam6"
DATABASE_ID = "trusted-router"

# The Phase A export's snapshot timestamp T0. Rows with updated_at <=
# T0 were already imported into nam6 by the Avro import job; only
# rows STRICTLY AFTER this need replay.
CUTOFF_ISO = "2026-05-10T16:36:40Z"

# Tables to NOT replay — their writes are state that's intentionally
# transient or already-reset post-cutover.
SKIP_KINDS = {"rate_limit"}

# An api_key body carries the key's spend configuration, which the store
# owns transactionally: every change goes through apply_key_patch (the
# strict-key first-limit rule, StrictKeyFirstLimit) and is mirrored into
# tr_key_limit in the same commit. A whole-body replay bypasses both, so a
# replayed key never changes these fields of a key the target already holds
# (api_key_replay_refusal); a key the target lacks is written as replayed.
KEY_CONFIG_FIELDS = (*KEY_LIMIT_FIELDS, "include_byok_in_limit", "usage_shard_count", "budget_strict")


def _has_limit(body: dict[str, Any]) -> bool:
    return any(body.get(field) is not None for field in KEY_LIMIT_FIELDS)


def api_key_replay_refusal(target_body: str | None, replay_body: str) -> str | None:
    """Why an api_key record is not replayed over the target's key, or None.

    - ``strict_first_limit``: the target holds a strict key with no limit,
      which can never gain one (storage_key_patch.StrictKeyFirstLimit), and
      the replayed body carries one.
    - ``config_differs``: the target holds the key, and the replayed body
      changes its spend configuration (KEY_CONFIG_FIELDS), which a raw write
      would leave out of tr_key_limit's mirror; change it through the key
      API instead.
    - ``unreadable``: either body is not a JSON object.
    """
    if target_body is None:
        return None
    try:
        target, replayed = json.loads(target_body), json.loads(replay_body)
    except ValueError:
        return "unreadable"
    if not isinstance(target, dict) or not isinstance(replayed, dict):
        return "unreadable"
    if target.get("budget_strict") is True and not _has_limit(target) and _has_limit(replayed):
        return "strict_first_limit"
    if any(target.get(field) != replayed.get(field) for field in KEY_CONFIG_FIELDS):
        return "config_differs"
    return None


def replay_api_keys(database: Any, rows: list[tuple[Any, ...]]) -> list[tuple[str, str]]:
    """Write api_key records into the target, each checked against the
    target's current key in the same read-write transaction (a point read
    by the complete primary key, kind and id), and return those refused,
    with why (api_key_replay_refusal). Nothing is written for a refused
    record."""

    def txn(transaction: Any) -> list[tuple[str, str]]:
        refused: list[tuple[str, str]] = []
        allowed = []
        for row in rows:
            kind, entity_id, body = row[0], row[1], row[2]
            if kind != "api_key":
                raise ValueError(f"replay_api_keys writes api_key records only, not {kind!r}")
            current = list(transaction.execute_sql(
                "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
                params={"kind": "api_key", "id": entity_id},
                param_types={"kind": spanner.param_types.STRING, "id": spanner.param_types.STRING},
            ))
            reason = api_key_replay_refusal(current[0][0] if current else None, body)
            if reason is None:
                allowed.append(row)
            else:
                refused.append((entity_id, reason))
        if allowed:
            transaction.insert_or_update(
                table="tr_entities",
                columns=("kind", "id", "body", "updated_at"),
                values=allowed,
            )
        return refused

    result: list[tuple[str, str]] = database.run_in_transaction(txn)
    return result


def _gcloud_credentials() -> google.oauth2.credentials.Credentials:
    """Use the active gcloud user's access token. Tr-deploy SA was
    granted spanner.databaseAdmin earlier in this session."""
    # gcloud is on PATH by definition for any operator running this.
    token = subprocess.check_output(
        ["gcloud", "auth", "print-access-token"],  # noqa: S607
        text=True,
    ).strip()
    return google.oauth2.credentials.Credentials(token=token)


def _connect(instance_id: str) -> spanner.Database:
    client = spanner.Client(project=PROJECT_ID, credentials=_gcloud_credentials())
    return client.instance(instance_id).database(DATABASE_ID)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the writes. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--cutoff",
        default=CUTOFF_ISO,
        help=f"Replay rows with updated_at > this ISO timestamp. Default: {CUTOFF_ISO}",
    )
    args = parser.parse_args()

    print(
        f"replay_orphan_writes: source={SOURCE_INSTANCE_ID} target={TARGET_INSTANCE_ID} "
        f"cutoff={args.cutoff} mode={'APPLY' if args.apply else 'DRY-RUN'}"
    )

    source = _connect(SOURCE_INSTANCE_ID)
    target = _connect(TARGET_INSTANCE_ID)

    # 1. Read every orphan row from source. Doing it in one snapshot
    #    keeps the read view consistent. The dataset is tiny (~2k
    #    rows) so a single snapshot read is fine.
    rows: list[tuple[str, str, str, object]] = []
    with source.snapshot() as snap:
        result = snap.execute_sql(
            "SELECT kind, id, body, updated_at "
            "FROM tr_entities "
            "WHERE updated_at > @cutoff "
            "ORDER BY kind, updated_at",
            params={"cutoff": args.cutoff},
            param_types={"cutoff": spanner.param_types.TIMESTAMP},
        )
        for row in result:
            rows.append(tuple(row))

    by_kind: Counter[str] = Counter(r[0] for r in rows)
    skipped: Counter[str] = Counter()
    print(f"\nfound {len(rows)} orphan rows on source:")
    for kind, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        marker = "  SKIP" if kind in SKIP_KINDS else ""
        print(f"  {kind:30s} {n:5d}{marker}")
        if kind in SKIP_KINDS:
            skipped[kind] = n

    # 2. Filter out skipped kinds.
    replay_rows = [r for r in rows if r[0] not in SKIP_KINDS]
    print(f"\nwill replay {len(replay_rows)} rows ({sum(skipped.values())} skipped)")

    if not args.apply:
        print("DRY-RUN: not writing. Re-run with --apply.")
        return 0
    if not replay_rows:
        print("nothing to replay")
        return 0

    # 3. Write into target via INSERT OR UPDATE. Spanner's batch
    #    write API caps mutations at ~80k per transaction; we batch
    #    to 1000 rows / transaction to stay well under that and so a
    #    transient error only retries a small chunk.
    BATCH = 1000
    written = 0
    # api_key records go through replay_api_keys: each is checked against
    # the target's key in the transaction that writes it, so a replay never
    # gives a limitless strict key a limit nor desynchronizes tr_key_limit.
    key_rows = [r for r in replay_rows if r[0] == "api_key"]
    other_rows = [r for r in replay_rows if r[0] != "api_key"]
    refused_keys: list[tuple[str, str]] = []
    for i in range(0, len(key_rows), 100):
        chunk = key_rows[i : i + 100]
        refused = replay_api_keys(target, chunk)
        refused_keys.extend(refused)
        written += len(chunk) - len(refused)
        print(f"  api_key: wrote {len(chunk) - len(refused)}, refused {len(refused)}")
    for i in range(0, len(other_rows), BATCH):
        batch = other_rows[i : i + BATCH]
        with target.batch() as txn:
            txn.insert_or_update(
                table="tr_entities",
                columns=("kind", "id", "body", "updated_at"),
                values=batch,
            )
        written += len(batch)
        print(f"  wrote {written}/{len(replay_rows)}")
    for key_id, reason in refused_keys:
        print(f"  REFUSED api_key {key_id[:12]}…: {reason}")
    # The sample check below reads only what was written.
    refused_ids = {key_id for key_id, _ in refused_keys}
    replay_rows = [r for r in replay_rows if not (r[0] == "api_key" and r[1] in refused_ids)]

    # 4. Verify by sampling: pick one row per replayed kind, read it
    #    from target, confirm it matches.
    print("\nverifying sample reads on target...")
    samples_per_kind: dict[str, tuple[str, str, object, object]] = {}
    for r in replay_rows:
        if r[0] not in samples_per_kind:
            samples_per_kind[r[0]] = r
    with target.snapshot(multi_use=True) as snap:  # read per sampled kind (loop)
        for kind, sample in samples_per_kind.items():
            result = list(
                snap.execute_sql(
                    "SELECT body, updated_at FROM tr_entities "
                    "WHERE kind = @k AND id = @i",
                    params={"k": sample[0], "i": sample[1]},
                    param_types={
                        "k": spanner.param_types.STRING,
                        "i": spanner.param_types.STRING,
                    },
                )
            )
            if not result:
                print(f"  ✗ MISSING on target: {kind} / {sample[1][:40]}")
            else:
                tgt_body, tgt_ts = result[0]
                ok = tgt_body == sample[2]
                marker = "✓" if ok else "✗"
                print(f"  {marker} {kind:30s} {sample[1][:40]}…")

    print(f"\nreplay complete: {written} rows written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
