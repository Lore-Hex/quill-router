from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from google.cloud.bigtable.row_filters import CellsColumnLimitFilter, ValueRegexFilter

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
    assert ledger.supports_region("us-central1") is True
    assert ledger.supports_region("europe-west4") is False
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


def test_bigtable_reads_use_a_bounded_hot_path_retry_deadline() -> None:
    table = _FakeBigtableTable()
    ledger = BigtableRegionalQuotaLedger(
        {"us-central1": table},
        operation_timeout_seconds=2.0,
    )
    ledger.initialize(_lease())

    assert ledger.get("rql-test", region="us-central1") == _lease()
    assert table.read_retry_deadlines
    assert max(table.read_retry_deadlines) <= 1.0


def test_bigtable_cas_commit_uses_remaining_operation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _FakeBigtableTable()
    ledger = BigtableRegionalQuotaLedger(
        {"us-central1": table},
        operation_timeout_seconds=2.0,
    )
    ledger.initialize(_lease())
    observed_timeouts: list[float] = []
    original = ledger._commit_conditional_row

    def recording_commit(row: Any, *, timeout_seconds: float) -> bool:
        observed_timeouts.append(timeout_seconds)
        return original(row, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(ledger, "_commit_conditional_row", recording_commit)

    ledger.reserve(
        "rql-test",
        region="us-central1",
        hold_id="bounded-hold",
        fingerprint="bounded-fingerprint",
        amount_microdollars=100,
        fencing_token=9,
        key_hash="key-1",
        key_shard=1,
        now=NOW,
    )

    assert observed_timeouts
    assert all(0 < timeout <= 2.0 for timeout in observed_timeouts)


def test_bigtable_conditional_commit_forwards_explicit_rpc_timeout() -> None:
    row = _FakeLegacyConditionalRow()

    assert BigtableRegionalQuotaLedger._commit_conditional_row(
        row,
        timeout_seconds=0.75,
    )
    assert row._table._instance._client.table_data_client.calls == [0.75]
    assert row.cleared is True


def test_bigtable_operation_timeout_rejects_unsafe_values() -> None:
    with pytest.raises(ValueError, match="between 1.1 and 10"):
        BigtableRegionalQuotaLedger(
            {"us-central1": _FakeBigtableTable()},
            operation_timeout_seconds=1.0,
        )


def test_bigtable_cas_matches_only_the_latest_version_cell() -> None:
    ledger = BigtableRegionalQuotaLedger({"us-central1": _FakeBigtableTable()})

    predicate = ledger._version_equals_filter(b"stale-version")
    filter_types = [type(filter_) for filter_ in predicate.filters]

    # Bigtable applies a RowFilterChain in order. Limit the version column to
    # its newest cell before checking the expected value, otherwise a retained
    # historical version can satisfy the CAS and let a stale writer win.
    assert filter_types.index(CellsColumnLimitFilter) < filter_types.index(
        ValueRegexFilter
    )


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
        self.read_retry_deadlines: list[float] = []

    def row(self, row_key: bytes, *, filter_: Any) -> _FakeConditionalRow:
        return _FakeConditionalRow(self, row_key, filter_)

    def read_row(self, row_key: bytes, *, filter_: Any, retry: Any) -> _ReadRow | None:
        del filter_
        self.read_retry_deadlines.append(float(retry._deadline))
        values = self.rows.get(row_key)
        return None if values is None else _ReadRow(values)


class _FakeGeneratedDataClient:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def check_and_mutate_row(self, **kwargs: Any) -> Any:
        self.calls.append(float(kwargs["timeout"]))
        return type("Response", (), {"predicate_matched": True})()


class _FakeLegacyConditionalRow:
    def __init__(self) -> None:
        data_client = _FakeGeneratedDataClient()
        client = type("Client", (), {"table_data_client": data_client})()
        instance = type("Instance", (), {"_client": client})()
        self._table = type(
            "Table",
            (),
            {
                "_instance": instance,
                "_app_profile_id": "tr-rql-us-central1",
                "name": "projects/test/instances/test/tables/quota",
            },
        )()
        self._row_key = b"row-key"
        self._filter = type("Filter", (), {"to_pb": lambda _self: object()})()
        self.cleared = False

    def _get_mutations(self, *, state: bool) -> list[object]:
        return [object()] if state else []

    def clear(self) -> None:
        self.cleared = True
