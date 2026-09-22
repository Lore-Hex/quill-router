"""Bound migration-only Bigtable writes on the post-settlement path."""

from __future__ import annotations

from typing import Any

MIRROR_WRITE_TIMEOUT_SECONDS = 1.0


def commit_mirror_rows(table: Any, rows: list[Any]) -> None:
    # DirectRow.commit uses the client's two-minute retry policy and may hide
    # per-row failures. Durable metadata is already in Spanner; never spend
    # that retry budget while the gateway waits for a settlement response.
    statuses = table.mutate_rows(rows, retry=None, timeout=MIRROR_WRITE_TIMEOUT_SECONDS)
    if len(statuses) != len(rows) or any(status.code != 0 for status in statuses):
        raise RuntimeError("Bigtable mirror mutation incomplete; reconciliation required")
