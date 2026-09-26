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
    Repair typed or legacy generations from Spanner with
    ``python -m trusted_router.activity_mirror_reconcile_cli --generation-id <id>``.
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
            "python -m trusted_router.activity_mirror_reconcile_cli --generation-id <id>"
        )


def _failed_indexes(statuses: Sequence[Any], expected: int) -> list[tuple[int, int | None]]:
    failed: list[tuple[int, int | None]] = []
    for index in range(expected):
        status = statuses[index] if index < len(statuses) else None
        code = None if status is None else int(status.code)
        if code != 0:
            failed.append((index, code))
    return failed


def _mutate_rows(table: Any, rows: list[Any], timeout: float) -> Sequence[Any]:
    from google.cloud.bigtable.table import Table, _RetryableMutateRowsWorker

    if not isinstance(table, Table):
        return table.mutate_rows(rows, retry=None, timeout=timeout)
    # Table.mutate_rows discards its worker when an incomplete stream raises.
    # Keep the SDK's indexed statuses, including permanent failures, and let
    # the SDK clear only successful mutations. No extra SDK retries are enabled.
    worker = _RetryableMutateRowsWorker(
        table._instance._client,
        table.name,
        rows,
        app_profile_id=table._app_profile_id,
        timeout=timeout,
    )
    try:
        return worker(retry=None)
    except RuntimeError as exc:
        if (
            len(exc.args) != 4
            or exc.args[0] != "Unexpected number of responses"
            or exc.args[2] != "Expected"
            or exc.args[3] != len(rows)
            or not isinstance(exc.args[1], int)
            or not 0 <= exc.args[1] < len(rows)
        ):
            raise
        return worker.responses_statuses


def commit_mirror_rows(table: Any, rows: list[Any]) -> None:
    # DirectRow.commit uses the client's two-minute retry policy and may hide
    # per-row failures. Durable metadata is already in Spanner; never spend
    # that retry budget while the gateway waits for a settlement response.
    # Instead: one attempt for every row, then at most one more attempt for
    # the rows that failed transiently, inside the same wall-clock budget.
    started = time.monotonic()
    statuses = _mutate_rows(table, rows, MIRROR_WRITE_TIMEOUT_SECONDS)
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
        if remaining < MIRROR_RETRY_MIN_BUDGET_SECONDS:
            raise MirrorWriteIncomplete(attempts=attempts, total=len(rows), codes=codes)
        # Successful rows had their mutations cleared by the SDK; resend only
        # the rows that failed, each still carrying its mutations.
        retry_rows = [rows[index] for index, _code in failed]
        statuses = _mutate_rows(table, retry_rows, remaining)
        attempts = 2
        failed = _failed_indexes(statuses, len(retry_rows))
        if not failed:
            return
        codes = [code for _index, code in failed]
    raise MirrorWriteIncomplete(attempts=attempts, total=len(rows), codes=codes)
