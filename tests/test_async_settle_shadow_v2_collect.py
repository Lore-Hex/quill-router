"""A3-C1 fake source captures; artifacts under test come only from collectors."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.async_settle.pilot_enablement import gate_roster_coverage, roster_coverage
from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_collect_inventory as inv
from trusted_router import async_settle_shadow_v2_collect_readiness as ready
from trusted_router import async_settle_shadow_v2_collect_shutdown as shut
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
from trusted_router.async_settle_shadow_v2_collect_ports import (
    TAGS,
    Files,
    Page,
    Pass,
    Query,
    day_bounds,
)
from trusted_router.async_settle_shadow_v2_gate_inputs import (
    SHUTDOWN_SOURCE,
    LoadingPorts,
    ReleasePlan,
)
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_gate_provenance import prerequisite
from trusted_router.detached_jws import canonical

DAY = '2026-10-12'
START, END = day_bounds(DAY)
REV = 'a' * 40
PINS = dict(TR_ASYNC_SETTLE_ENABLED='false', TR_ASYNC_SETTLE_PROTECTION='false',
            TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED='false')


class Fake:
    def __init__(self, records):
        self.records = records
        self.calls = []
        self.overrides = {}

    def read(self, query, token):
        self.calls.append((query, token))
        if (query.source, token) in self.overrides:
            value = self.overrides[query.source, token]
            if isinstance(value, Exception):
                raise value
            return value
        if query.source not in self.records:
            raise OSError('deleted source')
        rows = self.records[query.source]
        offset = int(token or 0)
        selected = rows[offset:offset + query.page_size]
        more = offset + len(selected) < len(rows)
        return Page(canonical(dict(records=selected)), str(offset + len(selected)) if more else None,
                    not more, 100, END)


def setup(tmp_path, names=('retired', 'killed', 'zero-row', 'overlap')):
    files = Files(tmp_path)
    witnesses = [files.obj(f'witness/{i}.json', dict(fake=True, test=i)) for i in range(2)]
    binding = files.obj('binding.json', dict(mode='supervisor', supervisor='external-parent',
        process_complete=True, delivery_complete=True, witnesses=witnesses, carry_in=[]))
    events, joins = [], []
    for i, boot in enumerate(names):
        common = dict(instance='vm1' if i < 2 else 'vm' + str(i), launch='launch-' + boot,
                      role='router', revision=REV, region='us-central1')
        at = START + 10 + i * 100
        for kind, event, when in [('a3_process_launch', 'start', at),
                                  ('a3_process_exit', 'kill' if boot == 'killed' else 'stop', at + 10000)]:
            events.append(dict(insertId=boot + event, jsonPayload=dict(**common, kind=kind,
                supervisor='external-parent', event=event, at_us=when)))
        joins.append(dict(**common, kind='a3_startup_correlation', boot=boot,
                          process_start_us=at + 1, recorded_at_us=at + 2))
    records = dict(platform=[dict(role='router', revision=REV, region='us-central1',
                    grace_us=10_000_000, pins=PINS,
                    original=files.obj('platform/revision.json', dict(fake_platform=True)))], lifecycle=events, startup=joins,
                   monitoring=[dict(resource='named-service', complete_through_us=END,
                    original=files.obj('platform/series.json', dict(fake_monitoring=True)))])
    queries = {k: Query(k, 'named-service', DAY) for k in records}
    fake = Fake(records)
    return files, binding, fake, queries


def inventory(files, binding, fake, queries, review=None):
    run = Pass(files)
    value = inv.collect(run, platform=fake, logs=fake, monitoring=fake, queries=queries,
                        binding_ref=binding, review=review)
    return run, value


def review(files, subject):
    record = dict(**TAGS, subject_sha256=subject['sha256'], reviewer='fake-human',
                  reviewed_at_us=END, decision='approved')
    ref = files.obj('reviews/' + subject['sha256'] + '.json', record, a2.REVIEW_RECORD)
    return {k: v for k, v in record.items() if k not in TAGS} | dict(record=ref)


def graph(files):
    raw = {str(p.relative_to(files.root)): p.read_bytes() for p in files.root.rglob('*') if p.is_file()}
    release_raw = canonical(dict(fake_release=True))
    release = ReleasePlan('release.json', release_raw, {p: file_digest(v) for p, v in raw.items()},
                          ready.CHECKS, {}, ())
    raw['release.json'] = release_raw
    def unused(*args, **kwargs):
        raise AssertionError('unexpected external function')
    ports = LoadingPorts(verify_certificate=unused, verify_original_receipt=unused, verify_binding=unused,
        comparator_positive=unused, positive_sample=unused, known_exclusion=unused,
        fleet_budgets_pre=unused, collect_rollbacks=unused, report=unused, post_flip=unused,
        roster_coverage=roster_coverage, gate_roster=gate_roster_coverage, release=release,
        measurements={p: SHUTDOWN_SOURCE.parse for p in raw if p.startswith('sources/measurement/')})
    return PayloadGraph(raw, completed_at_us=END, ports=ports)


def test_retired_killed_zero_row_overlap_and_gate_inventory(tmp_path):
    files, binding, fake, queries = setup(tmp_path)
    run, value = inventory(files, binding, fake, queries)
    assert value.complete and not run.missing
    _, sealed = inventory(files, binding, fake, queries, review(files, value.deployment))
    g = graph(files)
    envelope = a2.INDEPENDENT_INVENTORY.parse(files.read(sealed.envelope))
    actual = g.inventory({'manifest.independent_inventory_sha256': envelope}, g.ports)
    assert {b.boot for b in actual.boots} == {'retired', 'killed', 'zero-row', 'overlap'}
    assert all(b.end_us > b.start_us for b in actual.boots)


@pytest.mark.parametrize('source', ['platform', 'lifecycle', 'startup', 'monitoring'])
def test_inventory_delete_each_source(tmp_path, source):
    files, binding, fake, queries = setup(tmp_path)
    del fake.records[source]
    run, value = inventory(files, binding, fake, queries)
    assert not value.complete and run.missing
    assert value.envelope is None
    if source in ('platform', 'startup'):
        assert len(value.expected) == 4 and not value.boots


def test_killed_before_startup_retained_without_fictional_boot(tmp_path):
    files, binding, fake, queries = setup(tmp_path)
    fake.records['startup'] = [r for r in fake.records['startup'] if r['boot'] != 'killed']
    run, value = inventory(files, binding, fake, queries)
    missing = next(r for r in value.expected if r['launch'] == 'launch-killed')
    assert missing['boot'] is None and missing['missing']
    assert 'killed' not in {b['boot'] for b in value.boots}
    assert any(r['gap'] == 'writer_roster_gap' for r in run.missing)


@pytest.mark.parametrize('mutation', ['stop', 'binding', 'witness'])
def test_inventory_missing_boundary_or_qualification(tmp_path, mutation):
    files, binding, fake, queries = setup(tmp_path)
    if mutation == 'stop':
        fake.records['lifecycle'].pop()
    else:
        value = json.loads(files.read(binding))
        if mutation == 'binding':
            value['process_complete'] = False
        else:
            value['witnesses'] = []
        binding = files.obj('binding-other.json', value)
    run, value = inventory(files, binding, fake, queries)
    assert not value.complete and run.missing


def readiness_sources(files, value):
    config = dict(dependencies={role: [dict(operation=op, resource=role + '-dependency', region='us-central1')
                                     for op in ('transport', 'read')] for role in ('router', 'enclave')},
                  router_path=dict(resource='configured-router', region='us-central1'),
                  descriptor_ids=['released-public-1'], enclave_image_sha256='b' * 64)
    pins = {k: files.obj(f'pins/{k}.json', config if k == 'config' else dict(fake=k))
            for k in ('policy', 'fixture', 'config')}
    pins.update(router_revision=REV, enclave_revision=REV)
    artifacts, records = [], []
    for boot in value.boots:
        for criterion, checks in ready.CHECKS.items():
            inputs = {}
            for source in ready.SOURCES[criterion]:
                identity = boot['boot'] + '-' + criterion + '-' + source
                original = files.obj('native/' + identity + '.json', dict(fake=True, actual_trial_time=START,
                    started_at_us=START - 1000, completed_at_us=START))
                artifacts.append(dict(id=identity, source=source, criterion=criterion,
                    router_revision=REV, enclave_revision=REV, config_sha256=pins['config']['sha256'],
                    region=boot['region'], completed_at_us=START, started_at_us=START - 1000,
                    original=original, conclusion='pass', workspace_count=1, within_envelope=True))
                inputs[source] = identity
            records.append(dict(kind='a3_boot_check', **{k: boot[k] for k in
                ('role', 'boot', 'revision', 'region', 'boot_at_us')}, criterion=criterion,
                completed_at_us=boot['boot_at_us'] + 10, checks=dict.fromkeys(checks, 'pass'),
                config_sha256=pins['config']['sha256'], inputs=inputs))
        for record in records:
            if record.get('boot') != boot['boot']:
                continue
            criterion = record.get('criterion')
            if criterion in ('boot_transport', 'observer_freshness'):
                clock = dict(clock='utc_ns', boot=boot['boot'], boot_at_us=boot['boot_at_us'])
                record['clock_basis'] = dict(**clock, original=files.obj('native/' + boot['boot'] + '-clock.json', clock))
            if criterion == 'boot_transport':
                record['dependencies'] = [dict(boot=boot['boot'], region=boot['region'],
                    config_sha256=pins['config']['sha256'], operation=op, resource=boot['role'] + '-dependency',
                    attempts=1, marker_matches=True, elapsed_us=1, deadline_us=250000,
                    observed_at_us=boot['boot_at_us'] + 1)
                    for op in ('transport', 'read')]
            elif criterion == 'observer_freshness':
                body = dict(v=2, policy_version='shadow-v2', contract_revision='a1', authority='local',
                    source='shadow_observation', publisher_boot_id='publisher', publisher_region='us-central1',
                    router_revision=REV, complete=True, slot=START // 2000000, observed_at_ns=START * 1000,
                    publisher_heartbeat_ns=START * 1000, backlog_age_at_seconds=START // 1000000,
                    **dict.fromkeys(('sample_count', 'backlog_count', 'frozen_micro', 'p50_age_us',
                                     'p95_age_us', 'oldest_unresolved_age_us', 'dead_count'), 0))
                basis = dict(v=2, policy_version='shadow-v2', contract_revision='a1', reader_boot=boot['boot'],
                    source_state='installed', body=body, committed_at_ns=START * 1000,
                    read_at_ns=(boot['boot_at_us'] + 1) * 1000, read_elapsed_ns=1000, since_reply_ns=1000)
                record.update(health_basis=files.obj('native/' + boot['boot'] + '-basis.json', basis,
                                                     health.HEALTH_BASIS),
                              install_elapsed_us=1, installed_at_ns=(boot['boot_at_us'] + 2) * 1000, contender=False)
            elif criterion == 'marker_keys':
                record.update(effective_pins=boot['pins'], source_revision=boot['revision'], authority_epoch=1,
                    public_descriptors=files.obj('native/' + boot['boot'] + '-public.json', dict(authority_epoch=1, config_sha256=pins['config']['sha256'], source_revision=REV,
                        descriptor_ids=['released-public-1'])),
                    **{k + '_sha256': pins[k]['sha256'] for k in ('policy', 'fixture')})
        records.append(dict(kind='a3_traffic_ready' , boot=boot['boot'], at_us=boot['boot_at_us'] + 11))
    return pins, Fake(dict(artifacts=artifacts, readiness=records))


def get_readiness(files, value, pins, fake):
    run = Pass(files)
    result = ready.collect(run, inventory=value, ci=fake, logs=fake,
        artifact_query=Query('artifacts', 'ci-run-1', DAY, 100),
        startup_query=Query('readiness', 'named-service', DAY), pins=pins)
    return run, result


def test_all_six_proofs_load_original_trial_time_and_boot_adoption(tmp_path):
    files, binding, fake, queries = setup(tmp_path)
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    run, result = get_readiness(files, value, pins, fake)
    assert not run.missing and len(result.ledgers) == 4 and len(result.proofs) == 24
    g = graph(files)
    proofs = {ref['sha256']: a2.PROOF_RESULT.parse(files.read(ref)) for ref in result.proofs}
    for ref in result.ledgers:
        ledger = a1.PREREQUISITE_LEDGER.parse(files.read(ref))
        for criterion in ready.CHECKS:
            proof = proofs[ledger[criterion]['sha256']]
            prerequisite(g, proof, ledger, criterion)
            assert proof['completed_at_us'] >= ledger['boot_at_us']
    assert all(a['completed_at_us'] == START for a in fake.records['artifacts'])


@pytest.mark.parametrize('source', ['artifacts', 'readiness'])
def test_readiness_source_deletion(tmp_path, source):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    del fake.records[source]
    run, result = get_readiness(files, value, pins, fake)
    assert not result.complete and not result.ledgers and run.missing


@pytest.mark.parametrize('criterion', list(ready.CHECKS))
def test_missing_each_criterion_blocks(tmp_path, criterion):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    fake.records['readiness'] = [r for r in fake.records['readiness'] if r.get('criterion') != criterion]
    _, result = get_readiness(files, value, pins, fake)
    assert not result.complete and not result.ledgers


def test_early_and_failed_attempts_retained(tmp_path):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    fake.records['readiness'][0]['checks']['actual_hops'] = 'fail'
    attempt = attempt_record(files, value.boots[0], START + 12)
    fake.records['readiness'].append(attempt)
    run, result = get_readiness(files, value, pins, fake)
    assert result.attempts == (attempt,) and not result.complete
    assert {'early_opted_attempt', 'transport:failed_check'} <= {r['reason'] for r in run.missing}
    g = graph(files)
    ledger = a1.PREREQUISITE_LEDGER.parse(files.read(result.ledgers[0]))
    proof = a2.PROOF_RESULT.parse(files.read(result.proofs[0]))
    with pytest.raises(ValueError):
        prerequisite(g, proof, ledger, 'transport')


def shutdown_sources(value):
    return Fake(dict(shutdown=[dict(kind='a3_shutdown_span', **{k: b[k] for k in
        ('boot', 'instance', 'launch', 'revision', 'region')}, day=DAY,
        interval_end_us=b['end_us'], complete=True, clock='monotonic_us',
        clock_origin=dict(boot=b['boot'], monotonic_at_boot_us=0),
        outcome='hard_kill' if b['reason'] == 'hard_kill' else 'graceful',
        spans=dict(preparation=[100, 200], export_wait=[200, 400], final_pass=[400, 1400],
                   close=[500, 800], result=[900, 1000])) for b in value.boots]))


def get_shutdown(files, value, fake, approved=None):
    run = Pass(files)
    result = shut.collect(run, inventory=value, logs=fake, query=Query('shutdown', 'named-service', DAY),
                          originals={}, review=approved)
    return run, result


def test_shutdown_contract_and_real_payload_loader_aliases(tmp_path):
    files, binding, fake, queries = setup(tmp_path)
    _, value = inventory(files, binding, fake, queries)
    fake = shutdown_sources(value)
    _, draft = get_shutdown(files, value, fake)
    run, result = get_shutdown(files, value, fake, review(files, draft.measurements))
    assert any(r['reason'] == 'hard_kill' for r in run.missing)
    g = graph(files)
    envelope = a2.SHUTDOWN_INVENTORY.parse(files.read(result.envelope))
    g.materialize_one('proof.shutdown_inventory_sha256', envelope)
    assert g.shutdown and len(g.shutdown.entries) == 4
    for measurement in g.shutdown.measurements:
        assert measurement['preparation_us'] == 100
        assert measurement['export_wait_us'] == 200
        assert measurement['final_pass_us'] == 1000
        assert measurement['close_us'] == 300 and measurement['result_us'] == 100
    for alias in g.shutdown.aliases:
        if alias.key.kind == 'shutdown_measurement':
            assert alias.raw == g.files[alias.key.id]


@pytest.mark.parametrize('component', ['shutdown', 'preparation', 'export_wait', 'final_pass', 'close', 'result'])
def test_shutdown_source_and_span_deletion_never_zero(tmp_path, component):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    fake = shutdown_sources(value)
    if component == 'shutdown':
        del fake.records['shutdown']
    else:
        del fake.records['shutdown'][0]['spans'][component]
    run, result = get_shutdown(files, value, fake)
    assert not result.entries and not result.complete and run.missing
    assert not list(files.root.glob('sources/measurement/*.json'))


def test_pagination_to_eof_and_bounded_requests(tmp_path):
    fake = Fake(dict(x=[dict(i=i) for i in range(201)]))
    run = Pass(Files(tmp_path))
    assert len(list(run.scan(fake, Query('x', 'named', DAY)))) == 201
    assert [token for _, token in fake.calls] == [None, '200']
    assert not run.missing and len(run.sources) == 6


@pytest.mark.parametrize('failure', ['truncated', 'deadline', 'exception', 'delay', 'cycle', 'exhaustion', 'missing_page'])
def test_incomplete_pass_never_eof(tmp_path, failure):
    fake = Fake(dict(x=[dict(i=i) for i in range(201)]))
    base = Page(canonical(dict(records=[])), None, True, 10, END)
    if failure == 'truncated':
        fake.overrides['x', None] = replace(base, truncated=True)
    elif failure == 'deadline':
        fake.overrides['x', None] = replace(base, elapsed_us=10_000_001)
    elif failure == 'exception':
        fake.overrides['x', None] = TimeoutError()
    elif failure == 'delay':
        fake.overrides['x', '200'] = replace(base, complete_through_us=END - 1)
    elif failure == 'cycle':
        fake.overrides['x', '200'] = replace(base, next_token=str(200), eof=False)
    elif failure == 'missing_page':
        fake.overrides['x', '200'] = OSError()
    run = Pass(Files(tmp_path), limit=200 if failure == 'exhaustion' else 10_000)
    list(run.scan(fake, Query('x', 'named', DAY)))
    assert run.missing and len(fake.calls) <= 2


def test_deterministic_output_bytes(tmp_path):
    outputs = []
    for name in ('a', 'b'):
        files, binding, fake, queries = setup(tmp_path / name)
        _, value = inventory(files, binding, fake, queries)
        pins, fake = readiness_sources(files, value)
        get_readiness(files, value, pins, fake)
        get_shutdown(files, value, shutdown_sources(value))
        outputs.append({str(p.relative_to(files.root)): p.read_bytes() for p in files.root.rglob('*') if p.is_file()})
    assert outputs[0] == outputs[1]


def receipt_page(boot, event_at_us, receipt_at_us, day=DAY, sequence=1):
    page = json.loads(Path('tests/fixtures/async_settlement/shadow_v2/receipt_pages.json').read_bytes())['variants'][0]['object']
    seq = f'{sequence:020d}'
    page.update(boot=boot, day=day, first_seq=seq, last_seq=seq)
    event = page['events'][0]
    event.update(seq=seq, event_at_us=event_at_us, receipt_at_us=receipt_at_us, authorization_day=day)
    event['receipt_locator'].update(boot=boot, day=day, first_seq=seq, event_seq=seq)
    return page


def attempt_record(files, boot, at_us):
    return dict(kind='a3_opted_attempt', boot=boot['boot'], at_us=at_us, result='failed',
                receipt=dict(day=DAY, boot=boot['boot'], sequence=1,
                             original=files.obj('native/attempt.json', receipt_page(boot['boot'], at_us, at_us), p1.PAGE)))


@pytest.mark.parametrize('event_offset', [-1, 1], ids=['before_readiness', 'after_readiness'])
def test_attempt_delayed_storage_joins_event_time(tmp_path, event_offset):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    ready_at = next(row['at_us'] for row in fake.records['readiness'] if row['kind'] == 'a3_traffic_ready')
    event_at = ready_at + event_offset
    stored_at = ready_at + 100
    attempt = attempt_record(files, value.boots[0], event_at)
    page = receipt_page(attempt['boot'], event_at, stored_at)
    ref = files.obj('native/delayed-storage.json', page, p1.PAGE)
    assert p1.PAGE.parse(files.read(ref)) == page
    assert page['events'][0]['event_at_us'] == attempt['at_us'] < stored_at
    attempt['receipt']['original'] = ref
    fake.records['readiness'].append(attempt)
    run, result = get_readiness(files, value, pins, fake)
    assert len(result.ledgers) == 1 and len(result.proofs) == 6
    assert result.complete is (event_offset > 0)
    assert result.attempts == (attempt,)
    assert [row['reason'] for row in run.missing] == ([] if event_offset > 0 else ['early_opted_attempt'])
    status = json.loads((files.root / 'readiness/status.json').read_bytes())
    assert status['complete'] is (event_offset > 0)
    assert json.loads(files.read(status['attempts'])) == [attempt]
    assert p1.PAGE.parse(files.read(ref)) == page


def test_attempt_earlier_event_cannot_join_later_storage_time(tmp_path):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    ready_at = next(row['at_us'] for row in fake.records['readiness'] if row['kind'] == 'a3_traffic_ready')
    stored_at = ready_at + 100
    attempt = attempt_record(files, value.boots[0], stored_at)
    page = receipt_page(attempt['boot'], ready_at - 1, stored_at)
    ref = files.obj('native/earlier-event.json', page, p1.PAGE)
    assert p1.PAGE.parse(files.read(ref)) == page
    assert page['events'][0]['event_at_us'] < ready_at < attempt['at_us']
    assert page['events'][0]['receipt_at_us'] == attempt['at_us']
    attempt['receipt']['original'] = ref
    fake.records['readiness'].append(attempt)
    run, result = get_readiness(files, value, pins, fake)
    assert not result.complete and not result.ledgers and not result.proofs
    assert result.attempts == (attempt,)
    assert any(row['reason'] == 'attempt_without_inventory_interval' for row in run.missing)
    status = json.loads((files.root / 'readiness/status.json').read_bytes())
    assert not status['complete']
    assert json.loads(files.read(status['attempts'])) == [attempt]
    assert p1.PAGE.parse(files.read(ref)) == page


@pytest.mark.parametrize('case', ['valid', 'empty', 'boot', 'day', 'sequence', 'zero', 'event_time', 'early_event'])
def test_attempt_original_page_coordinates(tmp_path, case):
    files, binding, fake, queries = setup(tmp_path, ('zero-row',))
    _, value = inventory(files, binding, fake, queries)
    pins, fake = readiness_sources(files, value)
    attempt = attempt_record(files, value.boots[0], START + 100)
    boot = 'different-boot' if case == 'boot' else attempt['boot']
    day = '2026-10-11' if case == 'day' else DAY
    at_us = START - 86_400_000_000 + 100 if case == 'day' else START + 100
    if case in ('event_time', 'early_event'):
        at_us = START + (101 if case == 'event_time' else 12)
    page = receipt_page(boot, at_us, at_us, day, 2 if case == 'sequence' else 1)
    if case == 'zero':
        page = receipt_page(boot, at_us, at_us, day, 0)
        attempt['receipt']['sequence'] = 0
    # Changed coordinates, except zero, are independently valid PAGE objects.
    # Empty/zero originals exercise the existing parser, not FILE_REF reading.
    if case == 'empty':
        ref = files.put('native/selected-page.json', b'')
    else:
        ref = files.obj('native/selected-page.json', page, None if case == 'zero' else p1.PAGE)
    attempt['receipt']['original'] = ref
    fake.records['readiness'].append(attempt)
    run, result = get_readiness(files, value, pins, fake)
    assert result.attempts == (attempt,)
    assert result.complete is (case == 'valid')
    status = json.loads((files.root / 'readiness/status.json').read_bytes())
    assert status['complete'] is (case == 'valid')
    assert json.loads(files.read(status['attempts'])) == [attempt]
    if case == 'valid':
        assert len(result.ledgers) == 1 and len(result.proofs) == 6 and not run.missing
    else:
        assert not result.ledgers
        assert any(row['reason'] == 'attempt_without_inventory_interval' for row in run.missing)
