"""Original C1 reader and complete external-procedure paths."""
from __future__ import annotations

import copy
import hashlib
import json
import zipfile

import pytest

from tests.test_async_settle_shadow_v2_collect import inventory, readiness_sources, setup
from tests.test_async_settle_shadow_v2_pack import DAY, REV, START
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_pack_procedures import (
    REQUIRED_STAGES,
    check_scope,
    procedure_result,
)
from trusted_router.async_settle_shadow_v2_pack_readers import native_reader_index
from trusted_router.async_settle_shadow_v2_schema import sha256
from trusted_router.detached_jws import canonical


def reader_capture(path):
    files, binding, fake, queries = setup(path, names=('reader',))
    _, inv = inventory(files, binding, fake, queries)
    _, ready = readiness_sources(files, inv)
    record = next(r for r in ready.records['readiness'] if r.get('criterion') == 'observer_freshness')
    original = files.obj('captured-reader.json', record)
    basis = json.loads(files.read(record['health_basis']))
    page = dict(v=2, policy_version='shadow-v2', contract_revision='a1', day=DAY,
        boot='reader', sequence='00000000000000000001', previous_sha256=None, bases=[basis])
    key = 'health-basis-batches/' + DAY + '/reader/00000000000000000001'
    return files, original, record, key, page


@pytest.mark.parametrize('shape', ['record', 'records', 'log', 'emitted'])
def test_c1_reader_original_shapes(tmp_path, shape):
    files, ref, record, key, page = reader_capture(tmp_path)
    originals = {}
    if shape == 'emitted':
        ref_basis = record['health_basis']
        record['health_basis'] = dict(kind='health_basis', sha256=ref_basis['sha256'], bytes=ref_basis['bytes'])
        originals['health_basis', ref_basis['sha256'], ref_basis['bytes']] = ref_basis
    value = {'records': [record]} if shape == 'records' else {'records': [{'jsonPayload': record}]} if shape == 'log' else record
    ref = files.obj('shape.json', value)
    index = native_reader_index(files, records=[ref], receipts=[], samples=[], batches=[(key, page)], originals=originals)
    assert index.keys == (key,) and not index.missing
    assert index.digests == (sha256(page['bases'][0]),)
    absent = native_reader_index(files, records=[ref], receipts=[], samples=[], batches=[], originals=originals)
    assert absent.digests == index.digests
    assert absent.missing == ('basis:' + index.digests[0],)


def test_reader_reference_cannot_come_from_presence(tmp_path):
    files, ref, record, key, page = reader_capture(tmp_path)
    unreferenced = native_reader_index(files, records=[], receipts=[], samples=[], batches=[(key, page)])
    assert unreferenced.digests == ()
    assert 'unreferenced_basis:' + sha256(page['bases'][0]) in unreferenced.missing
    record['page'] = key + '-absent'
    ref = files.obj('named-missing.json', record)
    index = native_reader_index(files, records=[ref], receipts=[], samples=[], batches=[(key, page)])
    assert 'page:' + record['page'] in index.missing


def procedure_capture(files, name, stage):
    prefix = 'capture/' + stage + '/' + name
    scope = check_scope(name, stage)
    config = files.obj(prefix + '/config', dict(router_revision=REV, enclave_revision=REV,
        resource='fake-native-database', region='us-central1'))
    operation = files.obj(prefix + '/operation', dict(resource='fake-native-database', region='us-central1', configuration=config, complete=True,
        start_us=START, end_us=START + 1, native_rpcs=3))
    procedures = {}
    for check in scope:
        procedures[check] = files.obj(prefix + '/' + check, dict(criterion=name, check=check,
            kind='native_trial', resource='fake-native-database', region='us-central1', started_at_us=START,
            completed_at_us=START + 2, complete=True, conclusion='pass', router_revision=REV,
            enclave_revision=REV, configuration=config, operations=[operation], sources=[operation]))
    result = dict(revision=REV, enclave_revision=REV, run_id=1, job_id=2, artifact_id=3,
        conclusion='success', completed_at=DAY + 'T00:00:01Z', procedures=procedures)
    raw = canonical(result)
    archive = files.root / (prefix + '/download.zip')
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('exact/result.json', raw)
        output.writestr('different/result.json', b'{}')
    refs = dict(run=files.obj(prefix + '/run', dict(id=1, head_sha=REV, status='completed',
        conclusion='success', updated_at=DAY + 'T00:00:02Z')),
        job=files.obj(prefix + '/job', dict(id=2, run_id=1, head_sha=REV, status='completed',
            name='a passing job title', conclusion='success', completed_at=DAY + 'T00:00:01Z')),
        artifact=files.obj(prefix + '/artifact', dict(id=3, workflow_run=dict(id=1, head_sha=REV), expired=False)))
    enclave_ci = {key: files.obj(prefix + '/enclave-' + key, json.loads(files.read(ref)))
                  for key, ref in refs.items()}
    enclave_ci['result'] = files.obj(prefix + '/enclave-result', result)
    return dict(enclave_ci=enclave_ci, name=name, stage=stage, checks=list(scope), router_revision=REV, enclave_revision=REV,
        archive=archive.relative_to(files.root).as_posix(), archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        member=dict(name='exact/result.json', bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()), **refs)


@pytest.mark.parametrize('stage,name', [(s, n) for s, names in REQUIRED_STAGES.items() for n in names])
def test_every_external_name_from_archive_and_procedure_originals(tmp_path, stage, name):
    files = PackFiles(tmp_path)
    plan = procedure_capture(files, name, stage)
    result = procedure_result(files, plan, capture_root=tmp_path)
    assert [c['name'] for c in result['checks']] == list(check_scope(name, stage))
    for check in result['checks']:
        original = json.loads(files.read(check['evidence']))
        assert check['result'] == original['conclusion']
        assert result['router_revision'] == original['router_revision']
    assert result['completed_at_us'] == START + 1000000
    from tests.test_async_settle_shadow_v2_pack_round4 import rewrite_procedures
    files = PackFiles(tmp_path / 'negative')
    plan = procedure_capture(files, name, stage)
    rewrite_procedures(files, plan, lambda value: value.update(resource='other-database'))
    with pytest.raises((ValueError, OSError)):
        procedure_result(files, plan, capture_root=files.root)


@pytest.mark.parametrize('damage', ['job_title', 'short_scope', 'operation_missing', 'procedure_missing'])
def test_external_composition_missing_original_or_scope_blocks(tmp_path, damage):
    positive = PackFiles(tmp_path / 'positive')
    good = procedure_capture(positive, 'fleet_load_budget', 'fleet-budgets-pre')
    procedure_result(positive, good, capture_root=positive.root)
    files = PackFiles(tmp_path / 'negative')
    plan = procedure_capture(files, 'fleet_load_budget', 'fleet-budgets-pre')
    if damage == 'short_scope':
        plan['checks'].pop()
    elif damage == 'job_title':
        job = json.loads(files.read(plan['job']))
        job.update(name='success', conclusion='failure')
        plan['job'] = files.obj('changed-job', job)
    else:
        suffix = '/operation' if damage == 'operation_missing' else '/' + plan['checks'][0]
        next(path for path in files.root.rglob('*') if path.as_posix().endswith(suffix)).unlink()
    with pytest.raises((ValueError, OSError)):
        procedure_result(files, copy.deepcopy(plan), capture_root=files.root)


def test_peak_numbers_equal_every_original(tmp_path):
    from tests.test_async_settle_shadow_v2_pack import peaks
    from trusted_router.async_settle_shadow_v2_pack_sources import derived_peaks
    files = PackFiles(tmp_path)
    values = peaks()
    refs = {name: files.obj('original/' + name, dict(value=value)) for name, value in values.items()}
    derived = derived_peaks(files, {name: dict(kind='measured', original=ref, field=['value'])
                                   for name, ref in refs.items()})
    assert derived == {name: json.loads(files.read(ref))['value'] for name, ref in refs.items()}


@pytest.mark.parametrize('name', ['live', 'rare_cases'])
def test_external_pre_flip_requires_drain_handover(tmp_path, name):
    files = PackFiles(tmp_path)
    plan = procedure_capture(files, name, 'pre-flip')
    assert 'drain_handover' in plan['checks']
    plan['checks'].remove('drain_handover')
    with pytest.raises(ValueError, match='artifact_gap'):
        procedure_result(files, plan, capture_root=tmp_path)
