"""Arm TTL retention for settled legacy reservations.

This is a guarded, idempotent operator backfill. It is read-only by default;
pass ``--apply`` to set ``tr_reservation.terminal_at`` in bounded batches.

Examples:
  uv run python scripts/backfill_reservation_terminal_at.py
  uv run python scripts/backfill_reservation_terminal_at.py --batch 5000 --apply
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")

from trusted_router.config import Settings
from trusted_router.storage import create_store

DEFAULT_BATCH_SIZE = 5000

_CANDIDATE_COUNT_SQL = (
    "SELECT COUNT(*) FROM tr_reservation "
    "WHERE settled AND terminal_at IS NULL"
)
_ARMED_COUNT_SQL = (
    "SELECT COUNT(*) FROM tr_reservation "
    "WHERE settled AND terminal_at IS NOT NULL"
)
_OPEN_HOLD_COUNT_SQL = "SELECT COUNT(*) FROM tr_reservation WHERE NOT settled"
_EXCLUDED_COUNT_SQL = (
    "SELECT COUNT(*) FROM tr_reservation "
    "WHERE settled AND terminal_at IS NULL "
    "AND EXISTS (SELECT 1 FROM tr_settle_outbox o "
    "WHERE o.authorization_id = tr_reservation.authorization_id "
    "AND o.status IN ('pending', 'dead'))"
)
_GATEWAY_AUTHORIZATION_PINNED_COUNT_SQL = (
    "SELECT COUNT(*) FROM tr_gateway_authorization "
    "WHERE settled AND terminal_at IS NULL"
)
_SELECT_BATCH_SQL = (
    "SELECT reservation_id FROM tr_reservation "
    "WHERE settled AND terminal_at IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM tr_settle_outbox o "
    "WHERE o.authorization_id = tr_reservation.authorization_id "
    "AND o.status IN ('pending', 'dead')) "
    "ORDER BY reservation_id LIMIT @batch"
)

# The guard is load-bearing (issue #357, lesson from #339). A pending/dead
# outbox intent FREEZES its hold. Arming terminal_at under that intent would put
# a 30-day fuse on evidence the frozen intent needs. Keep the guard in the SAME
# transaction as the write; the production zero-frozen observation can change
# while this backfill runs.
#
# `settled` is equally load-bearing. An open hold must NEVER receive
# terminal_at: the reaper owns its lifecycle, and putting a TTL fuse on a live
# hold is corruption.
_UPDATE_BATCH_SQL = (
    "UPDATE tr_reservation SET terminal_at=@terminal_at "
    "WHERE reservation_id IN UNNEST(@ids) "
    "AND settled AND terminal_at IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM tr_settle_outbox o "
    "WHERE o.authorization_id = tr_reservation.authorization_id "
    "AND o.status IN ('pending', 'dead'))"
)


@dataclass(frozen=True)
class BackfillStatus:
    candidates: int
    armed: int
    open_holds: int
    excluded: int
    gateway_authorization_pinned: int

    @property
    def eligible(self) -> int:
        return self.candidates - self.excluded


def _scalar_count(reader: Any, sql: str) -> int:
    rows = list(reader.execute_sql(sql))
    if len(rows) != 1:
        raise RuntimeError(f"expected one count row, got {len(rows)}")
    return int(rows[0][0])


def inspect_status(store: Any) -> BackfillStatus:
    """Read all operator status counters from one consistent snapshot."""
    with store._database.snapshot(multi_use=True) as snapshot:
        return BackfillStatus(
            candidates=_scalar_count(snapshot, _CANDIDATE_COUNT_SQL),
            armed=_scalar_count(snapshot, _ARMED_COUNT_SQL),
            open_holds=_scalar_count(snapshot, _OPEN_HOLD_COUNT_SQL),
            excluded=_scalar_count(snapshot, _EXCLUDED_COUNT_SQL),
            gateway_authorization_pinned=_scalar_count(
                snapshot,
                _GATEWAY_AUTHORIZATION_PINNED_COUNT_SQL,
            ),
        )


def _print_status(status: BackfillStatus) -> None:
    print(
        "STATUS: tr_reservation "
        f"candidates={status.candidates} armed={status.armed} "
        f"open_holds={status.open_holds} excluded={status.excluded}"
    )
    print(
        "STATUS: tr_gateway_authorization "
        f"pinned={status.gateway_authorization_pinned} "
        "(informational cross-check)"
    )


def run_status(store: Any) -> BackfillStatus:
    """Inspect and print status. This is always the first action of a run."""
    status = inspect_status(store)
    _print_status(status)
    return status


def _select_batch(store: Any, batch: int) -> list[str]:
    with store._database.snapshot() as snapshot:
        rows = snapshot.execute_sql(
            _SELECT_BATCH_SQL,
            params={"batch": batch},
            param_types={"batch": store._param_types.INT64},
        )
        return [str(row[0]) for row in rows]


def _arm_batch(store: Any, reservation_ids: list[str]) -> int:
    # Legacy rows have no created_at, so "now" is the only honest timestamp.
    # The resulting 30-day tail before TTL deletion is the point, not a defect.
    terminal_at = datetime.now(UTC)

    def txn(transaction: Any) -> int:
        return int(
            transaction.execute_update(
                _UPDATE_BATCH_SQL,
                params={
                    "terminal_at": terminal_at,
                    "ids": reservation_ids,
                },
                param_types={
                    "terminal_at": store._param_types.TIMESTAMP,
                    "ids": store._param_types.Array(store._param_types.STRING),
                },
            )
        )

    return int(store._run_in_transaction(txn))


def _print_final(status: BackfillStatus, armed: int) -> None:
    print(
        f"FINAL: armed={armed} remaining_candidates={status.candidates} "
        f"excluded={status.excluded}"
    )


def run_backfill(
    store: Any,
    *,
    batch: int = DEFAULT_BATCH_SIZE,
    apply: bool = False,
) -> int:
    """Print status, then optionally arm all currently eligible legacy rows."""
    if batch < 1:
        raise ValueError("batch must be at least 1")

    initial = run_status(store)
    if not apply:
        print(
            f"DRY-RUN: would arm {initial.eligible} rows "
            f"in batches of {batch}"
        )
        return 0

    # This is a one-off debt drain. Steady-state arming keeps the table bounded,
    # so the candidate count remains approximately zero after it completes.
    # Re-running at any time is safe because every read and write requires
    # terminal_at IS NULL.
    batch_number = 0
    running_total = 0
    while True:
        reservation_ids = _select_batch(store, batch)
        if not reservation_ids:
            final = inspect_status(store)
            _print_final(final, running_total)
            if final.candidates:
                print(
                    "STOP: all remaining candidates are excluded by the "
                    "pending/dead tr_settle_outbox frozen-intent guard"
                )
            else:
                print("COMPLETE: no unarmed settled reservations remain")
            return 0

        batch_number += 1
        updated = _arm_batch(store, reservation_ids)
        running_total += updated
        print(
            f"batch {batch_number}: updated={updated} "
            f"running_total={running_total}"
        )

        if updated == 0:
            current = inspect_status(store)
            if current.candidates == 0:
                _print_final(current, running_total)
                print("COMPLETE: no unarmed settled reservations remain")
                return 0
            if current.eligible == 0:
                _print_final(current, running_total)
                print(
                    "STOP: all remaining candidates are excluded by the "
                    "pending/dead tr_settle_outbox frozen-intent guard"
                )
                return 0


def _positive_batch(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("batch must be an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("batch must be at least 1")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=_positive_batch, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    store = create_store(Settings())
    return run_backfill(store, batch=args.batch, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
