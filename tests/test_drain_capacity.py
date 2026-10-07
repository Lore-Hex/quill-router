import pytest

from scripts.async_settle.drain_capacity import parse_lines, summarize


def test_capacity_synthetic_peak_margin_and_recovery():
    rows = [dict(service_seconds=.5, arrival_at=i/10) for i in range(100)]
    result = summarize(rows, start=0, end=10, bucket_seconds=1, margin=.2,
                       backlog=60, target_seconds=10)
    assert result['service_seconds'] == dict(mean=.5, p50=.5, p95=.5, maximum=.5)
    assert result['arrival_rate'] == result['peak_arrival_rate'] == 10
    assert result['concurrency'] == 9
    assert result['ideal_service_rate'] == 18
    assert result['burst_clear_seconds'] == 7.5
    assert result['clears_within_target']


def test_logs_do_not_invent_arrival_rate():
    rows = parse_lines('INFO async_drain.timing outcome=settled_now service_seconds=0.25 observed_at=100')
    result = summarize(rows, start=0, end=101, bucket_seconds=1, margin=.1, backlog=1, target_seconds=60)
    assert result['arrival_rate'] is None and result['concurrency'] is None
    assert result['service_seconds']['p95'] == .25


def test_outbox_exports_and_partial_window():
    rows = parse_lines('{"created_at": "1970-01-01T00:00:01Z", "service_seconds": 1}\n')
    result = summarize(rows, start=0, end=1.5, bucket_seconds=1, margin=0, backlog=0, target_seconds=1)
    assert result['peak_arrival_rate'] == 2
    assert result['arrival_rate'] == 1/1.5
    assert result['concurrency'] == 3


@pytest.mark.parametrize('rows', [[], [dict(service_seconds=0)], [dict(service_seconds=float('nan'))],
    [dict(created_at=1, completed_at=2)], [dict(service_seconds=1, arrival_at=11)]])
def test_invalid_or_missing_service_evidence(rows):
    with pytest.raises(ValueError):
        summarize(rows, start=0, end=10, bucket_seconds=1, margin=.2, backlog=1, target_seconds=10)
