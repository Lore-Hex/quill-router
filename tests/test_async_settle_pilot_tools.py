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
    serving = [dict(instance='router', revision='a'*40, region='us-central1', role='router', pins={
        'TR_ASYNC_SETTLE_ENABLED': 'false', 'TR_ASYNC_SETTLE_SHADOW_WORKSPACES': pilot.PILOT}),
        dict(instance='enclave', revision=rows[-1]['body']['deployment']['go_revision'], region='us-central1', role='enclave', pins={
            'TR_ASYNC_SETTLE_NEGOTIATE': 'off', 'TR_ASYNC_SETTLE_SHADOW': 'on'})]
    transport = artifact(tmp_path, 'transport.json', dict(status='PASS', completed_at_us=1))
    fleet = artifact(tmp_path, 'fleet.json', dict(status='PASS', completed_at_us=1))
    proof.update(maximum_header_hops=transport['sha256'], fleet_load_budget=fleet['sha256'],
                 publisher_poll_freshness=fleet['sha256'])
    for row in rows:
        if row['id'].endswith('/manifest-v1'):
            row['body']['proof_manifest_sha256'] = hashlib.sha256(canonical(proof)).hexdigest()
    return dict(rows=rows, days=days, proof=proof, serving=serving,
                serving_instance_ids=['router', 'enclave'], transport=transport, fleet=fleet)


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


def fleet_bundle(tmp_path):
    data = bundle(tmp_path)
    row = copy.deepcopy(data['rows'][0])
    obs = row['body']['admission_observer']
    obs.update(workspace_reads=21600, health_reads=86400, prediction_yes=1)
    data.update(rows=[row], workspace_count=1, router_instance_ids=[row['body']['instance']],
        freshness_maxima_seconds=dict(publisher_period=2, publisher_jitter=.25, publication=.5,
            poll_period=1, poll_jitter=.25, install=.2, skew=.25,
            workspace_period=4, workspace_jitter=.25, workspace_install=.2),
        approved_fleet_reads_per_second=2, maximum_router_instances=1, maximum_router_instances_by_region={'us-central1': 1},
        regional_budgets={'us-central1': dict(reads_per_second=2, pending_rows_per_second=300)},
        headroom=artifact(tmp_path, 'headroom.json', {'measured_cpu': 20}))
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


@pytest.mark.parametrize('command', ['shadow-serving', 'fleet-budgets', 'pre-flip', 'post-flip'])
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
    transport = artifact(tmp_path, 'transport.json', dict(status='PASS', completed_at_us=first + 1))
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
