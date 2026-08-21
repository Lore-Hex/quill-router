from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from google.cloud.bigtable.row_filters import ValueRegexFilter

from trusted_router.regional_quota_ledger import (
    BigtableRegionalQuotaLedger,
    InMemoryRegionalQuotaLedger,
    RegionalLeaseLedgerError,
    settled_key_totals,
)
from trusted_router.services.regional_quota_leases import RegionalQuotaLease

NOW = datetime(2026, 8, 21, tzinfo=UTC)


def _lease(*, grant: int = 10_000) -> RegionalQuotaLease:
    return RegionalQuotaLease(
        lease_id="rql-test",
        workspace_id="workspace-test",
        region="us-central1",
        fencing_token=9,
        granted_microdollars=grant,
        expires_at=NOW + timedelta(minutes=1),
    )


def test_in_memory_ledger_serializes_concurrent_reservations_exactly() -> None:
    ledger = InMemoryRegionalQuotaLedger()
    ledger.initialize(_lease(grant=10_000))

    def reserve(index: int) -> None:
        ledger.reserve(
            "rql-test",
            region="us-central1",
            hold_id=f"hold-{index}",
            fingerprint=f"fingerprint-{index}",
            amount_microdollars=100,
            fencing_token=9,
            key_hash="key-1",
            key_shard=index % 16,
            hold_expires_at=NOW + timedelta(hours=2),
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(reserve, range(100)))

    current = ledger.get("rql-test", region="us-central1")
    assert current is not None
    assert current.reserved_microdollars == 10_000
    assert current.available_microdollars == 0
    assert len(current.holds) == 100


def test_ledger_replays_ambiguous_commit_without_duplicate_hold() -> None:
    table = _FakeBigtableTable()
    ledger = BigtableRegionalQuotaLedger({"us-central1": table})
    ledger.initialize(_lease())
    table.raise_after_next_applied_commit = True

    current = ledger.reserve(
        "rql-test",
        region="us-central1",
        hold_id="hold-1",
        fingerprint="same",
        amount_microdollars=500,
        fencing_token=9,
        key_hash="key-1",
        key_shard=3,
        hold_expires_at=NOW + timedelta(hours=2),
        now=NOW,
    )

    assert current.reserved_microdollars == 500
    assert len(current.holds) == 1
    durable = ledger.get("rql-test", region="us-central1")
    assert durable == current


def test_bigtable_round_trip_preserves_key_shard_expiry_and_settlement() -> None:
    table = _FakeBigtableTable()
    ledger = BigtableRegionalQuotaLedger({"us-central1": table})
    ledger.initialize(_lease())
    ledger.reserve(
        "rql-test",
        region="us-central1",
        hold_id="hold-1",
        fingerprint="same",
        amount_microdollars=500,
        fencing_token=9,
        key_hash="key-1",
        key_shard=7,
        hold_expires_at=NOW + timedelta(hours=2),
        now=NOW,
    )
    settled = ledger.settle(
        "rql-test",
        region="us-central1",
        hold_id="hold-1",
        actual_microdollars=275,
        fencing_token=9,
    )

    assert settled.spent_microdollars == 275
    assert settled.holds[0].expires_at == NOW + timedelta(hours=2)
    assert settled_key_totals(settled) == {("key-1", 7): 275}


def test_bigtable_ledger_fails_closed_without_authoritative_region_profile() -> None:
    ledger = BigtableRegionalQuotaLedger({"us-central1": _FakeBigtableTable()})
    with pytest.raises(RegionalLeaseLedgerError, match="no fixed Bigtable app profile"):
        ledger.get("rql-test", region="europe-west4")


def test_bigtable_health_check_proves_conditional_write_and_read() -> None:
    table = _FakeBigtableTable()
    ledger = BigtableRegionalQuotaLedger({"us-central1": table})

    assert ledger.health_check() == ("us-central1",)
    assert b"health#regional-quota#us-central1" in table.rows


def test_bigtable_health_check_fails_when_transactional_writes_fail() -> None:
    table = _FakeBigtableTable()
    table.reject_conditional_commits = True
    ledger = BigtableRegionalQuotaLedger({"us-central1": table})

    with pytest.raises(RegionalLeaseLedgerError, match="transactional health check"):
        ledger.health_check()


@dataclass
class _Cell:
    value: bytes


class _ReadRow:
    def __init__(self, values: dict[bytes, bytes]) -> None:
        self.cells = {"lease": {qualifier: [_Cell(value)] for qualifier, value in values.items()}}


class _FakeConditionalRow:
    def __init__(self, table: _FakeBigtableTable, row_key: bytes, filter_: Any) -> None:
        self.table = table
        self.row_key = row_key
        self.filter = filter_
        self.mutations: list[tuple[bool, bytes, bytes]] = []

    def set_cell(
        self,
        _family: str,
        column: bytes,
        value: bytes,
        *,
        state: bool,
    ) -> None:
        self.mutations.append((state, column, value))

    def commit(self) -> bool:
        if self.table.reject_conditional_commits:
            raise RuntimeError("transactional writes disabled")
        current = self.table.rows.get(self.row_key)
        regex_filter = next(
            (
                candidate
                for candidate in self.filter.filters
                if isinstance(candidate, ValueRegexFilter)
            ),
            None,
        )
        if regex_filter is None:
            matched = current is not None and b"version" in current
        else:
            expected = regex_filter.regex.removeprefix(b"^").removesuffix(b"$")
            matched = (
                current is not None
                and re.fullmatch(
                    expected,
                    current.get(b"version", b""),
                )
                is not None
            )
        selected = [mutation for mutation in self.mutations if mutation[0] == matched]
        if selected:
            values = self.table.rows.setdefault(self.row_key, {})
            for _state, column, value in selected:
                values[column] = value
        if self.table.raise_after_next_applied_commit and selected:
            self.table.raise_after_next_applied_commit = False
            raise TimeoutError("reply lost after durable apply")
        return matched


class _FakeBigtableTable:
    def __init__(self) -> None:
        self.rows: dict[bytes, dict[bytes, bytes]] = {}
        self.raise_after_next_applied_commit = False
        self.reject_conditional_commits = False

    def row(self, row_key: bytes, *, filter_: Any) -> _FakeConditionalRow:
        return _FakeConditionalRow(self, row_key, filter_)

    def read_row(self, row_key: bytes, *, filter_: Any) -> _ReadRow | None:
        del filter_
        values = self.rows.get(row_key)
        return None if values is None else _ReadRow(values)
