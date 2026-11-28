import copy
import datetime as dt

import pytest

from scripts.axiom_growth import model as m
from scripts.axiom_growth import source_windows as w
from tests.test_axiom_growth_worker import NOW, FakeIO, cycle, state, usage


class Dense(FakeIO):
    def __init__(self, daily=(), *, usage_limit=100, event_minutes=25):
        super().__init__(daily=daily)
        self.usage_limit = usage_limit
        self.event_minutes = event_minutes
        self.queries = []

    def events(self, left, right):
        self.queries.append(('events', left, right))
        if right-left > dt.timedelta(minutes=self.event_minutes):
            raise w.Capacity('events', 'source budget')
        return []

    def usage(self, left, right, *, bucket=(0, w.SPACE)):
        self.queries.append(('usage', left, right, bucket))
        rows = [dict(r) for r in self.daily if left.date() <= m.date(r['_time']).date() <= right.date()
                and bucket[0] <= w.usage_bucket(r) < bucket[1]]
        if len(rows) > self.usage_limit:
            raise w.Capacity('usage', 'aggregate cap')
        return rows


def test_replan_is_durable_without_publishing_success_or_advancing_a_cursor():
    original = state()
    before = copy.deepcopy(original)
    io = Dense()
    updated, count = cycle(original, io, NOW)
    assert not count and not io.sent and original == before
    assert updated['watermark'] == before['watermark'] == io.committed_through
    assert updated['source_windows']['event_next'] == (m.date(before['watermark']) - w.DAY).isoformat()
    assert len(io.queries) == 1
    restored, _ = cycle(updated, io, NOW+dt.timedelta(minutes=5))
    assert restored['watermark'] > updated['watermark']
    assert io.sent[-1]['source_window_mode'] == 'segmented'
    forward = restored['watermark']
    repair, _ = cycle(restored, io, NOW+dt.timedelta(minutes=10))
    assert repair['watermark'] == forward
    assert repair['source_windows']['event_next'] > restored['source_windows']['event_next']
    assert repair['source_windows']['usage_next'] < restored['source_windows']['usage_next']


def test_sharded_usage_never_deletes_unread_ranges_and_resumes_across_restart():
    today = NOW.date().isoformat() + 'T00:00:00Z'
    rows = [usage(event_id=f'usage{i}', _time=today, model=f'm{i}', provider='p') for i in range(10)]
    old = [dict(row, successful_calls=0) for row in rows]
    io = Dense(rows, usage_limit=4)
    original = state()
    original['daily'] = old
    current, _ = cycle(original, io, NOW)
    for step in range(1, 50):
        prev = copy.deepcopy(current)
        queried, sent = len(io.queries), len(io.sent)
        current, _ = cycle(copy.deepcopy(current), io, NOW+dt.timedelta(minutes=5*step))
        reads = io.queries[queried:]
        assert sum(q[0]=='usage' for q in reads) <= 1 and sum(q[0]=='events' for q in reads) <= 1
        if len(io.sent) == sent:
            assert current['daily'] == prev['daily'] and current['watermark'] == prev['watermark']
        assert {r['event_id'] for r in current['daily']} == {r['event_id'] for r in old}
        if current['source_windows'].get('last_live_usage_day') == today[:10]:
            break
    else:
        pytest.fail('split usage day never reached its completion marker')
    assert all(r['successful_calls'] == 5 for r in current['daily'])


def test_window_repair_covers_all_days_and_intervals_without_changing_caps():
    original = state()
    previous = m.date(original['watermark'])
    plan = w.begin(previous)
    io = Dense()
    current = {**original, 'source_windows': plan}
    intervals, days = [], set()
    for step in range(160):
        now = NOW+dt.timedelta(minutes=5*step)
        prior = m.date(current['watermark'])
        used = len(io.queries)
        current, _ = cycle(current, io, now)
        for kind, left, right, *_ in io.queries[used:]:
            if kind == 'events' and right <= previous:
                intervals.append((left, right))
            if kind == 'usage' and left.date() < previous.date():
                days.add(left.date())
        assert m.date(current['watermark']) >= prior
        if w.roll(current['source_windows'], prior)['event_next'] == plan['stop'] and len(days) == 30:
            break
    assert intervals[0][0] == previous-w.DAY and intervals[-1][1] == previous
    assert all(right == left for (_, right), (left, _) in zip(intervals, intervals[1:], strict=False))
    assert days == {previous.date()-i*w.DAY for i in range(1, 31)}


def test_minimum_slice_stops_and_noncapacity_errors_do_not_checkpoint():
    original = state()
    p = w.begin(m.date(original['watermark']))
    p['live_seconds'] = 1
    failure = w.Capacity('events', 'too dense')
    failure.lane = 'live'
    with pytest.raises(ValueError, match='minimum'):
        w.narrower(p, NOW, failure)
    io = Dense()
    def fail(*args):
        raise RuntimeError('source unauthorized')
    io.events = fail
    with pytest.raises(RuntimeError, match='unauthorized'):
        cycle(original, io, NOW)
    assert not hasattr(io, 'committed_through') and not io.sent


def test_sharded_live_days_cannot_starve_historical_usage():
    current = state()
    plan = w.begin(m.date(current['watermark']))
    plan['live_width'] = w.SPACE//4
    current['source_windows'] = plan
    io = Dense()
    for i in range(5):
        current, _ = cycle(current, io, NOW+dt.timedelta(minutes=i*5))
    usage_reads = [(left.date(), bucket) for kind, left, _, *rest in io.queries
                   if kind == 'usage' for bucket in rest]
    assert [bucket for _, bucket in usage_reads[:4]] == [(v, v+w.SPACE//4) for v in range(0, w.SPACE, w.SPACE//4)]
    assert usage_reads[-1] == (NOW.date()-w.DAY, (0, w.SPACE))
    assert w.usage_bucket({'workspace_fingerprint': 'workspace', 'model': 'example/model',
                           'provider': 'provider'}) == 0x7121  # ClickHouse 26.8 SHA256/hex vector.
