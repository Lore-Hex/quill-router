"""Checked, ordered batch DML in a read-write transaction."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from google.api_core.exceptions import Aborted, FailedPrecondition, from_grpc_status
from google.rpc import code_pb2
from google.rpc.error_details_pb2 import RetryInfo
from google.rpc.status_pb2 import Status

DmlStatement = tuple[str, dict[str, Any], dict[str, Any]]


class _BatchDmlAbortCause:
    """Supply the RPC retry metadata the SDK reads from ``Aborted.errors[0]``."""

    def __init__(self, status: Status) -> None:
        self._metadata: tuple[tuple[str, bytes], ...] = ()
        for detail in status.details:
            retry_info = RetryInfo()
            if detail.Unpack(retry_info):
                self._metadata = (("google.rpc.retryinfo-bin", retry_info.SerializeToString()),)
                break

    def trailing_metadata(self) -> tuple[tuple[str, bytes], ...]:
        return self._metadata


def execute_batch_dml(
    transaction: Any,
    statements: Sequence[DmlStatement],
    expected_counts: Sequence[tuple[int, ...]],
    *,
    check_prefix: Callable[[Sequence[int]], None] | None = None,
) -> None:
    """Raise before commit on a failed statement or unexpected affected-row count.

    Batch DML returns errors in its status, unlike execute_update. Convert these
    to the same API exceptions so ABORTED retries and ALREADY_EXISTS replay work.
    Retention clears allow 0 (absent/already clear) or 1; INSERTs require 1.
    check_prefix may raise a business fallback on a successfully executed prefix,
    even if a later speculative statement failed. ABORTED always takes precedence;
    otherwise status and exact counts must still pass before this helper returns.
    """
    if len(statements) != len(expected_counts):
        raise ValueError("Each batch statement requires a row-count contract")
    status, row_counts = transaction.batch_update(statements)
    if status.code == code_pb2.ABORTED:
        # from_grpc_status alone leaves errors empty, crashing the SDK retry
        # loop. Preserve the server delay, or let empty metadata select backoff.
        raise Aborted(
            status.message, errors=(_BatchDmlAbortCause(status),), details=tuple(status.details),
        )
    if check_prefix is not None:
        check_prefix(row_counts)
    if status.code != 0:
        raise from_grpc_status(status.code, status.message, details=tuple(status.details))
    if len(row_counts) != len(statements) or any(
        count not in allowed
        for count, allowed in zip(row_counts, expected_counts, strict=True)
    ):
        raise FailedPrecondition("Unexpected batch DML row counts; transaction must roll back")
