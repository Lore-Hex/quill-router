from __future__ import annotations

import copy
import hashlib
import json
import stat
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization

from scripts.async_settle import drain_settings as drain
from scripts.async_settle import pilot_enablement as pilot
from scripts.async_settle import ticket_keys
from tests.test_async_settle_shadow_accounting import synthetic_window
from tests.test_async_settle_ticket import CLAIMS, FIXTURE
from trusted_router.async_settle_shadow_evidence import COUNTER
from trusted_router.async_settle_ticket import PURPOSE, TicketSigner, verify_ticket
from trusted_router.config import Settings
from trusted_router.detached_jws import TrustedKey, b64encode, canonical


def artifact(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(json.dumps(value))
    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def bundle(tmp_path):
    rows, days, proof = synthetic_window()
    boot = rows[0]['body']['instance']
    intervals = []
    for row in rows:
        body = row['body']
        if row['kind'] == COUNTER:
            body['instance'] = boot
            row['id'] = row['id'].split('/')[0] + '/' + boot
            body['admission_observer'].update(workspace_reads=21600, health_reads=86400, prediction_yes=1)
            intervals.append(dict(day=row['id'].split('/')[0], started_at_us=body['started_at_us'], flushed_at_us=body['flushed_at_us']))
        elif row['id'].endswith('/manifest-v1'):
            body['instance_boot_ids'] = [boot]
            proof['instance_boot_ids_by_day'][body['day']] = [boot]
    serving = [dict(instance=boot, revision='a'*40, region='us-central1', role='router', intervals=copy.deepcopy(intervals), pins={
        'TR_ASYNC_SETTLE_ENABLED': 'false', 'TR_ASYNC_SETTLE_SHADOW_WORKSPACES': pilot.PILOT}),
        dict(instance='enclave', revision=rows[-1]['body']['deployment']['go_revision'], region='us-central1', role='enclave', intervals=copy.deepcopy(intervals), pins={
            'TR_ASYNC_SETTLE_NEGOTIATE': 'off', 'TR_ASYNC_SETTLE_SHADOW': 'on',
            'TR_ASYNC_SETTLE_TICKET_PUBLIC_KEYS': json.dumps({'kid': 'issuer~' + FIXTURE['public_key']})})]
    transport = artifact(tmp_path, 'transport.json', dict(status='PASS', completed_at_us=1, serving=serving))
    observer = fleet_fields(tmp_path, serving)
    fleet_result = pilot.fleet_budgets(dict(observer, serving=serving, rows=rows))
    fleet = artifact(tmp_path, 'fleet.json', dict(fleet_result, completed_at_us=1))
    proof.update(maximum_header_hops=transport['sha256'], fleet_load_budget=fleet['sha256'],
                 publisher_poll_freshness=fleet['sha256'])
    for row in rows:
        if row['id'].endswith('/manifest-v1'):
            row['body']['proof_manifest_sha256'] = hashlib.sha256(canonical(proof)).hexdigest()
    return dict(rows=rows, days=days, proof=proof, serving=serving, observer=observer,
                serving_instance_ids=[boot, 'enclave'], transport=transport, fleet=fleet)


def test_shadow_serving_reuses_report_and_blocks_pins_gaps(tmp_path):
    data = bundle(tmp_path)
    result = pilot.shadow_serving(data)
    assert result['status'] == 'PASS' and result['clock_start_us'] is not None
    data['serving'][1]['pins']['TR_ASYNC_SETTLE_SHADOW'] = 'off'
    assert pilot.shadow_serving(data)['clock_start_us'] is None
    data = bundle(tmp_path)
    data['rows'][0]['body']['closed'] = False
    assert pilot.shadow_serving(data)['status'] == 'BLOCKED'


def trial(**changes):
    return dict(id='measured-1', poll_seconds=.1, batch=2, concurrency=2,
        lease_seconds=8, pass_seconds=5, service_rate=10, claim_seconds=.1,
        resolution_seconds=.1, work_seconds=3, booking_p95_seconds=4,
        completion_max_seconds=10, crash_reclaim_seconds=9, burst_clear_seconds=2,
        lease_losses=0, fence_misses=0, contention_acceptable=True,
        health_freshness_pass=True, repair_included=True, **changes)


def select(trials):
    return drain.select([dict(created_at=1, service_seconds=.1)], trials,
        start=0, end=10, bucket_seconds=1, margin=.5, backlog=2,
        target_seconds=10, rpc_room_seconds=.5)


def test_drain_selection_requires_measured_capacity_and_joint_rpc_room():
    value = select([trial()])
    assert value['status'] == 'PASS'
    assert 'TR_SETTLE_OUTBOX_LEASE_SECONDS=8' in value['pins']
    for key, bad in [('service_rate', .1), ('pass_seconds', 7.9), ('lease_losses', 1),
                     ('booking_p95_seconds', 5.1), ('repair_included', False),
                     ('burst_clear_seconds', 11), ('work_seconds', 5), ('crash_reclaim_seconds', 61)]:
        damaged = trial()
        damaged[key] = bad
        assert select([damaged])['status'] == 'BLOCKED', key
    with pytest.raises(ValueError, match='arrival'):
        drain.select([dict(service_seconds=1)], [], start=0, end=10, bucket_seconds=1,
            margin=.1, backlog=1, target_seconds=10, rpc_room_seconds=1)


def test_ticket_keys_roundtrip_pr_b_and_pr_e_wire(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(FIXTURE['seed_hex']))
    monkeypatch.setattr(ticket_keys.Ed25519PrivateKey, 'generate', lambda: key)
    path = tmp_path / 'private.pem'
    output = ticket_keys.provision(path, kid='async-v1-fixture', issuer='router-fixture', epoch=1)
    assert output['enclave_keyring'] == {'async-v1-fixture': 'router-fixture~' + FIXTURE['public_key']}
    # PR E async_settlement_test.go fixtureKeyring, copied byte-for-byte.
    wire = (Path(__file__).parent / 'fixtures/async_settlement/enclave_keyring_v1.json').read_text().strip()
    assert json.dumps(output['enclave_keyring'], separators=(',', ':')) == wire
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    private = serialization.load_pem_private_key(path.read_bytes(), None)
    public = b64encode(private.public_key().public_bytes_raw())
    trusted = TrustedKey('async-v1-fixture', PURPOSE, public, 'router-fixture', 'router-settlement')
    signer = TicketSigner(private, trusted)
    assert verify_ticket(signer.sign(CLAIMS, CLAIMS['iat']), [trusted], CLAIMS, CLAIMS['iat']).epoch == 1
    assert signer.sign(CLAIMS, CLAIMS['iat']) == FIXTURE['response']['data']['settlement_ticket']
    assert 'PRIVATE KEY' not in json.dumps(output)
    with pytest.raises(FileExistsError):
        ticket_keys.provision(path, kid='async-v1-fixture', issuer='router-fixture', epoch=1)


@pytest.mark.parametrize('value', ['a b', ','.join(str(i) for i in range(33))])
def test_pilot_config_validation(value):
    with pytest.raises(ValueError):
        Settings(environment='test', async_settle_pilot_workspaces=value)


def test_pilot_config_environment(monkeypatch):
    monkeypatch.setenv('TR_ASYNC_SETTLE_PILOT_WORKSPACES', ' ws-v1, ws-v2, ws-v1, ')
    cfg = Settings(environment='test')
    assert cfg.async_settle_pilot_workspace_ids == frozenset({'ws-v1', 'ws-v2'})
    assert cfg.async_settle_pilot_allows('ws-v1') and cfg.async_settle_pilot_allows('ws-v2')
    assert not cfg.async_settle_pilot_allows('ws-v3')


def test_header_probe_exact_bound_and_sync_cleanup():
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path.endswith('authorize'):
            assert len(request.headers['X-TR-Settlement-Shadow'].encode()) == 12288
            assert json.loads(request.content)['max_output_tokens'] == 1
            return httpx.Response(200, json={'data': {'authorization_id': 'auth', 'workspace_id': pilot.PILOT, 'api_key_hash': 'key_1ZXjS8vNqWdQ7qRUkZj8Meuj'}})
        assert request.url.path.endswith('/refund')
        assert 'X-TR-Settlement-Mode' not in request.headers
        assert 'X-TR-Settlement-Shadow' not in request.headers
        assert json.loads(request.content)['actual_output_tokens'] == 0
        return httpx.Response(200, json={'data': {'authorization_id': 'auth', 'finalization_outcome': 'refunded', 'cost_microdollars': 0}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        value = pilot.header_probe('https://hop.test/v1', 'model', 'pilot-secret', 'gateway-secret', client)
    assert value['status'] == 'PASS' and len(requests) == 2
    assert 'secret' not in json.dumps(value)


@pytest.mark.parametrize('status', [401, 431, 503])
def test_header_probe_rejected_never_calls_settle(status):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
        result = pilot.header_probe('https://hop.test/v1', 'model', 'key', 'token', client)
    assert result['status'] == 'BLOCKED' and result['authorize_status'] == status


def test_bounded_production_reads_are_point_or_workspace_index():
    calls = []
    def execute(sql, **kwargs):
        calls.append((sql, kwargs))
        return [('{}',)] if 'tr_entities' in sql else []
    db = SimpleNamespace(snapshot=lambda: nullcontext(SimpleNamespace(execute_sql=execute)))
    assert pilot.point_evidence(db, [dict(kind=COUNTER, id='day/instance')])[0]['body'] == {}
    assert pilot.workspace_outbox(db, pilot.PILOT, 10) == []
    assert 'WHERE kind=@kind AND id=@id' in calls[0][0]
    assert 'FORCE_INDEX=tr_settle_outbox_workspace_status' in calls[1][0]
    assert calls[1][1]['params'] == dict(ws=pilot.PILOT, limit=11)
    assert all(kwargs['timeout'] == .2 and kwargs['retry'] is None for _, kwargs in calls)
    with pytest.raises(ValueError):
        pilot.workspace_outbox(db, 'other', 10)


def fleet_fields(tmp_path, serving):
    pre = pilot.fleet_budgets_pre(pre_enable_bundle(tmp_path, serving))
    return dict(pre_enable=artifact(tmp_path, 'pre-enable.json', dict(pre, completed_at_us=1)), workspace_count=1, router_instance_ids=[r['instance'] for r in serving if r['role'] == 'router'],
        freshness_maxima_seconds=dict(publisher_period=2, publisher_jitter=.25, publication=.5,
            poll_period=1, poll_jitter=.25, install=.2, skew=.25,
            workspace_period=4, workspace_jitter=.25, workspace_install=.2),
        approved_fleet_reads_per_second=2, maximum_router_instances=1, maximum_router_instances_by_region={'us-central1': 1},
        regional_budgets={'us-central1': dict(reads_per_second=2, pending_rows_per_second=300)},
        headroom=artifact(tmp_path, 'headroom.json', {'measured_cpu': 20}))


def fleet_bundle(tmp_path):
    data = bundle(tmp_path)
    data.update(data['observer'])
    return data


def test_fleet_counter_rates_freshness_and_roster(tmp_path):
    data = fleet_bundle(tmp_path)
    result = pilot.fleet_budgets(data)
    assert result['status'] == 'PASS' and result['fleet_reads_per_second'] == 1.25
    data['freshness_maxima_seconds']['install'] = .3
    assert pilot.fleet_budgets(data)['status'] == 'BLOCKED'
    data = fleet_bundle(tmp_path)
    data['rows'][0]['body']['admission_observer']['prediction_unknown'] = 1
    assert pilot.fleet_budgets(data)['status'] == 'BLOCKED'
    data = fleet_bundle(tmp_path)
    data['router_instance_ids'].append('missing')
    assert pilot.fleet_budgets(data)['status'] == 'BLOCKED'


def test_pre_flip_seven_days_artifacts_and_chosen_settings(tmp_path):
    data = bundle(tmp_path)
    source = artifact(tmp_path, 'export.json', [])
    selected = select([trial()])
    selected['exports'] = {source['path']: source['sha256']}
    data.update(drain=artifact(tmp_path, 'drain.json', selected),
        ci={name: artifact(tmp_path, name, {'passed': True}) for name in ('F1', 'F2b', 'F2c')},
        signer=dict(epoch=1, audience='router-settlement', kid='kid', issuer='issuer', mounted=True, public_key=FIXTURE['public_key'],
                    verification=artifact(tmp_path, 'signer.json', {'passed': True})))
    data['serving'][0]['pins'].update(TR_ASYNC_SETTLE_TICKET_KID='kid', TR_ASYNC_SETTLE_TICKET_ISSUER='issuer',
        TR_ASYNC_SETTLE_TICKET_AUDIENCE='router-settlement', TR_ASYNC_SETTLE_AUTHORITY_EPOCH='1',
        TR_ASYNC_SETTLE_TICKET_PRIVATE_KEY_FILE='/mounted/key.pem')
    data['serving'][1]['pins']['TR_ASYNC_SETTLE_TICKET_PUBLIC_KEYS'] = json.dumps({'kid': 'issuer~' + FIXTURE['public_key']})
    assert pilot.pre_flip(data)['status'] == 'PASS'
    data['serving'][0]['pins']['TR_ASYNC_SETTLE_AUTHORITY_EPOCH'] = '0'
    assert pilot.pre_flip(data)['status'] == 'BLOCKED'
    data['serving'][0]['pins']['TR_ASYNC_SETTLE_AUTHORITY_EPOCH'] = '1'
    data['days'] = data['days'][:7]
    assert pilot.pre_flip(data)['status'] == 'BLOCKED'
    data['days'].append('2026-10-13')
    Path(source['path']).write_text('tampered')
    assert pilot.pre_flip(data)['status'] == 'BLOCKED'


def test_post_flip_real_timings_rollback_and_missing_data(tmp_path):
    data = bundle(tmp_path)
    data['serving'][0]['pins'].update(TR_ASYNC_SETTLE_ENABLED='true', TR_ASYNC_SETTLE_PROTECTION='true',
        TR_ASYNC_SETTLE_PILOT_WORKSPACES=pilot.PILOT, TR_ASYNC_SETTLE_PILOT_CAP_MICRO='5000000')
    data['serving'][1]['pins'].update(TR_ASYNC_SETTLE_NEGOTIATE='on', TR_ASYNC_SETTLE_SHADOW='off')
    data.update(rollback=artifact(tmp_path, 'rollback.json', dict(status='PASS', states=[
        'admission_on', 'pending', 'admission_off', 'drained', 'protection_off'])),
        outbox=[dict(authorization_id='a', intent_kind='settle', workspace_id=pilot.PILOT,
            status='done', created_at=100, terminal_at=102, async_version=1)],
        handoff={'a.settle': dict(region='us-central1', handoff_us=3000)}, observed_peak_concurrency=4)
    result = pilot.post_flip(data)
    assert result['status'] == 'PASS'
    data['serving'][1]['pins']['TR_ASYNC_SETTLE_SHADOW'] = 'on'
    assert pilot.post_flip(data)['status'] == 'BLOCKED'
    data['serving'][1]['pins']['TR_ASYNC_SETTLE_SHADOW'] = 'off'
    assert result['regional_percentiles']['us-central1']['drain_us']['p99'] == 2000000
    data['outbox'][0]['terminal_at'] = None
    assert pilot.post_flip(data)['status'] == 'BLOCKED'


@pytest.mark.parametrize('command', ['shadow-serving', 'fleet-budgets', 'fleet-budgets-pre', 'pre-flip', 'post-flip'])
def test_cli_missing_evidence_fails_closed(command, tmp_path, monkeypatch, capsys):
    path = tmp_path / 'empty.json'
    path.write_text('{}')
    monkeypatch.setattr('sys.argv', ['pilot_enablement', command, '--bundle', str(path)])
    with pytest.raises(SystemExit) as exc:
        pilot.main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'BLOCKED'


def test_probe_unknown_cleanup_retains_authorization_identity():
    def respond(request):
        if request.url.path.endswith('authorize'):
            return httpx.Response(200, json={'data': {'authorization_id': 'recover-this'}})
        raise httpx.ReadTimeout('secret-bearing-message', request=request)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = pilot.header_probe('https://hop.test/v1', 'model', 'key', 'token', client)
    assert result['status'] == 'BLOCKED' and result['authorization_id'] == 'recover-this'
    assert 'secret-bearing-message' not in json.dumps(result)


def test_drain_and_key_clis(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'arrivals.jsonl'
    source.write_text(json.dumps(dict(created_at=1, service_seconds=.1)))
    trials = tmp_path / 'trials.json'
    trials.write_text(json.dumps([trial()]))
    monkeypatch.setattr('sys.argv', ['drain_settings', str(source), '--trials', str(trials),
        '--start', '0', '--end', '10', '--bucket-seconds', '1', '--margin', '.5',
        '--backlog', '2', '--target-seconds', '10', '--rpc-room-seconds', '.5'])
    with pytest.raises(SystemExit) as exc:
        drain.main()
    assert exc.value.code == 0
    selected = json.loads(capsys.readouterr().out)
    assert len(selected['exports']) == 2
    path = tmp_path / 'key.pem'
    monkeypatch.setattr('sys.argv', ['ticket_keys', '--private-key-file', str(path),
        '--kid', 'kid', '--issuer', 'issuer', '--epoch', '1'])
    ticket_keys.main()
    assert json.loads(capsys.readouterr().out)['enclave_keyring']['kid'].startswith('issuer~')


def test_header_probe_cli_env_and_status(monkeypatch, capsys):
    monkeypatch.setenv('TEST_PILOT_KEY', 'key-from-env')
    monkeypatch.setenv('TEST_GATEWAY_TOKEN', 'gateway-from-env')
    monkeypatch.setattr('sys.argv', ['pilot_enablement', 'header-probe', '--base-url', 'https://hop.test/v1',
        '--model', 'model', '--pilot-key-env', 'TEST_PILOT_KEY', '--gateway-token-env', 'TEST_GATEWAY_TOKEN'])
    def check(url, model, key, gateway, client):
        assert (key, gateway) == ('key-from-env', 'gateway-from-env')
        return pilot.checklist({'header': True})
    monkeypatch.setattr(pilot, 'header_probe', check)
    with pytest.raises(SystemExit) as exc:
        pilot.main()
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'PASS'


def test_live_database_requires_explicit_target_and_never_initializes(monkeypatch):
    from google.cloud import spanner
    calls = []
    def client(*, project):
        calls.append(project)
        return SimpleNamespace(instance=lambda instance: SimpleNamespace(database=lambda database: (instance, database)))
    monkeypatch.setattr(spanner, 'Client', client)
    assert pilot.live_database('p', 'i', 'd') == ('i', 'd')
    with pytest.raises(ValueError):
        pilot.live_database('', 'i', 'd')
    assert calls == ['p']


def test_clock_cannot_start_before_transport_and_fleet_gates(tmp_path):
    data = bundle(tmp_path)
    first = pilot.shadow_serving(data)['clock_start_us']
    transport = artifact(tmp_path, 'transport.json', dict(status='PASS', completed_at_us=first + 1, serving=data['serving']))
    data['transport'] = transport
    data['proof']['maximum_header_hops'] = transport['sha256']
    for row in data['rows']:
        if row['id'].endswith('/manifest-v1'):
            row['body']['proof_manifest_sha256'] = hashlib.sha256(canonical(data['proof'])).hexdigest()
    assert pilot.shadow_serving(data)['clock_start_us'] is None
    assert pilot.shadow_serving(data)['status'] == 'BLOCKED'


@pytest.mark.parametrize('malformed', ['authorize', 'refund'])
def test_probe_malformed_reply_retains_recovery_identity(malformed):
    def respond(request):
        if request.url.path.endswith(malformed):
            return httpx.Response(200, content=b'not-json')
        return httpx.Response(200, json={'data': {'authorization_id': 'recover-this'}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = pilot.header_probe('https://hop.test/v1', 'model', 'key', 'token', client)
    assert result['status'] == 'BLOCKED' and result['probe_id']
    assert result['cleanup'].startswith('UNKNOWN:')
    if malformed == 'refund':
        assert result['authorization_id'] == 'recover-this'


@pytest.mark.parametrize('damage', ['addition', 'addition_same_region', 'omission', 'region', 'revision', 'interval', 'gate_roster', 'gate_interval', 'observer'])
def test_shadow_serving_exact_roster_and_observer_coverage(tmp_path, damage):
    data = bundle(tmp_path)
    if damage in {'addition', 'addition_same_region'}:
        added = copy.deepcopy(data['serving'][0])
        added.update(instance='unobserved-router', region='europe-west1' if damage == 'addition' else added['region'])
        data['serving'].append(added)
        data['serving_instance_ids'].append(added['instance'])
    elif damage == 'omission':
        data['rows'] = data['rows'][2:]
    elif damage in {'region', 'revision'}:
        data['serving'][0][damage] = 'unreviewed'
    elif damage == 'interval':
        data['serving'][0]['intervals'][0]['started_at_us'] += 1
    elif damage in {'gate_roster', 'gate_interval'}:
        output = json.loads(Path(data['transport']['path']).read_text())
        if damage == 'gate_roster':
            output['serving'].pop()
        else:
            output['serving'][0]['intervals'][0]['started_at_us'] -= 1
        data['transport'] = artifact(tmp_path, 'transport.json', output)
    else:
        data['rows'][0]['body']['admission_observer']['health_reads'] = 0
    result = pilot.shadow_serving(data)
    assert result['status'] == 'BLOCKED' and result['clock_start_us'] is None


@pytest.mark.parametrize('damage', ['addition', 'addition_same_region', 'empty_keyring', 'negotiation'])
def test_pre_flip_rejects_unobserved_region_and_requires_installed_admission_off_keyring(tmp_path, damage):
    data = bundle(tmp_path)
    source = artifact(tmp_path, 'export.json', [])
    selected = select([trial()])
    selected['exports'] = {source['path']: source['sha256']}
    data.update(drain=artifact(tmp_path, 'drain.json', selected),
        ci={name: artifact(tmp_path, name, {'passed': True}) for name in ('F1', 'F2b', 'F2c')},
        signer=dict(epoch=1, audience='router-settlement', kid='kid', issuer='issuer', mounted=True,
                    public_key=FIXTURE['public_key'], verification=artifact(tmp_path, 'signer.json', {'passed': True})))
    data['serving'][0]['pins'].update(TR_ASYNC_SETTLE_TICKET_KID='kid', TR_ASYNC_SETTLE_TICKET_ISSUER='issuer',
        TR_ASYNC_SETTLE_TICKET_AUDIENCE='router-settlement', TR_ASYNC_SETTLE_AUTHORITY_EPOCH='1',
        TR_ASYNC_SETTLE_TICKET_PRIVATE_KEY_FILE='/mounted/key.pem')
    assert pilot.pre_flip(data)['status'] == 'PASS'
    if damage in {'addition', 'addition_same_region'}:
        added = copy.deepcopy(data['serving'][0])
        added.update(instance='new-instance-with-no-counters', region='new-region-with-no-budget' if damage == 'addition' else added['region'])
        data['serving'].append(added)
        data['serving_instance_ids'].append(added['instance'])
    elif damage == 'empty_keyring':
        data['serving'][1]['pins']['TR_ASYNC_SETTLE_TICKET_PUBLIC_KEYS'] = '{}'
    else:
        data['serving'][1]['pins']['TR_ASYNC_SETTLE_NEGOTIATE'] = 'on'
    assert pilot.pre_flip(data)['status'] == 'BLOCKED'


def pre_enable_bundle(tmp_path, serving=None):
    if serving is None:
        serving = [dict(instance='router', revision='a'*40, role='router', region='us-central1', pins={'TR_ASYNC_SETTLE_ENABLED': 'false'},
                        intervals=[dict(started_at_us=1791244800000000)]),
                   dict(instance='enclave', revision='b'*40, role='enclave', region='us-central1', pins={'TR_ASYNC_SETTLE_NEGOTIATE': 'off'},
                        intervals=[dict(started_at_us=1791244800000000)])]
    data = dict(serving=copy.deepcopy(serving), serving_instance_ids=[r['instance'] for r in serving])
    data['rows'] = []  # No observer exists on the dormant fleet.
    start = data['serving'][0]['intervals'][0]['started_at_us']
    end = start + 60_000000
    for row in data['serving']:
        row['intervals'] = [dict(day='2026-10-06', started_at_us=start, flushed_at_us=end)]
        if row['role'] == 'router':
            row['pins'].update(TR_ASYNC_SETTLE_SHADOW_WORKSPACES='', TR_ASYNC_SETTLE_PROTECTION='false')
        else:
            row['pins']['TR_ASYNC_SETTLE_SHADOW'] = 'off'
    data.update(started_at_us=start, flushed_at_us=end, workspace_count=1,
        deployment=artifact(tmp_path, 'deployment.json', dict(serving=data['serving'], maximum_router_instances_by_region={'us-central1': 1})),
        monitoring=artifact(tmp_path, 'monitoring.json', dict(started_at_us=start, flushed_at_us=end,
            approved_fleet_reads_per_second=2, regions={'us-central1': dict(cpu_peak_percent=20,
                approved_incremental_cpu_percent=5, approved_reads_per_second=2, read_headroom_per_second=4,
                approved_pending_rows_per_second=300, pending_row_headroom_per_second=700)})),
        read_health=artifact(tmp_path, 'read-health.json', dict(started_at_us=start, flushed_at_us=end, regions={'us-central1': [.01, .02]})))
    return data


def test_pre_enable_passes_without_observer_or_opt_in(tmp_path, monkeypatch, capsys):
    data = pre_enable_bundle(tmp_path)
    result = pilot.fleet_budgets_pre(data)
    assert result['status'] == 'PASS' and result['fleet_reads_per_second'] == 1.25
    path = tmp_path / 'pre.json'
    path.write_text(json.dumps(data))
    monkeypatch.setattr('sys.argv', ['pilot_enablement', 'fleet-budgets-pre', '--bundle', str(path)])
    monkeypatch.setattr(pilot, 'live_database', lambda *args: pytest.fail('pre-enable must not open a database'))
    with pytest.raises(SystemExit) as exc:
        pilot.main()
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)['mode'] == 'pre-enable'


@pytest.mark.parametrize('damage', ['opted_in', 'descriptor_pins', 'missing_region', 'instance', 'cpu', 'reads', 'rows', 'latency', 'interval', 'workspace_count', 'hash'])
def test_pre_enable_blocks_unbounded_or_unapproved_measurement(tmp_path, damage):
    data = pre_enable_bundle(tmp_path)
    if damage == 'opted_in':
        data['serving'][0]['pins']['TR_ASYNC_SETTLE_SHADOW_WORKSPACES'] = pilot.PILOT
    elif damage == 'descriptor_pins':
        value = json.loads(Path(data['deployment']['path']).read_text())
        value['serving'][0]['pins']['TR_ASYNC_SETTLE_ENABLED'] = 'true'
        data['deployment'] = artifact(tmp_path, 'deployment.json', value)
    elif damage == 'instance':
        data['serving'][0]['instance'] = 'unreviewed'
    elif damage == 'workspace_count':
        data['workspace_count'] = 33
    elif damage == 'hash':
        Path(data['monitoring']['path']).write_text('{}')
    else:
        key = 'read_health' if damage == 'latency' else 'monitoring'
        value = json.loads(Path(data[key]['path']).read_text())
        if damage == 'missing_region':
            value['regions']['extra-region'] = value['regions']['us-central1']
        elif damage == 'interval':
            value['flushed_at_us'] += 1
        elif damage == 'latency':
            value['regions']['us-central1'] = [.201]
        else:
            field = {'cpu': 'cpu_peak_percent', 'reads': 'read_headroom_per_second', 'rows': 'pending_row_headroom_per_second'}[damage]
            value['regions']['us-central1'][field] = 44 if damage == 'cpu' else 0
        data[key] = artifact(tmp_path, key+'.json', value)
    if damage == 'hash':
        with pytest.raises(KeyError):
            pilot.fleet_budgets_pre(data)
    else:
        assert pilot.fleet_budgets_pre(data)['status'] == 'BLOCKED'


@pytest.mark.parametrize('damage', ['missing', 'late', 'workspace', 'caps', 'status'])
def test_post_opt_in_requires_matching_earlier_pre_enable_gate(tmp_path, damage):
    data = fleet_bundle(tmp_path)
    if damage == 'missing':
        data['pre_enable']['sha256'] = '0'*64
    else:
        value = json.loads(Path(data['pre_enable']['path']).read_text())
        if damage == 'late':
            value['completed_at_us'] = data['rows'][0]['body']['started_at_us'] + 1
        elif damage == 'workspace':
            value['workspace_count'] = 2
        elif damage == 'caps':
            value['maximum_router_instances_by_region']['us-central1'] += 1
        else:
            value['status'] = 'BLOCKED'
        data['pre_enable'] = artifact(tmp_path, 'pre-enable.json', value)
    assert pilot.fleet_budgets(data)['status'] == 'BLOCKED'


def test_gate_measurement_may_be_bounded_within_reviewed_lifetime(tmp_path):
    data = bundle(tmp_path)
    measured = copy.deepcopy(data['serving'])
    for row in measured:
        row['intervals'] = [row['intervals'][0]]
        row['intervals'][0]['flushed_at_us'] -= 1
    assert pilot.gate_roster_coverage(measured, data['serving'])
    measured[0]['intervals'][0]['flushed_at_us'] += 2
    assert not pilot.gate_roster_coverage(measured, data['serving'])


@pytest.mark.parametrize('boundary', ['admission_disabled_from_us', 'admission_disabled_until_us'])
def test_serving_lifetimes_must_fit_control_flag_intervals(tmp_path, boundary):
    data = bundle(tmp_path)
    assert pilot.control_coverage(data)
    data['rows'][1]['body'][boundary] += 1 if boundary.endswith('from_us') else -1
    assert not pilot.control_coverage(data)
