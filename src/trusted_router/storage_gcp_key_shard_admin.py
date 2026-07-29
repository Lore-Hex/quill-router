"""Operator-only split/consolidation for API-key usage rows.

The default path requires a fully drained key. The opt-in online split keeps
usage, window usage, and reserved counters on existing shard IDs so already
authorized requests can settle normally after new shards are added.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trusted_router.spend_windows import utcnow, window_floors
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_counters import (
    KEY_LIMIT_TABLE,
    distribute_credit_amount,
    key_usage_shard_count,
)
from trusted_router.storage_gcp_legacy_reservations import (
    legacy_reservation_snapshot,
)
from trusted_router.storage_models import ApiKey, Workspace, iso_now

_KEY_RESHARD_COLUMNS = (
    "key_hash",
    "shard",
    "limit_micro",
    "usage",
    "byok_usage",
    "reserved",
    "include_byok",
    "day_limit_micro",
    "week_limit_micro",
    "month_limit_micro",
    "day_usage",
    "day_start",
    "week_usage",
    "week_start",
    "month_usage",
    "month_start",
    "source_updated_at",
    "updated_at",
)


@dataclass
class KeyUsageReshardResult:
    key_hash: str
    workspace_id: str | None
    target_shard_count: int
    current_shard_count: int | None = None
    usage_micro: int | None = None
    byok_usage_micro: int | None = None
    reserved_micro: int | None = None
    typed_open_reservations: int = 0
    legacy_open_reservations: int = 0
    stale_legacy_reservations_ignored: int = 0
    reasons: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def ready(self) -> bool:
        return not self.reasons


def _typed_key_state(
    store: Any,
    key_hash: str,
    shard_count: int,
) -> tuple[list[list[Any]], int, dict[int, int]]:
    pt = store._param_types
    with store._database.snapshot(multi_use=True) as snapshot:
        rows = list(
            snapshot.execute_sql(
                "SELECT shard, limit_micro, usage, byok_usage, reserved, include_byok, "
                "day_limit_micro, week_limit_micro, month_limit_micro, "
                "day_usage, day_start, week_usage, week_start, month_usage, month_start "
                "FROM tr_key_limit WHERE key_hash=@pk AND shard>=0 "
                "AND shard<@shard_count ORDER BY shard",
                params={"pk": key_hash, "shard_count": shard_count},
                param_types={"pk": pt.STRING, "shard_count": pt.INT64},
            )
        )
        hold_rows = list(
            snapshot.execute_sql(
                "SELECT key_shard, COUNT(*), "
                "COALESCE(SUM(key_reserved_micro), 0) "
                "FROM tr_reservation WHERE key_hash=@kh AND settled=false "
                "GROUP BY key_shard",
                params={"kh": key_hash},
                param_types={"kh": pt.STRING},
            )
        )
    open_reservations = 0
    reserved_by_shard: dict[int, int] = {}
    for key_shard, count, reserved in hold_rows:
        shard = int(key_shard or 0)
        open_reservations += int(count)
        reserved_by_shard[shard] = reserved_by_shard.get(shard, 0) + int(
            reserved or 0
        )
    return rows, open_reservations, reserved_by_shard


def _validate_open_holds(
    rows: list[list[Any]],
    reserved_by_shard: dict[int, int],
) -> list[str]:
    reasons: list[str] = []
    observed = {int(row[0]): int(row[4]) for row in rows}
    unknown = sorted(set(reserved_by_shard) - set(observed))
    if unknown:
        reasons.append(f"open typed key holds reference unknown shards {unknown}")
    for shard, reserved in observed.items():
        held = reserved_by_shard.get(shard, 0)
        if reserved != held:
            reasons.append(
                f"typed key shard {shard} reserved={reserved} but open holds={held}"
            )
    return reasons


def inspect_key_usage_reshard(
    store: Any,
    key_hash: str,
    target_shard_count: int,
    *,
    preserve_open_holds: bool = False,
) -> KeyUsageReshardResult:
    target_count = key_usage_shard_count({"usage_shard_count": target_shard_count})
    key = store.api_keys.get_by_hash(key_hash)
    result = KeyUsageReshardResult(
        key_hash=key_hash,
        workspace_id=key.workspace_id if key is not None else None,
        target_shard_count=target_count,
    )
    if key is None:
        result.reasons.append("API key not found")
        return result
    workspace = store.get_workspace(key.workspace_id)
    if workspace is None:
        result.reasons.append("workspace not found")
    elif not workspace.billing_paused:
        result.reasons.append("workspace not billing-paused")
    if target_count > 1 and key.limit_microdollars is not None:
        result.reasons.append(
            "API key with an exact lifetime limit must remain on one usage shard"
        )

    try:
        current_count = key_usage_shard_count(key)
    except ValueError as exc:
        result.reasons.append(str(exc))
        return result
    result.current_shard_count = current_count
    rows, typed_open, reserved_by_shard = _typed_key_state(
        store, key_hash, current_count
    )
    result.typed_open_reservations = typed_open
    legacy = legacy_reservation_snapshot(store)
    result.legacy_open_reservations = legacy.live_by_key.get(key_hash, 0)
    result.stale_legacy_reservations_ignored = legacy.stale_by_key.get(key_hash, 0)
    if [int(row[0]) for row in rows] != list(range(current_count)):
        result.reasons.append("configured typed key usage shard set is incomplete")
        return result

    usage = sum(int(row[2]) for row in rows)
    byok_usage = sum(int(row[3]) for row in rows)
    reserved = sum(int(row[4]) for row in rows)
    result.usage_micro = usage
    result.byok_usage_micro = byok_usage
    result.reserved_micro = reserved
    result.reasons.extend(_validate_open_holds(rows, reserved_by_shard))
    if any(int(row[2]) < 0 or int(row[3]) < 0 or int(row[4]) < 0 for row in rows):
        result.reasons.append("typed key usage shard has a negative counter")
    if preserve_open_holds:
        if target_count < current_count:
            result.reasons.append(
                "hold-preserving reshard only supports increasing the shard count"
            )
    else:
        if reserved != 0:
            result.reasons.append(f"typed key has reserved={reserved}; wait for drain")
        if typed_open != 0:
            result.reasons.append(f"{typed_open} open typed reservations; wait for drain")
    if result.legacy_open_reservations != 0:
        result.reasons.append(
            f"{result.legacy_open_reservations} open legacy reservations; wait for drain"
        )
    return result


def reshard_key_usage(
    store: Any,
    key_hash: str,
    target_shard_count: int,
    *,
    apply: bool = False,
    preserve_open_holds: bool = False,
) -> KeyUsageReshardResult:
    status = inspect_key_usage_reshard(
        store,
        key_hash,
        target_shard_count,
        preserve_open_holds=preserve_open_holds,
    )
    if not status.ready or not apply:
        return status
    assert status.current_shard_count is not None
    if status.current_shard_count == status.target_shard_count:
        return status
    target_count = status.target_shard_count
    pt = store._param_types

    def txn(transaction: Any) -> dict[str, int] | None:
        # Derive floors per transaction attempt so a calendar-boundary crossing
        # (including one during an abort retry) cannot rewrite a current window
        # to a stale floor.
        floors = window_floors(utcnow())
        key = store._read_entity_tx(transaction, "api_key", key_hash, ApiKey)
        if key is None:
            return None
        workspace = store._read_entity_tx(
            transaction,
            "workspace",
            key.workspace_id,
            Workspace,
        )
        if workspace is None or not workspace.billing_paused:
            return None
        if target_count > 1 and key.limit_microdollars is not None:
            return None
        current_count = key_usage_shard_count(key)
        rows = list(
            transaction.execute_sql(
                "SELECT shard, limit_micro, usage, byok_usage, reserved, include_byok, "
                "day_limit_micro, week_limit_micro, month_limit_micro, "
                "day_usage, day_start, week_usage, week_start, month_usage, month_start "
                "FROM tr_key_limit WHERE key_hash=@pk AND shard>=0 "
                "AND shard<@shard_count ORDER BY shard",
                params={"pk": key_hash, "shard_count": current_count},
                param_types={"pk": pt.STRING, "shard_count": pt.INT64},
            )
        )
        if [int(row[0]) for row in rows] != list(range(current_count)):
            return None
        usage = sum(int(row[2]) for row in rows)
        byok_usage = sum(int(row[3]) for row in rows)
        reserved = sum(int(row[4]) for row in rows)
        if any(
            int(row[2]) < 0 or int(row[3]) < 0 or int(row[4]) < 0
            for row in rows
        ):
            return None
        if preserve_open_holds:
            if target_count < current_count:
                return None
        else:
            open_typed = int(
                list(
                    transaction.execute_sql(
                        "SELECT COUNT(*) FROM tr_reservation "
                        "WHERE key_hash=@kh AND settled = false",
                        params={"kh": key_hash},
                        param_types={"kh": pt.STRING},
                    )
                )[0][0]
            )
            if open_typed != 0 or reserved != 0:
                return None

        def window_total(usage_index: int, start_index: int, window: str) -> int:
            return sum(
                int(row[usage_index] or 0)
                for row in rows
                if row[start_index] is not None and row[start_index] >= floors[window]
            )

        day_usage = window_total(9, 10, "daily")
        week_usage = window_total(11, 12, "weekly")
        month_usage = window_total(13, 14, "monthly")
        if preserve_open_holds:
            usage_parts = [int(row[2]) for row in rows] + [0] * (
                target_count - current_count
            )
            byok_parts = [int(row[3]) for row in rows] + [0] * (
                target_count - current_count
            )
            reserved_parts = [int(row[4]) for row in rows] + [0] * (
                target_count - current_count
            )

            def current_window_parts(
                usage_index: int, start_index: int, window: str
            ) -> list[int]:
                return [
                    int(row[usage_index] or 0)
                    if row[start_index] is not None
                    and row[start_index] >= floors[window]
                    else 0
                    for row in rows
                ] + [0] * (target_count - current_count)

            day_parts = current_window_parts(9, 10, "daily")
            week_parts = current_window_parts(11, 12, "weekly")
            month_parts = current_window_parts(13, 14, "monthly")
        else:
            usage_parts = list(distribute_credit_amount(usage, target_count))
            byok_parts = list(distribute_credit_amount(byok_usage, target_count))
            reserved_parts = [0] * target_count
            day_parts = list(distribute_credit_amount(day_usage, target_count))
            week_parts = list(distribute_credit_amount(week_usage, target_count))
            month_parts = list(distribute_credit_amount(month_usage, target_count))
        commit_timestamp = store._spanner.COMMIT_TIMESTAMP
        transaction.insert_or_update(
            table=KEY_LIMIT_TABLE,
            columns=_KEY_RESHARD_COLUMNS,
            values=[
                (
                    key_hash,
                    shard,
                    key.limit_microdollars,
                    usage_parts[shard],
                    byok_parts[shard],
                    reserved_parts[shard],
                    key.include_byok_in_limit,
                    key.limit_daily_microdollars,
                    key.limit_weekly_microdollars,
                    key.limit_monthly_microdollars,
                    day_parts[shard],
                    floors["daily"],
                    week_parts[shard],
                    floors["weekly"],
                    month_parts[shard],
                    floors["monthly"],
                    commit_timestamp,
                    commit_timestamp,
                )
                for shard in range(target_count)
            ],
        )
        if current_count > target_count:
            transaction.delete(
                KEY_LIMIT_TABLE,
                store._spanner.KeySet(
                    keys=[
                        (key_hash, shard)
                        for shard in range(target_count, current_count)
                    ]
                ),
            )
        key.usage_shard_count = target_count
        key.usage_microdollars = usage
        key.byok_usage_microdollars = byok_usage
        key.reserved_microdollars = reserved
        key.updated_at = iso_now()
        transaction.insert_or_update(
            table=store.entity_table,
            columns=("kind", "id", "body", "updated_at"),
            values=[
                (
                    "api_key",
                    key_hash,
                    json_body(key),
                    commit_timestamp,
                )
            ],
        )
        return {"usage": usage, "byok_usage": byok_usage}

    changed = store._run_in_transaction(txn)
    if changed is None:
        status.reasons.append(
            "atomic key reshard preconditions changed; workspace remains paused"
        )
        return status
    verified = inspect_key_usage_reshard(
        store,
        key_hash,
        target_count,
        preserve_open_holds=preserve_open_holds,
    )
    if not verified.ready:
        verified.reasons.append("post-commit key reshard verification failed")
        return verified
    verified.applied = True
    return verified
