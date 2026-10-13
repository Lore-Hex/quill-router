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
        operation=dict(last=False), insertId='insert-id', timestamp=DAY + 'T00:00:00Z')
    assert inv.lifecycle(entry, binding) is None
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


def test_enclave_readiness_uses_own_echo_and_router_path(tmp_path):
    files, binding, fake, queries = setup(tmp_path, ('enclave-boot',))
    for event in fake.records['lifecycle']:
        event['jsonPayload']['role'] = 'enclave'
    fake.records['startup'][0]['role'] = 'enclave'
    config = fake.records['platform'][0]
    config.update(role='enclave', pins=dict(TR_ASYNC_SETTLE_NEGOTIATE='off', TR_ASYNC_SETTLE_SHADOW='on'))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    warm = next(r for r in fake.records['readiness'] if r.get('criterion') == 'observer_freshness')
    del warm['health_basis']
    warm.update(router_path=dict(ready=True, resource='configured-router', router_boot='router-boot',
                                 region='us-central1'), configured_router_region='us-central1',
                local_transport_ready=True, echo_ready=True)
    marker = next(r for r in fake.records['readiness'] if r.get('criterion') == 'marker_keys')
    marker.update(attested_image=files.obj('native/attested.json', dict(fake_attestation=REV)),
                  attested_revision=REV)
    _, result = get_readiness(files, value, pins, fake)
    assert result.complete and len(result.ledgers) == 1


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
