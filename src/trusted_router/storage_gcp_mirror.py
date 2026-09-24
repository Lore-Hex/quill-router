"""Bound migration-only Bigtable writes on the post-settlement path."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

# The legacy SDK's ExponentialTimeout floors remaining seconds. A nominal
# one-second budget becomes zero before its first RPC. Leave room for transport
# setup while bounding each batch independently of the default retry policy.
MIRROR_WRITE_TIMEOUT_SECONDS = 5.0

# A second attempt only makes sense with enough budget left for a real RPC
# after the SDK floors the remaining seconds; below this, fail fast instead.
MIRROR_RETRY_MIN_BUDGET_SECONDS = 2.0

# Pause before the single retry so a tablet move or channel reset can finish.
MIRROR_RETRY_BACKOFF_SECONDS = 0.2

# gRPC codes the Bigtable SDK itself treats as transient for row mutations:
# DEADLINE_EXCEEDED, ABORTED, INTERNAL, UNAVAILABLE. A missing status means
# the transport failed before the server answered, which is transient too.
RETRYABLE_STATUS_CODES = frozenset({4, 10, 13, 14})


class MirrorWriteIncomplete(RuntimeError):
    """Some mirror rows did not land after the bounded attempts.

    The durable copies (Spanner, and the ClickHouse activity outbox) are
    unaffected; only the Bigtable fallback/shadow index is missing rows.
    ``reconcile_generation_activity(workspace_id, date=...)`` re-mirrors them
    from Spanner (``python -m trusted_router.activity_mirror_reconcile_cli``).
    """

    def __init__(self, *, attempts: int, total: int, codes: Sequence[int | None]) -> None:
        self.attempts = attempts
        self.total = total
        self.codes = tuple(codes)
        rendered = ",".join("missing" if code is None else str(code) for code in self.codes)
        super().__init__(
            "Bigtable mirror mutation incomplete after "
            f"{attempts} attempt(s): {len(self.codes)} of {total} rows failed "
            f"(status codes {rendered}); durable copies are intact, re-mirror via "
            "reconcile_generation_activity(workspace_id, date)"
        )


def _failed_indexes(statuses: Sequence[Any], expected: int) -> list[tuple[int, int | None]]:
    failed: list[tuple[int, int | None]] = []
    for index in range(expected):
        status = statuses[index] if index < len(statuses) else None
        code = None if status is None else int(status.code)
        if code != 0:
            failed.append((index, code))
    return failed


def commit_mirror_rows(table: Any, rows: list[Any]) -> None:
    # DirectRow.commit uses the client's two-minute retry policy and may hide
    # per-row failures. Durable metadata is already in Spanner; never spend
    # that retry budget while the gateway waits for a settlement response.
    # Instead: one attempt for every row, then at most one more attempt for
    # the rows that failed transiently, inside the same wall-clock budget.
    started = time.monotonic()
    statuses = table.mutate_rows(rows, retry=None, timeout=MIRROR_WRITE_TIMEOUT_SECONDS)
    failed = _failed_indexes(statuses, len(rows))
    if not failed:
        return
    attempts = 1
    codes = [code for _index, code in failed]
    retryable = all(code is None or code in RETRYABLE_STATUS_CODES for code in codes)
    remaining = MIRROR_WRITE_TIMEOUT_SECONDS - (time.monotonic() - started)
    if retryable and remaining - MIRROR_RETRY_BACKOFF_SECONDS >= MIRROR_RETRY_MIN_BUDGET_SECONDS:
        time.sleep(MIRROR_RETRY_BACKOFF_SECONDS)
        remaining = MIRROR_WRITE_TIMEOUT_SECONDS - (time.monotonic() - started)
        # Successful rows had their mutations cleared by the SDK; resend only
        # the rows that failed, each still carrying its mutations.
        retry_rows = [rows[index] for index, _code in failed]
        statuses = table.mutate_rows(
            retry_rows,
            retry=None,
            timeout=max(remaining, MIRROR_RETRY_MIN_BUDGET_SECONDS),
        )
        attempts = 2
        failed = _failed_indexes(statuses, len(retry_rows))
        if not failed:
            return
        codes = [code for _index, code in failed]
    raise MirrorWriteIncomplete(attempts=attempts, total=len(rows), codes=codes)
