"""Native-Spanner monotonic watermark for signed Stage D policies."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from trusted_router.storage_gcp_io import run_in_transaction_with_retry


def get_stage_d_policy_watermark(
    database: Any,
    param_types: Any,
    *,
    plane: str,
) -> int | None:
    """Return the durable acceptance floor for one plane, if initialized."""

    with database.snapshot() as snapshot:
        rows = list(
            snapshot.execute_sql(
                "SELECT highest_sequence FROM tr_stage_d_policy_watermark WHERE plane=@plane",
                params={"plane": plane},
                param_types={"plane": param_types.STRING},
            )
        )
    return int(rows[0][0]) if rows else None


def advance_stage_d_policy_watermark(
    database: Any,
    param_types: Any,
    *,
    plane: str,
    sequence: int,
    updated_at: datetime,
) -> bool:
    """Advance the singleton plane row iff ``sequence`` is strictly newer."""

    def txn(transaction: Any) -> bool:
        rows = list(
            transaction.execute_sql(
                "SELECT highest_sequence FROM tr_stage_d_policy_watermark WHERE plane=@plane",
                params={"plane": plane},
                param_types={"plane": param_types.STRING},
            )
        )
        if rows:
            if int(rows[0][0]) >= sequence:
                return False
            updated = transaction.execute_update(
                "UPDATE tr_stage_d_policy_watermark SET highest_sequence=@sequence, "
                "updated_at=@updated_at WHERE plane=@plane AND highest_sequence<@sequence",
                params={
                    "plane": plane,
                    "sequence": sequence,
                    "updated_at": updated_at,
                },
                param_types={
                    "plane": param_types.STRING,
                    "sequence": param_types.INT64,
                    "updated_at": param_types.TIMESTAMP,
                },
            )
            return int(updated) == 1
        inserted = transaction.execute_update(
            "INSERT INTO tr_stage_d_policy_watermark "
            "(plane, highest_sequence, updated_at) VALUES (@plane, @sequence, @updated_at)",
            params={
                "plane": plane,
                "sequence": sequence,
                "updated_at": updated_at,
            },
            param_types={
                "plane": param_types.STRING,
                "sequence": param_types.INT64,
                "updated_at": param_types.TIMESTAMP,
            },
        )
        return int(inserted) == 1

    return bool(
        run_in_transaction_with_retry(
            database,
            txn,
            transaction_tag="tr_stage_d_policy_watermark",
        )
    )
