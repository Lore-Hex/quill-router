from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_storage_gcp import _generation
from trusted_router.storage_gcp_activity_index import write_generation
from trusted_router.storage_gcp_benchmark_index import write_provider_benchmark
from trusted_router.storage_models import ProviderBenchmarkSample


class MirrorTable:
    def __init__(self, codes: list[int] | None = None) -> None:
        self.codes = codes
        self.calls: list[tuple[list[Any], Any, float]] = []

    def direct_row(self, key: bytes) -> Any:
        class Row:
            def __init__(self) -> None:
                self.row_key = key
                self.cells: list[tuple[Any, ...]] = []

            def set_cell(self, *args: Any) -> None:
                self.cells.append(args)

            def commit(self) -> None:
                raise AssertionError("unbounded individual commit is forbidden")

        return Row()

    def mutate_rows(self, rows: list[Any], *, retry: Any, timeout: float) -> list[Any]:
        self.calls.append((rows, retry, timeout))
        codes = [0] * len(rows) if self.codes is None else self.codes
        return [SimpleNamespace(code=code) for code in codes]


def write(table: MirrorTable, kind: str) -> None:
    generation = _generation("gen_deadline", "ws_deadline", "2026-09-22T00:00:00Z")
    if kind == "activity":
        write_generation(table, "activity", generation)
    else:
        write_provider_benchmark(
            table, "benchmark", ProviderBenchmarkSample.from_generation(generation)
        )


@pytest.mark.parametrize(("kind", "count"), [("activity", 3), ("benchmark", 6)])
def test_mirrors_batch_all_indexes_with_one_bounded_attempt(kind: str, count: int) -> None:
    table = MirrorTable()
    write(table, kind)
    assert len(table.calls) == 1
    rows, retry, timeout = table.calls[0]
    assert len(rows) == count
    assert retry is None
    assert timeout == 5.0
    assert len({row.row_key for row in rows}) == count
    assert all(len(row.cells) == 1 for row in rows)


@pytest.mark.parametrize("kind", ["activity", "benchmark"])
@pytest.mark.parametrize("code", [4, 7, 13, 14])
def test_partial_mutation_failure_is_not_reported_as_success(kind: str, code: int) -> None:
    count = 3 if kind == "activity" else 6
    table = MirrorTable([0] * (count - 1) + [code])
    with pytest.raises(RuntimeError, match="mirror mutation"):
        write(table, kind)
    assert len(table.calls) == 1


@pytest.mark.parametrize("kind", ["activity", "benchmark"])
def test_missing_mutation_results_are_a_failure(kind: str) -> None:
    table = MirrorTable([])
    with pytest.raises(RuntimeError, match="mirror mutation"):
        write(table, kind)


@pytest.mark.parametrize("kind", ["activity", "benchmark"])
def test_transport_timeout_is_not_retried(kind: str) -> None:
    class TimeoutTable(MirrorTable):
        def mutate_rows(self, rows: list[Any], *, retry: Any, timeout: float) -> list[Any]:
            self.calls.append((rows, retry, timeout))
            assert retry is None
            assert timeout == 5.0
            raise TimeoutError("simulated unavailable mirror")

    table = TimeoutTable()
    with pytest.raises(TimeoutError):
        write(table, kind)
    assert len(table.calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "status", "missing_status"])
def test_failed_mirrors_preserve_settlement_replay_and_durable_repair(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from tests.fakes.spanner import make_fake_store
    from tests.test_bigtable_retirement import _authorize
    from tests.test_bigtable_retirement import _generation as billing_generation
    from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE

    store, database, table = make_fake_store(
        request_record_write_mode="typed",
        operational_analytics_outbox_enabled=True,
        generation_records_enabled=True,
    )
    authorization, key = _authorize(store, "ws-bounded-mirror")
    generation = billing_generation(authorization, key.hash)
    calls: list[int] = []

    def unavailable(
        self: Any, rows: list[Any], *, retry: Any, timeout: float
    ) -> list[Any]:
        assert retry is None and timeout == 5.0
        calls.append(len(rows))
        if failure == "timeout":
            raise TimeoutError("simulated mirror timeout")
        if failure == "missing_status":
            return [None for _row in rows]
        return [SimpleNamespace(code=14) for _row in rows]

    with monkeypatch.context() as patch:
        patch.setattr(type(table), "mutate_rows", unavailable)
        for replay in (False, True):
            result = store.typed_finalize_gateway_authorization_result(
                authorization.id,
                success=True,
                actual_microdollars=900_000,
                selected_usage_type="Credits",
                generation=generation,
            )
            assert result.finalized is not replay
            if not replay:
                assert result.activity_indexed is True

    assert calls == [3, 6]
    assert len(database.operational_analytics_outbox) == 1
    assert generation.id in database.generation_records
    credit = database.typed[CREDIT_BALANCE_TABLE][(authorization.workspace_id, 0)]
    assert credit["total_usage"] == 900_000
    assert credit["reserved"] == 0
    assert not table.committed

    store.generation_store.mirror_after_commit(generation)
    assert len(table.committed) == 9
    assert credit["total_usage"] == 900_000


@pytest.mark.parametrize("kind", ["activity", "benchmark"])
def test_real_bigtable_sdk_has_a_positive_rpc_deadline(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigtable
    from google.cloud.bigtable_v2.types import MutateRowsResponse
    from google.rpc.status_pb2 import Status

    from trusted_router.storage_gcp_mirror import commit_mirror_rows

    client = bigtable.Client(project="local-test", credentials=AnonymousCredentials())
    table = client.instance("local").table("local")
    calls: list[float] = []

    def mutate_rows(*, entries: Any, timeout: Any, retry: Any, **kwargs: Any) -> Any:
        assert retry is None

        def rpc(*, timeout: float) -> list[Any]:
            # Apply the actual google-api-core decorator produced by Table.
            calls.append(timeout)
            assert 3.0 <= timeout <= 5.0
            return [
                MutateRowsResponse(entries=[
                    MutateRowsResponse.Entry(index=i, status=Status(code=0))
                    for i in range(len(entries))
                ])
            ]

        return timeout(rpc)()

    monkeypatch.setattr(client.table_data_client, "mutate_rows", mutate_rows)
    count = 3 if kind == "activity" else 6
    rows = [table.direct_row(f"local-{i}".encode()) for i in range(count)]
    for row in rows:
        row.set_cell("activity", b"metadata", b"test")
    commit_mirror_rows(table, rows)
    assert len(calls) == 1
    assert all(not row._get_mutations() for row in rows)


def test_real_sdk_unresolved_status_reports_repairable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.api_core.exceptions import DeadlineExceeded
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigtable

    from trusted_router.storage_gcp_mirror import commit_mirror_rows

    client = bigtable.Client(project="local-test", credentials=AnonymousCredentials())
    table = client.instance("local").table("local")
    calls = 0

    def mutate_rows(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        assert kwargs["retry"] is None
        raise DeadlineExceeded("simulated transport deadline")

    monkeypatch.setattr(client.table_data_client, "mutate_rows", mutate_rows)
    row = table.direct_row(b"local-test")
    row.set_cell("activity", b"metadata", b"test")
    with pytest.raises(RuntimeError, match="reconciliation required"):
        commit_mirror_rows(table, [row])
    assert calls == 1
    assert row._get_mutations()
