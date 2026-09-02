"""Bucketed synthetic histograms: bounded key cardinality, exact-rank
percentiles, and safe coexistence with rows written before bucketing.

Before this, `_increment_histogram` keyed every distinct millisecond, so a
month rollup for a probe firing every few seconds accumulated hundreds of
thousands of JSON keys — and on the Postgres/DSQL and Bigtable backends that
whole body is re-read and re-written on every sample (see
`PostgresStore.record_synthetic_probe_sample`). These tests pin the bound
and prove the readers stay correct rather than assuming it.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from typing import Any

from clickhouse.rollup_synthetic import monthly_from_daily
from trusted_router.storage_gcp_synthetic_rollups import (
    _rollup_key,
    _write_json_row,
    synthetic_rollups,
    write_synthetic_rollups,
)
from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup
from trusted_router.synthetic import status as synthetic_status
from trusted_router.synthetic.rollups import (
    HISTOGRAM_EXACT_BELOW,
    apply_sample_to_rollup,
    compact_histogram,
    histogram_bucket,
    merge_rollups,
    new_rollup_for_sample,
    percentile_from_histogram,
    sample_rollup_ids,
)


def _max_keys_up_to(power_of_ten: int) -> int:
    """Exact key count for every integer in 0..10**power_of_ten inclusive:
    100 exact keys, 90 per full decade above, plus the top value's own bucket."""
    return HISTOGRAM_EXACT_BELOW + 90 * (power_of_ten - 2) + 1


# Worst case for values in 0..10^7 ms inclusive (551 keys).
MAX_KEYS_TO_TEN_MILLION_MS = _max_keys_up_to(7)


def _exact_percentile(values: list[int], percentile: int) -> int:
    """The same rank rule `percentile_from_histogram` uses, on raw values."""
    ordered = sorted(values)
    threshold = max(1, math.ceil(len(ordered) * percentile / 100))
    return ordered[threshold - 1]


def _histogram_of(values: list[int]) -> dict[str, int]:
    histogram: dict[str, int] = {}
    for value in values:
        key = str(histogram_bucket(value))
        histogram[key] = histogram.get(key, 0) + 1
    return histogram


def _legacy_histogram_of(values: list[int]) -> dict[str, int]:
    """Exactly what pre-bucketing writers stored: one key per millisecond."""
    histogram: dict[str, int] = {}
    for value in values:
        key = str(max(int(value), 0))
        histogram[key] = histogram.get(key, 0) + 1
    return histogram


def _sample(
    sample_id: str, latency: int, *, created_at: str = "2026-05-05T12:00:00Z"
) -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=sample_id,
        probe_type="tls_health",
        target="api",
        target_url="https://api.trustedrouter.com/health",
        monitor_region="us-central1",
        status="up",
        latency_milliseconds=latency,
        ttfb_milliseconds=latency // 2,
        dns_milliseconds=3,
        tcp_connect_milliseconds=12,
        tls_handshake_milliseconds=26,
        gateway_processing_milliseconds=max(latency - 60, 0),
        created_at=created_at,
    )


def _distributions() -> dict[str, list[int]]:
    rng = random.Random(20260902)  # noqa: S311 - deterministic fixture, not security
    return {
        "uniform_ms": [rng.randint(0, 5_000) for _ in range(20_000)],
        "sub_100ms_exact": [rng.randint(0, 99) for _ in range(5_000)],
        "lognormal_gateway": [int(math.exp(rng.gauss(math.log(180), 0.6))) for _ in range(20_000)],
        "heavy_tail_timeouts": [rng.randint(20, 400) for _ in range(9_500)]
        + [rng.randint(30_000, 120_000) for _ in range(500)],
        "single_value": [4_242] * 1_000,
    }


# --- the map itself -------------------------------------------------------


def test_bucket_is_exact_below_threshold_and_two_significant_digits_above() -> None:
    assert [histogram_bucket(v) for v in (0, 1, 57, 99)] == [0, 1, 57, 99]
    assert histogram_bucket(100) == 100
    assert histogram_bucket(104) == 100
    assert histogram_bucket(105) == 110
    assert histogram_bucket(123) == 120
    assert histogram_bucket(994) == 990
    assert histogram_bucket(995) == 1000
    assert histogram_bucket(1_049) == 1_000
    assert histogram_bucket(1_050) == 1_100
    assert histogram_bucket(87_654) == 88_000
    assert histogram_bucket(-5) == 0


def test_bucket_is_monotone_and_idempotent() -> None:
    previous = histogram_bucket(0)
    for value in range(1, 300_000):
        bucket = histogram_bucket(value)
        assert bucket >= previous, (value, bucket, previous)
        assert histogram_bucket(bucket) == bucket, (value, bucket)
        # Never more than half a bucket away, and never more than 5% above 100.
        assert abs(bucket - value) <= (value // 20 if value >= HISTOGRAM_EXACT_BELOW else 0)
        previous = bucket


def test_key_cardinality_is_bounded_regardless_of_distinct_values() -> None:
    every_millisecond = {str(v): 1 for v in range(0, 2_000_000)}
    compacted = compact_histogram(every_millisecond)
    assert len(compacted) <= MAX_KEYS_TO_TEN_MILLION_MS
    assert len(compacted) < 600
    assert sum(compacted.values()) == 2_000_000


def test_key_cardinality_formula_is_exact_on_an_inclusive_decade_range() -> None:
    # Pins the formula, not just an upper bound: every integer in 0..10^6
    # inclusive lands in exactly 100 + 90*4 + 1 buckets (the top value,
    # 1_000_000, is a bucket of its own). Sweeping 0..10^7 would be the same
    # arithmetic with one more decade, at 10x the runtime.
    every_millisecond = {str(v): 1 for v in range(0, 10**6 + 1)}
    assert len(compact_histogram(every_millisecond)) == _max_keys_up_to(6) == 461


# --- readers stay correct ------------------------------------------------


def test_percentiles_from_bucketed_histograms_equal_bucket_of_exact_percentile() -> None:
    for name, values in _distributions().items():
        histogram = _histogram_of(values)
        assert len(histogram) <= MAX_KEYS_TO_TEN_MILLION_MS, name
        for percentile in (50, 90, 95, 99):
            exact = _exact_percentile(values, percentile)
            approx = percentile_from_histogram(histogram, percentile)
            assert approx is not None, (name, percentile)
            assert approx == histogram_bucket(exact), (name, percentile, exact, approx)
            if exact < HISTOGRAM_EXACT_BELOW:
                assert approx == exact, (name, percentile)
            else:
                assert abs(approx - exact) <= exact * 0.05, (name, percentile, exact, approx)


def test_legacy_exact_keys_and_bucketed_keys_merge_to_the_same_answer() -> None:
    values = _distributions()["lognormal_gateway"]
    half = len(values) // 2
    legacy = SyntheticRollup(
        id="legacy",
        period="month",
        period_start="2026-05-01T00:00:00Z",
        component="canonical_api",
        target="api",
        probe_type="tls_health",
        monitor_region="us-central1",
        sample_count=half,
        up_count=half,
        latency_histogram=_legacy_histogram_of(values[:half]),
    )
    bucketed = SyntheticRollup(
        id="bucketed",
        period="month",
        period_start="2026-05-01T00:00:00Z",
        component="canonical_api",
        target="api",
        probe_type="tls_health",
        monitor_region="us-central1",
        sample_count=len(values) - half,
        up_count=len(values) - half,
        latency_histogram=_histogram_of(values[half:]),
    )
    assert len(legacy.latency_histogram) > MAX_KEYS_TO_TEN_MILLION_MS  # the old shape

    merged = merge_rollups([legacy, bucketed])

    # Merging is a plain key-sum (it never changes precision), so a mixed
    # legacy+bucketed walk lands within half a step of the exact percentile:
    # every element moved by at most that much, so the order statistic does too.
    assert merged["sample_count"] == len(values)
    for percentile, key in ((50, "p50_latency_milliseconds"), (95, "p95_latency_milliseconds")):
        exact = _exact_percentile(values, percentile)
        assert abs(merged[key] - exact) <= exact * 0.05, (percentile, exact, merged[key])

    # And once both sides are bucketed (every persisted row after its next
    # write), the identity is exact.
    legacy.latency_histogram = _histogram_of(values[:half])
    merged = merge_rollups([legacy, bucketed])
    for percentile, key in ((50, "p50_latency_milliseconds"), (95, "p95_latency_milliseconds")):
        exact = _exact_percentile(values, percentile)
        assert merged[key] == histogram_bucket(exact), (percentile, exact, merged[key])


def test_applying_a_sample_compacts_a_legacy_rollup_and_preserves_counts() -> None:
    values = _distributions()["uniform_ms"]
    legacy = new_rollup_for_sample(
        _sample("seed", values[0]), period="month", component="uncategorized"
    )
    legacy.latency_histogram = _legacy_histogram_of(values)
    legacy.ttfb_histogram = _legacy_histogram_of([v // 2 for v in values])
    legacy.sample_count = len(values)
    legacy.up_count = len(values)
    assert len(legacy.latency_histogram) > MAX_KEYS_TO_TEN_MILLION_MS

    apply_sample_to_rollup(legacy, _sample("next", 777))

    assert legacy.sample_count == len(values) + 1
    assert len(legacy.latency_histogram) <= MAX_KEYS_TO_TEN_MILLION_MS
    assert len(legacy.ttfb_histogram) <= MAX_KEYS_TO_TEN_MILLION_MS
    assert sum(legacy.latency_histogram.values()) == len(values) + 1
    assert sum(legacy.ttfb_histogram.values()) == len(values) + 1
    assert legacy.latency_histogram[str(histogram_bucket(777))] >= 1
    # Every stored key is now a bucket representative: a second apply is a no-op fold.
    before = dict(legacy.latency_histogram)
    apply_sample_to_rollup(legacy, _sample("again", 777))
    assert set(legacy.latency_histogram) == set(before)
    assert (
        legacy.latency_histogram[str(histogram_bucket(777))]
        == before[str(histogram_bucket(777))] + 1
    )


def test_a_month_of_samples_stays_bounded_where_it_used_to_grow_per_millisecond() -> None:
    rng = random.Random(7)  # noqa: S311 - deterministic fixture, not security
    rollup = new_rollup_for_sample(_sample("first", 150), period="month", component="uncategorized")
    for index in range(1, 5_000):
        apply_sample_to_rollup(
            rollup, _sample(f"s{index}", int(math.exp(rng.gauss(math.log(150), 0.8))))
        )
    assert rollup.sample_count == 5_000
    for histogram in (
        rollup.latency_histogram,
        rollup.ttfb_histogram,
        rollup.gateway_processing_histogram,
    ):
        assert len(histogram) <= MAX_KEYS_TO_TEN_MILLION_MS
        assert sum(histogram.values()) == 5_000
    assert percentile_from_histogram(rollup.latency_histogram, 50) is not None


def test_compact_histogram_rejects_nothing_it_would_have_read_before() -> None:
    # Whatever percentile_from_histogram could parse, compaction can fold.
    histogram = {"0": 3, "99": 1, "100": 2, "123": 5, "987654": 1}
    assert compact_histogram(histogram) == {"0": 3, "99": 1, "100": 2, "120": 5, "990000": 1}
    assert sum(compact_histogram(histogram).values()) == sum(histogram.values())


# --- persistence boundary ------------------------------------------------


class _Cell:
    def __init__(self, value: bytes) -> None:
        self.value = value


class _Row:
    def __init__(self, value: bytes) -> None:
        self.cells = {"m": {b"body": [_Cell(value)]}}


class _DirectRow:
    def __init__(self, key: bytes, table: _MiniBigtable) -> None:
        self.key = key
        self.table = table
        self.value: bytes | None = None

    def set_cell(self, _family: str, _qualifier: bytes, value: bytes) -> None:
        self.value = value

    def commit(self) -> None:
        if self.value is not None:
            self.table.rows[self.key] = _Row(self.value)


class _MiniBigtable:
    """Just enough of the Bigtable table API for the rollup write/read path."""

    def __init__(self) -> None:
        self.rows: dict[bytes, _Row] = {}

    def read_rows(
        self, *, start_key: bytes, end_key: bytes, limit: int, filter_: Any = None
    ) -> list[_Row]:
        return [row for key, row in sorted(self.rows.items()) if start_key <= key < end_key][:limit]

    def direct_row(self, key: bytes) -> _DirectRow:
        return _DirectRow(key, self)


def test_store_round_trip_compacts_a_legacy_body_and_preserves_counts() -> None:
    # A month rollup persisted before bucketing: one key per millisecond,
    # well past the bound. Writing ONE more sample through the store must
    # shrink the persisted body and keep every count.
    seed = _sample("seed", 150)
    (period, component) = next(
        (period, component) for period, component in sample_rollup_ids(seed) if period == "month"
    )
    legacy_values = list(range(100, 2_100))
    legacy = new_rollup_for_sample(seed, period=period, component=component, bucket=False)
    legacy.latency_histogram = _legacy_histogram_of(legacy_values)
    legacy.sample_count = len(legacy_values)
    legacy.up_count = len(legacy_values)
    assert len(legacy.latency_histogram) > MAX_KEYS_TO_TEN_MILLION_MS
    table = _MiniBigtable()
    _write_json_row(table, "m", _rollup_key(legacy), legacy)

    write_synthetic_rollups(table, "m", _sample("next", 777))

    stored = next(
        row
        for row in synthetic_rollups(table, "m", period="month", limit=50)
        if row.component == component
    )
    assert stored.sample_count == len(legacy_values) + 1
    assert len(stored.latency_histogram) <= MAX_KEYS_TO_TEN_MILLION_MS
    assert sum(stored.latency_histogram.values()) == len(legacy_values) + 1
    assert all(str(histogram_bucket(int(key))) == key for key in stored.latency_histogram)
    exact_p95 = _exact_percentile([*legacy_values, 777], 95)
    assert percentile_from_histogram(stored.latency_histogram, 95) == histogram_bucket(exact_p95)


def test_clickhouse_month_rollups_are_bucketed_even_from_legacy_daily_rows() -> None:
    # clickhouse/rollup_synthetic.py is a second rollup writer: month rows are
    # folded from daily rows it reads back, so it must bucket on its own.
    def daily(day: int, values: list[int]) -> SyntheticRollup:
        return SyntheticRollup(
            id=f"day{day}",
            period="day",
            period_start=f"2026-05-{day:02d}T00:00:00Z",
            component="canonical_api",
            target="api",
            probe_type="tls_health",
            monitor_region="us-central1",
            sample_count=len(values),
            up_count=len(values),
            latency_histogram=_legacy_histogram_of(values),
            ttfb_histogram=_legacy_histogram_of([v // 2 for v in values]),
        )

    first, second = list(range(100, 1_100)), list(range(1_000, 2_000))
    (month,) = monthly_from_daily([daily(1, first), daily(2, second)])

    assert month.period == "month"
    assert month.sample_count == len(first) + len(second)
    for histogram in (month.latency_histogram, month.ttfb_histogram):
        assert len(histogram) <= MAX_KEYS_TO_TEN_MILLION_MS
        assert sum(histogram.values()) == len(first) + len(second)
    exact_p50 = _exact_percentile(first + second, 50)
    assert percentile_from_histogram(month.latency_histogram, 50) == histogram_bucket(exact_p50)


def test_status_window_percentiles_from_raw_samples_stay_exact() -> None:
    # The 5m window builds transient rollups from raw samples; those are never
    # persisted, so they keep exact keys and agree with the sibling fields
    # computed straight from the same samples.
    now = dt.datetime(2026, 5, 5, 12, 4, tzinfo=dt.UTC)
    latencies = [1210, 1230, 1240, 1249, 1270]
    samples = [
        SyntheticProbeSample(
            id=f"s{index}",
            probe_type="attestation_nonce",
            target="canonical",
            target_url="https://api.trustedrouter.com/health",
            monitor_region="us-central1",
            status="up",
            latency_milliseconds=latency,
            ttfb_milliseconds=latency,
            created_at=f"2026-05-05T12:0{index}:00Z",
        )
        for index, latency in enumerate(latencies)
    ]

    snapshot = synthetic_status.status_snapshot(samples, now=now)

    window = snapshot["windows"]["5m"]["groups"][0]
    breakdown = snapshot["components"][0]["latency_breakdown_5m"][0]
    assert window["p50_latency_milliseconds"] == _exact_percentile(latencies, 50) == 1240
    assert window["p95_latency_milliseconds"] == _exact_percentile(latencies, 95) == 1270
    assert window["p50_latency_milliseconds"] == breakdown["p50_latency_milliseconds"]
    assert window["p95_latency_milliseconds"] == breakdown["p95_latency_milliseconds"]
