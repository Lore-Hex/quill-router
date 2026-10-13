"""Fake producer and startup ordering checks; no platform calls."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from trusted_router.services import async_settle_shadow_launch_descriptor as launch
from trusted_router.services import async_settle_shadow_startup_records as records
from trusted_router.services.async_settle_shadow_startup_harness import (
    Acknowledgement,
    Admission,
    Harness,
    Ports,
    ReleaseManifest,
    listener_started,
    load_manifest,
)

REV = 'a'*40
REGION = 'us-central1'


def descriptor():
    return launch.parse(records.canonical(dict(instance='instance:one', launch='launch:one',
        role='router', revision=REV, region=REGION)))


def manifest():
    directory = tempfile.TemporaryDirectory(prefix='startup-originals-')
    config = dict(enclave_revision='d'*40, dependencies=dict(router=[dict(operation=o, resource='regional-store', region=REGION)
                                           for o in ('transport', 'read')]), descriptor_ids=['public-one'])
    config_sha = hashlib.sha256(records.canonical(config)).hexdigest()
    m = dict(launch=dict(mode='instance_as_launch', project='project', region=REGION,
        service='router', revisions={'revision-one': REV}), revision=REV, region=REGION,
        config_sha256=config_sha, policy_sha256='b'*64, fixture_sha256='c'*64,
        config=config, effective_pins=dict.fromkeys(records.PIN_KEYS, 'false'),
        public_descriptors=dict(authority_epoch=1, config_sha256=config_sha, source_revision=REV,
                                descriptor_ids=['public-one']), artifacts={}, selections={}, ready_key_prefix='startup/ready')
    for criterion, sources in records.SOURCES.items():
        m['selections'][criterion] = {}
        for source in sources:
            name = criterion+'/'+source
            m['selections'][criterion][source] = name
            m['artifacts'][name] = dict(id=name, criterion=criterion, source=source,
                router_revision=REV, enclave_revision='d'*40, region=REGION, config_sha256=config_sha, started_at_us=1,
                completed_at_us=2, checks=dict.fromkeys(records.CHECKS[criterion], 'pass'), conclusion='pass')
            raw = records.canonical(m['artifacts'][name])
            path = Path(directory.name)/(name.replace('/', '-')+'.json')
            path.write_bytes(raw)
            m['artifacts'][name]['path'] = str(path)
    result = ReleaseManifest(records.canonical(m))
    result.test_directory = directory
    return result


def fake_harness(*, contender=False, ack=True):
    lines, calls = [], []
    clock = [2_000_000]
    def now():
        clock[0] += 1000
        return clock[0]
    async def dependency(target, deadline):
        calls.append((target, deadline))
        return True
    async def observer():
        read_ns = now()*1000
        body = dict(v=2, policy_version='shadow-v2', contract_revision='a1', authority='local',
            source='shadow_observation', publisher_boot_id='boot-one', publisher_region=REGION,
            router_revision=REV, complete=True, slot=1, observed_at_ns=read_ns-1000,
            publisher_heartbeat_ns=read_ns-1000, backlog_age_at_seconds=2.,
            **dict.fromkeys(('sample_count', 'backlog_count', 'frozen_micro', 'p50_age_us',
                            'p95_age_us', 'oldest_unresolved_age_us', 'dead_count'), 0))
        value = dict(v=2, policy_version='shadow-v2', contract_revision='a1', reader_boot='boot-one',
            source_state='installed', body=body, committed_at_ns=read_ns-1000,
            read_at_ns=read_ns, read_elapsed_ns=1000, since_reply_ns=1000)
        result = dict(health_basis_raw=records.canonical(value), installed_at_ns=read_ns+2000,
                      contender=contender)
        if contender:
            claim = dict(v=2, policy_version='shadow-v2', contract_revision='a1', authority='local',
                         owner_boot_id='boot-one', slot=1, claim_at_ns=read_ns-2000)
            publication = body
            value['body'] = publication
            result.update(health_basis_raw=records.canonical(value), claim_raw=records.canonical(claim),
                publication_raw=records.canonical(publication), publication_elapsed_us=1,
                claim_at_ns=read_ns-2000, published_at_ns=read_ns-1000)
        return result
    async def retain(key, raw):
        calls.append(('ready', key, raw))
        assert not h.admission.ready
        return Acknowledgement(key, '17', hashlib.sha256(raw).hexdigest(), len(raw)) if ack else None
    h = Harness('boot-one', Admission(), emitter=records.Emitter(lines.append),
                ports=Ports(dependency, observer, retain), now=now, monotonic=now)
    def validate_observer(observed):
        from trusted_router.async_settle_shadow_v2_a1_health import (
            HEALTH,
            HEALTH_BASIS,
            HEALTH_CLAIM,
            validate_publication,
        )
        HEALTH_BASIS.parse(observed['health_basis_raw'])
        if observed['contender']:
            validate_publication(HEALTH_CLAIM.parse(observed['claim_raw']), HEALTH.parse(observed['publication_raw']))
    h.ports.validate_observer = validate_observer
    async def retain_original(key, raw):
        calls.append(('original', key, raw))
        return Acknowledgement(key, '19', hashlib.sha256(raw).hexdigest(), len(raw))
    h.ports.retain_original = retain_original
    return h, lines, calls


def test_inventory_equals_collectors():
    from trusted_router import async_settle_shadow_v2_collect_ports as collectors
    pairs = dict(fields='RECORD_FIELDS', role_fields='ROLE_FIELDS', limits='RECORD_LIMITS',
        checks='CHECKS', sources='SOURCES', attempt_results='ATTEMPT_RESULTS', channels='CHANNEL_KINDS',
        emitted_coordinate_fields='EMITTED_COORDINATE_FIELDS', emitted_coordinate_types='EMITTED_COORDINATE_TYPES',
        emitted_coordinates='EMITTED_COORDINATES', launch_descriptor_fields='LAUNCH_DESCRIPTOR_FIELDS',
        launch_coordinate_fields='LAUNCH_COORDINATE_FIELDS')
    for name, constant in pairs.items():
        assert records.canonical(records.INVENTORY[name]) == records.canonical(getattr(collectors, constant))


@pytest.mark.parametrize('field', records.INVENTORY['emitted_coordinates'])
def test_original_byte_limits_and_same_buffer(field):
    limit = records.INVENTORY['emitted_coordinates'][field]['max_bytes']
    raw = b'x'*limit
    value = records.coordinate(field, raw)
    assert value['bytes'] == limit and value['sha256'] == hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        records.coordinate(field, raw+b'x')
    with pytest.raises(ValueError):
        records.coordinate(field, b'')


@pytest.mark.parametrize('case', ['good', 'malformed', 'oversized', 'missing_eof', 'noncanonical', 'extra'])
def test_descriptor_pipe(case):
    read, write = os.pipe()
    raw = descriptor().original
    if case == 'malformed':
        raw = b'{}'
    elif case == 'oversized':
        raw = b'x'*2049
    elif case == 'noncanonical':
        raw += b'\n'
    elif case == 'extra':
        value = json.loads(raw)
        value['extra'] = 'incoming header'
        raw = records.canonical(value)
    try:
        os.write(write, raw)
        if case != 'missing_eof':
            os.close(write)
            write = -1
        if case == 'good':
            assert launch.read_fd(read).original == raw
        else:
            with pytest.raises(ValueError):
                launch.read_fd(read, timeout=.01)
    finally:
        os.close(read)
        if write != -1:
            os.close(write)


def test_descriptor_actual_fd_three(tmp_path):
    import subprocess
    import sys
    path = tmp_path/'descriptor.json'
    path.write_bytes(descriptor().original)
    # Child-only descriptor 3; the parent's descriptors remain untouched.
    code = f'''import os
import tempfile
from pathlib import Path
from trusted_router.services.async_settle_shadow_launch_descriptor import read_fd
f = os.open({str(path)!r}, os.O_RDONLY)
os.dup2(f, 3)
assert read_fd().mode == 'fd3'
'''
    result = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'PYTHONPATH': 'src'},  # noqa: S603 - fixed child script
                            capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_instance_mode_scoped_encoding():
    m = manifest()['launch']
    result = launch.acquire(m, fd=987654, environ={'K_REVISION': 'revision-one'}, instance_reader=lambda: '123')
    raw = records.canonical(dict(source='cloud_run_instance', coordinate=dict(project='project', location=REGION,
        service='router', revision_name='revision-one', instance_id='123')))
    digest = hashlib.sha256(raw).hexdigest()
    assert result.value['instance'] == 'instance:'+digest
    assert result.value['launch'] == 'launch:'+digest
    assert result.mode == 'instance_as_launch' and result.source_coordinate == raw
    with pytest.raises(KeyError):
        launch.acquire(m, fd=987654, environ={'K_REVISION': 'unmapped'}, instance_reader=lambda: '123')


async def test_ready_strictly_after_six_and_acknowledgement():
    h, lines, calls = fake_harness()
    m = manifest()
    originals = copy.deepcopy(m['artifacts'])
    await h.run(descriptor(), m)
    assert h.admission.ready and h.acknowledgement.generation == '17'
    decoded = [json.loads(raw) for raw in lines]
    assert [r['kind'] for r in decoded] == ['a3_startup_correlation'] + ['a3_boot_check']*6 + ['a3_traffic_ready']
    assert decoded[-1]['at_us'] > max(r['completed_at_us'] for r in decoded[1:-1])
    assert [r['criterion'] for r in decoded[1:-1]] == list(records.CHECKS)
    assert len([c for c in calls if c[0] == 'ready']) == 1
    assert m['artifacts'] == originals
    await h.run(descriptor(), m)
    assert len(lines) == 8


@pytest.mark.parametrize('criterion', records.CHECKS)
async def test_failed_check_never_adopts_pass(criterion):
    h, lines, _ = fake_harness()
    m = manifest()
    if records.SOURCES[criterion]:
        source = records.SOURCES[criterion][0]
        m['artifacts'][m['selections'][criterion][source]]['checks'][records.CHECKS[criterion][0]] = 'fail'
    else:
        async def unfinished():
            raise TimeoutError
        h.ports.observer = unfinished
    await h.run(descriptor(), m)
    assert not h.admission.ready
    assert all(json.loads(raw)['kind'] != 'a3_traffic_ready' for raw in lines)
    first = copy.deepcopy(h.adopted)
    await h.adopt(criterion)
    assert h.adopted == first


@pytest.mark.parametrize('criterion', records.CHECKS)
async def test_missing_input_stays_missing(criterion):
    h, lines, _ = fake_harness()
    m = manifest()
    if records.SOURCES[criterion]:
        del m['artifacts'][next(iter(m['selections'][criterion].values()))]
    else:
        h.ports.observer = None
    await h.run(descriptor(), m)
    assert criterion in h.missing and criterion not in h.adopted
    assert not h.admission.ready
    assert all(json.loads(raw)['kind'] != 'a3_traffic_ready' for raw in lines)


async def test_stdout_is_not_acknowledgement():
    h, lines, _ = fake_harness(ack=False)
    await h.run(descriptor(), manifest())
    assert json.loads(lines[-1])['kind'] == 'a3_traffic_ready'
    assert not h.admission.ready and h.acknowledgement is None


async def test_wrong_acknowledgement_stays_unavailable():
    h, _, _ = fake_harness()
    async def wrong(key, raw):
        return Acknowledgement(key, '17', '0'*64, len(raw))
    h.ports.retain_ready = wrong
    await h.run(descriptor(), manifest())
    assert not h.admission.ready


async def test_contender_original_coordinates():
    h, _, _ = fake_harness(contender=True)
    await h.run(descriptor(), manifest())
    assert h.admission.ready, h.missing
    record = h.adopted['observer_freshness']
    assert record['contender'] and record['publication_elapsed_us'] == 1
    for field in ('health_basis', 'claim', 'publication'):
        ref = record[field]
        raw = h.originals.buffers[(ref['kind'], ref['sha256'], ref['bytes'])]
        assert records.coordinate(field, raw) == ref


async def test_targets_only_from_release():
    h, _, calls = fake_harness()
    m = manifest()
    await h.run(descriptor(), m)
    assert [c[0] for c in calls if isinstance(c[0], dict)] == m['config']['dependencies']['router']
    assert all(c[1] == 10_000_000 for c in calls if isinstance(c[0], dict))


async def test_listener_answers_while_harness_waits():
    import httpx
    from fastapi import FastAPI
    app = FastAPI()
    entered, release = asyncio.Event(), asyncio.Event()
    h, _, _ = fake_harness()
    async def begin():
        entered.set()
        await release.wait()
    app.state.async_settle_shadow_startup = h
    app.state.async_settle_shadow_startup_begin = begin
    @app.get('/live')
    async def live():
        return {'live': True}
    listener_started(SimpleNamespace(app=app))
    await asyncio.wait_for(entered.wait(), .2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await asyncio.wait_for(client.get('/live'), .2)).json() == {'live': True}
    assert not h.task.done()
    release.set()
    await h.task


@pytest.mark.parametrize('partial', [False, True])
async def test_shutdown_measured_spans_and_midpass_termination(partial):
    h, lines, _ = fake_harness()
    h.correlate(descriptor())
    origin = h.monotonic_at_boot_us
    spans = {k: [origin+a, origin+b] for k, a, b in [('preparation', 1, 2), ('export_wait', 2, 3),
        ('final_pass', 3, 9), ('close', 4, 5), ('result', 6, 8)]}
    if partial:
        del spans['result']
    assert h.shutdown_span(day='2026-10-13', interval_end_us=99, spans=spans,
                           complete=not partial) is (not partial)
    assert not h.shutdown_span(day='2026-10-13', interval_end_us=99, spans=spans, complete=False)
    if partial:
        assert len(lines) == 1 and 'P3ShutdownEndpoints' in h.missing
    else:
        assert json.loads(lines[-1])['spans'] == spans
        assert h.shutdown_span(day='2026-10-14', interval_end_us=100, spans=spans, complete=False)


async def test_closed_records_drop_incoming_values_and_unknown_fields():
    h, lines, _ = fake_harness()
    await h.run(descriptor(), manifest())
    specimens = [json.loads(raw) for raw in lines]
    specimens.extend([
        dict(kind='a3_opted_attempt', boot='boot-one', at_us=1, result='rejected', receipt=dict(day='2026-10-13',
            boot='boot-one', sequence=1, original=records.coordinate('receipt.original', b'page'))),
        dict(kind='a3_shutdown_span', boot='boot-one', instance='one', launch='one', revision=REV,
            region=REGION, day='2026-10-13', interval_end_us=10, complete=False, clock='monotonic_us',
            clock_origin=dict(boot='boot-one', monotonic_at_boot_us=0),
            spans=dict(preparation=[0,1], export_wait=[1,2], final_pass=[2,5], close=[2,3], result=[3,4]), outcome='graceful')])
    output = []
    emitter = records.Emitter(output.append)
    for specimen in specimens:
        assert emitter.emit(specimen)
        for field in specimen:
            changed = {**specimen, field: 'incoming header: Bearer request body prompt'}
            assert emitter.emit(changed) is None
        assert emitter.emit({**specimen, 'request': {'headers': 'incoming value'}}) is None
    assert b'incoming' not in b''.join(output)
    assert emitter.dropped > 0


async def test_record_canonical_byte_boundaries(monkeypatch):
    h, lines, _ = fake_harness()
    await h.run(descriptor(), manifest())
    for raw in lines:
        value = json.loads(raw)
        kind = value['kind']
        # Exact canonical size: same serializer, inclusive boundary, one byte over.
        monkeypatch.setitem(records.LIMITS, kind, len(raw))
        assert records.validate(value) == raw
        monkeypatch.setitem(records.LIMITS, kind, len(raw)-1)
        with pytest.raises(ValueError):
            records.validate(value)


def test_emitter_does_not_raise_on_writer_failure():
    def broken(raw):
        raise OSError
    emitter = records.Emitter(broken)
    assert emitter.emit(dict(kind='a3_traffic_ready', boot='one', at_us=1)) is None
    assert emitter.dropped == 1


def test_manifest_bounded_canonical_and_missing(tmp_path):
    path = tmp_path/'release.json'
    path.write_bytes(records.canonical(manifest()))
    assert load_manifest(str(path)) == json.loads(path.read_bytes())
    path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError):
        load_manifest(str(path))
    path.write_bytes(b' '*262145)
    with pytest.raises(ValueError):
        load_manifest(str(path))
    with pytest.raises(ValueError):
        load_manifest(str(tmp_path/'absent'))


@pytest.mark.parametrize('result', ['accepted', 'failed', 'rejected', 'timeout'])
def test_attempt_result_enum(result):
    value = dict(kind='a3_opted_attempt', boot='boot-one', at_us=1, result=result,
        receipt=dict(day='2026-10-13', boot='boot-one', sequence=1,
                     original=records.coordinate('receipt.original', b'original')))
    assert records.validate(value) == records.canonical(value)


def test_descriptor_requires_read_only(tmp_path):
    path = tmp_path/'descriptor.json'
    path.write_bytes(descriptor().original)
    with path.open('rb+') as source, pytest.raises(ValueError):
        launch.read_fd(source.fileno())


@pytest.mark.parametrize('wrong', [False, True])
async def test_existing_object_transport_retains_generation(monkeypatch, wrong):
    import google.auth

    from trusted_router.services.async_settle_shadow_startup_harness import retain_ready
    from trusted_router.storage_gcp_shadow_attachments import Lifetime
    calls = []
    raw = records.canonical(dict(kind='a3_traffic_ready', boot='boot-one', at_us=1))
    def before_request(*args):
        return None
    monkeypatch.setattr(google.auth, 'default', lambda **kwargs:
                        (SimpleNamespace(before_request=before_request), None))
    def exchange(self, method, url, body, headers):
        calls.append((method, url, body))
        return 201, {}, records.canonical(dict(name='different' if wrong else 'planned/boot.json',
                                                generation='42', size=str(len(raw))))
    monkeypatch.setattr(Lifetime, 'exchange', exchange)
    if wrong:
        with pytest.raises(ValueError):
            await retain_ready('bucket', 'planned/boot.json', raw)
    else:
        ack = await retain_ready('bucket', 'planned/boot.json', raw)
        assert ack == Acknowledgement('planned/boot.json', '42', hashlib.sha256(raw).hexdigest(), len(raw))
    assert len(calls) == 1 and calls[0][0] == 'POST' and calls[0][2] is raw
    assert 'ifGenerationMatch=0' in calls[0][1]


def test_default_writer_failure_cannot_fail_startup(monkeypatch):
    calls = []
    def unavailable():
        calls.append('writer')
        raise OSError
    monkeypatch.setattr(records, 'stdout_writer', unavailable)
    emitter = records.Emitter()
    assert not calls
    assert emitter.emit(dict(kind='a3_traffic_ready', boot='one', at_us=1)) is None
    assert calls == ['writer'] and emitter.dropped == 1


async def test_original_artifact_clock_and_revision_joins(tmp_path):
    h, _, _ = fake_harness()
    m = manifest()
    selected = m['selections']['transport']['ci']
    declared = m['artifacts'][selected]
    path = Path(declared['path'])
    raw = path.read_bytes()
    changed = json.loads(raw)
    changed['completed_at_us'] = 999999999
    path.write_bytes(records.canonical(changed))
    await h.run(descriptor(), m)
    assert 'transport' in h.missing and 'transport' not in h.adopted
    assert path.read_bytes() in h.files.values()
    assert not h.admission.ready


async def test_absent_original_never_uses_manifest_projection():
    h, _, _ = fake_harness()
    m = manifest()
    selected = m['selections']['size']['ci']
    Path(m['artifacts'][selected]['path']).unlink()
    await h.run(descriptor(), m)
    assert 'size' in h.missing and 'size' not in h.adopted
    assert not h.admission.ready


async def test_all_startup_originals_retained_before_ready():
    h, _, calls = fake_harness()
    m = manifest()
    await h.run(descriptor(), m)
    assert h.admission.ready
    uploads = [c for c in calls if c[0] == 'original']
    keys = {c[1] for c in uploads}
    prefix = m['ready_key_prefix']+'/'+h.boot+'/'
    assert {prefix+'descriptor.json', prefix+'mode.json', prefix+'correlation.json', prefix+'index.json'} <= keys
    assert {prefix+'adoptions/'+c+'.json' for c in records.CHECKS} <= keys
    assert all(raw in [c[2] for c in uploads] for raw in m.artifact_originals.values())
    assert len(h.file_acknowledgements) == len(uploads)
    assert calls[-1][0] == 'ready'


async def test_original_generation_required():
    h, _, _ = fake_harness()
    async def missing_generation(key, raw):
        return Acknowledgement(key, '', hashlib.sha256(raw).hexdigest(), len(raw))
    h.ports.retain_original = missing_generation
    await h.run(descriptor(), manifest())
    assert not h.admission.ready and h.ready_raw is None
