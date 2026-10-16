"""Round-six original populations and transport record checks."""
from __future__ import annotations

import json

import pytest

from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
from tests.test_async_settle_shadow_v2_pack import review
from tests.test_async_settle_shadow_v2_pack_round3_budget import (  # noqa: F401
    budget_capture,
    captured,
)
from trusted_router.async_settle_shadow_v2_pack_captured_budget import CapturedBudget
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles


@pytest.mark.parametrize('damage', ['duration', 'resource', 'configuration', 'missing'])
def test_component_original_changes_block(tmp_path, captured, damage):  # noqa: F811
    files = PackFiles(tmp_path)
    plan = budget_capture(files, captured)
    name = next(iter(plan['component_spans']))
    ref = plan['component_spans'][name][0]
    if damage == 'missing':
        plan['component_spans'][name] = []
    else:
        span = json.loads(files.read(ref))
        if damage == 'duration':
            span['end_us'] += 1
        elif damage == 'resource':
            span['resource'] = 'different'
        else:
            span['configuration'] = files.obj('different/config', {})
        plan['component_spans'][name] = [files.obj('different/span', span)]
    with pytest.raises(ValueError, match='ComponentSpan'):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')


@pytest.mark.parametrize('kind', ['startup', 'close', 'export'])
def test_required_operation_deletion(tmp_path, captured, kind):  # noqa: F811
    files = PackFiles(tmp_path)
    plan = budget_capture(files, captured)
    item = plan['writer_operations'][0]
    body = json.loads(files.read(item['operation_refs'][0]))
    body['operations'] = [o for o in body['operations'] if o['operation'] != kind]
    item['operation_refs'] = [files.obj('changed/operations', body)]
    with pytest.raises(ValueError, match='OperationPopulation'):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')


def test_split_burst_retains_original_window(tmp_path, captured):  # noqa: F811
    files = PackFiles(tmp_path)
    plan = budget_capture(files, captured)
    item = plan['writer_operations'][0]
    body = json.loads(files.read(item['operation_refs'][0]))
    op = next(o for o in body['operations'] if o['operation'] == 'startup')
    other = {**op, 'id': 'second-startup', 'at_us': op['at_us'] + 1}
    body['operations'].append(other)
    item['operation_refs'] = [files.obj('changed/operations', body)]
    record = json.loads(files.read(plan['trial_record']))
    population = json.loads(files.read(record['operation_population']))
    population['operations'].append(dict(day=body['day'], boot=body['boot'], id=other['id'],
        operation='startup', at_us=other['at_us']))
    record['operation_population'] = files.obj('changed/population', population)
    plan['trial_record'] = files.obj('changed/record', record)
    plan['trial_review'] = review(files, plan['trial_record'], 'changed/review')
    for stage in ('pre', 'post'):
        plan[stage]['load_segments'][0]['derivations']['startup_operations_1s']['original'] = files.obj('changed/peak', {'value': 2})
    with pytest.raises(ValueError, match='LifecyclePopulation'):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')
    from trusted_router.async_settle_shadow_v2_pack_sources import publisher_projection
    from trusted_router.async_settle_shadow_v2_pack_trial import native_peaks
    computed = native_peaks(files, plan, plan['post']['load_segments'][0]['fields'],
        json.loads(files.read(plan['configuration'])), publisher_projection(fleet_cap=4, surge=1, publishers=2),
        dict(trial_peak=dict(numerator=25, denominator=1)))
    assert computed['startup_operations_1s'] == 2


def test_legacy_entry_uses_current_builder(tmp_path, captured):  # noqa: F811
    from trusted_router.async_settle_shadow_v2_pack_measurements import BudgetSourceAssembly
    files = PackFiles(tmp_path)
    plan = budget_capture(files, captured)
    result = BudgetSourceAssembly.build(files, plan, checkpoint='fleet-budgets-pre')
    assert result.pre['stage'] == 'pre' and result.post['stage'] == 'post'


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'pass', 'day', 'later'])
def test_output_population_and_later_costs(tmp_path, captured, damage):  # noqa: F811
    from trusted_router.async_settle_shadow_v2_pack_daypack import AccountingResult
    from trusted_router.async_settle_shadow_v2_pack_passes import measured_export_costs
    files = PackFiles(tmp_path)
    plan = budget_capture(files, captured)
    allowances = json.loads(files.read(plan['pre_allowances']))
    plan['export_ceilings'] = files.obj('ceilings', {k: allowances[k] for k in
        ('export_passes', 'export_operations', 'export_bytes', 'export_write_bytes')} | {'native_rpcs': 99})
    plan['export_review'] = review(files, plan['export_ceilings'], 'ceilings-review')
    receipts = list(plan['output_receipts'])
    if damage == 'missing':
        receipts = []
    elif damage == 'duplicate':
        receipts *= 2
    elif damage == 'later':
        value = json.loads(files.read(plan['output_plan']))
        value['later_passes'][0]['export_write_bytes'] = 10000001
        plan['output_plan'] = files.obj('changed/plan', value)
        plan['output_plan_review'] = review(files, plan['output_plan'], 'changed/review')
    else:
        value = json.loads(files.read(receipts[0]))
        value['pass' if damage == 'pass' else 'at_us'] = 777 if damage == 'pass' else 1
        receipts = [files.obj('changed/receipt', value)]
    with pytest.raises(ValueError, match='Output|evidence_export_budget_gap'):
        measured_export_costs(files, plan, [AccountingResult('2026-10-12', (), (), 1, 1, 1)], receipts)


@pytest.mark.parametrize('stage,name', [(s, n) for s, names in __import__(
    'trusted_router.async_settle_shadow_v2_pack_external', fromlist=['REQUIRED_STAGES']).REQUIRED_STAGES.items() for n in names])
def test_every_required_name_omission(tmp_path, stage, name):
    from trusted_router.async_settle_shadow_v2_pack_external import REQUIRED_STAGES, external_pack
    from trusted_router.async_settle_shadow_v2_pack_review import review_days
    policy = {s: list(names) for s, names in REQUIRED_STAGES.items()}
    policy[stage].remove(name)
    files = PackFiles(tmp_path)
    with pytest.raises(ValueError):
        external_pack(files, stage=stage, required_stages=policy, supplied=[])
    with pytest.raises(ValueError):
        review_days(files, days=[], subjects={}, criteria={}, required_stages=policy, verifiers={})


def test_false_contender_blocks(tmp_path, captured):  # noqa: F811
    files = PackFiles(tmp_path)
    plan = budget_capture(files, captured)
    for stage in ('pre', 'post'):
        plan[stage]['load_segments'][0]['fields']['contender'] = False
    with pytest.raises(ValueError, match='TrialContender'):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')


def test_missing_approved_pre_blocks(tmp_path, captured):  # noqa: F811
    files = PackFiles(tmp_path)
    plan = budget_capture(files, captured)
    for field in ('post_scope', 'trial_record'):
        value = json.loads(files.read(plan[field]))
        value['scope'] = 'production_warmup'
        plan[field] = files.obj('changed/' + field, value)
    plan['trial_review'] = review(files, plan['trial_record'], 'changed/review')
    with pytest.raises(ValueError, match='MissingApprovedPre'):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets')


def test_independent_ci_completions(tmp_path):
    from tests.test_async_settle_shadow_v2_pack_round3_sources import procedure_capture
    from trusted_router.async_settle_shadow_v2_pack_procedures import procedure_result
    files = PackFiles(tmp_path)
    plan = procedure_capture(files, 'shared_fixtures', 'transport', enclave_revision='b' * 40)
    for name, field in [('job', 'completed_at'), ('result', 'completed_at'), ('run', 'updated_at')]:
        value = json.loads(files.read(plan['enclave_ci'][name]))
        value[field] = '2026-10-12T00:00:03Z'
        plan['enclave_ci'][name] = files.obj('later/' + name, value)
    result = procedure_result(files, plan, capture_root=files.root)
    from trusted_router.async_settle_shadow_v2_collect_inventory import timestamp
    assert result['completed_at_us'] == timestamp('2026-10-12T00:00:03Z')
    assert result['router_revision'] != result['enclave_revision']


@pytest.fixture(scope='module')
def generated_transport(tmp_path_factory):
    from scripts.async_settle.shadow_v2_window import collect_day
    from tests.shadow_v2_pack_round3_captures import writer_capture
    from tests.test_async_settle_shadow_v2_pack_round3_composition import stage_capture
    root = tmp_path_factory.mktemp('round6-transport')
    capture = writer_capture(root / 'writer')
    plan = stage_capture(root / 'source', capture, checkpoint='transport')
    result = collect_day(root / 'source', root / 'out', plan)
    return root, plan, result, json.loads((root / 'out/stage/transport.json').read_bytes())


def test_generated_transport_roots_and_projection(generated_transport):
    from tests.shadow_v2_pack_round3_matrix import stage_ports
    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router.async_settle_shadow_v2_gate_bundle import decode_bundle
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.detached_jws import canonical
    _, _, _, bundle = generated_transport
    ports = stage_ports(bundle)
    loaded = load(canonical(bundle), ports)
    context = decode_bundle(loaded, ports, 'transport')
    coverage = list(context.attached('coverage'))
    assert {page['stream'] for page in coverage} == set(a1.COVERAGE_STREAMS)
    manifest = next(json.loads(row['raw']) for row in bundle['rows'] if row['id'].endswith('/manifest-v2'))
    from trusted_router import async_settle_shadow_v2_a2_contract as contract
    from trusted_router.async_settle_shadow_v2_a2_roots import entry_root
    for field, stream in contract.COVERAGE_PREIMAGES.items():
        assert a1.coverage_root([p for p in coverage if p['stream'] == stream]) == manifest[field.split('.')[1]]
    proof = json.loads(bundle['proof'])
    for field, stream in contract.CROSS_DAY_PREIMAGES.items():
        pages = [p for p in coverage if p['stream'] == stream]
        entries = [dict(day=manifest['day'], root=a1.coverage_root(pages), count=sum(len(p['entries']) for p in pages))]
        assert entry_root(entries, stream=stream + '_root') == proof[field.split('.')[1]]
    generated = [r for r in bundle['attachments'] if r['kind'] in
        ('coverage', 'authorization_obligation', 'handoff_accounting')]
    originals = {row['raw'] for row in bundle['preimages']}
    assert len(generated) == 8 and all(row['raw'] in originals for row in generated)
    projection = next(r for r in generated if 'closure_sha256' in json.loads(r['raw']))
    entries = [e for p in coverage if p['stream'] == 'authorization_obligation' for e in p['entries']]
    import hashlib
    assert any(e['key'] == projection['id'] and e['sha256'] == hashlib.sha256(projection['raw'].encode()).hexdigest()
        and e['bytes'] == len(projection['raw'].encode()) for e in entries)
    # Exact existing loader result: this kind currently parses only a page.
    assert any(i.gap == 'authorization_obligation_gap' and 'invalid required row' in i.detail
               for i in context.issues)
    from trusted_router.async_settle_shadow_v2_gate_bundle import coverage_issues
    before = len(context.issues)
    coverage_issues(context)
    assert context.issues[before:] == []


@pytest.mark.parametrize('index', range(8))
def test_each_generated_attachment_deletion_blocks(generated_transport, index):
    import copy

    from tests.shadow_v2_pack_round3_matrix import stage_ports
    from trusted_router.async_settle_shadow_v2_gate_bundle import coverage_issues, decode_bundle
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.detached_jws import canonical
    _, _, _, original = generated_transport
    bundle = copy.deepcopy(original)
    generated = [r for r in bundle['attachments'] if r['kind'] in
        ('coverage', 'authorization_obligation', 'handoff_accounting')]
    removed = generated[index]
    bundle['attachments'].remove(removed)
    ports = stage_ports(bundle)
    context = decode_bundle(load(canonical(bundle), ports), ports, 'transport')
    before = len(context.issues)
    coverage_issues(context)
    stream = json.loads(removed['raw'])['stream'] if removed['kind'] == 'coverage' else removed['kind']
    assert any(i.gap == 'artifact_gap' and i.subject.object_id == stream for i in context.issues[before:])


@pytest.mark.parametrize('support', ['absent', 'incomplete', 'tail'])
def test_support_completion_precedes_inner_flags(tmp_path, captured, monkeypatch, support):  # noqa: F811
    from scripts.async_settle import shadow_v2_window as command
    from tests.test_async_settle_shadow_v2_pack_round3_composition import stage_capture
    from trusted_router import async_settle_shadow_v2_evidence as p1
    from trusted_router.async_settle_shadow_v2_pack_composition import compose_day
    from trusted_router.async_settle_shadow_v2_pack_references import TerminalReconciliation
    plan = stage_capture(tmp_path / 'source', captured)
    source_files = PackFiles(tmp_path / 'source')
    plan['completion'] = source_files.obj('captures/support-completion', dict(
        command='collect', complete=True, completed_at_us=plan['complete_through_us'] + 1))
    plan['files'].append(plan['completion'])
    pages = [json.loads(r['raw']) for r in captured['bundle']['rows'] if r['kind'] == p1.KINDS[2]]
    samples = [json.loads(r['raw']) for r in captured['bundle']['rows'] if r['kind'] == p1.KINDS[0]]
    joined = TerminalReconciliation(pages, samples)
    days = joined.support_days([plan['day']])
    if support != 'absent':
        joined.day_status = {day: False for day in days if day != plan['day']}
    seen = {}
    def composed(files, **kwargs):
        kwargs['producers']['NativeExport']['completed_at_us'] = kwargs['complete_through_us']
        result = compose_day(files, **kwargs)
        seen.update(result)
        return result
    monkeypatch.setattr(command, 'compose_day', composed)
    command.collect_day(tmp_path / 'source', tmp_path / 'out', plan, reconciliation=joined)
    assert not seen['manifest']['complete']
    proof = json.loads((tmp_path / 'out/proof_manifest').read_bytes())
    assert not proof['complete']
    assert any('SupportDay' in reason for reason in seen['missing'])


def test_production_keeps_earlier_pre_bytes(tmp_path, captured, monkeypatch):  # noqa: F811
    from tests import test_async_settle_shadow_v2_pack as common
    from tests import test_async_settle_shadow_v2_pack_round3_budget as budget_test
    from tests.shadow_v2_pack_round5_captures import captured_day
    from trusted_router.async_settle_shadow_v2_pack_originals import ScopedOutputs
    from trusted_router.async_settle_shadow_v2_pack_plan import input_plan
    from trusted_router.async_settle_shadow_v2_schema import sha256
    first = PackFiles(tmp_path / 'first')
    earlier_inputs = ScopedOutputs(first.root, refs=first.refs, prefix='earlier/')
    earlier = budget_capture(earlier_inputs, captured)
    earlier['output_prefix'] = 'earlier-output/'
    first_result = CapturedBudget.build(first, earlier, checkpoint='fleet-budgets-pre')
    raw = first.read(first.refs['proof.pre_budget_sha256'])
    approved = first.refs['proof.pre_budget_sha256']
    approved_review = review(first, approved, 'earlier-review')
    registered = [r for p, r in first.refs.items() if p != 'proof.post_budget_sha256']
    inputs = input_plan(first, registered, days=['2026-10-12'], predecessors=[])
    inputs_ref = first.refs['inputs/plan.json']
    inputs_review = review(first, inputs_ref, 'earlier-inputs-review')
    second = PackFiles(tmp_path / 'second')
    second.capture(first, [r for p, r in first.refs.items() if p != 'proof.post_budget_sha256'])
    captured_day(tmp_path / 'later', '2026-10-13')
    writer_root = tmp_path / 'later/writer'
    objects = PackFiles(writer_root / 'objects')
    for path in sorted(objects.root.rglob('*')):
        if path.is_file():
            objects.put(path.relative_to(objects.root).as_posix(), path.read_bytes())
    later_capture = dict(objects=objects, bundle=json.loads((writer_root / 'native-complete/bundle.json').read_bytes()))
    for module in (common, budget_test):
        monkeypatch.setattr(module, 'DAY', '2026-10-13')
        if hasattr(module, 'START'):
            monkeypatch.setattr(module, 'START', module.START + 86400000000)
    current_inputs = ScopedOutputs(second.root, refs=second.refs, prefix='current/')
    current = budget_capture(current_inputs, later_capture)
    for field in ('post_scope', 'trial_record'):
        value = json.loads(second.read(current[field]))
        value['scope'] = 'production_warmup'
        current[field] = second.obj('production-input/' + field, value)
    current['trial_review'] = review(second, current['trial_record'], 'production-input/review')
    current['approved_pre'] = dict(envelope=approved, review=approved_review, inputs=inputs,
        inputs_ref=inputs_ref, inputs_review=inputs_review,
        allowances=earlier['pre_allowances'], configuration=earlier['configuration'])
    result = CapturedBudget.build(second, current, checkpoint='fleet-budgets')
    assert second.read(second.refs['proof.pre_budget_sha256']) == raw
    assert result.pre == first_result.pre and result.post['pre_envelope_sha256'] == sha256(first_result.pre)
    assert result.pre['v2_load']['interval'] != result.post['v2_load']['interval']
    assert second.read(result.pre['v2_load']['limits']) == first.read(first_result.pre['v2_load']['limits'])


def test_descriptors_are_read_and_compared(tmp_path, generated_transport, monkeypatch):
    import hashlib

    from scripts.async_settle import shadow_report, shadow_v2_window
    from trusted_router.detached_jws import canonical
    root, _, _, _ = generated_transport
    files = PackFiles(tmp_path)
    refs = [files.put('loading/' + name, b'[]') for name in ('certificate-public.json', 'binding-public.json')]
    release = json.loads((root / 'out/loading/release-plan.json').read_bytes())
    release['artifacts'].update({ref['path']: ref['sha256'] for ref in refs})
    raw = canonical(release)
    files.put('loading/release-plan.json', raw)
    calls = []
    original = shadow_report.shadow_v2_ports
    def ports(certificates, bindings, **kwargs):
        calls.append((certificates, bindings))
        return original(certificates, bindings, **kwargs)
    monkeypatch.setattr(shadow_report, 'shadow_v2_ports', ports)
    result = shadow_v2_window.evaluate_stage(root / 'out/stage/transport.json', tmp_path, 'transport',
        hashlib.sha256(raw).hexdigest())
    assert calls == [(b'[]', b'[]')]
    assert not any(reason.startswith('MissingDescriptor') for reason in result['missing'])
    (tmp_path / 'loading/certificate-public.json').write_bytes(b'[]\n')
    with pytest.raises(ValueError, match='DescriptorReview'):
        shadow_v2_window.evaluate_stage(root / 'out/stage/transport.json', tmp_path, 'transport',
            hashlib.sha256(raw).hexdigest())


def test_final_review_replaces_provisional_diagnostics(tmp_path, generated_transport, monkeypatch):
    import copy
    import shutil

    from scripts.async_settle import shadow_v2_window as command
    from trusted_router.detached_jws import canonical
    root, selected, captured_result, _ = generated_transport
    selected = copy.deepcopy(selected)
    source = tmp_path / 'source'
    shutil.copytree(root / 'out', source)
    captured_result = {**captured_result, 'stage': 'pre',
        'missing': [*captured_result['source_missing'], 'ProvisionalReviewGap']}
    (source / 'stage/result.json').write_bytes(canonical(captured_result))
    files = PackFiles(source)
    selected['daily_reviews'] = {day: review(files, ref, 'final-reviews/' + day)
        for day, ref in captured_result['review']['subjects'].items()}
    selected['pack_completion'] = files.obj('final-completion', dict(command='pack', complete=True,
        completed_at_us=json.loads(files.read(selected['completion']))['completed_at_us'] + 1))
    plan = dict(stages=dict(pre=selected))
    reviewed = command.window('review-inputs', plan, source=source, out=tmp_path / 'review')
    selected['release_plan_sha256'] = reviewed['review']['release_plan']['sha256']
    monkeypatch.setattr(command, 'evaluate_stage', lambda *args, **kwargs: dict(missing=[], verdict='BLOCKED'))
    result = command.window('pack', plan, source=source, out=tmp_path / 'pack')
    assert 'ProvisionalReviewGap' not in result['missing']
    assert set(captured_result['source_missing']) <= set(result['missing'])


@pytest.mark.parametrize('damage', ['incomplete', 'receipt_tail'])
def test_support_source_population_comparison(tmp_path, damage):
    from tests.shadow_v2_pack_round5_captures import captured_day
    from trusted_router import async_settle_shadow_v2_evidence as p1
    from trusted_router.async_settle_shadow_v2_pack_window import support_complete
    from trusted_router.detached_jws import canonical
    plan = captured_day(tmp_path / 'support', '2026-10-13')
    files = PackFiles(tmp_path / 'support/capture')
    native = json.loads(files.read(plan['captures']['NativeExport']))
    path = files.root / native['bundle']
    bundle = json.loads(path.read_bytes())
    bundle['completed_at_us'] = plan['complete_through_us']
    path.write_bytes(canonical(bundle))
    assert support_complete(files, plan, native, path, minimum_through_us=plan['complete_through_us'])
    if damage == 'receipt_tail':
        receipt = next(r for r in bundle['rows'] if r['kind'] == p1.KINDS[2])
        bundle['rows'].remove(receipt)
        path.write_bytes(canonical(bundle))
    else:
        inventory = json.loads(files.read(plan['captures']['Inventory']))
        inventory['complete'] = False
        plan['captures']['Inventory'] = files.obj('incomplete/inventory', inventory)
    assert not support_complete(files, plan, native, path, minimum_through_us=plan['complete_through_us'])


def test_predecessor_namespaces_preserve_reused_paths(tmp_path):
    from tests.test_async_settle_shadow_v2_pack_window import make_predecessor
    from trusted_router.async_settle_shadow_v2_collect_ports import Files
    from trusted_router.async_settle_shadow_v2_pack_plan import check_plan
    from trusted_router.async_settle_shadow_v2_pack_window import predecessors
    source = PackFiles(tmp_path / 'source')
    roots = []
    for name in ('pre', 'fleet'):
        files = PackFiles(source.root / name)
        descriptor = make_predecessor(files, 'graph', [])
        value = json.loads(files.read(descriptor))
        value['source_root'] = name
        roots.append(source.obj('descriptors/' + name, value))
    output = PackFiles(tmp_path / 'out')
    retained = predecessors(Files(source.root), output, roots)
    assert retained == sorted(roots, key=lambda r: r['path'])
    for ref in roots:
        value = json.loads(output.read(ref))
        original = Files(source.root / value['source_root'])
        copied = Files(output.root / value['source_root'])
        for member in check_plan(copied, value['inputs']):
            assert copied.read(member) == original.read(member)


def test_role_transition_inside_supplied_segment_blocks(tmp_path, captured):  # noqa: F811
    files = PackFiles(tmp_path)
    plan = budget_capture(files, captured)
    record = json.loads(files.read(plan['trial_record']))
    roles = json.loads(files.read(record['roles']))
    interval = roles['intervals'][0]
    transition = interval['end_us'] - 10
    roles['intervals'] = [{**interval, 'end_us': transition},
        {**interval, 'start_us': transition, 'contender': False}]
    record['roles'] = files.obj('changed/roles', roles)
    plan['trial_record'] = files.obj('changed/record', record)
    plan['trial_review'] = review(files, plan['trial_record'], 'changed/review')
    with pytest.raises(ValueError, match='TrialContender'):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')


def test_nonempty_history_keeps_original_resolution_support(tmp_path):
    from pathlib import Path

    from tests.test_async_settle_shadow_r11_validity import signed_observation
    from tests.test_async_settle_shadow_v2_pack import START
    from trusted_router.async_settle_shadow_evidence import SAMPLE
    from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
    from trusted_router.async_settle_shadow_v2_pack_history import history
    files = PackFiles(tmp_path / 'positive')
    at = START - 86400000000 + 1000000
    _, _, observed = signed_observation(at, phase='settle', charge=999)
    revision = observed['deployment']['router_revision']
    fixed = 'b' * 40 if revision != 'b' * 40 else 'c' * 40
    original = files.obj('history-original/resolution', dict(revision=revision, fixed_revision=fixed, at_us=at))
    resolution = dict(at_us=at, revision=revision, fixed_revision=fixed, serving_since_us=START,
        artifact_sha256=original['sha256'])
    rows = [dict(kind=SAMPLE, id=observed['authorization_day'] + '/' + observed['authorization_id'], body=observed)]
    payload = files.obj('history-original/payload', dict(rows=rows, days=['2026-10-12'],
        proof=dict(resolved_mismatches=[resolution]), resets=[], serving=[[START, START + 86400000000, fixed]], manifests={}))
    reviewed = files.obj('history-original/reviewed', dict(rows=rows, resets=[], resolutions=[resolution]))
    source = files.put('history-original/source', Path('scripts/async_settle/shadow_report.py').read_bytes())
    arguments = dict(payload=payload, source=source, dependencies=[original], reviewed_history=reviewed,
        approvals=[review(files, reviewed, 'history-original/review'), review(files, original, 'history-original/resolution-review')])
    result = history(files, **arguments)
    assert PayloadGraph(files.originals()).detail(result['history'], 'reset_history')
    (files.root / original['path']).unlink()
    with pytest.raises(OSError):
        history(files, **arguments)
