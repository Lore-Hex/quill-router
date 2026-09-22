"""Regression contracts for off-path attempt and commercial-account linkage."""
import copy
import datetime as dt
import json
import logging

import pytest

from clickhouse.refresh_workspace_directory import _project_workspace
from scripts.axiom_growth import gateway_attempts as ga
from scripts.axiom_growth import model as m
from scripts.axiom_growth.runtime import cycle
from tests.test_axiom_growth_worker import NOW, FakeIO, state


def audit(stage, *, status=200, workspace='ws-private', instance='vm-1', route='/v1/responses'):
    message = (f'enclave.request_{stage} request_log_id="req-1" method="POST" '
               f'route="{route}" body_bytes=123 credential_fingerprint="{"c" * 64}"')
    if stage == 'end':
        message += (f' status={status} outcome="success" elapsed_ms=250 '
                    f'workspace_id="{workspace}" credential_id="private-key-hash" response_bytes=99')
    return {'timestamp': (NOW-dt.timedelta(minutes=2, seconds=1 if stage == 'start' else 0)).isoformat(),
            'resource': {'labels': {'instance_id': instance}}, 'jsonPayload': {'MESSAGE': message},
            'email': 'private@example.com', 'prompt': 'do not export', 'output': 'private'}


@pytest.mark.parametrize('status,outcome', [(200, 'http_response'), (400, 'http_error'),
    (401, 'http_error'), (429, 'http_error'), (502, 'http_error'), (0, 'connection_closed')])
def test_gateway_pairs_are_actual_records_and_http_is_not_inference_success(status, outcome):
    rows = [ga.project(audit('start')), ga.project(audit('end', status=status))]
    result = ga.snapshots(rows, [], NOW)[0]
    assert result['paired'] is True
    assert result['http_status'] == status and result['gateway_outcome'] == outcome
    assert result['started_at'] < result['finished_at']
    serialized = json.dumps(result)
    for forbidden in ('private', 'req-1', 'vm-1', 'prompt', 'credential', 'output', 'body'):
        assert forbidden not in serialized
    assert 'finish_reason' not in result  # HTTP status cannot supply the provider's stop reason.


def test_missing_start_never_synthesized_from_elapsed_time():
    row = ga.snapshots([ga.project(audit('end', status=403))], [], NOW)[0]
    assert row['started_at'] is None and not row['paired']
    assert row['gateway_outcome'] == 'missing_start'


def test_gateway_retry_and_cross_instance_request_ids_are_distinct():
    one, two = ga.project(audit('start')), ga.project(audit('start', instance='vm-2'))
    assert one['attempt_id'] != two['attempt_id']


def test_gateway_disagreeing_ends_or_reversed_timestamps_are_conflicting():
    start = ga.project(audit('start'))
    end = ga.project(audit('end'))
    failed = ga.project(audit('end', status=500))
    assert ga.snapshots([start, end, failed], [], NOW)[0]['gateway_outcome'] == 'conflicting'
    start['_time'] = (NOW + dt.timedelta(seconds=1)).isoformat()
    assert ga.snapshots([start, end], [], NOW)[0]['gateway_outcome'] == 'conflicting'


def test_gateway_health_and_user_controlled_routes_are_not_marketing_events():
    assert ga.project(audit('start', route='/health')) is None
    assert ga.project(audit('start', route='/v1/responses?key=private')) is None
    invalid = audit('end')
    invalid['jsonPayload']['MESSAGE'] += ' unparseable'
    assert ga.project(invalid) is None


def test_gateway_failed_delivery_keeps_watermark_and_replay_ids(monkeypatch):
    monkeypatch.setenv('GROWTH_GATEWAY_ATTEMPTS_ENABLED', 'true')
    class IO(FakeIO):
        def gateway_audits(self, start, end):
            return [ga.project(audit('start')), ga.project(audit('end', status=429))]
    original = state()
    original['watermark'] = (NOW-dt.timedelta(minutes=5)).isoformat()
    before = copy.deepcopy(original)
    with pytest.raises(RuntimeError):
        cycle(original, IO(fail=True), NOW)
    assert original == before
    io = IO()
    updated, _ = cycle(original, io, NOW)
    assert len(updated['gateway_audits']) == 2
    again, changed = cycle(updated, IO(), NOW+dt.timedelta(minutes=5))
    assert changed == 0 and len(again['gateway_audits']) == 2


def test_billing_owner_projection_never_claims_historical_caller():
    raw = {'id': 'ws-private', 'name': 'Workspace', 'owner_user_id': 'user-private',
           'created_at': NOW.isoformat(), 'email': 'secret@example.com'}
    projected = _project_workspace(raw, refreshed_at=NOW)
    owner = m.digest('tr-account:user-private')
    assert projected['billing_account_fingerprint'] == owner
    assert 'user-private' not in json.dumps(projected) and 'email' not in json.dumps(projected)
    daily = [{'workspace_fingerprint': m.digest('ws-private'), 'account_fingerprint': ''}]
    m.link_billing_accounts(daily, [{'workspace_fingerprint': m.digest('ws-private'),
        'billing_account_fingerprint': owner, 'billing_owner_observed_at': NOW.isoformat()}],
        [{'account_fingerprint': owner, 'first_source': 'google'}])
    assert daily[0]['account_fingerprint'] == ''  # Do not relabel lost historical identity.
    assert daily[0]['billing_source'] == 'google'
    assert daily[0]['billing_identity_basis'] == 'current_workspace_owner'
    for field in ('deleted', 'federated_home'):
        assert _project_workspace({**raw, field: True}, refreshed_at=NOW)['billing_account_fingerprint'] == ''


def test_billing_conflicting_source_is_unknown_and_ownership_is_validated():
    owner = {'workspace_fingerprint': 'b'*64, 'billing_account_fingerprint': 'a'*64}
    daily = [{'workspace_fingerprint': 'b'*64}]
    journeys = [{'account_fingerprint': 'a'*64, 'first_source': s} for s in ('google', 'x')]
    m.link_billing_accounts(daily, [owner], journeys)
    assert daily[0]['billing_source'] == '(unattributed)'
    with pytest.raises(ValueError, match='Conflicting'):
        m.link_billing_accounts(daily, [owner, {**owner, 'billing_account_fingerprint': 'c'*64}], [])


@pytest.mark.parametrize('host', ['www.yahoo.com', 'search.yahoo.com', 'search.yahoo.co.jp'])
def test_yahoo_is_one_channel(host):
    assert m.canonical_source(host) == 'yahoo'


def test_onboarding_assignment_is_balanced_and_stable():
    from trusted_router.marketing_experiments import assigned_onboarding_cell
    cells = [assigned_onboarding_cell(f'{i:032x}') for i in range(2000)]
    assert set(cells) == {'run_request', 'get_answer'}
    assert 900 <= cells.count('run_request') <= 1100
    assert cells == [assigned_onboarding_cell(f'{i:032x}') for i in range(2000)]


@pytest.mark.parametrize('cell', ['run_request', 'get_answer'])
def test_onboarding_experiment_roundtrip_and_projection(cell, caplog):
    from starlette.requests import Request

    from trusted_router import acquisition as a
    from trusted_router.config import Settings
    from trusted_router.main import _ApplicationConsoleFormatter
    from trusted_router.marketing_experiments import ONBOARDING_EXPERIMENT_ID
    settings = Settings()
    now = dt.datetime.now(dt.UTC).isoformat()
    touch = {'utm_source': 'qa', 'utm_campaign': 'attribution_test', 'landing_path': '/',
             'captured_at': now, 'experiment_id': ONBOARDING_EXPERIMENT_ID, 'experiment_cell_id': cell}
    context = a.AttributionContext('a'*32, touch, touch, now)
    request = Request({'type': 'http', 'headers': []})
    request.state.acquisition_attribution = context
    snapshot = a.oauth_attribution_snapshot(request, settings, 'state')
    callback = Request({'type': 'http', 'headers': []})
    a.restore_oauth_attribution(callback, settings, 'state', snapshot)
    caplog.set_level(logging.INFO, logger='trusted_router.acquisition')
    assert a.onboarding_exposure(callback, user_id='private', workspace_id='ws-private') == cell
    record = next(r for r in caplog.records if r.getMessage() == 'acquisition.experiment_exposed')
    payload = json.loads(_ApplicationConsoleFormatter().format(record))
    assert payload['account_fingerprint'] == m.digest('tr-account:private')
    assert payload['workspace_fingerprint'] == m.digest('ws-private')
    projected = m.project_event({**payload, '_time': now}, source='cloud_logging')
    assert projected['experiment_cell_id'] == cell and projected['utm_source'] == 'qa'
    assert 'private' not in json.dumps(projected)


def test_cloud_acquisition_formatter_rejects_raw_identity():
    from trusted_router.main import _ApplicationConsoleFormatter
    record = logging.makeLogRecord({'name': 'trusted_router.acquisition',
        'levelname': 'INFO', 'msg': 'acquisition.experiment_exposed',
        'event': 'acquisition.experiment_exposed', 'account_fingerprint': 'user_private',
        'workspace_fingerprint': 'ws_private', 'prompt': 'secret'})
    payload = json.loads(_ApplicationConsoleFormatter().format(record))
    assert not {'account_fingerprint', 'workspace_fingerprint', 'prompt'} & payload.keys()


def test_first_attempt_milestones_survive_short_buffer_and_do_not_cross_accounts():
    signup = (NOW-dt.timedelta(days=10)).isoformat()
    journey = {'event_id': 'journey', 'account_fingerprint': 'a'*64,
               'workspace_fingerprint': m.digest('ws-private'), 'signup_completed_at': signup,
               'first_successful_api_call_at': NOW.isoformat()}
    attempt = ga.snapshots([ga.project(audit('start')), ga.project(audit('end', status=429))], [], NOW)
    ga.link_first_attempts([journey], attempt)
    checkpoint = {}
    ga.preserve_milestones(checkpoint, [journey])
    later = {k: v for k, v in journey.items() if not k.startswith('first_gateway_')}
    ga.preserve_milestones(checkpoint, [later])
    assert later['first_gateway_attempt_at'] == journey['first_gateway_attempt_at']
    assert later['first_gateway_http_status'] == 429
    new_owner = {k: v for k, v in later.items() if not k.startswith('first_gateway_')}
    new_owner['account_fingerprint'] = 'b'*64
    ga.preserve_milestones(checkpoint, [new_owner])
    assert 'first_gateway_attempt_at' not in new_owner


def test_gateway_source_failure_does_not_stop_conversion_export(monkeypatch, capsys):
    from tests.test_axiom_growth_worker import event
    monkeypatch.setenv('GROWTH_GATEWAY_ATTEMPTS_ENABLED', 'true')
    class IO(FakeIO):
        def gateway_audits(self, start, end):
            raise RuntimeError('secret private response')
    io = IO([event()])
    updated, _ = cycle(state(), io, NOW)
    assert updated['watermark'] != state()['watermark']
    assert 'gateway_watermark' not in updated
    assert io.sent[-1]['gateway_status'] == 'unavailable'
    assert any(r['event'] == 'acquisition.signup_completed' for r in io.sent)
    assert 'secret' not in capsys.readouterr().out


def test_established_traffic_is_not_reexported_as_acquisition_attempts():
    attempt = ga.snapshots([ga.project(audit('start')), ga.project(audit('end'))], [], NOW)
    workspace = m.digest('ws-private')
    journeys = [{'workspace_fingerprint': workspace,
                 'signup_completed_at': (NOW-dt.timedelta(days=3)).isoformat()}]
    assert ga.pre_activation_attempts(attempt, [], journeys) == attempt
    earlier = [{'workspace_fingerprint': workspace, 'first_call_at': (NOW-dt.timedelta(days=2)).isoformat()}]
    assert not ga.pre_activation_attempts(attempt, earlier, journeys)
    first_call = [{'workspace_fingerprint': workspace, 'first_call_at': NOW.isoformat()}]
    assert ga.pre_activation_attempts(attempt, first_call, journeys) == attempt


def test_monitors_without_organic_usage_are_not_customer_first_attempts():
    attempts = ga.snapshots([ga.project(audit('start')), ga.project(audit('end'))], [], NOW)
    assert not ga.pre_activation_attempts(attempts, [], [])
    assert not ga.pre_activation_attempts(attempts, [], [{'workspace_fingerprint': m.digest('ws-private')}])
    later_signup = [{'workspace_fingerprint': m.digest('ws-private'), 'signup_completed_at': NOW.isoformat()}]
    assert not ga.pre_activation_attempts(attempts, [], later_signup)


def test_owner_refresh_time_alone_does_not_reexport_years_of_usage():
    from scripts.axiom_growth.runtime import content_hash
    row = {'billing_account_fingerprint': 'a'*64, 'billing_owner_observed_at': NOW.isoformat()}
    later = {**row, 'billing_owner_observed_at': (NOW+dt.timedelta(hours=1)).isoformat()}
    assert content_hash(row) == content_hash(later)
    assert content_hash(row) != content_hash({**later, 'billing_account_fingerprint': 'b'*64})


@pytest.mark.parametrize('has_cache', [False, True])
def test_directory_failure_preserves_verified_snapshot_and_conversion_export(monkeypatch, capsys, has_cache):
    from tests.test_axiom_growth_worker import event
    monkeypatch.setenv('GROWTH_BILLING_OWNERS_ENABLED', 'true')
    class IO(FakeIO):
        def billing_owners(self):
            raise RuntimeError('private directory error')
    original = state()
    owners = [{'workspace_fingerprint': 'b'*64, 'billing_account_fingerprint': 'a'*64,
               'billing_owner_observed_at': NOW.isoformat()}] if has_cache else []
    original['billing_owners'] = owners
    io = IO([event()])
    updated, _ = cycle(original, io, NOW)
    assert updated['billing_owners'] == owners
    assert io.sent[-1]['billing_owner_status'] == ('stale' if has_cache else 'unavailable')
    assert any(r['event'] == 'acquisition.signup_completed' for r in io.sent)
    assert 'private' not in capsys.readouterr().out


def test_completed_audits_expire_but_unmatched_are_retained():
    start, end = ga.project(audit('start')), ga.project(audit('end'))
    unmatched = ga.project(audit('start', instance='vm-2'))
    for row in (start, end, unmatched):
        row['_time'] = (NOW-dt.timedelta(hours=1)).isoformat()
    expired = {**unmatched, 'event_id': 'expired', 'attempt_id': 'expired',
               '_time': (NOW-dt.timedelta(days=4)).isoformat()}
    checkpoint = {'gateway_audits': [start, end, unmatched, expired]}
    class IO:
        def gateway_audits(self, start, end):
            return []
    assert ga.collect(checkpoint, IO(), NOW, NOW) == [unmatched]


def test_schema_registration_is_not_a_fake_customer_or_conversion():
    from scripts.axiom_growth.register_schema import registration
    one, two = registration(NOW), registration(NOW+dt.timedelta(minutes=1))
    assert one['event_id'] == two['event_id']
    assert one['record_type'] == 'schema_registration'
    assert one['event'] not in m.BROWSER_EVENTS | m.CONVERSION_EVENTS
    assert not {'account_fingerprint', 'anonymous_fingerprint', 'amount_microdollars',
                'input_tokens', 'output_tokens'} & one.keys()


def test_worker_release_preserves_main_ci_gate_and_existing_configuration():
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load(Path('.github/workflows/deploy-growth-sync.yml').read_text())
    job = workflow['jobs']['deploy']
    assert "github.ref == 'refs/heads/main'" in job['if']
    steps = job['steps']
    gate = next(s['run'] for s in steps if s.get('name', '').startswith('Require confirmation'))
    assert '--commit "$GITHUB_SHA"' in gate and '= success' in gate and '= APPLY' in gate
    run = '\n'.join(s.get('run', '') for s in steps)
    assert '--update-env-vars' in run and '--set-env-vars' not in run
    assert '--image "$old_image"' in run and '--wait' in run
    assert 'sha256:' in run
    for forbidden in ('--set-secrets', '--service-account', 'add-iam-policy-binding', '--hotfix'):
        assert forbidden not in run
    for field in ('gateway_attempts', 'billing_owners'):
        assert workflow[True]['workflow_dispatch']['inputs'][field]['default'] is False


@pytest.mark.parametrize('cell,label', [('run_request', 'Run my first API request'),
                                      ('get_answer', 'Get my first AI answer')])
def test_welcome_renders_actual_assigned_treatment(client, cell, label, caplog, monkeypatch):
    from trusted_router.acquisition import (
        ATTRIBUTION_COOKIE_NAME,
        AttributionContext,
        encode_attribution_cookie,
    )
    from trusted_router.marketing_experiments import ONBOARDING_EXPERIMENT_ID
    from trusted_router.routes.console import welcome
    from trusted_router.storage import STORE
    user = STORE.ensure_user('experiment@example.test')
    token, _ = STORE.create_auth_session(user_id=user.id, provider='google',
        label='experiment', ttl_seconds=3600, state='active')
    client.cookies.set('tr_session', token)
    client.cookies.set('tr_pending_reveal', 'sk-tr-test-only')
    now = dt.datetime.now(dt.UTC).isoformat()
    touch = {'utm_source': 'qa', 'utm_campaign': 'test', 'landing_path': '/', 'captured_at': now,
             'experiment_id': ONBOARDING_EXPERIMENT_ID, 'experiment_cell_id': cell}
    client.cookies.set(ATTRIBUTION_COOKIE_NAME,
        encode_attribution_cookie(AttributionContext('a'*32, touch, touch, now), client.app.state.settings))
    monkeypatch.setattr(welcome, 'live_credit_summary', lambda _: {'total_credits': 300000})
    caplog.set_level(logging.INFO, logger='trusted_router.acquisition')
    response = client.get('/console/welcome?first=1')
    assert response.status_code == 200 and label in response.text
    assert 'data-copy-template-target="welcome-agent-message"' in response.text
    assert len([r for r in caplog.records if r.getMessage() == 'acquisition.experiment_exposed']) == 1
    caplog.clear()
    client.cookies.delete('tr_pending_reveal')
    assert client.get('/console/welcome').status_code == 200
    assert not any(r.getMessage() == 'acquisition.experiment_exposed' for r in caplog.records)
