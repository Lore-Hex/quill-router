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
  - Guarded for API keys: an api_key record, with the index records
    that name it (api_key_lookup, api_key_by_workspace), is written only
    when the target holds no trace of that key, read in the same
    transaction: no api_key entity, no tr_key_limit row, no lookup or
    workspace-index record. Anything else is refused and reported. A raw
    write bypasses apply_key_patch and tr_key_limit's mirror, and a key
    deleted on the target keeps its tr_key_limit rows, so replaying an older
    body could bring back a deleted key, its credential and its limits.

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
from collections.abc import Sequence
from typing import Any, NamedTuple

import google.oauth2.credentials
from google.cloud import spanner

from trusted_router.storage_key_patch import key_enforcement_fields

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
# tr_key_limit in the same commit, and a deleted key leaves its tr_key_limit
# rows behind. A whole-body replay bypasses all of that, so a key's records are
# replayed only into a target that holds no trace of the key (key_traces); a
# key the target has any record of is refused and reported, whatever it would
# change. Spanner keeps no key tombstone: the rows a deletion leaves are the
# trace.

#: The index records written with their key, each naming it by key_id: the
#: credential lookup (by lookup hash) and the workspace's key list.
KEY_INDEX_KINDS = ("api_key_lookup", "api_key_by_workspace")

#: What decides which spend limits a key enforces, derived from the code that
#: reads it (storage_key_patch.key_enforcement_fields), not listed here. A
#: refused record's report names those it would have changed.
KEY_CONFIG_FIELDS = key_enforcement_fields()


class KeyRecords(NamedTuple):
    """One key's replay rows: its api_key record and the index records that
    name it."""

    key_id: str
    key_row: tuple[Any, ...]
    index_rows: tuple[tuple[Any, ...], ...] = ()


def group_key_records(
    rows: Sequence[tuple[Any, ...]],
) -> tuple[list[KeyRecords], list[tuple[Any, ...]], list[tuple[str, str, str]]]:
    """Split replay rows into keys (an api_key record with the index records
    that name it), the other rows, and refused index records as (kind, id,
    reason): one whose key has no api_key record in this replay
    (index_without_key: its key is not being written, so its credential is
    not restored) or whose body names no key (unreadable)."""
    keys = {row[1]: row for row in rows if row[0] == "api_key"}
    indexes: dict[str, list[tuple[Any, ...]]] = {key_id: [] for key_id in keys}
    others: list[tuple[Any, ...]] = []
    refused: list[tuple[str, str, str]] = []
    for row in rows:
        if row[0] == "api_key":
            continue
        if row[0] not in KEY_INDEX_KINDS:
            others.append(row)
            continue
        try:
            key_id = json.loads(row[2]).get("key_id")
        except (AttributeError, TypeError, ValueError):
            key_id = None
        if not isinstance(key_id, str):
            refused.append((row[0], row[1], "unreadable"))
        elif key_id not in keys:
            refused.append((row[0], row[1], "index_without_key"))
        else:
            indexes[key_id].append(row)
    grouped = [KeyRecords(key_id, row, tuple(indexes[key_id])) for key_id, row in keys.items()]
    return grouped, others, refused


def _replayed_key(records: KeyRecords) -> dict[str, Any] | None:
    """The replayed api_key body, when it is one this replay can check: a
    JSON object for this key id, with the lookup hash and workspace its
    index records are found by, and index records naming exactly those."""
    try:
        body = json.loads(records.key_row[2])
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("hash") != records.key_id:
        return None
    lookup, workspace = body.get("lookup_hash"), body.get("workspace_id")
    if not isinstance(lookup, str) or not lookup or not isinstance(workspace, str) or not workspace:
        return None
    expected = {("api_key_lookup", lookup), ("api_key_by_workspace", f"{workspace}#{records.key_id}")}
    if any((row[0], row[1]) not in expected for row in records.index_rows):
        return None
    return body


def config_changes(target_body: str | None, replayed: dict[str, Any]) -> tuple[str, ...]:
    """The KEY_CONFIG_FIELDS a replayed body would change on the target's key."""
    if target_body is None:
        return ()
    try:
        target = json.loads(target_body)
    except (TypeError, ValueError):
        return KEY_CONFIG_FIELDS
    if not isinstance(target, dict):
        return KEY_CONFIG_FIELDS
    return tuple(field for field in KEY_CONFIG_FIELDS if target.get(field) != replayed.get(field))


def api_key_replay_refusal(
    held: Sequence[str], target_body: str | None, replayed: dict[str, Any] | None,
) -> str | None:
    """Why a key's records are not replayed, or None to write them.

    ``held`` is what the target holds of the key (key_traces). Only a key it
    holds nothing of is written; ``unreadable`` is a body this replay cannot
    check (_replayed_key). The reason names what the target holds and, when
    it holds the key itself, what the replayed body would have changed.
    """
    if replayed is None:
        return "unreadable"
    if not held:
        return None
    reason = "target holds " + ", ".join(held)
    changes = config_changes(target_body, replayed)
    if changes:
        reason += "; would change " + ", ".join(changes)
    return reason


def key_traces(transaction: Any, key_id: str, replayed: dict[str, Any]) -> tuple[list[str], str | None]:
    """What the target holds of a key, read in ``transaction``, and its
    api_key body if it holds one. Each read names its complete primary key
    (kind and id), or its leading key_hash for tr_key_limit."""
    string = spanner.param_types.STRING
    held: list[str] = []
    target_body = None
    for kind, entity_id in (
        ("api_key", key_id),
        ("api_key_lookup", replayed["lookup_hash"]),
        ("api_key_by_workspace", f"{replayed['workspace_id']}#{key_id}"),
    ):
        found = list(transaction.execute_sql(
            "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
            params={"kind": kind, "id": entity_id},
            param_types={"kind": string, "id": string},
        ))
        if found:
            held.append(kind)
            if kind == "api_key":
                target_body = found[0][0]
    if list(transaction.execute_sql(
        "SELECT 1 FROM tr_key_limit WHERE key_hash=@kh LIMIT 1",
        params={"kh": key_id},
        param_types={"kh": string},
    )):
        held.append("tr_key_limit rows")
    return held, target_body


def replay_api_keys(database: Any, rows: Sequence[tuple[Any, ...]]) -> list[tuple[str, str]]:
    """Write api_key records, each with the index records that name it, into
    the target only when the target holds no trace of the key, read in the
    same read-write transaction, and return what was refused as (id, why):
    each refused key (api_key_replay_refusal) and each index record whose key
    is not among ``rows`` (group_key_records). Nothing of a refused key is
    written."""
    keys, others, refused_indexes = group_key_records(rows)
    if others:
        raise ValueError(
            f"replay_api_keys writes api_key and key index records only, not {others[0][0]!r}"
        )

    def txn(transaction: Any) -> list[tuple[str, str]]:
        refused: list[tuple[str, str]] = []
        allowed: list[tuple[Any, ...]] = []
        for records in keys:
            replayed = _replayed_key(records)
            held, target_body = key_traces(transaction, records.key_id, replayed) if replayed else ([], None)
            reason = api_key_replay_refusal(held, target_body, replayed)
            if reason is None:
                allowed.extend([records.key_row, *records.index_rows])
            else:
                refused.append((records.key_id, reason))
        if allowed:
            transaction.insert_or_update(
                table="tr_entities",
                columns=("kind", "id", "body", "updated_at"),
                values=allowed,
            )
        return refused

    result: list[tuple[str, str]] = database.run_in_transaction(txn)
    return result + [(entity_id, f"{kind}: {reason}") for kind, entity_id, reason in refused_indexes]


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
    # A key's records (api_key and its index records) go through
    # replay_api_keys: written only when the target holds no trace of the
    # key, read in the transaction that writes them. Index records whose key
    # is not replayed are refused here, so no credential comes back alone.
    keys, other_rows, refused_indexes = group_key_records(replay_rows)
    refused_keys: list[tuple[str, str]] = []
    for i in range(0, len(keys), 25):
        chunk = keys[i : i + 25]
        refused = replay_api_keys(target, [row for k in chunk for row in (k.key_row, *k.index_rows)])
        refused_keys.extend(refused)
        refused_ids = {key_id for key_id, _ in refused}
        wrote = sum(1 + len(k.index_rows) for k in chunk if k.key_id not in refused_ids)
        written += wrote
        print(f"  api keys: wrote {wrote} records, refused {len(refused)} keys")
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
    for kind, entity_id, reason in refused_indexes:
        print(f"  REFUSED {kind} {entity_id[:12]}…: {reason}")
    # The sample check below reads only what was written.
    refused_ids = {key_id for key_id, _ in refused_keys}
    replay_rows = list(other_rows) + [
        row for k in keys if k.key_id not in refused_ids for row in (k.key_row, *k.index_rows)
    ]

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
