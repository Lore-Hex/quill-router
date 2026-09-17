"""Continuity, replay, failure and privacy contracts for the off-path worker."""
import copy
import datetime as dt

import httpx
import pytest

from scripts.axiom_growth import model as m
from scripts.axiom_growth.runtime import Sources, complete, content_hash, cycle, project

NOW = dt.datetime(2026, 9, 16, 22, tzinfo=dt.UTC)


def event(name='signup_completed', **kwargs):
    return project({'_time': '2026-09-16T21:55:00.000123456Z',
        'event': 'acquisition.' + name, 'anonymous_fingerprint': 'a'*64,
        'utm_source': 'google', **kwargs}, 'axiom')


class FakeIO:
    def __init__(self, events=(), daily=(), fail=False):
        self.rows = list(events)
        self.daily = list(daily)
        self.fail = fail
        self.sent = []
        self.windows = []

    def events(self, start, end):
        self.windows.append((start, end))
        return copy.deepcopy(self.rows)

    def usage(self, start, end):
        return copy.deepcopy(self.daily)

    def ingest(self, rows):
        if self.fail:
            raise RuntimeError('test delivery failure')
        self.sent.extend(rows)


def state():
    return {'watermark': (NOW-dt.timedelta(minutes=5)).isoformat(),
            'events': [], 'daily': [], 'hashes': {}, 'repair_day': NOW.date().isoformat()}


def test_repeat_export_never_duplicates_purchase_or_unchanged_journey():
    row = event('credit_purchase_completed', amount_microdollars=20_000_000)
    io = FakeIO([row, row])
    first, count = cycle(state(), io, NOW)
    assert count == 2
    journey = next(r for r in io.sent if r['event'] == 'growth.journey')
    assert journey['purchase_count'] == 1
    assert journey['purchase_microdollars'] == 20_000_000
    io.sent.clear()
    second, count = cycle(first, io, NOW + dt.timedelta(minutes=5))
    assert count == 0
    assert [r['event'] for r in io.sent] == ['growth.sync_completed']
    assert second['watermark'] > first['watermark']


def test_failed_ingest_does_not_mutate_checkpoint_or_emit_success():
    original = state()
    before = copy.deepcopy(original)
    io = FakeIO([event()], fail=True)
    with pytest.raises(RuntimeError):
        cycle(original, io, NOW)
    assert original == before
    assert not io.sent


def test_partial_batch_replay_converges_after_failure():
    class Partial(FakeIO):
        def ingest(self, rows):
            if rows:
                self.sent.extend(rows[:1])
            raise RuntimeError('partial delivery')
    original = state()
    io = Partial([event()])
    with pytest.raises(RuntimeError):
        cycle(original, io, NOW)
    retry = FakeIO([event()])
    cycle(original, retry, NOW)
    assert io.sent[0]['event_id'] == retry.sent[0]['event_id']


def test_daily_repair_has_24h_overlap_and_catches_late_events():
    original = state()
    original['repair_day'] = '2026-09-15'
    io = FakeIO([event()])
    updated, _ = cycle(original, io, NOW)
    assert io.windows[0][0] == m.date(original['watermark'])-dt.timedelta(hours=24)
    assert updated['repair_day'] == '2026-09-16'


def test_backlog_and_retention_fail_closed():
    original = state()
    original['watermark'] = (NOW-dt.timedelta(days=3)).isoformat()
    io = FakeIO()
    updated, _ = cycle(original, io, NOW)
    assert m.date(updated['watermark']) == NOW-dt.timedelta(days=2)
    original['watermark'] = (NOW-dt.timedelta(days=30)).isoformat()
    with pytest.raises(ValueError, match='retention'):
        cycle(original, io, NOW)


def test_raw_content_and_identifiers_never_survive_projection():
    row = event(prompt='private', output='private', body={'text': 'private'},
                email='x@example.com', workspace_id='real-id',
                workspace_fingerprint='not-a-hash', landing_path='/console/secret',
                creative_id='sk-secret', first_landing_path='/blog/a?email=x@y.com')
    assert not {'prompt', 'output', 'body', 'email', 'workspace_id', 'workspace_fingerprint'} & row.keys()
    assert row['landing_path'] == row['first_landing_path'] == '(other)'
    assert row['creative_id'] == '(redacted)'


@pytest.mark.parametrize('status', [
    {'isPartial': True}, {'isEstimate': True},
    {'messages': [{'code': 'default_limit_warning'}]},
    {'messages': [{'code': 'max_limit_warning'}]},
])
def test_query_incompleteness_refused(status):
    with pytest.raises(ValueError):
        complete({'status': status})


def test_only_refresh_timestamps_excluded_from_change_hash():
    row = event()
    assert content_hash(row) == content_hash({**row, 'exported_at': 'later', 'observed_through': 'later'})
    assert content_hash(row) != content_hash({**row, 'utm_source': 'x'})


@pytest.mark.parametrize('response', [{'ingested': 0, 'failed': 1}, {'ingested': 0, 'failed': 0}])
def test_ingest_http_200_is_not_enough(response):
    sources = object.__new__(Sources)
    sources.http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    sources.edge = 'https://example.com'
    sources.headers = {}
    with pytest.raises(ValueError, match='complete batch'):
        sources.ingest([event()])


def test_bootstrap_private_field_refused_before_network():
    sources = object.__new__(Sources)
    with pytest.raises(ValueError, match='Private field'):
        sources.ingest([{**event(), 'prompt': 'do not export'}])


def test_history_identity_not_duplicate_conversion_and_ambiguous_usage_unlinked():
    first = event(workspace_fingerprint='b'*64)
    history = {**first, 'event': 'history.signup_completed', 'event_id': 'history',
               'marketing_workspace_fingerprint': 'd'*64}
    second = {**first, 'anonymous_fingerprint': 'c'*64, 'event_id': 'second'}
    daily = [{'_time': '2026-09-16T00:00:00Z', 'workspace_fingerprint': 'b'*64,
              'marketing_workspace_fingerprint': 'd'*64, 'successful_calls': 5,
              'input_tokens': 100, 'output_tokens': 20}]
    journeys = m.build_journeys([first, history, second], daily, NOW)
    assert len(journeys) == 2
    assert daily[0]['identity_link_status'] == 'ambiguous'
    assert daily[0]['anonymous_fingerprint'] == ''


def test_older_usage_days_are_preserved_when_recent_days_refresh():
    original = state()
    original['daily'] = [{'_time': '2026-08-24T00:00:00Z', 'event_id': 'older',
                          'workspace_fingerprint': '', 'marketing_workspace_fingerprint': ''}]
    updated, _ = cycle(original, FakeIO(), NOW)
    assert updated['daily'][0]['event_id'] == 'older'


def test_dashboards_use_live_cohort_age_and_source_watermark():
    from scripts.axiom_growth.dashboards import FRESH, JOURNEY, build
    assert 'Minutes_behind>15' in FRESH
    assert 'Observed_through' in FRESH
    assert "datetime_diff('day', now(), _time)" in JOURNEY
    for doc in build():
        assert doc['dashboard']['refreshTime'] == 300
        assert doc['dashboard']['owner'] == 'X-AXIOM-EVERYONE'


def test_stale_alert_debounces_and_checks_observed_data_not_only_job_time():
    from scripts.axiom_growth.alerts import QUERY
    assert 'observed_through' in QUERY
    assert '999999' in QUERY  # An empty time window is stale, not healthy.


def test_clickhouse_formatted_day_does_not_shadow_date_predicate(monkeypatch):
    monkeypatch.setenv('GROWTH_CH_PASSWORD', 'test-only')
    queries = []
    def respond(request):
        queries.append(request.content.decode())
        return httpx.Response(200, text='')
    sources = object.__new__(Sources)
    sources.http = httpx.Client(transport=httpx.MockTransport(respond))
    assert sources.usage(NOW-dt.timedelta(days=1), NOW) == []
    assert "toString(day, 'UTC') AS usage_day" in queries[0]
    assert "toString(day, 'UTC') AS day," not in queries[0]
    assert 'FROM tr.growth_daily_usage' in queries[0]


def test_logging_sink_uses_exact_event_allowlist_without_regex_escaping():
    from scripts.axiom_growth.provision import source_filter
    value = source_filter()
    assert '=~' not in value
    assert '\\' not in value
    assert value.count('jsonPayload.event=') == 15
    for name in m.BROWSER_EVENTS | m.CONVERSION_EVENTS:
        assert f'jsonPayload.event="{name}"' in value
    assert 'resource.type="cloud_run_revision"' in value


def onboarding(name='succeeded', **kwargs):
    return project({'_time': '2026-09-16T21:55:00Z',
        'event': 'acquisition.onboarding_call_' + name,
        'anonymous_fingerprint': 'a'*64, 'flow': 'welcome_test',
        'attempt_id': 'cd0fb379-6d48-4660-bde1-7c80b7be174d',
        'http_status': 200, 'elapsed_ms': 1000, **kwargs}, 'cloud_logging')


def test_onboarding_replay_deduplicates_but_distinguishes_attempts_and_outcomes():
    row = onboarding()
    assert row['event_id'] == onboarding(_time='2026-09-16T21:55:01Z')['event_id']
    assert row['event_id'] != onboarding('started')['event_id']
    assert row['event_id'] != onboarding(attempt_id='cd0fb379-6d48-4660-bde1-7c80b7be174e')['event_id']


def test_onboarding_projection_exports_only_classified_metadata():
    row = onboarding('failed', failure_reason='output_budget_exhausted', finish_reason='length',
                     prompt='secret', output='secret', error='secret', api_key='sk-secret')
    assert row['http_status'] == 200
    assert row['failure_reason'] == 'output_budget_exhausted'
    assert not {'prompt', 'output', 'error', 'api_key'} & row.keys()
    assert onboarding(finish_reason='private provider message').get('finish_reason') is None


@pytest.mark.parametrize('details', [
    {'attempt_id': 'sk-secret'}, {'flow': 'private flow'},
    {'http_status': 99}, {'http_status': True}, {'elapsed_ms': -1},
    {'http_status': 500}, {'failure_reason': 'network_error'},
])
def test_onboarding_projection_rejects_invalid_metadata(details):
    with pytest.raises(ValueError):
        onboarding(**details)


def test_onboarding_dashboard_separates_attempts_from_server_activation():
    from scripts.axiom_growth.dashboards import ATTEMPTS, onboarding_query
    query = onboarding_query()
    assert 'first_successful_api_call' not in query
    assert 'first_call_started' not in query
    assert 'by anonymous_fingerprint, attempt_id' in ATTEMPTS
    assert 'Matched_completed>0' in query
    for field in ('Missing_outcome_5m', 'Pending', 'Orphan_outcomes', 'Conflicting_outcomes'):
        assert field in query
