"""Bounded read/load witnesses, including half-open and independent fleet peaks."""
from dataclasses import replace

import pytest

from tests.shadow_v2_gate_cache import (  # noqa: F401
    default_dependency_lookup_cache,
    payloadgraph_shared_lookup_cache,
)
from trusted_router.async_settle_shadow_v2_gate_export import (
    KeyRange,
    Lane,
    MemoryReader,
    execute,
    plan,
)
from trusted_router.async_settle_shadow_v2_gate_inputs import Boot, Key, Rate, Row
from trusted_router.async_settle_shadow_v2_gate_load import sweep

pytestmark = pytest.mark.timeout(method='signal')

def test_reader_exact_keys_ranges_and_no_discovery() -> None:
    key = Key('kind', 'day/boot/0001')
    reader = MemoryReader((Row(key, b'original'),))
    export = plan((key,), (), (), source_bytes=8, write_bytes=0, passes=1, lanes=())
    assert execute(reader, export) == reader.rows
    assert reader.get(Key('kind', 'day/boot/0002')) is None
    assert reader.read_range(KeyRange('kind', 'day/boot/0001', 'day/boot/0002', 1)) == reader.rows
    assert export.priority == 'LOW' and export.read_timeout_ms == 200 and export.retries == 0


def test_duplicate_source_and_range_conflict() -> None:
    key = Key('kind', 'a')
    with pytest.raises(ValueError):
        plan((key, key), (), (), source_bytes=0, write_bytes=0, passes=1, lanes=())
    with pytest.raises(ValueError):
        plan((key,), (KeyRange('kind', 'a', 'b', 1),), (), source_bytes=0, write_bytes=0, passes=1, lanes=())
    with pytest.raises(ValueError):
        MemoryReader((Row(key, b'a'), Row(key, b'b'))).get(key)


@pytest.mark.parametrize('field,value', [('source_bytes', 262145), ('passes', 0), ('cleanup_sink_keys', 30001), ('cleanup_spanner_keys', 30001)])
def test_export_numeric_bounds(field, value) -> None:
    kwargs = dict(source_bytes=0, write_bytes=0, passes=1, lanes=())
    kwargs[field] = value
    with pytest.raises(ValueError):
        plan((Key('k', 'id'),), (), (), **kwargs)


def test_churn_tokens_and_reruns_are_charged() -> None:
    lanes = tuple(Lane(5000000, 3, 3 * 65536, 1) for _ in range(8))
    export = plan((Key('k', 'id'),), (), (Key('report', 'page'),),
                  source_bytes=123, write_bytes=456, passes=3, lanes=lanes,
                  cleanup_spanner_keys=201, cleanup_sink_keys=1000, cleanup_seconds=1)
    assert export.operations == 6 and export.actual_bytes == 1737
    assert export.coarse_bytes == 262144 * 6
    assert export.lane_operation_bound == 24 and export.lane_bytes_bound == 24 * 65536
    assert export.cleanup_spanner_transactions == 2
    with pytest.raises(ValueError):
        plan((), (), (), source_bytes=0, write_bytes=0, passes=1, lanes=(Lane(4999999, 2, 1, 0),))


def test_sequential_churn_is_one_simultaneous_boot() -> None:
    boots = tuple(Boot(str(i), 'router', 'a' * 40, 'r', i * 10000000, (i + 1) * 10000000) for i in range(8))
    fleet = sweep(boots, (), 1, 80000000)[0]
    assert fleet.simultaneous == 1 and fleet.reads == 1.25
    overlapping = (*boots, replace(boots[0], boot='extra', start_us=9999999, end_us=10000001))
    assert sweep(overlapping, (), 1, 80000000)[0].simultaneous == 2


def test_fleet_peak_is_not_sum_of_historical_regional_peaks() -> None:
    boots = (Boot('a', 'router', 'a' * 40, 'r1', 0, 10000000), Boot('b', 'router', 'a' * 40, 'r2', 10000000, 20000000))
    peaks = sweep(boots, (), 1, 20000000)
    assert peaks[0].reads == 1.25 and sum(p.reads for p in peaks[1:]) == 2.5


def test_startup_burst_cannot_be_averaged_away() -> None:
    boot = Boot('b', 'router', 'a' * 40, 'r', 0, 100000000)
    rate = Rate('b', 0, 100000000, 25, 100, burst_reads=20, burst_pending=4000)
    fleet = sweep((boot,), (rate,), 1, 100000000)[0]
    assert fleet.reads == 20 and fleet.pending == 4000
    with pytest.raises(ValueError):
        sweep((boot, boot), (), 1, 100000000)
    with pytest.raises(ValueError):
        sweep((boot,), (rate, rate), 1, 100000000)


@pytest.mark.parametrize('end', [9, 10, 11])
def test_half_open_overlap_boundaries(end) -> None:
    a = Boot('a', 'router', 'a' * 40, 'r', 0, end)
    b = Boot('b', 'router', 'a' * 40, 'r', 10, 30)
    assert sweep((a, b), (), 1, 30)[0].simultaneous == (2 if end > 10 else 1)
