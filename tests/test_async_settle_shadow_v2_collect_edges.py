"""Capture CLI, source loss, role checks and explicit continuation boundaries."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from scripts.async_settle.shadow_v2_collect import collect
from tests.test_async_settle_shadow_v2_collect import (
    DAY,
    END,
    REV,
    START,
    Fake,
    get_readiness,
    get_shutdown,
    graph,
    inventory,
    readiness_sources,
    setup,
    shutdown_sources,
)
from trusted_router import async_settle_shadow_v2_collect_inventory as inv
from trusted_router.async_settle_shadow_v2_collect_ports import Files, Page, Pass, Query
from trusted_router.async_settle_shadow_v2_gate_inputs import Inventory
from trusted_router.async_settle_shadow_v2_gate_views import budget_view
from trusted_router.detached_jws import canonical


def test_capture_cli_no_network_and_no_overwrite(tmp_path):
    files, binding, fake, queries = setup(tmp_path / 'captured', ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, readiness = readiness_sources(files, value)
    shutdown = shutdown_sources(value)
    sources = fake.records | readiness.records | shutdown.records
    queries.update(artifacts=Query('artifacts', 'ci-run-1', DAY, 100),
                   readiness=Query('readiness', 'named-service', DAY),
                   shutdown=Query('shutdown', 'named-service', DAY))
    calls = []
    for name, records in sources.items():
        original = files.obj('input_calls/' + name + '.json', dict(records=records))
        q = queries[name]
        calls.append(dict(source=q.source, resource=q.resource, day=q.day, token=None, next_token=None,
            eof=True, elapsed_us=1, page_size=q.page_size, deadline_seconds=10,
            complete_through_us=END, truncated=False, error=None, original=original))
    inputs = []
    for path in sorted(files.root.rglob('*.json')):
        relative = str(path.relative_to(files.root))
        if relative.startswith(('sources/', 'inventory/')):
            continue
        inputs.append(files.put(relative, path.read_bytes()))
    plan = dict(files=inputs, calls=calls, queries={k: vars(v) for k, v in queries.items()},
                binding=binding, pins=pins)
    capture = files.root / 'capture.json'
    capture.write_bytes(canonical(plan))
    result = collect(capture, tmp_path / 'out')
    assert not result['complete']  # Human approvals and original close rows are absent.
    assert len(result['readiness']['ledgers']) == 1
    assert len(result['shutdown']['entries']) == 1
    assert (tmp_path / 'out/collection-status.json').is_file()
    with pytest.raises(ValueError):
        collect(capture, tmp_path / 'out')


def test_full_page_then_terminal_empty_page(tmp_path):
    fake = Fake(dict(x=[dict(i=i) for i in range(200)]))
    fake.overrides['x', None] = Page(canonical(dict(records=fake.records['x'])), 'next', False, 1, END)
    fake.overrides['x', 'next'] = Page(canonical(dict(records=[])), None, True, 1, END)
    run = Pass(Files(tmp_path))
    assert len(list(run.scan(fake, Query('x', 'named', DAY)))) == 200
    assert len(fake.calls) == 2 and not run.missing


def test_empty_page_with_continuation_is_not_eof(tmp_path):
    fake = Fake(dict(x=[]))
    fake.overrides['x', None] = Page(canonical(dict(records=[])), 'next', False, 1, END)
    fake.overrides['x', 'next'] = Page(canonical(dict(records=[dict(i=1)])), None, True, 1, END)
    run = Pass(Files(tmp_path))
    assert list(run.scan(fake, Query('x', 'named', DAY))) == [dict(i=1)]
    assert not run.missing


def test_pass_budget_shared_across_systems(tmp_path):
    fake = Fake(dict(x=[dict(i=1)], y=[dict(i=2)]))
    run = Pass(Files(tmp_path), limit=1)
    assert len(list(run.scan(fake, Query('x', 'named', DAY)))) == 1
    assert not list(run.scan(fake, Query('y', 'ci-run', DAY, 100)))
    assert len(fake.calls) == 1 and run.missing[0]['reason'] == 'records_per_pass'


def test_bound_missing_capture_bytes(tmp_path):
    files, binding, fake, queries = setup(tmp_path)
    (files.root / binding['path']).unlink()
    run, value = inventory(files, binding, fake, queries)
    assert not value.complete and not value.boots
    assert any(r['reason'] == 'binding_missing' for r in run.missing)


@pytest.mark.parametrize('source', ['policy', 'fixture', 'config', 'native_trial', 'health_basis', 'public_descriptors'])
def test_delete_pinned_original_input(tmp_path, source):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    if source in pins:
        ref = pins[source]
    elif source == 'native_trial':
        ref = fake.records['artifacts'][0]['original']
    else:
        ref = next(r[source] for r in fake.records['readiness'] if source in r)
    (files.root / ref['path']).unlink()
    run, result = get_readiness(files, value, pins, fake)
    assert not result.complete and not result.ledgers and run.missing


@pytest.mark.parametrize('damage', ['wrong_boot', 'late_trial', 'wrong_region', 'failed_ci', 'early_adoption', 'stale_basis'])
def test_readiness_cannot_borrow_or_backdate(tmp_path, damage):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    if damage == 'wrong_boot':
        fake.records['readiness'][0]['boot'] = 'predecessor'
    elif damage == 'late_trial':
        fake.records['artifacts'][0]['completed_at_us'] = END
    elif damage == 'wrong_region':
        fake.records['artifacts'][0]['region'] = 'europe-west4'
    elif damage == 'failed_ci':
        fake.records['artifacts'][0]['conclusion'] = 'fail'
    elif damage == 'early_adoption':
        fake.records['readiness'][0]['completed_at_us'] = START
    else:
        record = next(r for r in fake.records['readiness'] if 'health_basis' in r)
        basis = json.loads(files.read(record['health_basis']))
        basis['read_elapsed_ns'] = 250000001
        record['health_basis'] = files.obj('native/stale.json', basis)
    run, result = get_readiness(files, value, pins, fake)
    assert not result.complete and run.missing


def test_same_instance_restart_is_two_independent_launches(tmp_path):
    files, binding, fake, queries = setup(tmp_path, ('first', 'second'))
    # Make the second start follow the first exit, keeping the same VM identity.
    for row in fake.records['lifecycle'][2:]:
        row['jsonPayload']['at_us'] += 20000
    for key in ('process_start_us', 'recorded_at_us'):
        fake.records['startup'][1][key] += 20000
    _, value = inventory(files, binding, fake, queries)
    assert value.complete and len(value.boots) == 2
    assert value.boots[0]['instance'] == value.boots[1]['instance']
    assert value.boots[0]['launch'] != value.boots[1]['launch']
    assert value.boots[0]['end_us'] < value.boots[1]['start_us']


def test_sealed_alive_day_boundary_without_invented_exit(tmp_path):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    record = fake.records['lifecycle'][-1]['jsonPayload']
    record.update(kind='a3_process_boundary', at_us=END)
    _, value = inventory(files, binding, fake, queries)
    assert value.complete and value.boots[0]['reason'] == 'utc_boundary'
    assert value.boots[0]['end_us'] == END


def test_platform_router_parser_and_unknown_crash(tmp_path):
    binding = dict(mode='cloud_run_instance', log_name='projects/p/logs/run', revisions={'revision-name': REV})
    entry = dict(resource=dict(type='cloud_run_revision', labels=dict(service_name='trusted-router',
        revision_name='revision-name', location='us-central1')), logName=binding['log_name'],
        labels=dict(instanceId='platform-id'), insertId='start-id', timestamp=DAY + 'T00:00:01.000001Z',
        textPayload='Starting new instance. Reason: DEPLOYMENT_ROLLOUT - test')
    value = inv.lifecycle(entry, binding)
    assert value['at_us'] == START + 1000001 and value['launch'] == 'platform-id'
    entry['textPayload'] = 'undocumented crash kind'
    with pytest.raises(ValueError):
        inv.lifecycle(entry, binding)


def test_compute_parser_needs_completed_operation_and_membership():
    binding = dict(mode='compute_instance', log_name='projects/p/logs/audit', migs=['named-mig'],
        instances={'123': dict(resource_name='projects/p/zones/z/instances/vm', mig='named-mig',
                               revision=REV, region='us-central1')})
    entry = dict(logName=binding['log_name'], resource=dict(labels=dict(instance_id='123')),
        protoPayload=dict(serviceName='compute.googleapis.com', methodName='v1.compute.instances.insert',
                          resourceName='projects/p/zones/z/instances/vm'),
        operation=dict(id='operation-1', last=False), insertId='insert-id', timestamp=DAY + 'T00:00:00Z')
    assert inv.lifecycle(entry, binding)['operation_state'] == 'pending'
    entry['operation']['last'] = True
    assert inv.lifecycle(entry, binding)['event'] == 'start'
    entry['protoPayload']['resourceName'] = 'unapproved-vm'
    with pytest.raises(ValueError):
        inv.lifecycle(entry, binding)


def test_shutdown_budget_view_seam_is_closed_without_c2_inputs(tmp_path):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    _, result = get_shutdown(files, value, shutdown_sources(value))
    assert len(result.entries) == 1
    g = graph(files)
    # The real gate_views entry point requires approved Budget/retained inputs.
    # No fake budget or fixture generator substitutes for the separate C2 package.
    with pytest.raises(ValueError, match='artifact_gap'):
        budget_view(g, {}, Inventory(()), ())


@pytest.mark.parametrize('case', ['valid', 'unrelated_path', 'old_path', 'changed_epoch', 'changed_image'])
def test_enclave_readiness_uses_own_echo_and_router_path(tmp_path, case):
    files, binding, fake, queries = setup(tmp_path, ('enclave-boot',))
    for event in fake.records['lifecycle']:
        event['jsonPayload']['role'] = 'enclave'
    fake.records['startup'][0]['role'] = 'enclave'
    config = fake.records['platform'][0]
    config.update(role='enclave', pins=dict(TR_ASYNC_SETTLE_NEGOTIATE='off', TR_ASYNC_SETTLE_SHADOW='on'))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    warm = next(r for r in fake.records['readiness'] if r.get('criterion') == 'observer_freshness')
    for key in ('health_basis', 'install_elapsed_us', 'installed_at_ns', 'contender'):
        del warm[key]
    warm.update(router_path=dict(ready=True, resource='configured-router', router_boot='router-boot',
                                 region='us-central1', observed_at_us=value.boots[0]['boot_at_us'] + 1), configured_router_region='us-central1',
                local_transport_ready=True, echo_ready=True)
    marker = next(r for r in fake.records['readiness'] if r.get('criterion') == 'marker_keys')
    marker.update(attested_image=files.obj('native/attested.json', dict(revision=REV, image_sha256='b' * 64)),
                  attested_revision=REV)
    if case == 'unrelated_path':
        warm['router_path']['resource'] = 'unconfigured-router'
    elif case == 'old_path':
        warm['router_path']['observed_at_us'] = START
    elif case == 'changed_epoch':
        marker['authority_epoch'] = 2
    elif case == 'changed_image':
        marker['attested_image'] = files.obj('native/changed-image.json', dict(revision=REV, image_sha256='c' * 64))
    _, result = get_readiness(files, value, pins, fake)
    assert result.complete is (case == 'valid')
    assert len(result.ledgers) == (1 if case == 'valid' else 0)


def test_shutdown_disjoint_outer_nested_transactions_and_grace():
    record = dict(spans=dict(preparation=[0, 100], export_wait=[100, 200], final_pass=[200, 500],
                             close=[250, 350], result=[350, 450]))
    from trusted_router.async_settle_shadow_v2_collect_shutdown import durations
    measured = durations(record)
    assert measured['preparation_us'] + measured['export_wait_us'] + measured['final_pass_us'] == 500
    record['spans']['result'] = [300, 450]
    with pytest.raises(ValueError):
        durations(record)


def test_late_source_delivered_rows_remain_missing(tmp_path):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    page = fake.read(queries['lifecycle'], None)
    fake.overrides['lifecycle', None] = replace(page, complete_through_us=END - 1)
    _, value = inventory(files, binding, fake, queries)
    assert len(value.boots) == 1 and not value.complete


@pytest.mark.parametrize('source', ['platform', 'monitoring'])
def test_delete_platform_original_page(tmp_path, source):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    ref = fake.records[source][0]['original']
    (files.root / ref['path']).unlink()
    run, value = inventory(files, binding, fake, queries)
    assert not value.complete and run.missing
    if source == 'platform':
        assert not value.boots and len(value.expected) == 1


def test_explicit_deadline_call_is_recorded_without_retry(tmp_path):
    fake = Fake(dict(x=[]))
    fake.overrides['x', None] = TimeoutError()
    run = Pass(Files(tmp_path))
    assert not list(run.scan(fake, Query('x', 'named', DAY)))
    assert run.calls == 1 and len(fake.calls) == 1
    failures = list(tmp_path.rglob('*-failure.json'))
    assert len(failures) == 1
    failure = json.loads(failures[0].read_bytes())
    assert failure['eof'] is False and failure['elapsed_us'] is None
    assert failure['request']['deadline_seconds'] == 10


def close_sources(files, boot):
    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router.async_settle_shadow_v2_schema import sha256
    # Explicit fake native originals, not a generated gate/report fixture.
    fake_counter = files.obj('native/fake-counter-source.json', dict(fake=True, boot=boot['boot']))
    closure = dict(v=2, policy_version='shadow-v2', contract_revision='a1', day=DAY, boot=boot['boot'],
        router_revision=REV, region=boot['region'], reason='shutdown', interval_end_us=boot['end_us'],
        final_event_seq=0, final_checkpoint_seq=1, durable_event_hwm=0, receipt_chain_sha256=sha256([]),
        counter_sha256=fake_counter['sha256'], retained_work=0, pending_comparisons=0, unresolved_commits=0,
        loss_count=0, observer_fold_through_us=boot['end_us'], previous_day_closure_sha256=None,
        authorization_obligations='receipt_chain')
    result = dict(v=2, policy_version='shadow-v2', contract_revision='a1', day=DAY, boot=boot['boot'],
        router_revision=REV, closure_sha256=sha256(closure), final_checkpoint_seq=1,
        attempt_started_at_us=boot['end_us'], observed_at_us=boot['end_us'] + 300, elapsed_us=300,
        physical_commit_ack_at_us=boot['end_us'] + 300, flush_remaining_us=1000,
        outcome='acknowledged', shutdown_remaining_us=1000, handoff_attempt_limit=1)
    return {DAY + '/' + boot['boot']: dict(
        original_close=files.obj('native/closure.json', closure, a1.CLOSURE),
        close_result=files.obj('native/close-result.json', result, a1.CLOSE_RESULT))}


@pytest.mark.parametrize('deleted', [None, 'original_close', 'close_result'])
def test_native_close_original_sources_and_deletion(tmp_path, deleted):
    from tests.test_async_settle_shadow_v2_collect import review
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    from trusted_router import async_settle_shadow_v2_collect_shutdown as shut
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    originals = close_sources(files, value.boots[0])
    if deleted is not None:
        (files.root / originals[DAY + '/zero-row'][deleted]['path']).unlink()
    def execute(approved=None):
        run = Pass(files)
        result = shut.collect(run, inventory=value, logs=shutdown_sources(value),
            query=Query('shutdown', 'named-service', DAY), originals=originals, review=approved)
        return run, result
    run, result = execute()
    if deleted is not None:
        assert not result.complete and run.missing
    else:
        assert result.complete
        _, sealed = execute(review(files, result.measurements))
        g = graph(files)
        g.materialize_one('proof.shutdown_inventory_sha256',
                         a2.SHUTDOWN_INVENTORY.parse(files.read(sealed.envelope)))
        assert len(g.shutdown.measurements) == 1


@pytest.mark.parametrize('channel', ['lifecycle', 'startup', 'readiness', 'shutdown'])
def test_unclassified_original_records_block_every_channel(tmp_path, channel):
    from tests.test_async_settle_shadow_v2_collect import review
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    unknown = dict(kind='a3_new_exit', boot='zero-row')
    if channel in ('lifecycle', 'startup'):
        fake.records[channel].append(dict(insertId='unknown', jsonPayload=unknown) if channel == 'lifecycle' else unknown)
        run, result = inventory(files, binding, fake, queries)
        _, sealed = inventory(files, binding, fake, queries, review(files, result.deployment))
        envelope = a2.INDEPENDENT_INVENTORY.parse(files.read(sealed.envelope))
        assert not envelope['complete']
        with pytest.raises(ValueError):
            g = graph(files)
            g.inventory({'manifest.independent_inventory_sha256': envelope}, g.ports)
    else:
        _, value = inventory(files, binding, fake, queries)
        if channel == 'readiness':
            pins, fake = readiness_sources(files, value)
            fake.records[channel].append(unknown)
            run, result = get_readiness(files, value, pins, fake)
            assert not result.ledgers
        else:
            fake = shutdown_sources(value)
            fake.records[channel].append(unknown)
            run, draft = get_shutdown(files, value, fake)
            run, result = get_shutdown(files, value, fake, review(files, draft.measurements))
            envelope = a2.SHUTDOWN_INVENTORY.parse(files.read(result.envelope))
            assert not envelope['complete']
            with pytest.raises(ValueError):
                graph(files).materialize_one('proof.shutdown_inventory_sha256', envelope)
    assert not result.complete and run.missing
    originals = [files.read(ref) for ref in run.sources]
    assert any(b'a3_new_exit' in raw for raw in originals)


@pytest.mark.parametrize('state', ['pending', 'failed', 'completed', 'pending_new_instance'])
def test_compute_operations_retain_resource_and_resolve_only_completion(tmp_path, state):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    body = json.loads(files.read(binding))
    body.update(mode='compute_instance', log_name='projects/p/logs/audit', migs=['mig'],
                instances={'vm1': dict(resource_name='projects/p/zones/z/instances/vm', mig='mig',
                                       revision=REV, region='us-central1')})
    binding = files.obj('compute-binding.json', body)
    fake.records['platform'][0]['role'] = 'enclave'
    fake.records['startup'][0].update(role='enclave', launch='vm1', process_start_us=START + 1000001,
                                      recorded_at_us=START + 1000002)
    def event(op, method, last, second, failed=False):
        return dict(logName=body['log_name'], resource=dict(type='gce_instance', labels=dict(instance_id='vm1')),
            protoPayload=dict(serviceName='compute.googleapis.com', methodName='v1.compute.instances.' + method,
                resourceName='projects/p/zones/z/instances/vm', status=dict(code=1 if failed else 0)),
            operation=dict(id=op, first=not last, last=last), insertId=op + str(last),
            timestamp=DAY + f'T00:00:0{second}Z')
    fake.records['lifecycle'] = [event('start', 'insert', False, 0), event('start', 'insert', True, 1),
                                 event('stop', 'delete', False, 1), event('stop', 'delete', True, 2)]
    if state != 'completed':
        fake.records['lifecycle'].append(event('pending', 'insert', state == 'failed', 3, state == 'failed'))
    if state == 'pending_new_instance':
        body['instances']['vm2'] = dict(body['instances']['vm1'], resource_name='projects/p/zones/z/instances/vm2')
        binding = files.obj('compute-binding-new-instance.json', body)
        fake.records['lifecycle'][-1]['resource']['labels']['instance_id'] = 'vm2'
        fake.records['lifecycle'][-1]['protoPayload']['resourceName'] = 'projects/p/zones/z/instances/vm2'
    run, value = inventory(files, binding, fake, queries)
    assert value.complete is (state == 'completed')
    retained = json.loads(files.read(value.deployment))['operations']
    assert len(retained['start']) == 2
    if state == 'pending_new_instance':
        assert retained['pending'][0]['instance'] == 'vm2'
        assert len(value.expected) == 1 and len(value.boots) == 1
        assert run.missing
    elif state != 'completed':
        assert retained['pending'][0]['resource_name'] == 'projects/p/zones/z/instances/vm'
        assert retained['pending'][0]['operation_state'] == state
        assert run.missing and not value.boots
    else:
        assert len(value.boots) == 1 and len(value.expected) == 1


@pytest.mark.parametrize('case', ['unknown', 'malformed', 'outside', 'early', 'valid'])
def test_attempt_population_and_packaged_missing_state(tmp_path, case):
    from tests.test_async_settle_shadow_v2_collect import attempt_record, receipt_page
    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    from trusted_router import async_settle_shadow_v2_evidence as p1
    from trusted_router.async_settle_shadow_v2_gate_provenance import prerequisite
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    attempt = attempt_record(files, value.boots[0], START + (12 if case == 'early' else 100))
    if case == 'unknown':
        attempt['boot'] = attempt['receipt']['boot'] = 'not-in-inventory'
        attempt['receipt']['original'] = files.obj('native/unmatched-attempt.json',
            receipt_page('not-in-inventory', attempt['at_us']), p1.PAGE)
    elif case == 'malformed':
        attempt['at_us'] = 'unknown'
    elif case == 'outside':
        attempt['at_us'] = END
    fake.records['readiness'].append(attempt)
    run, result = get_readiness(files, value, pins, fake)
    assert result.attempts == (attempt,)
    assert result.complete is (case == 'valid')
    status = json.loads((files.root / 'readiness/status.json').read_bytes())
    assert json.loads(files.read(status['attempts'])) == [attempt]
    assert status['complete'] is (case == 'valid')
    if case == 'early':
        assert any(r['reason'] == 'early_opted_attempt' for r in status['missing'])
        ledger = a1.PREREQUISITE_LEDGER.parse(files.read(result.ledgers[0]))
        proofs = {r['sha256']: a2.PROOF_RESULT.parse(files.read(r)) for r in result.proofs}
        for criterion in ready_checks():
            prerequisite(graph(files), proofs[ledger[criterion]['sha256']], ledger, criterion)
    if case not in ('early', 'valid'):
        assert not result.ledgers and run.missing


def ready_checks():
    from trusted_router.async_settle_shadow_v2_collect_readiness import CHECKS
    return CHECKS


@pytest.mark.parametrize('case', ['old_read', 'old_install', 'old_dependency', 'clock_boot', 'clock_original',
                                  'large_check', 'extra_input', 'extra_field', 'attempt_result',
                                  'wrong_dependency', 'duplicate_dependency', 'missing_dependency', 'wrong_role'])
def test_original_readiness_records_and_boot_local_observations(tmp_path, case):
    from tests.test_async_settle_shadow_v2_collect import attempt_record
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    warm = next(r for r in fake.records['readiness'] if r.get('criterion') == 'observer_freshness')
    transport = next(r for r in fake.records['readiness'] if r.get('criterion') == 'boot_transport')
    first = fake.records['readiness'][0]
    if case == 'large_check':
        first['inputs']['extra'] = 'x' * 20000
        assert len(canonical(first)) == 20499
    if case == 'old_read':
        basis = json.loads(files.read(warm['health_basis']))
        for key in ('committed_at_ns', 'read_at_ns'):
            basis[key] -= 86400 * 1000000000
        for key in ('observed_at_ns', 'publisher_heartbeat_ns'):
            basis['body'][key] -= 86400 * 1000000000
        basis['body']['slot'] -= 43200
        basis['body']['backlog_age_at_seconds'] -= 86400
        warm['health_basis'] = files.obj('native/old-basis.json', basis)
        warm['installed_at_ns'] -= 86400 * 1000000000
    elif case == 'old_install':
        warm['installed_at_ns'] = START * 1000
    elif case == 'old_dependency':
        transport['dependencies'][0]['observed_at_us'] = START
    elif case == 'clock_boot':
        warm['clock_basis']['boot_at_us'] = START
    elif case == 'clock_original':
        warm['clock_basis']['original'] = files.obj('native/changed-clock.json', dict(clock='unqualified'))
    elif case in ('large_check', 'extra_input'):
        first['inputs']['extra'] = 'x' * (20000 if case == 'large_check' else 1)
    elif case == 'extra_field':
        first['unlisted'] = 'unlisted-value'
    elif case == 'attempt_result':
        attempt = attempt_record(files, value.boots[0], START + 100)
        attempt['result'] = 'unlisted-result'
        fake.records['readiness'].append(attempt)
    elif case == 'wrong_dependency':
        for row in transport['dependencies']:
            row['resource'] = 'unconfigured-target'
    elif case == 'duplicate_dependency':
        transport['dependencies'].append(dict(transport['dependencies'][0]))
    elif case == 'missing_dependency':
        transport['dependencies'].pop()
    else:
        transport['dependencies'][0]['resource'] = 'enclave-dependency'
    run, result = get_readiness(files, value, pins, fake)
    assert not result.complete and not result.ledgers and run.missing
    assert any(b'records' in files.read(ref) for ref in run.sources)


@pytest.mark.parametrize('source', ['ci', 'regional_trial', 'sizing_trial', 'budget_trial'])
@pytest.mark.parametrize('case', ['reversed', 'typed', 'over_hour'])
def test_each_original_artifact_time_is_validated(tmp_path, source, case):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    artifact = next(r for r in fake.records['artifacts'] if r['source'] == source)
    artifact['started_at_us'] = END if case == 'reversed' else str(START) if case == 'typed' else START - 3600000001
    run, result = get_readiness(files, value, pins, fake)
    assert result.complete is (source == 'ci' and case == 'over_hour')
    if not result.complete:
        assert run.missing and not result.ledgers


def test_expected_launch_map_exists_before_first_startup_page(tmp_path):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    original_read = fake.read
    def read(query, token):
        if query.source == 'startup':
            expected = json.loads((files.root / f'inventory/{DAY}/expected-launches.json').read_bytes())
            assert set(expected) == {'launch-zero-row'}
            assert [e['event'] for e in expected['launch-zero-row']] == ['start', 'stop']
        return original_read(query, token)
    fake.read = read
    _, value = inventory(files, binding, fake, queries)
    assert value.complete


@pytest.mark.parametrize('case', ['orphan', 'two_starts'])
def test_original_launch_population_cannot_be_enlarged_or_merged(tmp_path, case):
    from tests.test_async_settle_shadow_v2_collect import review
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    if case == 'orphan':
        fake.records['startup'].append(dict(fake.records['startup'][0], boot='other', launch='other-launch'))
    else:
        fake.records['lifecycle'].append(dict(fake.records['lifecycle'][0], insertId='second-start'))
    run, value = inventory(files, binding, fake, queries)
    assert len(value.expected) == 1 and not value.complete
    assert len(value.boots) == (1 if case == 'orphan' else 0)
    assert len(json.loads(files.read(value.serving))) == len(value.boots)
    if case == 'two_starts':
        assert value.expected[0]['missing'] == ['lifecycle_boundary']
    _, sealed = inventory(files, binding, fake, queries, review(files, value.deployment))
    assert not a2.INDEPENDENT_INVENTORY.parse(files.read(sealed.envelope))['complete']
    assert run.missing


def test_remaining_capacity_and_continuation_exhaustion(tmp_path):
    for count, limit, sizes in ((201, 201, [200, 1]), (202, 201, [200, 1]), (202, 200, [200])):
        fake = Fake(dict(x=[dict(i=i) for i in range(count)]))
        run = Pass(Files(tmp_path / f'{count}-{limit}'), limit=limit)
        assert len(list(run.scan(fake, Query('x', 'named', DAY)))) == limit
        assert [q.page_size for q, _ in fake.calls] == sizes
        assert bool(run.missing) is (count > limit)
        if count > limit:
            assert any(r['reason'] == 'records_per_pass' for r in run.missing)


def test_ready_equal_to_last_completion_blocks(tmp_path):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    fake.records['readiness'][-1]['at_us'] = max(r['completed_at_us'] for r in fake.records['readiness'][:-1])
    _, result = get_readiness(files, value, pins, fake)
    assert not result.complete and not result.ledgers


@pytest.mark.parametrize('criterion', ['transport', 'size', 'regional_load', 'boot_transport', 'observer_freshness', 'marker_keys'])
@pytest.mark.parametrize('changed', [False, True])
def test_duplicate_final_adoptions_block_by_criterion(tmp_path, criterion, changed):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    record = dict(next(r for r in fake.records['readiness'] if r.get('criterion') == criterion))
    if changed:
        record['completed_at_us'] -= 1
    fake.records['readiness'].append(record)
    _, result = get_readiness(files, value, pins, fake)
    assert not result.complete and not result.ledgers


@pytest.mark.parametrize('case', ['valid', 'close_binding', 'result_outcome'])
def test_changed_original_close_and_result_independently_block(tmp_path, case):
    from trusted_router import async_settle_shadow_v2_collect_shutdown as shut
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    originals = close_sources(files, value.boots[0])
    refs = originals[DAY + '/zero-row']
    body = json.loads(files.read(refs['close_result']))
    if case == 'close_binding':
        body['closure_sha256'] = 'f' * 64
    elif case == 'result_outcome':
        body['outcome'] = 'deadline'
    if case != 'valid':
        refs['close_result'] = files.obj('native/changed-result.json', body)
    run = Pass(files)
    result = shut.collect(run, inventory=value, logs=shutdown_sources(value), query=Query('shutdown', 'named', DAY), originals=originals)
    assert result.complete is (case == 'valid')
    if case != 'valid':
        assert run.missing


def test_retained_shared_trial_times_and_original_bytes(tmp_path):
    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    from trusted_router.async_settle_shadow_v2_gate_provenance import prerequisite
    files, binding, fake, queries = setup(tmp_path, ('first', 'second'))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    for record in fake.records['readiness']:
        if record.get('kind') == 'a3_boot_check' and record['boot'] == 'second':
            record['inputs'] = {k: v.replace('second-', 'first-') for k, v in record['inputs'].items()}
    _, result = get_readiness(files, value, pins, fake)
    assert result.complete and len(result.ledgers) == 2
    proofs = {r['sha256']: a2.PROOF_RESULT.parse(files.read(r)) for r in result.proofs}
    for ref in result.ledgers:
        ledger = a1.PREREQUISITE_LEDGER.parse(files.read(ref))
        for criterion in ready_checks():
            prerequisite(graph(files), proofs[ledger[criterion]['sha256']], ledger, criterion)
    for boot in ('first', 'second'):
        projections = list((files.root / 'readiness' / boot).glob('*-ci.json'))
        projections += list((files.root / 'readiness' / boot).glob('*_trial.json'))
        assert len(projections) == 7
        for path in projections:
            row = json.loads(path.read_bytes())
            assert row['completed_at_us'] == START and row['started_at_us'] == START - 1000
            original = json.loads(files.read(row['original']))
            assert original['actual_trial_time'] == START
            assert original['completed_at_us'] == START and original['started_at_us'] == START - 1000
            assert row['id'].startswith('first-')


@pytest.mark.parametrize('case', ['valid', 'close', 'result', 'final', 'total', 'grace'])
def test_shutdown_independent_limits_and_valid_eight_point_five_seconds(tmp_path, case):
    from trusted_router import async_settle_shadow_v2_collect_shutdown as shut
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    originals = close_sources(files, value.boots[0])
    fake = shutdown_sources(value)
    spans = dict(preparation=[0, 3000000], export_wait=[3000000, 6000000], final_pass=[6000000, 8500000],
                 close=[6000000, 6000300], result=[6100000, 7100000])
    if case == 'close':
        spans.update(preparation=[0, 1], export_wait=[1, 2], final_pass=[2, 3000000], close=[2, 2000003], result=[2000003, 2000004])
    elif case == 'result':
        spans['result'] = [6000300, 8000301]
    elif case == 'final':
        spans.update(preparation=[0, 1], export_wait=[1, 2], final_pass=[2, 4000003], close=[2, 302], result=[302, 303])
    elif case == 'total':
        spans['preparation'][1] += 500001
        for key in ('export_wait', 'final_pass', 'close', 'result'):
            spans[key] = [t + 500001 for t in spans[key]]
        spans['export_wait'][0] = spans['preparation'][1]
    elif case == 'grace':
        value.boots[0]['grace_us'] = 8000000
    fake.records['shutdown'][0]['spans'] = spans
    run = Pass(files)
    result = shut.collect(run, inventory=value, logs=fake, query=Query('shutdown', 'named', DAY), originals=originals)
    assert result.complete is (case == 'valid')
    if case != 'valid':
        assert any(r['reason'] == 'combined_grace_or_pass_exceeded' for r in run.missing)


def test_runtime_interface_matches_closed_collector_schemas():
    from pathlib import Path

    from trusted_router import async_settle_shadow_v2_collect_ports as ports
    doc = Path('docs/design/async-settle-shadow-v2-runtime-harness-interface.md').read_text()
    inventory = json.loads(doc.split('```json\n', 1)[1].split('\n```', 1)[0])
    expected = dict(fields=ports.RECORD_FIELDS, role_fields=ports.ROLE_FIELDS, limits=ports.RECORD_LIMITS,
                    checks=ports.CHECKS, sources=ports.SOURCES, attempt_results=ports.ATTEMPT_RESULTS,
                    channels=ports.CHANNEL_KINDS,
                    emitted_coordinate_fields=ports.EMITTED_COORDINATE_FIELDS,
                    emitted_coordinate_types=ports.EMITTED_COORDINATE_TYPES,
                    emitted_coordinates=ports.EMITTED_COORDINATES,
                    launch_descriptor_fields=ports.LAUNCH_DESCRIPTOR_FIELDS,
                    launch_coordinate_fields=ports.LAUNCH_COORDINATE_FIELDS)
    assert inventory == json.loads(json.dumps(expected))


@pytest.mark.parametrize('field', ['clock_basis.original', 'health_basis', 'claim', 'publication',
                                 'public_descriptors', 'attested_image', 'receipt.original',
                                 'originals.original_close', 'originals.close_result'])
@pytest.mark.parametrize('case', ['valid', 'extra', 'kind', 'digest', 'bytes', 'missing', 'changed_file'])
def test_emitted_coordinate_file_join(tmp_path, field, case):
    from trusted_router import async_settle_shadow_v2_collect_ports as ports
    files = Files(tmp_path)
    # This tests the byte join only. Original shape and coordinate checks are
    # performed by the collecting module after resolution.
    ref = files.put('original.json', b'{}')
    spec = ports.EMITTED_COORDINATES[field]
    coordinate = dict(kind=spec['kind'], sha256=ref['sha256'], bytes=ref['bytes'])
    originals = {(coordinate['kind'], coordinate['sha256'], coordinate['bytes']): ref}
    if case == 'extra':
        coordinate['extra'] = 'unused'
    elif case == 'kind':
        coordinate['kind'] = 'other_kind'
    elif case == 'digest':
        coordinate['sha256'] = 'b' * 64
    elif case == 'bytes':
        coordinate['bytes'] = spec['max_bytes'] + 1
    elif case == 'missing':
        originals.clear()
    elif case == 'changed_file':
        (files.root / ref['path']).write_bytes(b'[]')
    if case == 'valid':
        assert ports.resolve_emitted_reference(files, field, coordinate, originals) == ref
    else:
        with pytest.raises((ValueError, KeyError)):
            ports.resolve_emitted_reference(files, field, coordinate, originals)


@pytest.mark.parametrize('case', ['valid', 'old_claim', 'old_publication'])
def test_contender_claim_publication_and_install_are_boot_local(tmp_path, case):
    from trusted_router import async_settle_shadow_v2_a1_health as health
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    record = next(r for r in fake.records['readiness'] if r.get('criterion') == 'observer_freshness')
    boot = value.boots[0]
    basis = json.loads(files.read(record['health_basis']))
    body = basis['body']
    body.update(publisher_boot_id=boot['boot'], observed_at_ns=boot['boot_at_us'] * 1000,
                publisher_heartbeat_ns=boot['boot_at_us'] * 1000)
    claim = dict(v=2, policy_version='shadow-v2', contract_revision='a1', authority='local',
                 slot=body['slot'], owner_boot_id=boot['boot'], claim_at_ns=body['observed_at_ns'])
    basis['committed_at_ns'] = body['observed_at_ns']
    if case == 'old_claim':
        claim['claim_at_ns'] = START * 1000
    elif case == 'old_publication':
        claim['claim_at_ns'] = START * 1000
        body['observed_at_ns'] = body['publisher_heartbeat_ns'] = START * 1000
        basis['committed_at_ns'] = START * 1000
    record.update(contender=True, claim=files.obj('native/claim.json', claim, health.HEALTH_CLAIM),
        publication=files.obj('native/publication.json', body, health.HEALTH),
        claim_at_ns=claim['claim_at_ns'], published_at_ns=body['observed_at_ns'],
        publication_elapsed_us=(body['observed_at_ns'] - claim['claim_at_ns']) // 1000,
        health_basis=files.obj('native/contender-basis.json', basis, health.HEALTH_BASIS))
    _, result = get_readiness(files, value, pins, fake)
    assert result.complete is (case == 'valid')


@pytest.mark.parametrize('kind', ['a3_startup_correlation', 'a3_boot_check', 'a3_traffic_ready', 'a3_opted_attempt', 'a3_shutdown_span'])
@pytest.mark.parametrize('case', ['extra', 'identifier', 'over_limit', 'reference'])
def test_closed_runtime_records_reject_extra_or_over_limit_originals(tmp_path, kind, case):
    from tests.test_async_settle_shadow_v2_collect import attempt_record
    from trusted_router.async_settle_shadow_v2_collect_ports import RECORD_LIMITS, runtime_record
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, ready = readiness_sources(files, value)
    records = {r['kind']: r for r in ready.records['readiness']}
    records.update(a3_startup_correlation=fake.records['startup'][0],
                   a3_opted_attempt=attempt_record(files, value.boots[0], START + 100),
                   a3_shutdown_span=shutdown_sources(value).records['shutdown'][0])
    record = records[kind]
    runtime_record(record)
    if case == 'extra':
        record['unlisted'] = 'unlisted-value'
    elif case == 'identifier':
        record['boot'] = 'x' * 129
    elif case == 'over_limit':
        record['boot'] = 'x' * RECORD_LIMITS[kind]
    else:
        record['unlisted_source'] = dict(path='x' * 257, sha256='a' * 64, bytes=262145)
    with pytest.raises(ValueError, match='runtime_record_bytes' if case == 'over_limit' else None):
        runtime_record(record)


@pytest.mark.parametrize('missing', ['preparation', 'export_wait', 'final_pass', 'close', 'result'])
def test_duration_reader_rejects_missing_original_interval(missing):
    from trusted_router.async_settle_shadow_v2_collect_shutdown import durations
    record = dict(spans=dict(preparation=[0, 100], export_wait=[100, 200], final_pass=[200, 500],
                             close=[250, 350], result=[350, 450]))
    del record['spans'][missing]
    with pytest.raises(ValueError):
        durations(record)


@pytest.mark.parametrize('kind', ['a3_startup_correlation', 'a3_boot_check', 'a3_traffic_ready', 'a3_opted_attempt', 'a3_shutdown_span'])
def test_runtime_record_exact_byte_limit(tmp_path, monkeypatch, kind):
    from tests.test_async_settle_shadow_v2_collect import attempt_record
    from trusted_router import async_settle_shadow_v2_collect_ports as ports
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    _, ready = readiness_sources(files, value)
    records = {r['kind']: r for r in ready.records['readiness']}
    records.update(a3_startup_correlation=fake.records['startup'][0],
                   a3_opted_attempt=attempt_record(files, value.boots[0], START + 100),
                   a3_shutdown_span=shutdown_sources(value).records['shutdown'][0])
    record = records[kind]
    ports.runtime_record(record)
    # Isolate serialized length with a valid record. This does not claim that
    # each kind has a fully valid canonical encoding of exactly its byte cap.
    monkeypatch.setattr(ports, 'canonical', lambda _: b'x' * ports.RECORD_LIMITS[kind])
    ports.runtime_record(record)
    monkeypatch.setattr(ports, 'canonical', lambda _: b'x' * (ports.RECORD_LIMITS[kind] + 1))
    with pytest.raises(ValueError, match='runtime_record_bytes'):
        ports.runtime_record(record)
