"""Bounded historical recovery must preserve evidence and fail closed on gaps."""
import copy
import datetime as dt
import json

import httpx
import pytest

from scripts.axiom_growth import gateway_attempts as ga
from scripts.axiom_growth import model as m
from scripts.axiom_growth.runtime import (
    MAX_GATEWAY_PAGES,
    Sources,
    collect_gateway,
    cycle,
)
from tests.test_axiom_growth_worker import NOW, FakeIO, state
from tests.test_growth_completion import audit


def projected(stage, when, *, instance='vm-1', status=200):
    entry = audit(stage, instance=instance, status=status)
    entry['timestamp'] = when.isoformat()
    return ga.project(entry)


class GatewayIO(FakeIO):
    def __init__(self, records=(), *, fail=False):
        super().__init__(fail=fail)
        self.records = records
        self.gateway_windows = []

    def gateway_audits(self, start, end):
        self.gateway_windows.append((start, end))
        return copy.deepcopy([r for r in self.records if start <= m.date(r['_time']) < end])


def journey(signup):
    return {'event_id': 'journey', 'account_fingerprint': 'a'*64,
            'workspace_fingerprint': m.digest('ws-private'),
            'signup_completed_at': signup.isoformat()}


def test_month_backlog_replays_one_hour_at_most_without_resetting_current_feed(monkeypatch):
    monkeypatch.setenv('GROWTH_GATEWAY_ATTEMPTS_ENABLED', 'true')
    previous = NOW-dt.timedelta(days=30)
    original = {**state(), 'gateway_watermark': previous.isoformat(),
                'gateway_settled_through': previous.isoformat()}
    before = copy.deepcopy(original)
    io = GatewayIO()
    updated, _ = cycle(original, io, NOW)
    assert original == before
    assert m.date(updated['watermark']) == NOW-dt.timedelta(minutes=1)
    assert m.date(updated['gateway_watermark']) == previous+dt.timedelta(hours=1)
    assert len(io.gateway_windows) == 12
    # Settled history: every window is its own five minutes, no overlap.
    assert io.gateway_windows[0][0] == previous
    assert all(right-left == ga.RECOVERY_STEP for left, right in io.gateway_windows)
    assert io.sent[-1]['gateway_status'] == 'catching_up'
    assert io.sent[-1]['gateway_observed_through'] == updated['gateway_watermark']


def test_recovery_can_reach_present_and_status_is_then_current():
    previous = NOW-dt.timedelta(minutes=11)
    checkpoint = {'gateway_watermark': previous.isoformat()}
    io = GatewayIO()
    _, status = collect_gateway(checkpoint, io, NOW, NOW, [], [], None)
    assert status == 'current'
    assert checkpoint['gateway_watermark'] == NOW.isoformat()
    assert len(io.gateway_windows) == 3
    # Near the present, late deliveries are still possible: keep the overlap.
    assert io.gateway_windows[0][0] == previous-ga.OVERLAP
    assert io.gateway_windows[-1][1] == NOW


def test_dense_settled_history_fits_the_audit_cap_and_advances():
    # Production stall from 2026-09-28: the checkpoint held ~14,500 completed
    # audits from the ten minutes before the cursor, and re-reading that overlap
    # with the next window exceeded the 20,000-audit cap on every run.
    previous = NOW-dt.timedelta(days=30)
    def pair(i, at):
        return [projected(stage, at+dt.timedelta(seconds=s), instance=f'vm-{i}')
                for stage, s in (('start', 0), ('end', 1))]
    buffered = [r for i in range(7_000)
                for r in pair(i, previous-ga.OVERLAP+dt.timedelta(milliseconds=80*i))]
    window = [r for i in range(7_000, 14_000)
              for r in pair(i, previous+dt.timedelta(milliseconds=40*(i-7_000)))]
    io = GatewayIO(buffered+window)
    # Production's checkpoint predates the flag: its tail was read near the
    # present, so the tail is re-read once (it fits), then replay advances.
    checkpoint = {'gateway_watermark': previous.isoformat(), 'gateway_audits': buffered}
    assert len(ga.collect(checkpoint, io, NOW, NOW)) == len(buffered)
    assert checkpoint['gateway_watermark'] == checkpoint['gateway_settled_through'] == previous.isoformat()
    audits = ga.collect(checkpoint, io, NOW, NOW)
    assert io.gateway_windows == [(previous-ga.OVERLAP, previous), (previous, previous+ga.RECOVERY_STEP)]
    assert len(audits) == len(window)
    assert checkpoint['gateway_watermark'] == (previous+ga.RECOVERY_STEP).isoformat()


def test_late_start_in_a_near_present_tail_is_recovered_after_a_gap():
    # A run checkpoints near the present; a start for its tail is delivered
    # late; the next run comes more than an hour later. The tail is re-read.
    previous = NOW-dt.timedelta(minutes=70)
    start = projected('start', previous-dt.timedelta(minutes=4))
    end = projected('end', previous-dt.timedelta(minutes=3))
    checkpoint = {'gateway_watermark': previous.isoformat(),
                  'gateway_settled_through': (previous-dt.timedelta(hours=1)).isoformat(),
                  'gateway_audits': [end]}
    io = GatewayIO([start, end])
    attempts, _ = collect_gateway(checkpoint, io, NOW, previous+ga.RECOVERY_STEP,
                                  [journey(previous-dt.timedelta(hours=1))], [], None)
    assert io.gateway_windows[0] == (previous-ga.OVERLAP, previous)
    assert attempts and all(a['paired'] for a in attempts)


def test_pair_survives_the_switch_from_settled_to_near_present_windows():
    # The first window is settled; the second ends near the present. Settled
    # history left no late tail, so the second reads no overlap and cannot
    # re-read an end whose start was compacted.
    start = projected('start', NOW-dt.timedelta(minutes=71))
    end = projected('end', NOW-dt.timedelta(minutes=69))
    checkpoint = {'gateway_watermark': (NOW-dt.timedelta(minutes=65)).isoformat(),
                  'gateway_settled_through': (NOW-dt.timedelta(minutes=65)).isoformat(),
                  'gateway_audits': [start, end]}
    io = GatewayIO([start, end])
    attempts, status = collect_gateway(checkpoint, io, NOW, NOW-dt.timedelta(minutes=55),
                                      [journey(NOW-dt.timedelta(hours=2))], [], None)
    assert status == 'current'
    assert io.gateway_windows == [
        (NOW-dt.timedelta(minutes=65), NOW-dt.timedelta(minutes=60)),
        (NOW-dt.timedelta(minutes=60), NOW-dt.timedelta(minutes=55))]
    assert checkpoint['gateway_settled_through'] == (NOW-dt.timedelta(minutes=60)).isoformat()


def test_resumed_overlap_never_reaches_into_compacted_settled_history():
    # A settled window pairs and compacts an attempt; later near-present
    # windows re-read their overlap only back to the settled boundary, so the
    # end is never read again without its start.
    previous = NOW-dt.timedelta(minutes=65)
    start = projected('start', NOW-dt.timedelta(minutes=66))
    end = projected('end', NOW-dt.timedelta(minutes=62))
    checkpoint = {'gateway_watermark': previous.isoformat(),
                  'gateway_settled_through': previous.isoformat(), 'gateway_audits': [start]}
    io = GatewayIO([start, end])
    attempts, status = collect_gateway(checkpoint, io, NOW, NOW-dt.timedelta(minutes=50),
                                      [journey(NOW-dt.timedelta(hours=2))], [], None)
    assert status == 'current'
    assert io.gateway_windows == [
        (NOW-dt.timedelta(minutes=65), NOW-dt.timedelta(minutes=60)),
        (NOW-dt.timedelta(minutes=60), NOW-dt.timedelta(minutes=55)),
        (NOW-dt.timedelta(minutes=60), NOW-dt.timedelta(minutes=50))]
    assert attempts and all(a['paired'] for a in attempts)
    assert attempts and all(a['paired'] for a in attempts)


def test_retention_expired_history_is_not_silently_skipped():
    previous = NOW-dt.timedelta(days=90)
    checkpoint = {'gateway_watermark': previous.isoformat()}
    io = GatewayIO()
    _, status = collect_gateway(checkpoint, io, NOW, NOW, [], [], None)
    assert status == 'unavailable'
    assert checkpoint['gateway_watermark'] == previous.isoformat()
    assert not io.gateway_windows


def test_future_checkpoint_is_not_rewound():
    previous = NOW+dt.timedelta(minutes=5)
    checkpoint = {'gateway_watermark': previous.isoformat()}
    io = GatewayIO()
    with pytest.raises(ValueError, match='ahead'):
        ga.collect(checkpoint, io, NOW, NOW)
    assert checkpoint['gateway_watermark'] == previous.isoformat()
    assert not io.gateway_windows


def test_historical_start_survives_until_end_in_a_later_window():
    previous = NOW-dt.timedelta(days=30)
    start = projected('start', previous-dt.timedelta(seconds=1))
    end = projected('end', previous+dt.timedelta(minutes=7), status=429)
    checkpoint = {'gateway_watermark': previous.isoformat(), 'gateway_audits': [start]}
    journeys = [journey(previous-dt.timedelta(minutes=2))]
    attempts, status = collect_gateway(checkpoint, GatewayIO([end]), NOW,
                                      previous+dt.timedelta(minutes=10), journeys, [], None)
    assert status == 'current'
    assert len(attempts) == 1
    assert attempts[0]['paired'] and attempts[0]['http_status'] == 429
    assert attempts[0]['started_at'] == start['_time']
    assert journeys[0]['first_gateway_attempt_at'] == start['_time']
    assert journeys[0]['first_gateway_http_status'] == 429


def test_existing_checkpoint_evidence_is_projected_before_compaction():
    previous = NOW-dt.timedelta(days=30)
    start = projected('start', previous-dt.timedelta(minutes=30))
    end = projected('end', previous-dt.timedelta(minutes=29), status=403)
    checkpoint = {'gateway_watermark': previous.isoformat(), 'gateway_audits': [start, end]}
    journeys = [journey(previous-dt.timedelta(hours=1))]
    attempts, _ = collect_gateway(checkpoint, GatewayIO(), NOW, NOW, journeys, [], None)
    assert len(attempts) == 1 and attempts[0]['paired']
    assert not checkpoint['gateway_audits']
    assert journeys[0]['first_gateway_attempt_at'] == start['_time']
    assert checkpoint['gateway_milestones']['journey']['first_gateway_http_status'] == 403


def test_first_milestone_does_not_move_forward_across_recovery_windows():
    previous = NOW-dt.timedelta(days=2)
    records = [projected(stage, previous+dt.timedelta(minutes=minute, seconds=offset),
                         instance=str(minute), status=429)
               for minute in (1, 8, 16) for offset, stage in enumerate(('start', 'end'))]
    checkpoint = {'gateway_watermark': previous.isoformat()}
    journeys = [journey(previous)]
    attempts, _ = collect_gateway(checkpoint, GatewayIO(records), NOW,
                                 previous+dt.timedelta(minutes=20), journeys, [], None)
    assert len(attempts) == 3
    assert journeys[0]['first_gateway_attempt_at'] == records[0]['_time']
    assert journeys[0]['first_gateway_http_error_at'] == records[1]['_time']


@pytest.mark.parametrize('failure,status', [
    (ga.AuditBudgetExceeded('bounded'), 'catching_up'),
    (RuntimeError('private response must not be logged'), 'unavailable'),
])
def test_incomplete_next_window_keeps_only_complete_cursor(failure, status, capsys):
    previous = NOW-dt.timedelta(days=2)
    class Partial(GatewayIO):
        def gateway_audits(self, start, end):
            if self.gateway_windows:
                raise failure
            return super().gateway_audits(start, end)
    checkpoint = {'gateway_watermark': previous.isoformat(), 'gateway_settled_through': previous.isoformat()}
    _, actual = collect_gateway(checkpoint, Partial(), NOW, NOW, [], [], None)
    assert actual == status
    assert m.date(checkpoint['gateway_watermark']) == previous+ga.RECOVERY_STEP
    assert 'private response' not in capsys.readouterr().out


def test_failed_delivery_replays_the_same_historical_windows(monkeypatch):
    monkeypatch.setenv('GROWTH_GATEWAY_ATTEMPTS_ENABLED', 'true')
    previous = NOW-dt.timedelta(days=2)
    original = {**state(), 'gateway_watermark': previous.isoformat()}
    before = copy.deepcopy(original)
    failed = GatewayIO(fail=True)
    with pytest.raises(RuntimeError):
        cycle(original, failed, NOW)
    assert original == before
    retry = GatewayIO()
    cycle(original, retry, NOW)
    assert retry.gateway_windows == failed.gateway_windows


def test_out_of_window_source_records_cannot_advance_gateway_checkpoint():
    previous = NOW-dt.timedelta(days=2)
    original = {'gateway_watermark': previous.isoformat()}
    class Invalid:
        def gateway_audits(self, start, end):
            return [projected('start', end)]
    with pytest.raises(ValueError, match='outside'):
        ga.collect(original, Invalid(), NOW, NOW)
    assert original == {'gateway_watermark': previous.isoformat()}


def test_audit_state_cap_still_fails_without_truncation(monkeypatch):
    monkeypatch.setattr(ga, 'MAX_AUDITS', 2)
    previous = NOW-dt.timedelta(minutes=5)
    records = [projected('start', previous, instance=str(i)) for i in range(3)]
    original = {'gateway_watermark': previous.isoformat()}
    with pytest.raises(ValueError, match='state bound'):
        ga.collect(original, GatewayIO(records), NOW, NOW)
    assert original == {'gateway_watermark': previous.isoformat()}


def test_source_page_budget_is_shared_across_recovery_windows():
    sources = object.__new__(Sources)
    sources.project = 'test-project'
    calls = []
    def google(method, url, **kwargs):
        calls.append(kwargs)
        assert 0 < kwargs['timeout'] <= 10
        return httpx.Response(200, json={})
    sources.google = google
    for _ in range(MAX_GATEWAY_PAGES):
        assert sources.gateway_audits(NOW-dt.timedelta(minutes=1), NOW) == []
    with pytest.raises(ga.AuditBudgetExceeded, match='pagination'):
        sources.gateway_audits(NOW-dt.timedelta(minutes=1), NOW)
    assert len(calls) == MAX_GATEWAY_PAGES
    assert all('tr-growth-source/views/_AllLogs' in c['json']['resourceNames'][0] for c in calls)


def test_partial_paginated_window_never_returns_partial_rows():
    sources = object.__new__(Sources)
    sources.project = 'test-project'
    calls = []
    def google(method, url, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        return httpx.Response(200, json={'entries': [audit('start')], 'nextPageToken': 'more'})
    sources.google = google
    with pytest.raises(ga.AuditBudgetExceeded):
        sources.gateway_audits(NOW-dt.timedelta(minutes=5), NOW)
    assert len(calls) == MAX_GATEWAY_PAGES
    assert calls[1]['json']['pageToken'] == 'more'


def test_time_budget_prevents_another_source_request(monkeypatch):
    sources = object.__new__(Sources)
    sources.project = 'test-project'
    sources.gateway_deadline = 100
    sources.gateway_pages = 0
    monkeypatch.setattr('scripts.axiom_growth.runtime.time.monotonic', lambda: 101)
    sources.google = lambda *a, **kw: pytest.fail('No request after deadline')
    with pytest.raises(ga.AuditBudgetExceeded, match='time'):
        sources.gateway_audits(NOW-dt.timedelta(minutes=1), NOW)
    assert sources.gateway_pages == 0


def test_only_content_free_projected_fields_reach_recovery_output():
    previous = NOW-dt.timedelta(minutes=5)
    records = [ga.project(audit(stage)) for stage in ('start', 'end')]
    attempts, _ = collect_gateway({'gateway_watermark': previous.isoformat()},
        GatewayIO(records), NOW, NOW, [journey(previous)], [], None)
    assert attempts
    for forbidden in ('ws-private', 'req-1', 'vm-1', 'private@example.com', 'do not export'):
        assert forbidden not in json.dumps(attempts)
