"""Operator-only split/consolidation for uncapped API-key usage rows."""

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
from trusted_router.storage_models import ApiKey, Reservation, Workspace, iso_now

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
    reasons: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def ready(self) -> bool:
        return not self.reasons


def _limits(key: ApiKey) -> tuple[int | None, int | None, int | None, int | None]:
    return (
        key.limit_microdollars,
        key.limit_daily_microdollars,
        key.limit_weekly_microdollars,
        key.limit_monthly_microdollars,
    )


def _typed_key_state(
    store: Any,
    key_hash: str,
    shard_count: int,
) -> tuple[list[list[Any]], int]:
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
        open_reservations = int(
            list(
                snapshot.execute_sql(
                    "SELECT COUNT(*) FROM tr_reservation "
                    "WHERE key_hash=@kh AND settled = false",
                    params={"kh": key_hash},
                    param_types={"kh": pt.STRING},
                )
            )[0][0]
        )
    return rows, open_reservations


def inspect_key_usage_reshard(
    store: Any,
    key_hash: str,
    target_shard_count: int,
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
    if target_count > 1 and any(limit is not None for limit in _limits(key)):
        result.reasons.append("capped API key must remain on one usage shard")

    try:
        current_count = key_usage_shard_count(key)
    except ValueError as exc:
        result.reasons.append(str(exc))
        return result
    result.current_shard_count = current_count
    rows, typed_open = _typed_key_state(store, key_hash, current_count)
    result.typed_open_reservations = typed_open
    result.legacy_open_reservations = sum(
        1
        for reservation in store._list_entities("reservation", cls=Reservation)
        if reservation.key_hash == key_hash and not reservation.settled
    )
    if [int(row[0]) for row in rows] != list(range(current_count)):
        result.reasons.append("configured typed key usage shard set is incomplete")
        return result

    usage = sum(int(row[2]) for row in rows)
    byok_usage = sum(int(row[3]) for row in rows)
    reserved = sum(int(row[4]) for row in rows)
    result.usage_micro = usage
    result.byok_usage_micro = byok_usage
    result.reserved_micro = reserved
    if any(int(row[2]) < 0 or int(row[3]) < 0 or int(row[4]) < 0 for row in rows):
        result.reasons.append("typed key usage shard has a negative counter")
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
) -> KeyUsageReshardResult:
    status = inspect_key_usage_reshard(store, key_hash, target_shard_count)
    if not status.ready or not apply:
        return status
    assert status.current_shard_count is not None
    if status.current_shard_count == status.target_shard_count:
        return status
    target_count = status.target_shard_count
    pt = store._param_types
    floors = window_floors(utcnow())

    def txn(transaction: Any) -> dict[str, int] | None:
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
        if target_count > 1 and any(limit is not None for limit in _limits(key)):
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
        usage = sum(int(row[2]) for row in rows)
        byok_usage = sum(int(row[3]) for row in rows)
        reserved = sum(int(row[4]) for row in rows)
        if (
            open_typed != 0
            or reserved != 0
            or any(int(row[2]) < 0 or int(row[3]) < 0 or int(row[4]) < 0 for row in rows)
        ):
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
        usage_parts = distribute_credit_amount(usage, target_count)
        byok_parts = distribute_credit_amount(byok_usage, target_count)
        day_parts = distribute_credit_amount(day_usage, target_count)
        week_parts = distribute_credit_amount(week_usage, target_count)
        month_parts = distribute_credit_amount(month_usage, target_count)
        commit_timestamp = store._spanner.COMMIT_TIMESTAMP
        transaction.insert_or_update(
            table=KEY_LIMIT_TABLE,
            columns=_KEY_RESHARD_COLUMNS,
            values=[
                (
                    key_hash,
                    shard,
                    None,
                    usage_parts[shard],
                    byok_parts[shard],
                    0,
                    key.include_byok_in_limit,
                    None,
                    None,
                    None,
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
        key.reserved_microdollars = 0
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
    verified = inspect_key_usage_reshard(store, key_hash, target_count)
    if not verified.ready:
        verified.reasons.append("post-commit key reshard verification failed")
        return verified
    verified.applied = True
    return verified
