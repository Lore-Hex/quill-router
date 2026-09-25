"""Checked batch DML for independent statements in a read-write transaction."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from google.api_core.exceptions import FailedPrecondition, from_grpc_status

DmlStatement = tuple[str, dict[str, Any], dict[str, Any]]


def execute_batch_dml(
    transaction: Any,
    statements: Sequence[DmlStatement],
    expected_counts: Sequence[tuple[int, ...]],
) -> None:
    """Raise before commit on a failed statement or unexpected affected-row count.

    Batch DML returns errors in its status, unlike execute_update. Convert these
    to the same API exceptions so ABORTED retries and ALREADY_EXISTS replay work.
    Retention clears allow 0 (absent/already clear) or 1; INSERTs require 1.
    """
    if len(statements) != len(expected_counts):
        raise ValueError("Each batch statement requires a row-count contract")
    status, row_counts = transaction.batch_update(statements)
    if status.code != 0:
        raise from_grpc_status(status.code, status.message, details=tuple(status.details))
    if len(row_counts) != len(statements) or any(
        count not in allowed
        for count, allowed in zip(row_counts, expected_counts, strict=True)
    ):
        raise FailedPrecondition("Unexpected batch DML row counts; transaction must roll back")
