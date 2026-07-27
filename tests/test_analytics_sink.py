"""The analytics mirror must be invisible to the request path.

`record_provider_benchmark` runs on the gateway settle path, which a customer
is paying for and waiting on. Every test here is really one assertion: nothing
about ClickHouse — being slow, full, broken, or absent — may change what that
path does.
"""

from __future__ import annotations

import threading
import time

from trusted_router.analytics_sink import (
    ClickHouseAnalyticsSink,
    NullAnalyticsSink,
    _optional_int,
    _row_from_sample,
    create_analytics_sink,
)
from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.types import UsageType

# Not a credential — the sink never connects in these tests (the transport is
# injected). Named rather than inlined so the linter does not read a literal
# password assignment at five call sites.
_UNUSED_URL = "http://unused"
_UNUSED_USER = "u"
_UNUSED_PASSWORD = "p"  # noqa: S105 - placeholder for an injected transport


def _sample(**overrides: object) -> ProviderBenchmarkSample:
    kwargs: dict[str, object] = {
        "id": "s1",
        "model": "acme/m1",
        "provider": "acme",
        "provider_name": "Acme",
        "status": "success",
        "usage_type": UsageType.CREDITS,
        "streamed": True,
        "source": "synthetic",
    }
    kwargs.update(overrides)
    return ProviderBenchmarkSample(**kwargs)  # type: ignore[arg-type]


def _drain_sink(sink: ClickHouseAnalyticsSink, *, predicate, timeout: float = 3.0) -> None:
    """Wait for the worker thread to reach a state, without a fixed sleep."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(sink.stats()):
            return
        time.sleep(0.01)
    raise AssertionError(f"sink never reached expected state; stats={sink.stats()}")


# --------------------------------------------------------------------------
# Default is off
# --------------------------------------------------------------------------


def test_sink_defaults_to_noop_without_a_url() -> None:
    """No configuration must mean no background thread and no network.

    Importing or booting the app in a test, a CLI, or a non-GCP deployment
    should not start an analytics worker.
    """

    class _Settings:
        clickhouse_url = ""

    assert isinstance(create_analytics_sink(_Settings()), NullAnalyticsSink)


def test_null_sink_accepts_anything_silently() -> None:
    sink = NullAnalyticsSink()
    sink.record_benchmark_sample(_sample())
    sink.record_benchmark_sample(None)
    assert sink.stats()["written"] == 0


# --------------------------------------------------------------------------
# The request path is never blocked or broken
# --------------------------------------------------------------------------


def test_enqueue_returns_immediately_even_when_the_transport_hangs() -> None:
    """A hung ClickHouse must not stall settle.

    The worker is blocked for the whole test; the producer still returns.
    """
    release = threading.Event()

    def hanging_transport(_payload: bytes) -> None:
        release.wait(timeout=5.0)

    sink = ClickHouseAnalyticsSink(
        url=_UNUSED_URL, user=_UNUSED_USER, password=_UNUSED_PASSWORD, transport=hanging_transport
    )
    try:
        started = time.perf_counter()
        for _ in range(50):
            sink.record_benchmark_sample(_sample())
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5, f"producer blocked for {elapsed:.2f}s"
    finally:
        release.set()
        sink.close()


def test_transport_failure_never_propagates() -> None:
    """ClickHouse being down is not an error the settle path can see."""

    def broken_transport(_payload: bytes) -> None:
        raise RuntimeError("clickhouse is down")

    sink = ClickHouseAnalyticsSink(
        url=_UNUSED_URL, user=_UNUSED_USER, password=_UNUSED_PASSWORD, transport=broken_transport
    )
    try:
        sink.record_benchmark_sample(_sample())  # must not raise
        _drain_sink(sink, predicate=lambda s: s["failed"] >= 1)
    finally:
        sink.close()


def test_full_queue_drops_rather_than_blocking() -> None:
    """Bounded by design: analytics loss beats unbounded memory growth.

    With the worker wedged and a queue of 2, the third row is dropped and
    counted rather than queued forever.
    """
    release = threading.Event()

    def hanging_transport(_payload: bytes) -> None:
        release.wait(timeout=5.0)

    sink = ClickHouseAnalyticsSink(
        url=_UNUSED_URL,
        user=_UNUSED_USER,
        password=_UNUSED_PASSWORD,
        queue_size=2,
        transport=hanging_transport,
    )
    try:
        for _ in range(50):
            sink.record_benchmark_sample(_sample())
        assert sink.stats()["dropped"] > 0, "a full queue must drop, not grow"
    finally:
        release.set()
        sink.close()


def test_a_malformed_row_is_counted_not_raised() -> None:
    """An unencodable sample must not break the settle path either."""
    sink = ClickHouseAnalyticsSink(
        url=_UNUSED_URL, user=_UNUSED_USER, password=_UNUSED_PASSWORD, transport=lambda _p: None
    )
    try:
        sink.record_benchmark_sample(object())  # not a dataclass or mapping
        assert sink.stats()["failed"] >= 1
    finally:
        sink.close()


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------


def test_rows_are_batched_not_one_insert_each() -> None:
    """One INSERT per row is a ClickHouse anti-pattern — each creates a part.

    Asserts the worker coalesces: far fewer transport calls than rows.
    """
    payloads: list[bytes] = []
    lock = threading.Lock()

    def recording_transport(payload: bytes) -> None:
        with lock:
            payloads.append(payload)

    sink = ClickHouseAnalyticsSink(
        url=_UNUSED_URL, user=_UNUSED_USER, password=_UNUSED_PASSWORD, transport=recording_transport
    )
    try:
        for index in range(200):
            sink.record_benchmark_sample(_sample(id=f"s{index}"))
        _drain_sink(sink, predicate=lambda s: s["written"] >= 200)
        with lock:
            calls = len(payloads)
            total_rows = sum(len(p.decode().splitlines()) for p in payloads)
        assert total_rows == 200, f"expected every row written, got {total_rows}"
        assert calls < 200, f"rows were not batched: {calls} inserts for 200 rows"
    finally:
        sink.close()


# --------------------------------------------------------------------------
# Row encoding matches the schema and the proof harness
# --------------------------------------------------------------------------


def test_row_shape_matches_the_clickhouse_columns() -> None:
    row = _row_from_sample(_sample())
    assert row["id"] == "s1"
    assert row["provider"] == "acme"
    assert row["streamed"] == 1  # UInt8, not a bool
    assert isinstance(row["usage_type"], str)  # enum stringified
    # DateTime64(3) text format, not ISO-8601.
    assert "T" not in row["created_at"] and row["created_at"].count("-") == 2


def test_error_status_normalisation_matches_the_proof_harness() -> None:
    """A string "429" must coerce the same way on both paths.

    The differential proof found that Python's `not in {429}` and ClickHouse's
    UInt16 column disagree unless normalisation is shared, which would make
    dual-write rows silently differ from the Bigtable original.
    """
    assert _optional_int("429") == 429
    assert _optional_int(429) == 429
    assert _optional_int(None) is None
    assert _optional_int("") is None
    assert _optional_int("not-a-status") is None
