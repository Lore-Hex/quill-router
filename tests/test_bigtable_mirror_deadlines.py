from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_storage_gcp import _generation
from trusted_router.storage_gcp_activity_index import write_generation
from trusted_router.storage_gcp_benchmark_index import write_provider_benchmark
from trusted_router.storage_models import ProviderBenchmarkSample


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router import storage_gcp_mirror

    monkeypatch.setattr(storage_gcp_mirror, "MIRROR_RETRY_BACKOFF_SECONDS", 0.0)


class MirrorTable:
    """``codes`` is the per-row status list for every call, or a list of such
    lists consumed one per call (the last one repeats)."""

    def __init__(self, codes: list[int] | list[list[int]] | None = None) -> None:
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
        if self.codes is None:
            codes: list[int] = [0] * len(rows)
        elif self.codes and isinstance(self.codes[0], list):
            per_call = self.codes
            codes = per_call[min(len(self.calls) - 1, len(per_call) - 1)]
        else:
            codes = self.codes  # type: ignore[assignment]
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
@pytest.mark.parametrize("code", [4, 10, 13, 14])
def test_transient_row_failure_is_retried_once_for_the_failed_rows_only(
    kind: str, code: int
) -> None:
    count = 3 if kind == "activity" else 6
    table = MirrorTable([[0] * (count - 1) + [code], [0]])
    write(table, kind)
    assert len(table.calls) == 2
    first_rows, _retry, first_timeout = table.calls[0]
    retry_rows, retry, retry_timeout = table.calls[1]
    assert first_timeout == 5.0
    assert retry is None
    # Only the row that failed is resent, with the budget that is left.
    assert [row.row_key for row in retry_rows] == [first_rows[-1].row_key]
    assert 2.0 <= retry_timeout < 5.0


@pytest.mark.parametrize("kind", ["activity", "benchmark"])
@pytest.mark.parametrize("code", [4, 10, 13, 14])
def test_transient_failure_that_survives_the_retry_reports_codes_and_attempts(
    kind: str, code: int
) -> None:
    from trusted_router.storage_gcp_mirror import MirrorWriteIncomplete

    count = 3 if kind == "activity" else 6
    table = MirrorTable([[0] * (count - 1) + [code], [code]])
    with pytest.raises(MirrorWriteIncomplete, match="mirror mutation incomplete") as failure:
        write(table, kind)
    assert len(table.calls) == 2
    assert failure.value.attempts == 2
    assert failure.value.total == count
    assert failure.value.codes == (code,)
    message = str(failure.value)
    assert f"status codes {code}" in message
    assert f"1 of {count} rows failed" in message
    assert "reconcile_generation_activity" in message


@pytest.mark.parametrize("kind", ["activity", "benchmark"])
@pytest.mark.parametrize("code", [3, 7])
def test_permanent_row_failure_is_not_retried(kind: str, code: int) -> None:
    from trusted_router.storage_gcp_mirror import MirrorWriteIncomplete

    count = 3 if kind == "activity" else 6
    table = MirrorTable([0] * (count - 1) + [code])
    with pytest.raises(MirrorWriteIncomplete, match=f"status codes {code}"):
        write(table, kind)
    assert len(table.calls) == 1


@pytest.mark.parametrize("kind", ["activity", "benchmark"])
def test_no_retry_when_the_first_attempt_used_the_budget(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from trusted_router import storage_gcp_mirror

    clock = [100.0]

    def monotonic() -> float:
        return clock[0]

    class SlowTable(MirrorTable):
        def mutate_rows(self, rows: list[Any], *, retry: Any, timeout: float) -> list[Any]:
            clock[0] += 3.5  # the first RPC ate most of the 5 s budget
            return super().mutate_rows(rows, retry=retry, timeout=timeout)

    monkeypatch.setattr(storage_gcp_mirror.time, "monotonic", monotonic)
    count = 3 if kind == "activity" else 6
    table = SlowTable([0] * (count - 1) + [14])
    with pytest.raises(RuntimeError, match="after 1 attempt"):
        write(table, kind)
    assert len(table.calls) == 1


def test_retry_waits_for_the_backoff_before_resending(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router import storage_gcp_mirror

    monkeypatch.setattr(storage_gcp_mirror, "MIRROR_RETRY_BACKOFF_SECONDS", 0.25)
    slept: list[float] = []
    monkeypatch.setattr(storage_gcp_mirror.time, "sleep", slept.append)
    table = MirrorTable([[0, 0, 14], [0]])
    write(table, "activity")
    assert slept == [0.25]
    assert len(table.calls) == 2


@pytest.mark.parametrize("kind", ["activity", "benchmark"])
def test_missing_mutation_results_are_retried_then_reported(kind: str) -> None:
    from trusted_router.storage_gcp_mirror import MirrorWriteIncomplete

    table = MirrorTable([])
    with pytest.raises(MirrorWriteIncomplete, match="status codes missing") as failure:
        write(table, kind)
    assert len(table.calls) == 2
    count = 3 if kind == "activity" else 6
    assert failure.value.codes == (None,) * count


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
        # First attempt carries the full budget; the single retry carries what is left.
        assert retry is None and 2.0 <= timeout <= 5.0
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

    if failure == "timeout":
        assert calls == [3, 6]
    else:
        # Transient statuses earn one retry of the failed rows per mirror.
        assert calls == [3, 3, 6, 6]
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
    with pytest.raises(RuntimeError, match="mirror mutation incomplete after 2 attempt") as failure:
        commit_mirror_rows(table, [row])
    assert calls == 2
    assert "status codes missing" in str(failure.value)
    assert "reconcile_generation_activity" in str(failure.value)
    assert row._get_mutations()
